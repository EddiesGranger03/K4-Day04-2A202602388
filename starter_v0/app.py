"""Streamlit live-chat UI for the IT Helpdesk Agent.

Reuses `run_model_tool_loop` from chat.py so the UI, CLI chat and eval evidence
share one agent loop. Timing and reasoning come from a provider wrapper (ui_trace.py).
Run from starter_v0/:  streamlit run app.py
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from chat import ARTIFACTS_DIR, ROOT, now_iso, run_model_tool_loop, safe_slug, trim_history, write_transcript
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from ui_team import (
    EVAL_SETS,
    case_user_turns,
    check_tavily_boundary,
    expected_summary,
    grade_turn,
    list_run_files,
    load_eval_set,
    load_run,
    load_version_log,
    matching_version,
    run_case_rows,
    run_is_valid,
    run_summary_row,
    tool_group,
    tool_metadata,
    validate_tools_schema,
)
from ui_trace import TracingProvider, format_ms, turn_log
from versioning import artifact_version_dict, build_artifact_version


PROVIDERS = ["openrouter", "openai", "anthropic", "gemini"]
SYSTEM_PROMPT_PATH = ARTIFACTS_DIR / "system_prompt.md"
TOOLS_PATH = ARTIFACTS_DIR / "tools.yaml"
TRANSCRIPTS_DIR = ROOT / "transcripts"
TICKETS_DIR = ROOT / "tickets"

# One scenario per decision the lab grades: routing, missing identifier,
# parallel tools and the write-action confirmation boundary.
SCENARIOS = {
    ":material/dns: Tra trạng thái dịch vụ": "Wi-Fi production hôm nay có sự cố gì không?",
    ":material/help: Thiếu mã thiết bị": "Laptop của mình không vào được VPN, kiểm tra máy giúp nhé.",
    ":material/call_split: Hai tool một lượt": "Email production có lỗi không, và kiểm tra luôn máy LT-318 giúp mình.",
    ":material/lock: Hành động cần xác nhận": "Tạo ticket mức medium cho lỗi máy in tầng 3.",
    ":material/confirmation_number: Tra cứu ticket": "Ticket LAB-A1B2C3D4 hiện đang ở trạng thái nào?",
}

CHAT_TAB = ":material/forum: Hội thoại"
EVAL_TAB = ":material/fact_check: Eval"
LOG_TAB = ":material/receipt_long: Nhật ký chạy"
ARTIFACT_TAB = ":material/fingerprint: Artifact"

STATUS_BADGES = {
    "answered": ("green", ":material/check_circle:", "Đã trả lời"),
    "waiting_for_user": ("orange", ":material/pending:", "Chờ người dùng"),
    "max_tool_rounds": ("red", ":material/warning:", "Chạm giới hạn vòng"),
    "provider_error": ("red", ":material/error:", "Lỗi provider"),
}

SIDE_EFFECT_LABELS = {"False": "không", "True": "có", "local_file_write": "ghi file local", "None": "không rõ"}

LOG_KINDS = ["user", "model", "tool", "tool_error", "ticket_write", "reply", "error"]

# Transcript header fields that must stay constant within one transcript file,
# otherwise the evidence would mix artifact versions or run settings.
SESSION_KEYS = ("artifact_version", "provider", "model", "history_window", "max_tool_rounds")


# ---------------------------------------------------------------- state

def init_state() -> None:
    st.session_state.setdefault("turns", [])
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("transcript", None)
    st.session_state.setdefault("transcript_path", None)
    st.session_state.setdefault("pending_prompt", None)
    st.session_state.setdefault("pending_turns", [])
    st.session_state.setdefault("eval_case", None)
    st.session_state.setdefault("role_b_checks", {})


def reset_conversation() -> None:
    st.session_state.turns = []
    st.session_state.history = []
    st.session_state.transcript = None
    st.session_state.transcript_path = None
    st.session_state.pending_prompt = None
    st.session_state.pending_turns = []
    st.session_state.eval_case = None


def start_eval_case(case: dict[str, Any]) -> None:
    """Replay an eval case's user turns in a fresh conversation, then grade the last turn."""
    reset_conversation()
    st.session_state.pending_turns = case_user_turns(case)
    st.session_state.eval_case = case
    st.session_state.main_tab = CHAT_TAB


def run_role_b_check(name: str) -> None:
    check = validate_tools_schema if name == "schema" else check_tavily_boundary
    st.session_state.role_b_checks[name] = check()


def ticket_files() -> set[str]:
    if not TICKETS_DIR.exists():
        return set()
    return {path.name for path in TICKETS_DIR.glob("*.json")}


