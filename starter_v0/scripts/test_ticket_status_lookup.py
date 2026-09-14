#!/usr/bin/env python
"""
test_ticket_status_lookup.py — smoke test for the team-built ticket_status_lookup bonus tool.

Checks, without network or model calls:
  1. Registry and tools.yaml declare the tool exactly once.
  2. Committed mock ticket in helpdesk_data/tickets/ is found.
  3. Malformed IDs (asset ID, employee ID, empty, non-string) are rejected.
  4. Unknown ticket IDs return ticket_not_found.
  5. Credential-like text in a ticket summary is redacted.
  6. The tool never writes files.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import TOOL_FUNCTIONS, load_tool_declarations

# tools/__init__.py re-exports the function under the package name, so load the module explicitly.
lookup_module = importlib.import_module("tools.ticket_status_lookup.tool")


def check(label: str, condition: bool, detail: object = "") -> bool:
    print(f"    [{'PASS' if condition else 'FAIL'}] {label}" + ("" if condition else f" -> {detail}"))
    return condition


def run() -> bool:
    print("=" * 65)
    print(" [Bonus] ticket_status_lookup smoke test")
    print("=" * 65)
    results: list[bool] = []
    lookup = TOOL_FUNCTIONS["ticket_status_lookup"]

    print("[1] Registry and declaration")
    names = [tool["name"] for tool in load_tool_declarations(ROOT / "artifacts" / "tools.yaml")]
    results.append(check("declared once in tools.yaml", names.count("ticket_status_lookup") == 1, names))

    print("[2] Committed mock ticket")
    found = lookup("lab-a1b2c3d4")
    results.append(check("fixture found (case-insensitive ID)", found.get("status") == "found", found))
    results.append(check("returns normalized ticket_id", found.get("ticket_id") == "LAB-A1B2C3D4", found))

    print("[3] Malformed identifiers are rejected")
    for bad in ("LT-204", "EMP-1001", "LAB-XYZ", ""):
        result = lookup(bad)
        expected = "missing_ticket_id" if bad == "" else "invalid_ticket_id_format"
        results.append(check(f"{bad or '<empty>'} -> {expected}", result.get("error") == expected, result))
    result = lookup(12345)
    results.append(check("non-string -> invalid_ticket_id_type", result.get("error") == "invalid_ticket_id_type", result))

    print("[4] Unknown ticket")
    result = lookup("LAB-00000000")
    results.append(check("LAB-00000000 -> ticket_not_found", result.get("error") == "ticket_not_found", result))

    print("[5] Secret redaction")
    with tempfile.TemporaryDirectory() as temp:
        original = lookup_module.TICKET_DIR
        lookup_module.TICKET_DIR = Path(temp)
        try:
            (Path(temp) / "LAB-DEADBEEF.json").write_text(
                json.dumps({"ticket_id": "LAB-DEADBEEF", "summary": "VPN loi, password: hunter2", "priority": "low"}),
                encoding="utf-8",
            )
            result = lookup("LAB-DEADBEEF")
        finally:
            lookup_module.TICKET_DIR = original
    summary = result.get("summary", "")
    results.append(check("password value removed from summary", "hunter2" not in summary and "[REDACTED]" in summary, result))

    print("[6] No side effects")
    before = sorted(path.name for path in (ROOT / "helpdesk_data" / "tickets").glob("*"))
    lookup("LAB-A1B2C3D4")
    after = sorted(path.name for path in (ROOT / "helpdesk_data" / "tickets").glob("*"))
    results.append(check("fixture directory unchanged", before == after, (before, after)))

    passed = all(results)
    print("=" * 65)
    print(f"[{'SUCCESS' if passed else 'FAIL'}] {sum(results)}/{len(results)} checks passed")
    print("=" * 65)
    return passed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
