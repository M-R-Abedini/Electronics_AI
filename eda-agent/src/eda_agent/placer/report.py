# SPDX-License-Identifier: MIT
"""``run_report.json`` (architecture 4.6), schema version 0.

One file per run: tool versions and hashes, input hashes, configuration,
every gate with value and threshold, metrics, and every failure with net,
cause and stage.  Keys are sorted on write so two runs diff cleanly.
"""

from __future__ import annotations

import json
import operator
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__ as EDA_AGENT_VERSION
from . import toolchain

SCHEMA = "electronics-ai/run_report"
SCHEMA_VERSION = 0
_OPS = {"==": operator.eq, "<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt}


def gate(name: str, value: Any, threshold: Any, op: str = "==") -> dict:
    return {"name": name, "value": value, "op": op, "threshold": threshold,
            "passed": bool(_OPS[op](value, threshold))}


def tools() -> dict:
    kc = toolchain.kicad()
    fr = toolchain.freerouting()
    core_info: dict | None = None
    try:
        import placer_core  # type: ignore[import-not-found]

        core_info = {"version": getattr(placer_core, "__version__", "installed")}
    except ImportError:
        core_info = None
    return {
        "kicad": {"version": kc.version, "cli": str(kc.cli)},
        "java": {"version": fr.java_version, "path": str(fr.java)},
        "freerouting": {"version": fr.version, "sha256": fr.sha256, "jar": str(fr.jar)},
        "eda_agent": {"version": EDA_AGENT_VERSION},
        "placer_core": core_info,
    }


def build(*, kind: str, inputs: dict, config: dict, stages: dict, metrics: dict, gates: list[dict],
          failures: list[dict], **extra: Any) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "tools": tools(),
        "inputs": inputs,
        "config": config,
        "passed": all(g["passed"] for g in gates),
        "gates": gates,
        "metrics": metrics,
        "failures": failures,
        "stages": stages,
        **extra,
    }


def write(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False,
                                     mode="w", encoding="utf-8") as file:
        pending = Path(file.name)
        file.write(content)
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
    return path