def start_transcript(settings: dict[str, Any], artifact: Any) -> None:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    transcript_id = "_".join([safe_slug(artifact.version), safe_slug(settings["provider"]), "ui", timestamp])
    st.session_state.transcript_path = TRANSCRIPTS_DIR / f"{transcript_id}.transcript.json"
    st.session_state.transcript = {
        "transcript_id": transcript_id,
        "client": "streamlit_ui",
        **artifact_version_dict(artifact),
        "provider": settings["provider"],
        "model": settings["model"],
        "system_prompt": str(SYSTEM_PROMPT_PATH),
        "tools": str(TOOLS_PATH),
        "history_window": settings["history_window"],
        "max_tool_rounds": settings["max_tool_rounds"],
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "turns": [],
    }


def session_drift(settings: dict[str, Any], artifact: Any) -> list[str]:
    transcript = st.session_state.transcript
    if not transcript:
        return []
    current = {**settings, "artifact_version": artifact.artifact_version}
    return [key for key in SESSION_KEYS if transcript.get(key) != current[key]]


# ---------------------------------------------------------------- agent turn

def run_turn(user_text: str, settings: dict[str, Any], artifact: Any, live: Any) -> dict[str, Any]:
    """Mirror chat.py main(): same message layout, same loop, same turn record, plus timings."""
    if st.session_state.transcript is None:
        start_transcript(settings, artifact)

    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    openai_tools = to_openai_tools(load_tool_declarations(TOOLS_PATH))
    history: list[dict[str, str]] = st.session_state.history
    messages = [
        {"role": "system", "content": system_prompt},
        *trim_history(history, settings["history_window"]),
        {"role": "user", "content": user_text},
    ]

    live_steps: dict[int, Any] = {}

    def on_start(round_index: int) -> None:
        with live:
            live_steps[round_index] = st.status(f"Vòng {round_index}: model đang suy nghĩ", type="step")

    def on_end(record: dict[str, Any]) -> None:
        step = live_steps.get(record["round"])
        if step is None:
            return
        if record.get("error"):
            step.update(label=f"Vòng {record['round']}: lỗi sau {format_ms(record['model_ms'])}", state="error")
            return
        decided = ", ".join(record["tool_names"]) or "trả lời trực tiếp"
        step.update(label=f"Vòng {record['round']}: suy nghĩ {format_ms(record['model_ms'])}, chọn {decided}", state="complete")
        if record.get("reasoning"):
            step.caption(record["reasoning"])

    tracer = TracingProvider(make_provider(settings["provider"]), on_start=on_start, on_end=on_end)
    turn: dict[str, Any] = {
        "turn_index": len(st.session_state.turns) + 1,
        "started_at": now_iso(),
        "user": user_text,
        "status": "started",
        "assistant_text": None,
        "rounds": [],
        "tool_events": [],
    }
    tickets_before = ticket_files()
    started = time.perf_counter()
    try:
        result = run_model_tool_loop(
            provider=tracer,
            messages=messages,
            tools=openai_tools,
            model=settings["model"],
            max_tool_rounds=settings["max_tool_rounds"],
        )
        turn.update(result)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": result["assistant_text"]})
    except Exception as exc:
        turn.update({"status": "provider_error", "error": f"{type(exc).__name__}: {exc}"})
    finished = time.perf_counter()

    turn["ended_at"] = now_iso()
    turn["trace"] = tracer.finalize(finished)
    turn["thinking_ms"] = sum(call.get("model_ms", 0) for call in turn["trace"])
    turn["total_ms"] = round((finished - started) * 1000)
    # Filesystem evidence for the action boundary: which ticket files this turn wrote.
    turn["tickets_created"] = sorted(ticket_files() - tickets_before)

    st.session_state.turns.append(turn)
    transcript = st.session_state.transcript
    transcript["turns"].append(turn)
    write_transcript(st.session_state.transcript_path, transcript)
    return turn


# ---------------------------------------------------------------- rendering helpers

def explain_error(error: str) -> str:
    if "429" in error:
        return (
            "Model đang bị giới hạn request (429). Đợi khoảng một phút rồi gửi lại, "
            "hoặc đổi sang model dự phòng trong phần cấu hình."
        )
    if "Missing API key" in error:
        return f"Chưa có API key cho provider này ({error.split(': ')[-1]}). Điền key vào `starter_v0/.env` rồi tải lại trang."
    return error


