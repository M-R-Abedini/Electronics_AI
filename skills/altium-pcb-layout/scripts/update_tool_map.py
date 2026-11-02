"""Create a versioned schema reference from a complete exported suite catalog.

This helper never starts a worker, calls a CAD tool or changes client settings.
Export all pages first; source-schema exports must be labelled as such.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

SELECTED = {
    "eda__pcb_plan_placement", "eda__pcb_check_placement_collision",
    "eda__pcb_move_components", "eda__pcb_get_components",
    "eda__pcb_get_layer_stackup", "eda__pcb_get_board_outline",
    "eda__pcb_get_nets", "eda__pcb_get_unrouted_nets", "eda__pcb_run_drc",
    "eda__pcb_render_svg", "eda__route_plan", "eda__route_plan_repairs",
    "eda__design_next_action", "eda__design_session_log",
    "eda__pcb_capture_snapshot", "eda__design_read_snapshot",
    "suite__create_checkpoint", "suite__list_checkpoints",
    "suite__restore_checkpoint",
}
REQUIRED_LAYOUT = {
    "layout__capture_context", "layout__plan_placement", "layout__validate_plan",
    "layout__apply_plan", "layout__get_evidence",
}


def generate(catalog: dict) -> dict:
    if not isinstance(catalog, dict) or catalog.get("complete") is not True:
        raise ValueError("A complete exported catalog is required")
    rows = catalog.get("tools")
    if (not isinstance(rows, list) or catalog.get("total") != len(rows)
            or catalog.get("next_offset") is not None or catalog.get("backend_errors")):
        raise ValueError("Consolidate every catalog page without backend errors first")
    provenance = catalog.get("provenance", {})
    if provenance.get("kind") not in {"runtime_catalog", "source_schema_export"}:
        raise ValueError("Declare runtime_catalog or source_schema_export provenance")
    names = set()
    selected = []
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or "__" not in name or name in names:
            raise ValueError("Catalog names must be unique qualified tool names")
        names.add(name)
        Draft202012Validator.check_schema(row["inputSchema"])
        if row.get("outputSchema") is not None:
            Draft202012Validator.check_schema(row["outputSchema"])
        if name in SELECTED or name.startswith(("layout__", "si_pi__")):
            selected.append({key: row[key] for key in (
                "name", "description", "inputSchema", "outputSchema", "annotations"
            ) if key in row})
    if missing := REQUIRED_LAYOUT - names:
        raise ValueError("Required layout capabilities absent: " + ", ".join(sorted(missing)))
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return {
        "schema_version": 1,
        "provenance": provenance,
        "native_editor_verified_by_generator": False,
        "catalog_sha256": hashlib.sha256(encoded).hexdigest(),
        "catalog_total": len(rows),
        "selected_tool_count": len(selected),
        "tools": sorted(selected, key=lambda row: row["name"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "references/tool-map.json")
    args = parser.parse_args()
    if args.catalog.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Catalog exceeds the helper's 32 MiB input bound")
    result = generate(json.loads(args.catalog.read_text(encoding="utf-8-sig")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2,
                                     ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "catalog_sha256", "catalog_total", "selected_tool_count"
    )}))


if __name__ == "__main__":
    main()
