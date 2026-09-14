"""Streamlit live-chat UI for the IT Helpdesk Agent.

Reuses `run_model_tool_loop` from chat.py so the UI, CLI chat and eval evidence
share one agent loop. Timing and reasoning come from a provider wrapper (ui_trace.py).
Run from starter_v0/:  streamlit run app.py
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from chat import ARTIFACTS_DIR, ROOT, now_iso, run_model_tool_loop, safe_slug, trim_history, write_transcript
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
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
}

STATUS_BADGES = {
    "answered": ("green", ":material/check_circle:", "Đã trả lời"),
    "waiting_for_user": ("orange", ":material/pending:", "Chờ người dùng"),
    "max_tool_rounds": ("red", ":material/warning:", "Chạm giới hạn vòng"),
    "provider_error": ("red", ":material/error:", "Lỗi provider"),
}

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


def reset_conversation() -> None:
    st.session_state.turns = []
    st.session_state.history = []
    st.session_state.transcript = None
    st.session_state.transcript_path = None
    st.session_state.pending_prompt = None


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


# ---------------------------------------------------------------- sidebar

def render_sidebar() -> tuple[dict[str, Any], Any]:
    with st.sidebar:
        st.title("Helpdesk trace", icon=":material/support_agent:")
        st.caption("Northstar Labs, dữ liệu giả lập.")

        st.subheader("Cấu hình run", icon=":material/tune:")
        provider = st.selectbox("Provider", PROVIDERS, key="provider")
        default_model = getattr(make_provider(provider), "default_model", "")
        model = st.text_input("Model", value=default_model, key=f"model_{provider}").strip()
        version = st.text_input("Nhãn version", value="v0", key="version").strip() or "v0"
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
    if st.session_state.transcript_path:
        st.caption("Transcript của phiên này")
        st.code(str(st.session_state.transcript_path), language=None, wrap_lines=True)

    st.subheader("Tools model nhìn thấy", icon=":material/build:")
    tool_rows = []
    for item in load_tool_declarations(TOOLS_PATH):
        parameters = item.get("parameters", {})
        tool_rows.append({
            "Tool": item["name"],
            "Bắt buộc": ", ".join(parameters.get("required", [])),
            "Tham số": ", ".join(parameters.get("properties", {})),
            "Mô tả": item.get("description", ""),
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

chat_tab, log_tab, artifact_tab = st.tabs([
    ":material/forum: Hội thoại",
    ":material/receipt_long: Nhật ký chạy",
    ":material/fingerprint: Artifact",
])

drift = session_drift(settings, artifact)
with chat_tab:
    if drift:
        st.warning(
            f"Cấu hình đã đổi so với transcript hiện tại ({', '.join(drift)}). "
            "Bấm **Hội thoại mới** để mỗi transcript chỉ chứa một artifact version.",
            icon=":material/sync_problem:",
        )
    for past_turn in st.session_state.turns:
        render_turn(past_turn)
    if not st.session_state.turns:
        with st.container(border=True):
            st.subheader("Bắt đầu bằng một sự cố", icon=":material/add_comment:")
            st.caption(
                "Mỗi kịch bản kiểm tra một quyết định của agent: chọn đúng tool, hỏi lại khi thiếu mã, "
                "gọi hai tool cùng lúc, và xin xác nhận trước khi ghi dữ liệu."
            )
            selected = st.pills("Kịch bản demo", list(SCENARIOS), key="scenario", label_visibility="collapsed")
            if selected:
                st.session_state.pending_prompt = SCENARIOS[selected]

with log_tab:
    render_log_tab()

with artifact_tab:
    render_artifact_tab(artifact)

prompt = st.chat_input("Mô tả sự cố IT, ví dụ: máy LT-204 không vào được VPN", submit_mode="disable", disabled=bool(drift))
prompt = prompt or st.session_state.pending_prompt
if prompt and not drift:
    st.session_state.pending_prompt = None
    with chat_tab:
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
    st.rerun()