def parse_structured_reply(text: str) -> dict[str, Any] | None:
    """Return the JSON object if the prompt's output format was followed, else None."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "reply" in data else None


def tool_outcome(result: Any) -> tuple[str, str]:
    """Icon and short outcome text for one executed tool call."""
    if not isinstance(result, dict):
        return ":material/check:", "trả kết quả"
    if result.get("error"):
        return ":material/error:", f"trả lỗi {result['error']}"
    if result.get("awaiting_user"):
        return ":material/pending:", "đang chờ người dùng trả lời"
    if result.get("status") == "created":
        return ":material/edit_document:", "đã ghi ticket"
    if result.get("status") == "needs_confirmation":
        return ":material/lock:", "chặn lại, cần xác nhận"
    if result.get("status") == "found":
        return ":material/confirmation_number:", "tìm thấy ticket"
    return ":material/check:", "trả kết quả"


def render_assistant_text(text: str | None) -> None:
    if not text:
        st.caption("Model không trả nội dung.")
        return
    structured = parse_structured_reply(text)
    if structured is None:
        st.markdown(text)
        return
    st.markdown(str(structured.get("reply", "")))
    with st.container(horizontal=True, gap="small"):
        if structured.get("intent"):
            st.badge(f"intent: {structured['intent']}", icon=":material/target:", color="violet")
        if structured.get("action"):
            st.badge(f"action: {structured['action']}", icon=":material/bolt:", color="violet")
        for evidence in structured.get("evidence_ids") or []:
            st.badge(str(evidence), icon=":material/link:", color="gray")


def render_model_step(call: dict[str, Any], round_record: dict[str, Any]) -> None:
    decided = ", ".join(f"`{name}`" for name in call.get("tool_names", [])) or "trả lời trực tiếp"
    if call.get("error"):
        label, icon = f"Vòng {call['round']}: lỗi sau {format_ms(call.get('model_ms'))}", ":material/error:"
    else:
        label, icon = f"Vòng {call['round']}: suy nghĩ {format_ms(call.get('model_ms'))}, chọn {decided}", ":material/psychology:"

    with st.expander(label, type="step", icon=icon):
        if call.get("error"):
            st.error(explain_error(call["error"]), icon=":material/error:")
        if call.get("reasoning"):
            st.markdown("\n".join(f"> {line}" for line in call["reasoning"].splitlines() if line.strip()))
        elif not call.get("error"):
            st.caption("Model không trả nội dung suy nghĩ cho vòng này.")
        if round_record.get("assistant_text") and round_record.get("tool_calls"):
            st.caption(f"Model viết kèm lời gọi tool: {round_record['assistant_text']}")
        with st.container(horizontal=True, gap="small"):
            st.badge(f"suy nghĩ {format_ms(call.get('model_ms'))}", icon=":material/timer:", color="primary")
            if call.get("tool_ms") is not None:
                st.badge(f"chạy tool {format_ms(call['tool_ms'])}", icon=":material/build:", color="gray")
            if call.get("reasoning_tokens"):
                st.badge(f"{call['reasoning_tokens']} token suy nghĩ", icon=":material/token:", color="gray")
            if call.get("prompt_tokens"):
                st.badge(f"{call['prompt_tokens']} token vào, {call.get('completion_tokens') or 0} token ra", color="gray")


def render_tool_step(tool_call: dict[str, Any], event: dict[str, Any] | None) -> None:
    if event is None:
        with st.expander(f"`{tool_call['name']}` không được chạy", type="step", icon=":material/block:"):
            st.caption("Loop dừng ở tool trước để chờ người dùng, nên lời gọi này không được thực thi.")
            st.json(tool_call.get("args", {}))
        return

    result = event.get("result")
    icon, outcome = tool_outcome(result)
    with st.expander(f"`{tool_call['name']}` {outcome}", type="step", icon=icon):
        if isinstance(result, dict) and result.get("error"):
            st.error(f"Tool trả lỗi `{result['error']}`. {result.get('message', '')}", icon=":material/error:")
        args_col, result_col = st.columns([2, 3])
        with args_col:
            st.markdown("**Tham số**")
            st.json(tool_call.get("args", {}))
        with result_col:
            st.markdown("**Kết quả**")
            st.json(result, expanded=2)


def render_thinking(turn: dict[str, Any]) -> None:
    trace = turn.get("trace", [])
    if not trace:
        return
    rounds = {item["round"]: item for item in turn.get("rounds", [])}
    tool_count = len(turn.get("tool_events", []))
    label = f"Suy nghĩ {format_ms(turn.get('thinking_ms'))}, {tool_count} tool, {len(trace)} vòng"
    with st.expander(label, type="compact", icon=":material/psychology:"):
        for call in trace:
            round_record = rounds.get(call["round"], {})
            render_model_step(call, round_record)
            events = round_record.get("tool_results", [])
            for index, tool_call in enumerate(round_record.get("tool_calls", [])):
                render_tool_step(tool_call, events[index] if index < len(events) else None)


def render_turn(turn: dict[str, Any]) -> None:
    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(turn["user"])

    with st.chat_message("assistant", avatar=":material/support_agent:"):
        render_thinking(turn)
        if turn["status"] == "provider_error":
            st.error(explain_error(turn.get("error", "")), icon=":material/error:")
        else:
            render_assistant_text(turn.get("assistant_text"))

        color, icon, label = STATUS_BADGES.get(turn["status"], ("gray", ":material/info:", turn["status"]))
        with st.container(horizontal=True, gap="small"):
            st.badge(label, icon=icon, color=color)
            st.badge(f"tổng {format_ms(turn.get('total_ms'))}", icon=":material/schedule:", color="gray")
            if turn.get("tickets_created"):
                st.badge(
                    f"đã ghi {', '.join(turn['tickets_created'])}",
                    icon=":material/edit_document:",
                    color="orange",
                )
        if turn.get("eval_grade"):
            render_eval_grade(turn["eval_grade"])


def render_eval_grade(grade: dict[str, Any]) -> None:
    passed = grade.get("passed")
    with st.container(border=True):
        with st.container(horizontal=True, gap="small", vertical_alignment="center"):
            st.badge(
                f"{grade['case_id']} {'PASS' if passed else 'FAIL'}",
                icon=":material/check_circle:" if passed else ":material/cancel:",
                color="green" if passed else "red",
            )
            if grade.get("observed_mismatch"):
                st.badge(grade["observed_mismatch"], color="gray")
        if grade.get("expected"):
            st.caption(f"Kỳ vọng: `{grade['expected']}`")
        actual = ", ".join(call["name"] for call in grade.get("actual_tool_calls") or []) or "không gọi tool"
        st.caption(f"Vòng đầu của lượt cuối đã gọi: `{actual}`")
        for failure in grade.get("failures") or []:
            st.markdown(f"- {failure}")


# ---------------------------------------------------------------- sidebar

def render_sidebar() -> tuple[dict[str, Any], Any]:
    with st.sidebar:
        st.title("Helpdesk trace", icon=":material/support_agent:")
        st.caption("Northstar Labs, dữ liệu giả lập.")

        st.subheader("Cấu hình run", icon=":material/tune:")
        provider = st.selectbox("Provider", PROVIDERS, key="provider")
        default_model = getattr(make_provider(provider), "default_model", "")
        model = st.text_input("Model", value=default_model, key=f"model_{provider}").strip()
        hashes = build_artifact_version("probe", SYSTEM_PROMPT_PATH, TOOLS_PATH)
        log_rows = load_version_log()
        matched = matching_version(log_rows, hashes.prompt_hash, hashes.tools_hash)
        main_line = [row["version"] for row in log_rows if re.fullmatch(r"v\d+", row.get("version") or "")]
        suggested = matched or (f"{main_line[-1]}-dev" if main_line else "v0")
        version = st.text_input(
            "Nhãn version",
            value=suggested,
            key="version",
            help="Tự điền theo dòng trong version_log.csv khớp hash; thêm hậu tố -dev nếu artifact chưa được ghi log.",
        ).strip() or suggested
        history_window = st.number_input("Số cặp hội thoại giữ lại", 0, 20, 5, key="history_window")
        max_tool_rounds = st.number_input("Số vòng tool tối đa", 1, 10, 4, key="max_tool_rounds")
        st.button("Hội thoại mới", icon=":material/add_comment:", on_click=reset_conversation, width="stretch")

        artifact = build_artifact_version(version, SYSTEM_PROMPT_PATH, TOOLS_PATH)
        turns = st.session_state.turns
        st.subheader("Phiên hiện tại", icon=":material/monitoring:")
        with st.container(horizontal=True, gap="small"):
            st.badge(f"{len(turns)} lượt", icon=":material/forum:", color="primary")
            st.badge(f"{sum(len(t.get('tool_events', [])) for t in turns)} tool call", icon=":material/build:", color="primary")
            st.badge(f"suy nghĩ {format_ms(sum(t.get('thinking_ms', 0) for t in turns))}", icon=":material/timer:", color="primary")
        st.code(artifact.artifact_version, language=None, wrap_lines=True)
        if st.session_state.transcript_path:
            st.caption(f"Transcript: `{Path(st.session_state.transcript_path).relative_to(ROOT)}`")
        else:
            st.caption("Transcript được tạo khi gửi tin nhắn đầu tiên.")

    settings = {
        "provider": provider,
        "model": model or None,
        "history_window": int(history_window),
        "max_tool_rounds": int(max_tool_rounds),
    }
    return settings, artifact


# ---------------------------------------------------------------- tabs

def render_log_tab() -> None:
    turns = st.session_state.turns
    rows = [row for turn in turns for row in turn_log(turn)]
    if not rows:
        st.caption("Chưa có log. Gửi một tin nhắn ở tab Hội thoại để bắt đầu ghi.")
        return

    calls = [call for turn in turns for call in turn.get("trace", [])]
    events = [event for turn in turns for event in turn.get("tool_events", [])]
    failed_tools = sum(1 for event in events if isinstance(event.get("result"), dict) and event["result"].get("error"))
    with st.container(horizontal=True):
        st.metric("Thời gian suy nghĩ", format_ms(sum(call.get("model_ms", 0) for call in calls)), border=True)
        st.metric("Lượt gọi model", len(calls), border=True)
        st.metric("Tool call", len(events), border=True)
        st.metric("Tool trả lỗi", failed_tools, border=True)
        st.metric("Token suy nghĩ", sum(call.get("reasoning_tokens") or 0 for call in calls), border=True)

    st.subheader("Thời gian từng vòng", icon=":material/timer:")
    timing = [
        {
            "Bước": f"L{turn['turn_index']} V{call['round']}",
            "Model suy nghĩ (ms)": call.get("model_ms", 0),
            "Chạy tool (ms)": call.get("tool_ms", 0),
        }
        for turn in turns
        for call in turn.get("trace", [])
    ]
    st.bar_chart(
        timing,
        x="Bước",
        y=["Model suy nghĩ (ms)", "Chạy tool (ms)"],
        color=["#BE185D", "#F9A8D4"],
        horizontal=True,
        stack=True,
        sort=False,
        x_label="Lượt (L), vòng (V)",
        y_label="ms",
        height=90 + 44 * len(timing),
    )

    st.subheader("Sự kiện", icon=":material/receipt_long:")
    present = [kind for kind in LOG_KINDS if any(row["kind"] == kind for row in rows)]
    # Key follows the option set, so a kind that appears later starts selected.
    kinds = st.pills(
        "Lọc theo loại sự kiện",
        present,
        selection_mode="multi",
        default=present,
        key=f"log_kinds_{'-'.join(present)}",
    )
    filtered = [{**row, "time": (row["time"] or "")[11:23]} for row in rows if row["kind"] in (kinds or [])]
    st.dataframe(
        filtered,
        hide_index=True,
        column_order=["time", "turn", "round", "kind", "name", "duration_ms", "detail"],
        column_config={
            "time": st.column_config.TextColumn("Thời điểm", width="small"),
            "turn": st.column_config.NumberColumn("Lượt", width="small"),
            "round": st.column_config.NumberColumn("Vòng", width="small"),
            "kind": st.column_config.TextColumn("Loại", width="small"),
            "name": st.column_config.TextColumn("Tên", width="small"),
            "duration_ms": st.column_config.NumberColumn("ms", width="small"),
            "detail": st.column_config.TextColumn("Chi tiết", width="large"),
        },
    )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["time", "turn", "round", "kind", "name", "duration_ms", "detail"])
    writer.writeheader()
    writer.writerows(rows)
    transcript = st.session_state.transcript or {}
    stem = transcript.get("transcript_id", "helpdesk_ui")
    with st.container(horizontal=True):
        st.download_button(
            "Tải transcript JSON",
            data=json.dumps(transcript, ensure_ascii=False, indent=2, default=str),
            file_name=f"{stem}.transcript.json",
            mime="application/json",
            icon=":material/download:",
            type="primary",
            on_click="ignore",
        )
        st.download_button(
            "Tải log CSV",
            data=buffer.getvalue().encode("utf-8-sig"),
            file_name=f"{stem}.log.csv",
            mime="text/csv",
            icon=":material/table_view:",
            on_click="ignore",
        )


def percent(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.1f}%".replace(".", ",")


@st.cache_data(max_entries=32, show_spinner=False)
def cached_run(path: str, mtime: float) -> dict[str, Any]:
    return load_run(path)


def render_eval_tab() -> None:
    st.subheader("Chạy thử một case eval", icon=":material/checklist:")
    st.caption(
        "Chọn một case để chạy lại các lượt của người dùng trong tab Hội thoại. Lượt cuối được chấm bằng "
        "`evaluate_phase_b` của run_eval.py. Khác run_eval ở chỗ các lượt trước chạy tool thật và không ép "
        "tool_choice, nên phần này dùng để soát lỗi và demo; số liệu cho report vẫn lấy từ run_eval."
    )
    set_label = st.segmented_control("Bộ eval", list(EVAL_SETS), default=list(EVAL_SETS)[0], required=True, key="eval_set")
    dataset = load_eval_set(EVAL_SETS[set_label])
    cases = dataset.get("cases", [])
    single_count = sum("turns" not in case for case in cases)
    st.caption(f"{len(cases)} case: {single_count} single-turn, {len(cases) - single_count} multi-turn.")

    table = st.dataframe(
        [
            {
                "Case": case["id"],
                "Kiểu": "multi-turn" if "turns" in case else "single-turn",
                "Failure type": case.get("failure_type"),
                "Kỳ vọng": expected_summary(case),
                "Kiểm tra điều gì": case.get("metadata", {}).get("what_it_tests", ""),
            }
            for case in cases
        ],
        hide_index=True,
        height=(min(len(cases), 12) + 1) * 35 + 3,
        on_select="rerun",
        selection_mode="single-row",
        key=f"eval_cases_{set_label}",
        column_config={"Kiểm tra điều gì": st.column_config.TextColumn(width="large")},
    )
    if not table.selection.rows:
        st.caption("Bấm vào một dòng để xem chi tiết và chạy case.")
    else:
        case = cases[table.selection.rows[0]]
        with st.container(border=True):
            st.markdown(f"**{case['id']}**")
            st.caption(case.get("metadata", {}).get("what_it_tests", ""))
            for index, text in enumerate(case_user_turns(case), start=1):
                st.markdown(f"{index}. {text}")
            st.caption(f"Kỳ vọng ở lượt cuối: `{expected_summary(case)}`")
            st.button(
                "Chạy case này trong hội thoại",
                icon=":material/play_arrow:",
                type="primary",
                on_click=start_eval_case,
                args=(case,),
                key=f"run_case_{case['id']}",
            )

    st.subheader("Kết quả run_eval", icon=":material/fact_check:")
    files = list_run_files()
    if not files:
        st.caption("Chưa có file trong runs/. Chạy run_eval.py để tạo evidence rồi tải lại trang.")
        return
    runs = {path.name: cached_run(str(path), path.stat().st_mtime) for path in files}
    percent_column = st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)
    st.dataframe(
        [run_summary_row(path, runs[path.name]) for path in files],
        hide_index=True,
        column_config={
            "Case accuracy": percent_column,
            "Routing": percent_column,
            "Arguments": percent_column,
            "Multi-turn": percent_column,
            "Hợp lệ": st.column_config.CheckboxColumn(help="provider_error_cases == 0 và measured_cases == total_cases"),
        },
    )

    name = st.selectbox("Xem chi tiết run", list(runs), key="run_file")
    run = runs[name]
    summary = run.get("summary", {})
    with st.container(horizontal=True):
        st.metric("Case accuracy", percent(summary.get("case_accuracy")), border=True)
        st.metric("Routing", percent(summary.get("tool_routing_accuracy")), border=True)
        st.metric("Arguments", percent(summary.get("argument_accuracy")), border=True)
        st.metric("Multi-turn", percent(summary.get("multiturn_accuracy")), border=True)
        st.metric("Provider error", summary.get("provider_error_cases"), border=True)
    with st.container(horizontal=True, gap="small"):
        if run_is_valid(run):
            st.badge("Hợp lệ làm evidence", icon=":material/verified:", color="green")
        else:
            st.badge("Không hợp lệ làm evidence", icon=":material/block:", color="red")
        st.badge(run.get("artifact_version") or "không có artifact_version", icon=":material/fingerprint:", color="gray")
        st.badge(run.get("model") or "không rõ model", icon=":material/memory:", color="gray")
    if run.get("dataset_role") and run.get("suite") and run["dataset_role"] != run["suite"]:
        st.warning(
            f"Run được gắn nhãn suite `{run['suite']}` nhưng dữ liệu là `{run['dataset_role']}` "
            f"(`{run.get('eval_cases')}`). Sửa nhãn trước khi đưa vào report.",
            icon=":material/label_off:",
        )

    only_failed = st.toggle("Chỉ hiện case fail", value=True, key="only_failed")
    rows = [row for row in run_case_rows(run) if not (only_failed and row["passed"])]
    st.dataframe(
        rows,
        hide_index=True,
        column_order=["case_id", "is_multiturn", "passed", "case_failure_type", "observed_mismatch", "expected_tool", "actual_tool", "failures"],
        column_config={
            "case_id": st.column_config.TextColumn("Case"),
            "is_multiturn": st.column_config.CheckboxColumn("Multi-turn"),
            "passed": st.column_config.CheckboxColumn("Pass"),
            "case_failure_type": st.column_config.TextColumn("Failure type"),
            "observed_mismatch": st.column_config.TextColumn("Mismatch"),
            "expected_tool": st.column_config.TextColumn("Kỳ vọng"),
            "actual_tool": st.column_config.TextColumn("Thực tế"),
            "failures": st.column_config.TextColumn("Chi tiết", width="large"),
        },
    )


def render_artifact_tab(artifact: Any) -> None:
    st.subheader("Phiên bản đang chạy", icon=":material/fingerprint:")
    st.caption("Mọi lượt chat ghi lại hash này, nên đổi một ký tự trong prompt hoặc tools cũng tạo version mới.")
    st.code(artifact.artifact_version, language=None, wrap_lines=True)
    st.dataframe(
        [
            {"File": "artifacts/system_prompt.md", "SHA-256": artifact.prompt_hash},
            {"File": "artifacts/tools.yaml", "SHA-256": artifact.tools_hash},
        ],
        hide_index=True,
    )
    log_rows = load_version_log()
    matched = matching_version(log_rows, artifact.prompt_hash, artifact.tools_hash)
    if matched:
        st.success(f"Artifact đang chạy khớp dòng `{matched}` trong version_log.csv.", icon=":material/verified:")
    else:
        st.warning(
            "Artifact đang chạy chưa khớp dòng nào trong version_log.csv. Sau khi chạy eval cho bản này, thêm một "
            "dòng mới với hash ở trên. Git trên Windows có thể đổi xuống dòng LF sang CRLF, nên cùng một file "
            "vẫn cho hash khác nhau giữa các máy.",
            icon=":material/rule:",
        )
    if st.session_state.transcript_path:
        st.caption("Transcript của phiên này")
        st.code(str(st.session_state.transcript_path), language=None, wrap_lines=True)

    st.subheader("Version log", icon=":material/history:")
    if log_rows:
        st.dataframe(
            [
                {
                    "Đang chạy": row.get("version") == matched,
                    **{key: row.get(key) for key in ("version", "author", "changed_artifact", "prompt_hash", "tools_hash", "hypothesis", "metric_name", "metric_before", "metric_after", "run_file")},
                }
                for row in log_rows
            ],
            hide_index=True,
            height=(len(log_rows) + 1) * 35 + 3,
            column_config={
                "Đang chạy": st.column_config.CheckboxColumn(),
                "hypothesis": st.column_config.TextColumn("Hypothesis", width="large"),
            },
        )
    else:
        st.caption("version_log.csv chưa có dòng nào.")

    st.subheader("Kiểm tra schema và ranh giới dữ liệu", icon=":material/verified_user:")
    st.caption(
        "Chạy trực tiếp scripts/validate_tools_schema.py và scripts/test_tavily_boundary.py của Role B. "
        "Kiểm tra Tavily chỉ gọi mạng khi .env có TAVILY_API_KEY."
    )
    with st.container(horizontal=True):
        st.button("Kiểm tra schema tools.yaml", icon=":material/rule:", on_click=run_role_b_check, args=("schema",))
        st.button("Kiểm tra ranh giới Tavily", icon=":material/shield:", on_click=run_role_b_check, args=("tavily",))
    for name, label in (("schema", "Schema tools.yaml"), ("tavily", "Ranh giới Tavily")):
        result = st.session_state.role_b_checks.get(name)
        if result is None:
            continue
        passed = result["passed"]
        with st.expander(
            f"{label}: {'PASS' if passed else 'FAIL'}",
            icon=":material/check_circle:" if passed else ":material/error:",
            expanded=not passed,
        ):
            st.code(result["output"], language=None)

    st.subheader("Tools model nhìn thấy", icon=":material/build:")
    tool_rows = []
    for item in load_tool_declarations(TOOLS_PATH):
        parameters = item.get("parameters", {})
        metadata = tool_metadata(item["name"])
        tool_rows.append({
            "Tool": item["name"],
            "Nhóm": tool_group(item["name"]),
            "Ghi dữ liệu": SIDE_EFFECT_LABELS.get(str(metadata.get("side_effect")), str(metadata.get("side_effect", "không rõ"))),
            "Bắt buộc": ", ".join(parameters.get("required", [])),
            "Mô tả": " ".join(item.get("description", "").split()),
        })
    st.dataframe(tool_rows, hide_index=True, column_config={"Mô tả": st.column_config.TextColumn(width="large")})

    with st.expander("System prompt đang dùng", icon=":material/description:"):
        st.code(SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"), language="markdown", wrap_lines=True)


# ---------------------------------------------------------------- page

st.set_page_config(page_title="Helpdesk trace", page_icon=":material/support_agent:", layout="wide")
init_state()
settings, artifact = render_sidebar()

st.title("Helpdesk agent")
st.caption("Xem agent suy nghĩ gì, gọi tool nào với tham số nào, và dữ liệu nào dẫn tới câu trả lời.")
with st.container(horizontal=True, gap="small"):
    st.badge(settings["model"] or "model mặc định", icon=":material/memory:", color="primary")
    st.badge(settings["provider"], icon=":material/cloud:", color="gray")
    st.badge(artifact.version, icon=":material/sell:", color="gray")

# on_change="rerun" keeps the selected tab in session state, so start_eval_case can switch to the chat tab.
chat_tab, eval_tab, log_tab, artifact_tab = st.tabs(
    [CHAT_TAB, EVAL_TAB, LOG_TAB, ARTIFACT_TAB],
    key="main_tab",
    on_change="rerun",
)

drift = session_drift(settings, artifact)
eval_case = st.session_state.eval_case
with chat_tab:
    if drift:
        st.warning(
            f"Cấu hình đã đổi so với transcript hiện tại ({', '.join(drift)}). "
            "Bấm **Hội thoại mới** để mỗi transcript chỉ chứa một artifact version.",
            icon=":material/sync_problem:",
        )
    if eval_case:
        total_turns = len(case_user_turns(eval_case))
        done_turns = total_turns - len(st.session_state.pending_turns)
        st.info(
            f"Đang chạy case **{eval_case['id']}**: lượt {min(done_turns + 1, total_turns)} trên {total_turns}. "
            "Lượt cuối sẽ được chấm theo kỳ vọng của case.",
            icon=":material/play_circle:",
        )
    # A fixed-height chat panel scrolls on its own; a page-pinned chat_input would pull
    # every tab to the bottom on each rerun.
    chat_box = st.container(height=620, border=False, autoscroll=True)
    with chat_box:
        for past_turn in st.session_state.turns:
            render_turn(past_turn)
        if not st.session_state.turns and not eval_case:
            with st.container(border=True):
                st.subheader("Bắt đầu bằng một sự cố", icon=":material/add_comment:")
                st.caption(
                    "Mỗi kịch bản kiểm tra một quyết định của agent: chọn đúng tool, hỏi lại khi thiếu mã, "
                    "gọi hai tool cùng lúc, xin xác nhận trước khi ghi dữ liệu, và tra cứu ticket bằng bonus tool. "
                    "Muốn chạy đúng case chấm điểm của nhóm, mở tab Eval."
                )
                selected = st.pills("Kịch bản demo", list(SCENARIOS), key="scenario", label_visibility="collapsed")
                if selected:
                    st.session_state.pending_prompt = SCENARIOS[selected]
    queue: list[str] = st.session_state.pending_turns
    prompt = st.chat_input(
        "Mô tả sự cố IT, ví dụ: máy LT-204 không vào được VPN",
        submit_mode="disable",
        disabled=bool(drift) or bool(queue),
    )

with eval_tab:
    render_eval_tab()

with log_tab:
    render_log_tab()

with artifact_tab:
    render_artifact_tab(artifact)

from_queue = False
if not prompt and st.session_state.pending_prompt:
    prompt = st.session_state.pending_prompt
if not prompt and queue:
    prompt, from_queue = queue[0], True

if prompt and not drift:
    st.session_state.pending_prompt = None
    if from_queue:
        queue.pop(0)
    with chat_box:
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=":material/support_agent:"):
            live = st.status(":shimmer[Agent đang xử lý]", type="compact", expanded=True)
            turn = run_turn(prompt, settings, artifact, live)
            live.update(
                label=f"Xong sau {format_ms(turn['total_ms'])}",
                state="error" if turn["status"] == "provider_error" else "complete",
                expanded=False,
            )
    # Grade after the case's last user turn, or stop early if the provider failed mid-case.
    if eval_case and from_queue and (not queue or turn["status"] == "provider_error"):
        turn["eval_grade"] = grade_turn(eval_case, turn)
        st.session_state.eval_case = None
        st.session_state.pending_turns = []
        write_transcript(st.session_state.transcript_path, st.session_state.transcript)
    st.rerun()
