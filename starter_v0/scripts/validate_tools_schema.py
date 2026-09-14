#!/usr/bin/env python
"""
validate_tools_schema.py — Tool & Schema Engineer (Role B) Verification Utility.

Checks:
  1. YAML syntax and file readability.
  2. Completeness of declarations (name, description, parameters, properties, required).
  3. Strict synchronization between tools.yaml and tools/__init__.py (TOOL_FUNCTIONS).
  4. Conversion into OpenAI tool specification format.
  5. Parameter enum validity and types.
  6. Computes tools_hash (SHA-256) for version_log.csv tracking.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Adjust path to import from starter_v0
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from tools import TOOL_FUNCTIONS, load_tool_declarations, to_openai_tools
from versioning import file_hash, short_hash


def validate() -> bool:
    print("=" * 60)
    print(" [Role B] Tool & Schema Engineer — Validation Suite")
    print("=" * 60)

    tools_yaml_path = ROOT / "artifacts" / "tools.yaml"
    if not tools_yaml_path.exists():
        print(f"[-] ERROR: {tools_yaml_path} not found.")
        return False

    # 1. Load YAML
    try:
        declarations = load_tool_declarations(tools_yaml_path)
        print(f"[+] Successfully parsed {tools_yaml_path.name}: {len(declarations)} tools declared.")
    except Exception as exc:
        print(f"[-] ERROR parsing YAML: {exc}")
        return False

    # 2. Synchronize with Python TOOL_FUNCTIONS
    declared_names = {item["name"] for item in declarations if "name" in item}
    registered_names = set(TOOL_FUNCTIONS.keys())

    missing_in_code = declared_names - registered_names
    missing_in_yaml = registered_names - declared_names

    if missing_in_code:
        print(f"[-] ERROR: Tools in tools.yaml missing in tools/__init__.py: {missing_in_code}")
        return False
    if missing_in_yaml:
        print(f"[-] ERROR: Tools in tools/__init__.py missing in tools.yaml: {missing_in_yaml}")
        return False

    print(f"[+] Registry synchronization: 100% in sync ({len(declared_names)} tools).")

    # 3. Inspect each tool definition
    errors = []
    for tool in declarations:
        name = tool.get("name", "<unnamed>")
        desc = tool.get("description", "").strip()
        params = tool.get("parameters", {})

        if len(desc) < 40:
            errors.append(f"Tool '{name}' description is too short ({len(desc)} chars). Needs clearer boundary guidance.")

        if params.get("type") != "object":
            errors.append(f"Tool '{name}' parameters type must be 'object'.")

        props = params.get("properties", {})
        required = params.get("required", [])

        for req in required:
            if req not in props:
                errors.append(f"Tool '{name}' required field '{req}' not defined in properties.")

        for prop_name, prop_spec in props.items():
            if "enum" in prop_spec:
                enum_vals = prop_spec["enum"]
                if not isinstance(enum_vals, list) or len(enum_vals) == 0:
                    errors.append(f"Tool '{name}' property '{prop_name}' has invalid enum.")

    if errors:
        print("[-] Validation errors found:")
        for err in errors:
            print(f"    - {err}")
        return False

    print("[+] All parameter schemas, enums, and required fields validated successfully.")

    # 4. OpenAI format conversion
    try:
        openai_tools = to_openai_tools(declarations)
        assert len(openai_tools) == len(declarations)
        print("[+] Converted to OpenAI Function Calling format without errors.")
    except Exception as exc:
        print(f"[-] ERROR converting to OpenAI format: {exc}")
        return False

    # 5. Compute tools_hash
    current_tools_hash = file_hash(tools_yaml_path)
    print("-" * 60)
    print(f"[i] Current tools.yaml SHA-256 Hash : {current_tools_hash}")
    print(f"[i] Short Hash (for version_log.csv): {short_hash(current_tools_hash)}")
    print("=" * 60)
    print("[SUCCESS] All schema checks PASSED for Role B!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = validate()
    sys.exit(0 if success else 1)
