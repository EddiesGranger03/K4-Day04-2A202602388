from __future__ import annotations

import json
import re
from typing import Any

from tools._shared import ROOT, err


TICKET_DIR = ROOT / "tickets"
TICKET_ID_PATTERN = re.compile(r"^LAB-[A-Fa-f0-9]{8}$")
SENSITIVE_DATA_PATTERN = re.compile(
    r"\b(?:password|passwd|token|api[ _-]?key|mfa|otp|recovery[ _-]?code)"
    r"(?:\s*[:=]\s*|\s+(?:is|la|l\u00e0)\s+)\S+",
    re.IGNORECASE,
)


def _redact_sensitive(text: str) -> str:
    return SENSITIVE_DATA_PATTERN.sub("[REDACTED]", text)


def ticket_status_lookup(ticket_id: str = "") -> dict[str, Any]:
    if not isinstance(ticket_id, str):
        return {"tool": "ticket_status_lookup", "error": "invalid_ticket_id_type"}

    normalized = (ticket_id or "").strip().upper()

    if not normalized:
        return {
            "tool": "ticket_status_lookup",
            "error": "missing_ticket_id",
            "message": "Provide a ticket ID in the format LAB-XXXXXXXX.",
        }

    if not TICKET_ID_PATTERN.fullmatch(normalized):
        return {
            "tool": "ticket_status_lookup",
            "error": "invalid_ticket_id_format",
            "message": f"Ticket ID '{normalized}' does not match LAB-XXXXXXXX. Check the format.",
            "expected_format": "LAB-XXXXXXXX (8 hex characters)",
        }

    ticket_path = TICKET_DIR / f"{normalized}.json"

    if not ticket_path.exists():
        return {
            "tool": "ticket_status_lookup",
            "error": "ticket_not_found",
            "ticket_id": normalized,
            "message": f"No ticket found with ID {normalized}.",
        }

    try:
        data = json.loads(ticket_path.read_text(encoding="utf-8-sig"))

        safe_summary = _redact_sensitive(str(data.get("summary", "")))

        return {
            "tool": "ticket_status_lookup",
            "status": "found",
            "ticket_id": data.get("ticket_id", normalized),
            "summary": safe_summary,
            "priority": data.get("priority", "unknown"),
            "asset_id": data.get("asset_id"),
            "created_at": data.get("created_at"),
            "source": data.get("source", "unknown"),
        }
    except Exception as exc:
        return err("ticket_status_lookup", exc)
