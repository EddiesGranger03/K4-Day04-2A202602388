"""Team artifacts surfaced in the UI: eval sets, run files, version log, tool metadata, Role B checks.

Grading, run parsing and schema checks call the team's own code (run_eval.py, scripts/*.py)
so the UI never disagrees with the evidence the lab is graded on.
"""
from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import yaml

from chat import ROOT
from run_eval import evaluate_phase_b


DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
TOOLS_DIR = ROOT / "tools"
SCRIPTS_DIR = ROOT / "scripts"
VERSION_LOG_PATH = ROOT / "artifacts" / "version_log.csv"

EVAL_SETS = {
    "Nhóm": "eval_group.json",
    "Base": "eval_base.json",
    "Extension": "eval_helpdesk_extension.json",
    "Adversarial": "eval_adversarial.json",
}

# README: six core tools, three built-in advanced tools; anything else is team-built.
CORE_TOOLS = {"clarify", "search_kb", "check_service_status", "inspect_device", "lookup_user", "format_incident_report"}
ADVANCED_TOOLS = {"policy", "create_ticket", "search_device_info"}


# ---------------------------------------------------------------- scripts

def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"team_script_{name}", SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_captured(check: Callable[[], bool]) -> dict[str, Any]:
    """Run a script entry point that reports by printing, and keep its output."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            passed = bool(check())
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}")
            passed = False
    return {"passed": passed, "output": buffer.getvalue()}


def validate_tools_schema() -> dict[str, Any]:
    return run_captured(load_script("validate_tools_schema").validate)


def check_tavily_boundary() -> dict[str, Any]:
    return run_captured(load_script("test_tavily_boundary").test_tavily_boundary)


# ---------------------------------------------------------------- eval sets

def load_eval_set(file_name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / file_name).read_text(encoding="utf-8"))


def case_user_turns(case: dict[str, Any]) -> list[str]:
    if "turns" in case:
        return [turn["content"] for turn in case["turns"] if turn.get("role") == "user"]
    return [case.get("input") or case.get("query", "")]


def expected_summary(case: dict[str, Any]) -> str:
    expect = case.get("expect", {})
    if expect.get("no_tool"):
        return f"không gọi tool ({expect.get('behavior', 'no_tool')})"
    return ", ".join(
        f"{call['name']}({', '.join(f'{k}={v}' for k, v in call.get('args', {}).items())})"
        for call in expect.get("tool_calls", [])
    )


def grade_turn(case: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    """Grade the final replayed turn with run_eval's grader (first model round = eval's single call)."""
    if turn.get("status") == "provider_error":
        return {"case_id": case["id"], "passed": False, "failures": [turn.get("error", "provider_error")], "provider_error": True}
    rounds = turn.get("rounds") or []
    calls = rounds[0].get("tool_calls", []) if rounds else []
    result = evaluate_phase_b(case, calls, turn.get("assistant_text"))
    return {"case_id": case["id"], "expected": expected_summary(case), **result}


# ---------------------------------------------------------------- run files

def list_run_files() -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    return sorted(RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def load_run(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_is_valid(run: dict[str, Any]) -> bool:
    summary = run.get("summary", {})
    return summary.get("provider_error_cases") == 0 and summary.get("measured_cases") == summary.get("total_cases")


def run_summary_row(path: Path, run: dict[str, Any]) -> dict[str, Any]:
    summary = run.get("summary", {})
    return {
        "File": path.name,
        "Version": run.get("version"),
        "Suite": run.get("suite"),
        "Dataset": run.get("dataset_role") or run.get("dataset_id"),
        "Model": run.get("model"),
        "Cases": summary.get("total_cases"),
        "Case accuracy": summary.get("case_accuracy"),
        "Routing": summary.get("tool_routing_accuracy"),
        "Arguments": summary.get("argument_accuracy"),
        "Multi-turn": summary.get("multiturn_accuracy"),
        "Hợp lệ": run_is_valid(run),
    }


def run_case_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    row_for = load_script("parse_runs").row_for
    return [row_for(run, item) for item in run.get("results", [])]


# ---------------------------------------------------------------- version log and tools

def load_version_log() -> list[dict[str, str]]:
    if not VERSION_LOG_PATH.exists():
        return []
    with VERSION_LOG_PATH.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def matching_version(rows: list[dict[str, str]], prompt_hash: str, tools_hash: str) -> str | None:
    for row in reversed(rows):
        row_prompt = (row.get("prompt_hash") or "")[:12]
        row_tools = (row.get("tools_hash") or "")[:12]
        if row_prompt and row_tools and prompt_hash.startswith(row_prompt) and tools_hash.startswith(row_tools):
            return row.get("version")
    return None


def tool_group(name: str) -> str:
    if name in CORE_TOOLS:
        return "core"
    if name in ADVANCED_TOOLS:
        return "advanced có sẵn"
    return "bonus nhóm tự xây"


def tool_metadata(name: str) -> dict[str, Any]:
    path = TOOLS_DIR / name / "TOOL.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except yaml.YAMLError:
        return {}
