# SPDX-License-Identifier: MIT
"""MCP tools ``placer__inspect`` and ``placer__score`` (architecture 9, plan F0b task 7).

``PlacerService`` follows the suite component contract (``list_tools`` and
async ``call_tool``); the suite gateway prefixes the namespace, so the tools
reach clients as ``placer__inspect`` and ``placer__score``.  The same service
also runs as a small standalone stdio server (``pixi run placer-mcp``) for
development in Claude Code.

Both tools only read the user's board; every output goes to a new run folder.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import report as run_report
from .eval.score import evaluate
from .io.kicad import read_board
from .ir import Board
from .render import Panel, gallery
from .route.check import route_check
from .toolchain import RUNTIME_DIR

_STRING = {"type": "string"}


def _inspect_summary(board: Board) -> dict:
    by_class: dict[str, int] = {}
    for net in board.nets:
        by_class[net.netclass] = by_class.get(net.netclass, 0) + 1
    return {
        "name": board.name,
        "source": board.source,
        "footprints": len(board.footprints),
        "sides": board.side_counts(),
        "fixed_parts": sorted(board.fixed_refs()),
        "copper_layers": [cl.name for cl in board.copper_layers],
        "board_area_mm2": board.board_area_mm2,
        "outline_valid": board.outline_valid,
        "stackup": {"source": board.stackup.source, "policy": board.stackup.policy,
                    "board_thickness_mm": board.stackup.board_thickness_mm,
                    "layers": [asdict(layer) for layer in board.stackup.layers if layer.type in ("copper", "core",
                                                                                                  "prepreg")],
                    "roles": [asdict(r) for r in board.stackup.roles]},
        "rules": board.rules,
        "netclasses": [asdict(c) for c in board.netclasses],
        "nets": {"count": len(board.nets), "by_class": dict(sorted(by_class.items()))},
        "diff_pairs": [asdict(p) for p in board.diff_pairs],
        "rule_areas": len(board.rule_areas),
        "obstacles": {"mask_apertures": sum(1 for o in board.obstacles if o["kind"] == "mask_aperture"),
                      "copper_graphics": sum(1 for o in board.obstacles if o["kind"] == "copper_graphic")},
        "capabilities": asdict(board.capabilities),
        "drc_severity_overrides": board.drc_severities,
        "unknowns": board.unknowns,
        "warnings": board.warnings,
    }


class PlacerService:
    """Suite component: namespace ``placer``."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.runs = Path(data_dir) / "placer_runs" if data_dir else RUNTIME_DIR / "placer_runs"

    @staticmethod
    def list_tools() -> list[dict[str, Any]]:
        definitions = [
            ("inspect",
             ("Read a KiCad board (any format KiCad 10 opens) into the placer's Board IR and summarise it: layers and "
             "inferred layer roles, stackup and its policy (locked / needs_decision), sides, net classes, "
             "differential pairs, fixed parts, unknowns and capacity warnings. Read-only; does not modify the board."),
             {"board_path": _STRING}, ["board_path"], True),
            ("score",
             ("Evaluate the board's current placement: legality gate (courtyard overlap, outline, keepouts), HPWL, "
             "ratsnest crossings, RUDY, critical-loop proxies, regularity and Pro Score v0 (uncalibrated). With "
             "route=true it also runs the strict Freerouting route check (about 1 to 5 minutes) and reports DRC "
             "attribution, coverage and every unrouted connection with its cause. Writes run_report.json and an "
             "HTML gallery to a new run folder; never writes the board."),
             {"board_path": _STRING, "route": {"type": "boolean", "default": True}}, ["board_path"], True),
        ]
        return [{"name": name, "description": description,
                 "inputSchema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False},
                 "annotations": {"readOnlyHint": readonly, "openWorldHint": False}}
                for name, description, props, required, readonly in definitions]

    # ------------------------------------------------------------------ tools
    def inspect(self, board_path: str) -> dict:
        return _inspect_summary(read_board(board_path))

    def score(self, board_path: str, route: bool = True) -> dict:
        board = read_board(board_path)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = (self.runs / f"{board.name}-{stamp}").resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        routed = route_check(board_path, out_dir=run_dir / "route") if route else None
        evaluation = evaluate(board, routed["metrics"] if routed else None)
        page = gallery([Panel(f"{board.name} (current placement)", board, evaluation,
                              routed["metrics"] if routed else None)], run_dir / "gallery.html",
                       title=f"{board.name}: score")
        gates = [run_report.gate("legality_violations", evaluation["legality"]["violations"], 0, "==")]
        if routed:
            gates += routed["gates"]
        rep = run_report.build(
            kind="score", inputs={"board": board.source}, config={"route": route},
            stages={"evaluate": {"legality": {k: v for k, v in evaluation["legality"].items() if k != "findings"}},
                    "route_check": {"run_report": str(run_dir / "route" / "run_report.json")} if routed else None},
            metrics={**evaluation["metrics"], "pro_score": evaluation["pro_score"],
                     "route": routed["metrics"] if routed else None},
            gates=gates,
            failures=(routed["failures"] if routed else []) + [
                {"stage": "legality", "cause": f["kind"], "refs": f["refs"], "waived_by": f["waived_by"]}
                for f in evaluation["legality"]["findings"] if not f["fixed"]],
            artifacts={"run_dir": str(run_dir), "gallery": str(page)},
            unknowns=board.unknowns, warnings=board.warnings,
        )
        path = run_report.write(rep, run_dir / "run_report.json")
        return {
            "passed": rep["passed"],
            "gates": [{k: g[k] for k in ("name", "value", "op", "threshold", "passed")} for g in gates],
            "legality": {k: v for k, v in evaluation["legality"].items() if k != "findings"},
            "unwaived_findings": [f for f in evaluation["legality"]["findings"] if not f["waived_by"] and not f["fixed"]],
            "metrics": {"hpwl_signal_mm": evaluation["metrics"]["hpwl"]["signal_mm"],
                        "crossings": evaluation["metrics"]["crossings"],
                        "rudy": evaluation["metrics"]["rudy"],
                        "critical_loops": evaluation["metrics"]["critical_loops"],
                        "regularity": evaluation["metrics"]["regularity"]},
            "pro_score": evaluation["pro_score"],
            "route": ({k: routed["metrics"][k] for k in ("coverage", "unrouted", "vias", "track_length_mm",
                                                         "router_stop", "seconds_total")}
                      | {"unrouted_causes": [{"net": f["net"], "cause": f["cause"]} for f in routed["failures"]]}
                      if routed else None),
            "unknowns": board.unknowns,
            "warnings": board.warnings,
            "run_report": str(path),
            "gallery": str(page),
        }

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        functions = {"inspect": self.inspect, "score": self.score}
        if name not in functions:
            raise ValueError(f"Unknown placer tool: {name}")
        return await asyncio.to_thread(functions[name], **(arguments or {}))

    async def call_tool_async(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.call_tool(name, arguments)


def serve() -> None:
    """Standalone stdio server exposing the tools as ``placer__<name>`` (development use)."""
    import json

    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    service = PlacerService()
    server = Server("electronics-ai-placer")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name="placer__" + t["name"], description=t["description"], inputSchema=t["inputSchema"],
                           annotations=types.ToolAnnotations(**t["annotations"])) for t in service.list_tools()]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        if not name.startswith("placer__"):
            raise ValueError(f"Unknown tool: {name}")
        result = await service.call_tool(name.removeprefix("placer__"), arguments)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=1))]

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    serve()
