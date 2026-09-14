#!/usr/bin/env python
"""
test_tavily_boundary.py — Tool & Schema Engineer (Role B) Privacy Boundary & Tavily Checker.

Verifies:
  1. Internal identifier exfiltration prevention (blocks LT-xxx, EMP-xxx).
  2. Input validation (type checking, required public identity).
  3. Live Tavily Search API connectivity (if TAVILY_API_KEY is configured in .env).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env_loader import load_lab_env
from tools import TOOL_FUNCTIONS


def test_tavily_boundary() -> bool:
    print("=" * 65)
    print(" [Role B] Tavily API & Privacy Boundary Security Test")
    print("=" * 65)

    load_lab_env(ROOT)
    search_tool = TOOL_FUNCTIONS.get("search_device_info")
    if not search_tool:
        print("[-] ERROR: 'search_device_info' not found in TOOL_FUNCTIONS.")
        return False

    all_passed = True

    # 1. Test Privacy Boundary: Blocking Asset ID exfiltration
    print("[1] Testing Boundary Guardrail: Block internal Asset ID (LT-204)...")
    res_asset = search_tool("Lenovo", "ThinkPad T14 Gen 4 LT-204", "drivers", 2)
    if res_asset.get("error") == "restricted_internal_identifier":
        print("    [PASS] Correctly blocked internal asset identifier (restricted_internal_identifier).")
    else:
        print(f"    [FAIL] Boundary guardrail failed! Result: {res_asset}")
        all_passed = False

    # 2. Test Privacy Boundary: Blocking Employee ID exfiltration
    print("[2] Testing Boundary Guardrail: Block Employee ID (EMP-1001)...")
    res_emp = search_tool("Dell", "Latitude 5420 EMP-1001", "specs", 2)
    if res_emp.get("error") == "restricted_internal_identifier":
        print("    [PASS] Correctly blocked employee identifier (restricted_internal_identifier).")
    else:
        print(f"    [FAIL] Boundary guardrail failed! Result: {res_emp}")
        all_passed = False

    # 3. Test Invalid Query Type
    print("[3] Testing Schema Validation: Reject invalid query_type...")
    res_invalid_type = search_tool("Lenovo", "ThinkPad T14", "hack_tool", 2)
    if res_invalid_type.get("error") == "invalid_query_type":
        print("    [PASS] Correctly rejected invalid query_type (invalid_query_type).")
    else:
        print(f"    [FAIL] Invalid query type was not rejected! Result: {res_invalid_type}")
        all_passed = False

    # 4. Test Live Tavily API Connectivity
    print("[4] Checking Tavily API Key & Live Connectivity...")
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key or api_key.startswith("tvly-placeholder") or api_key == "...":
        print("    [NOTE] TAVILY_API_KEY is not set or using placeholder in .env.")
        print("           (Local security & boundary guards passed. Live web call skipped).")
    else:
        try:
            print("    [i] Attempting live search query to Tavily API...")
            res_live = search_tool("Lenovo", "ThinkPad T14 Gen 4", "drivers", 2)
            if res_live.get("error"):
                print(f"    [WARN] Tavily API returned error: {res_live.get('error')}")
            else:
                items = res_live.get("items", [])
                domains = res_live.get("official_domains", [])
                print(f"    [PASS] Live Tavily search succeeded! Returned {len(items)} results from domains: {domains}")
        except Exception as exc:
            print(f"    [WARN] Network error during live Tavily call: {exc}")

    print("=" * 65)
    if all_passed:
        print("[SUCCESS] All privacy boundary guardrails and schema tests PASSED!")
    else:
        print("[FAIL] Some boundary tests failed. Check implementation.")
    print("=" * 65)
    return all_passed


if __name__ == "__main__":
    success = test_tavily_boundary()
    sys.exit(0 if success else 1)
