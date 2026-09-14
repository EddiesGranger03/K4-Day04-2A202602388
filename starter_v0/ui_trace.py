"""Instrumentation for the Streamlit UI: per-round timing, model reasoning and an event log.

The provider is wrapped instead of editing chat.py, so the UI still runs the exact
`run_model_tool_loop` used by the CLI chat.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable


def now_clock() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def format_ms(ms: int | None) -> str:
    if ms is None:
        return "–"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s".replace(".", ",")


def _usage_numbers(raw: Any) -> dict[str, int | None]:
    usage = getattr(raw, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
    }


def _reasoning_text(raw: Any) -> str:
    """OpenAI-compatible providers (OpenRouter) put reasoning in message extras."""
    try:
        message = raw.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return ""
    extra = getattr(message, "model_extra", None) or {}
    return str(extra.get("reasoning") or extra.get("reasoning_content") or "").strip()


class TracingProvider:
    """Provider proxy that records one entry per model call made by the agent loop."""

    def __init__(
        self,
        inner: Any,
        *,
        on_start: Callable[[int], None] | None = None,
        on_end: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.inner = inner
        self.default_model = getattr(inner, "default_model", None)
        self.on_start = on_start
        self.on_end = on_end
        self.calls: list[dict[str, Any]] = []
        self._perf: list[tuple[float, float]] = []

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> Any:
        round_index = len(self.calls) + 1
        if self.on_start:
            self.on_start(round_index)
        record: dict[str, Any] = {"round": round_index, "started_at": now_clock()}
        start = time.perf_counter()
        try:
            response = self.inner.complete(messages, tools, **kwargs)
        except Exception as exc:
            end = time.perf_counter()
            record.update({"model_ms": round((end - start) * 1000), "error": f"{type(exc).__name__}: {exc}"})
            self._store(record, start, end)
            raise
        end = time.perf_counter()
        record.update({
            "model_ms": round((end - start) * 1000),
            "reasoning": _reasoning_text(response.raw),
            "tool_names": [call.name for call in response.tool_calls],
            **_usage_numbers(response.raw),
        })
        self._store(record, start, end)
        return response

    def _store(self, record: dict[str, Any], start: float, end: float) -> None:
        self.calls.append(record)
        self._perf.append((start, end))
        if self.on_end:
            self.on_end(record)

    def finalize(self, loop_end: float) -> list[dict[str, Any]]:
        """Attach tool time: the gap between a model call and the next call (or loop end)."""
        for index, record in enumerate(self.calls):
            if not record.get("tool_names"):
                continue
            next_start = self._perf[index + 1][0] if index + 1 < len(self._perf) else loop_end
            record["tool_ms"] = max(0, round((next_start - self._perf[index][1]) * 1000))
        return self.calls


def turn_log(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one transcript turn into log rows for the log table and CSV export."""
    index = turn.get("turn_index")
    rows: list[dict[str, Any]] = [
        {"time": turn.get("started_at"), "turn": index, "round": None, "kind": "user", "name": "user", "duration_ms": None, "detail": turn.get("user")},
    ]
    rounds = {item["round"]: item for item in turn.get("rounds", [])}
    for call in turn.get("trace", []):
        number = call["round"]
        rows.append({
            "time": call.get("started_at"),
            "turn": index,
            "round": number,
            "kind": "error" if call.get("error") else "model",
            "name": "model call",
            "duration_ms": call.get("model_ms"),
            "detail": call.get("error") or call.get("reasoning") or "",
        })
        for event in rounds.get(number, {}).get("tool_results", []):
            result = event.get("result")
            failed = isinstance(result, dict) and bool(result.get("error"))
            rows.append({
                "time": call.get("started_at"),
                "turn": index,
                "round": number,
                "kind": "tool_error" if failed else "tool",
                "name": event.get("tool"),
                "duration_ms": call.get("tool_ms"),
                "detail": f"args={event.get('args')} result={result}",
            })
    for ticket in turn.get("tickets_created", []):
        rows.append({"time": turn.get("ended_at"), "turn": index, "round": None, "kind": "ticket_write", "name": "create_ticket", "duration_ms": None, "detail": ticket})
    rows.append({
        "time": turn.get("ended_at"),
        "turn": index,
        "round": None,
        "kind": "reply" if turn.get("status") != "provider_error" else "error",
        "name": turn.get("status"),
        "duration_ms": turn.get("total_ms"),
        "detail": turn.get("assistant_text") or turn.get("error") or "",
    })
    return rows
