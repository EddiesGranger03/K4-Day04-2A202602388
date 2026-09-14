"""Streamlit live-chat UI for the IT Helpdesk Agent.

Reuses `run_model_tool_loop` from chat.py so the UI, CLI chat and eval evidence
share one agent loop. Run from starter_v0/:  streamlit run app.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from chat import ARTIFACTS_DIR, ROOT, now_iso, run_model_tool_loop, safe_slug, trim_history, write_transcript
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version


PROVIDERS = ["openrouter", "openai", "anthropic", "gemini"]
SYSTEM_PROMPT_PATH = ARTIFACTS_DIR / "system_prompt.md"
TOOLS_PATH = ARTIFACTS_DIR / "tools.yaml"
TRANSCRIPTS_DIR = ROOT / "transcripts"
TICKETS_DIR = ROOT / "tickets"

# Starter prompts for the four demo stories: normal lookup, missing identifier,
# multi-tool request and write-action boundary.
DEMO_PROMPTS = {
    ":material/dns: Trạng thái dịch vụ": "Wi-Fi production hôm nay có sự cố gì không?",
    ":material/help: Thiếu mã máy": "Laptop của mình không vào được VPN, kiểm tra máy giúp nhé.",
    ":material/call_split: Nhiều tool": "Email production có lỗi không, và kiểm tra luôn máy LT-318 giúp mình.",
    ":material/confirmation_number: Tạo ticket": "Tạo ticket mức medium cho lỗi máy in tầng 3.",
}

STATUS_BADGES = {
    "answered": ("green", ":material/check_circle:", "answered"),
    "waiting_for_user": ("orange", ":material/pending:", "waiting_for_user"),
    "max_tool_rounds": ("red", ":material/warning:", "max_tool_rounds"),
    "provider_error": ("red", ":material/error:", "provider_error"),
}

# Transcript header fields that must stay constant within one transcript file,
# otherwise the evidence would mix artifact versions or run settings.
SESSION_KEYS = ("artifact_version", "provider", "model", "history_window", "max_tool_rounds")


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
    transcript_id = "_".join([
        safe_slug(artifact.version),
        safe_slug(settings["provider"]),
        "ui",
        timestamp,
    ])
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


def run_turn(user_text: str, settings: dict[str, Any], artifact: Any) -> dict[str, Any]:
    """Mirror chat.py main(): same message layout, same loop, same turn record."""
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
    try:
        result = run_model_tool_loop(
            provider=make_provider(settings["provider"]),
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

    turn["ended_at"] = now_iso()
    # Filesystem evidence for the action boundary: which ticket files this turn wrote.
    turn["tickets_created"] = sorted(ticket_files() - tickets_before)

    st.session_state.turns.append(turn)
    transcript = st.session_state.transcript
    transcript["turns"].append(turn)
    write_transcript(st.session_state.transcript_path, transcript)
    return turn


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


def render_assistant_text(text: str | None) -> None:
    if not text:
        st.markdown("_(empty response)_")
        return
    structured = parse_structured_reply(text)
    if structured is None:
        st.markdown(text)
        return
    st.markdown(str(structured.get("reply", "")))
    meta = [f"{key} `{structured[key]}`" for key in ("intent", "action", "evidence_ids") if key in structured]
    if meta:
        st.caption(" · ".join(meta))


def event_icon(result: Any) -> str:
    if not isinstance(result, dict):
        return ":material/check:"
    if result.get("error"):
        return ":material/error:"
    if result.get("awaiting_user"):
        return ":material/pending:"
    if result.get("status") == "created":
        return ":material/edit_document:"
    if result.get("status") == "needs_confirmation":
        return ":material/lock:"
    return ":material/check:"


def render_round(round_record: dict[str, Any]) -> None:
    number = round_record["round"]
    calls = round_record.get("tool_calls", [])
    events = round_record.get("tool_results", [])

    if not calls:
        st.expander(f"Round {number} · không gọi tool, trả lời trực tiếp", type="step", icon=":material/chat:")
        return

    for index, call in enumerate(calls):
        executed = index < len(events)
        result = events[index].get("result") if executed else None
        icon = event_icon(result) if executed else ":material/block:"
        with st.expander(f"Round {number} · `{call['name']}`", type="step", icon=icon):
            if index == 0 and round_record.get("assistant_text"):
                st.caption(f"Model text: {round_record['assistant_text']}")
            st.markdown("**Arguments**")
            st.json(call.get("args", {}))
            if not executed:
                st.info("Không thực thi: loop đã dừng ở tool trước để chờ người dùng.", icon=":material/pause:")
                continue
            if isinstance(result, dict) and result.get("error"):
                st.error(f"Tool error: `{result.get('error')}` {result.get('message', '')}", icon=":material/error:")
            st.markdown("**Result**")
            st.json(result)


def render_turn(turn: dict[str, Any]) -> None:
    with st.chat_message("user"):
        st.markdown(turn["user"])

    with st.chat_message("assistant", avatar=":material/support_agent:"):
        for round_record in turn.get("rounds", []):
            render_round(round_record)

        if turn["status"] == "provider_error":
            st.error(turn.get("error", "Provider error"), icon=":material/error:")
        else:
            render_assistant_text(turn.get("assistant_text"))

        color, icon, label = STATUS_BADGES.get(turn["status"], ("gray", ":material/info:", turn["status"]))
        tool_count = len(turn.get("tool_events", []))
        with st.container(horizontal=True):
            st.badge(label, icon=icon, color=color)
            st.badge(f"{len(turn.get('rounds', []))} round · {tool_count} tool call", icon=":material/build:", color="gray")
            if turn.get("tickets_created"):
                st.badge(
                    f"Ticket file đã ghi: {', '.join(turn['tickets_created'])}",
                    icon=":material/edit_document:",
                    color="orange",
                )


def render_sidebar() -> tuple[dict[str, Any], Any]:
    with st.sidebar:
        st.header("Cấu hình run")
        provider = st.selectbox("Provider", PROVIDERS, key="provider")
        default_model = getattr(make_provider(provider), "default_model", "")
        model = st.text_input("Model", value=default_model, key=f"model_{provider}").strip()
        version = st.text_input("Version label", value="v0", key="version").strip() or "v0"
        history_window = st.number_input("History window (cặp user/assistant)", 0, 20, 5, key="history_window")
        max_tool_rounds = st.number_input("Max tool rounds", 1, 10, 4, key="max_tool_rounds")
        st.button(
            "Hội thoại mới",
            icon=":material/add_comment:",
            on_click=reset_conversation,
            width="stretch",
        )

        artifact = build_artifact_version(version, SYSTEM_PROMPT_PATH, TOOLS_PATH)
        st.subheader("Artifact version")
        st.code(artifact.artifact_version, language=None, wrap_lines=True)
        st.markdown(f"prompt_hash `{artifact.prompt_hash[:12]}`  \ntools_hash `{artifact.tools_hash[:12]}`")

        st.subheader("Transcript")
        if st.session_state.transcript_path:
            st.code(str(Path(st.session_state.transcript_path).relative_to(ROOT)), language=None, wrap_lines=True)
        else:
            st.caption("Tạo khi gửi tin nhắn đầu tiên.")

        with st.expander("Tools model nhìn thấy", icon=":material/build:"):
            for item in load_tool_declarations(TOOLS_PATH):
                st.markdown(f"**`{item['name']}`**: {item.get('description', '')}")

    settings = {
        "provider": provider,
        "model": model or None,
        "history_window": int(history_window),
        "max_tool_rounds": int(max_tool_rounds),
    }
    return settings, artifact


st.set_page_config(page_title="IT Helpdesk Agent", page_icon=":material/support_agent:", layout="wide")
init_state()
settings, artifact = render_sidebar()

st.title("IT Helpdesk Agent")
st.caption("Northstar Labs service desk (dữ liệu giả lập) · mỗi bước tool hiển thị arguments và result/error thật.")

drift = session_drift(settings, artifact)
if drift:
    st.warning(
        f"Cấu hình đã đổi so với transcript hiện tại ({', '.join(drift)}). "
        "Bấm **Hội thoại mới** để transcript chỉ chứa một artifact version.",
        icon=":material/sync_problem:",
    )

for past_turn in st.session_state.turns:
    render_turn(past_turn)

if not st.session_state.turns:
    selected = st.pills("Kịch bản demo", list(DEMO_PROMPTS), key="demo_pill", label_visibility="collapsed")
    if selected:
        st.session_state.pending_prompt = DEMO_PROMPTS[selected]

prompt = st.chat_input("Mô tả sự cố IT…", submit_mode="disable", disabled=bool(drift))
prompt = prompt or st.session_state.pending_prompt
if prompt and not drift:
    st.session_state.pending_prompt = None
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.spinner("Agent đang xử lý…"):
        run_turn(prompt, settings, artifact)
    st.rerun()
