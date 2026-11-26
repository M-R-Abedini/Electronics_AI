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
import base64
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import types

from . import report as run_report
from .eval.score import evaluate
from .io.kicad import read_board
from .ir import Board
from .render import Panel, assembly_overlay_image, finding_image, gallery, review_images
from .route.check import route_check
from .toolchain import RUNTIME_DIR

_STRING = {"type": "string"}


def _stackup_guidance(board: Board) -> dict:
    """Physics-based, non-hardcoded stackup & GND-plane guidance for connected AI agents."""
    from .route.bga_escape import (
        MICROVIA_AUTO_PITCH_MM,
        check_hdi_rule_conflicts,
        detect_bga,
    )

    n_layers = len(board.copper_layers)
    layer_names = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    fine_bgas = []
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        bga = detect_bga(fp)
        if bga is not None and min(bga["pitch_x"], bga["pitch_y"]) <= MICROVIA_AUTO_PITCH_MM:
            fine_bgas.append({
                "ref": fp.ref,
                "side": fp.side,
                "pitch_mm": round(min(bga["pitch_x"], bga["pitch_y"]), 3),
                "balls": len(bga["grid"]),
            })
    hdi_conflicts = check_hdi_rule_conflicts(board)
    has_plane = any(r.role in ("plane", "mixed") for r in board.stackup.roles)
    if has_plane:
        return {
            "gnd_routing_mode": "plane_taps_and_refill",
            "fine_pitch_bgas": fine_bgas,
            "hdi_rule_conflicts": hdi_conflicts,
            "note": (
                "Active plane/pour detected: S6a SMD GND/power taps and post-route zone refill "
                "are enabled, and dedicated inner plane nets are excluded from point-to-point trace routing."
            ),
            "options": [],
        }
    options: list[dict[str, str]] = []
    if n_layers == 2:
        options.append({
            "id": "2l_bottom_gnd_pour",
            "layers": "F.Cu=SIG+PWR / B.Cu=Maximum-Continuity GND Pour",
            "when_to_use": "Standard 2-layer board: route signals on F.Cu with short B.Cu jumpers, then pour & stitch GND on B.Cu.",
        })
    elif n_layers == 4:
        if fine_bgas:
            options.append({
                "id": "4l_hdi_bga_escape",
                "layers": "F.Cu=SIG+PWR / In1.Cu=Signal (L1-L2 Microvia Escape) / In2.Cu=Solid GND Plane / B.Cu=SIG+PWR",
                "when_to_use": (
                    "Recommended for 4L boards with fine-pitch HDI BGAs (<= 0.65 mm) on F.Cu: keeps In1.Cu open for "
                    "L1-L2 microvia-in-pad escape while In2.Cu provides an unbroken Solid GND reference & shield plane."
                ),
            })
        options.append({
            "id": "4l_hartley_dual_gnd",
            "layers": "F.Cu=SIG+PWR / In1.Cu=Solid GND / In2.Cu=Solid GND / B.Cu=SIG+PWR",
            "when_to_use": "Hartley dual-GND stackup: best EMI/return-path continuity (L1<->L4 stays GND<->GND with 1 adjacent return via).",
        })
        options.append({
            "id": "4l_classic_split",
            "layers": "F.Cu=SIG / In1.Cu=Solid GND / In2.Cu=Solid PWR / B.Cu=SIG",
            "when_to_use": "Dense 4L MCU/bus boards needing a dedicated internal power plane (add GND-PWR stitching caps <= 3 mm near L1<->L4 fast vias).",
        })
    else:
        options.append({
            "id": f"{n_layers}l_paired_gnd_planes",
            "layers": f"Pair every signal layer across thin prepreg with an unbroken Solid GND plane ({', '.join(layer_names)})",
            "when_to_use": "6L+ high-density / RF / BGA boards: dedicate inner GND planes before routing so S6a taps & DSN plane exclusion activate.",
        })
    return {
        "gnd_routing_mode": "point_to_point_traces_warning",
        "fine_pitch_bgas": fine_bgas,
        "hdi_rule_conflicts": hdi_conflicts,
        "note": (
            "No GND/power plane zone exists on this board yet (stackup_policy == 'needs_decision'). "
            "If you route now without adding a Solid GND plane zone first, S6a plane taps will stay 0/0 "
            "and the autorouter will route GND as cluttered point-to-point traces. "
            "Choose the best stackup option below (confirming with the user if ambiguous), add the GND plane zone "
            "before calling placer__place_board(..., route=true), and verify S6a taps + post-route zone refill."
        ),
        "options": options,
    }


def _drc_signoff(rep: dict) -> dict | None:
    """Explicit fabrication DRC sign-off summary so connected AIs never confuse
    ``passed=True`` (placement/accounting gates) with ``0 unconnected items``."""
    route_m = (rep.get("metrics") or {}).get("route")
    if not route_m:
        return None
    gates = {g["name"]: g["value"] for g in (rep.get("gates") or [])}
    legality_v = int(gates.get("legality_violations") or 0)
    drc_router = int(gates.get("drc_router_caused") or 0)
    drc_new = int(gates.get("drc_new_non_routing") or 0)
    unrouted = int(route_m.get("unrouted") or 0)
    dangling_tracks = int(route_m.get("drc_track_dangling") or 0)
    dangling_vias = int(route_m.get("drc_via_dangling") or 0)
    mirror_warns = int(route_m.get("drc_text_mirror_warnings") or 0)
    unconn_nets = sorted({str(f["net"]) for f in (rep.get("failures") or []) if f.get("net")})
    fab_ready = (
        legality_v == 0
        and drc_router == 0
        and drc_new == 0
        and unrouted == 0
        and dangling_tracks == 0
        and dangling_vias == 0
        and route_m.get("drc_absolute_errors", 0) == 0
        and (not rep.get("quality") or rep["quality"].get("routing", {}).get("status") == "approved")
    )
    return {
        "fab_ready": fab_ready,
        "drc_errors": drc_router + drc_new,
        "drc_unconnected": unrouted,
        "drc_track_dangling": dangling_tracks,
        "drc_absolute_errors": route_m.get("drc_absolute_errors"),
        "drc_via_dangling": dangling_vias,
        "drc_text_mirror_warnings": mirror_warns,
        "unconnected_nets": unconn_nets,
        "note": (
            "DRC clean and 100% connected (0 violations, 0 unconnected items, 0 dangling tracks/vias)."
            if fab_ready else
            f"NOT fab-ready: {drc_router + drc_new} DRC errors, {unrouted} Unconnected Items, "
            f"{dangling_tracks} dangling tracks, {dangling_vias} dangling vias "
            f"({', '.join(unconn_nets[:8]) or 'see failures'}). Run a closed diagnose -> fix -> re-route loop "
            "before declaring the board DRC-clean or ready for fabrication."
        ),
    }


def _quality_summary(rep):
    return {
        stage: {key: row[key] for key in ("status", "reasons", "artifact", "findings_path", "zones_require_refill")
                if key in row}
        for stage, row in rep.get("quality", {}).items()
    }


def _inspect_summary(board: Board) -> dict:
    from .route import dru as dru_mod
    from .route.bga_escape import detect_bga

    by_class: dict[str, int] = {}
    for net in board.nets:
        by_class[net.netclass] = by_class.get(net.netclass, 0) + 1
    sheets = sorted({s for fp in board.footprints for s in ([fp.sheet] if fp.sheet else [])})
    multi_unit_ics = sum(1 for fp in board.footprints if len(fp.unit_sheets) > 1)
    bga_packages = []
    qfn_thermal_pads = []
    hollow_shields = 0
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        if fp.courtyard.holes and any(fp.courtyard.holes):
            hollow_shields += 1
        bga = detect_bga(fp)
        if bga is not None:
            bga_packages.append({
                "ref": fp.ref,
                "side": fp.side,
                "balls": len(bga["grid"]),
                "pitch_mm": round(min(bga["pitch_x"], bga["pitch_y"]), 3),
            })
        if len(fp.pads) >= 8:
            ep = [p for p in fp.pads if p.kind == "smd" and p.size[0] * p.size[1] >= 3.5 and min(p.size) >= 1.6]
            if ep:
                qfn_thermal_pads.append({"ref": fp.ref, "side": fp.side, "nets": sorted({p.net for p in ep if p.net})})
    src_path = (board.source or {}).get("path")
    dru_rules = dru_mod.extract_effective_rules(Path(src_path).with_suffix(".kicad_dru")) if src_path else {}
    return {
        "name": board.name,
        "source": board.source,
        "footprints": len(board.footprints),
        "sides": board.side_counts(),
        "fixed_parts": sorted(board.fixed_refs()),
        "copper_layers": [cl.name for cl in board.copper_layers],
        "board_area_mm2": board.board_area_mm2,
        "outline_valid": board.outline_valid,
        "schematic_hierarchy": {"sheets": sheets, "sheet_count": len(sheets), "multi_unit_ics": multi_unit_ics},
        "bga_packages": bga_packages,
        "qfn_thermal_pads": qfn_thermal_pads,
        "hollow_shields": hollow_shields,
        "dru_rules": dru_rules,
        "stackup": {"source": board.stackup.source, "policy": board.stackup.policy,
                    "board_thickness_mm": board.stackup.board_thickness_mm,
                    "layers": [asdict(layer) for layer in board.stackup.layers if layer.type in ("copper", "core",
                                                                                                  "prepreg")],
                    "roles": [asdict(r) for r in board.stackup.roles],
                    "guidance": _stackup_guidance(board)},
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
              "differential pairs, fixed parts, unknowns and capacity warnings. Returns measured mechanical evidence "
              "for connectors or the supplied mechanical_refs. An optional intent supplies reviewed dependencies. "
              "Read-only; does not modify the board."),
             {"board_path": _STRING, "intent_path": _STRING,
              "mechanical_refs": {"type": "array", "items": _STRING}}, ["board_path"], True),
            ("score",
             ("Evaluate the board's current placement: legality gate (courtyard overlap, outline, keepouts), HPWL, "
             "ratsnest crossings, RUDY, critical loops from the design intent, regularity and Pro Score v1. With "
             "route=true it also runs the strict Freerouting route check (optionally with S6a critical copper when "
             "s6a=true) and reports DRC attribution, drc_signoff, coverage and every unrouted connection with its "
             "cause. Writes run_report.json and an HTML gallery to a new run folder; never writes the board."),
             {"board_path": _STRING, "route": {"type": "boolean", "default": True}, "intent_path": _STRING,
              "s6a": {"type": "boolean", "default": False},
              "router_timeout_s": {"type": "integer", "minimum": 15, "maximum": 1800}},
             ["board_path"], True),
            ("intent_draft",
             ("Draft the board's design intent (design_intent.json) from its netlist alone: the role of every part "
              "(decap and its target IC, crystal and load caps, converter parts, ESD and its connector, pull-ups, "
              "terminations, ...), net classes, differential pairs and blocks, each claim with its source and "
              "confidence. Optionally apply structured Law-P1-safe patches (never raw coordinates) before writing. "
              "Writes the draft next to the board as <board>.design_intent.json unless that file exists "
              "(then into the run folder; never overwrites). Returns the questions it could not settle, critical "
              "ones first. Settle them with datasheet or schematic evidence by editing the file: set the claim's "
              "value, source {kind: 'llm', ref: '<datasheet URL + page>', client: '<your client>'} and confidence, "
              "or ask the user and set confirmed_by_user=true. Never write coordinates. Then call intent_validate."),
             {"board_path": _STRING, "out_path": _STRING,
              "patches": {"type": "array", "items": {"type": "object"}}}, ["board_path"], False),
            ("intent_validate",
             ("Validate a design_intent.json against its board: JSON schema, every reference, net and pin, target "
              "roles, sources. Returns errors (the intent is refused), warnings, the canonical intent hash (the same "
              "for two intents that differ only in order), claim counts (hard = confirmed or confidence >= 0.9; "
              "uncertain < 0.6) and the open questions. Read-only."),
             {"board_path": _STRING, "intent_path": _STRING}, ["board_path", "intent_path"], True),
            ("place_board",
             ("Place the whole board from scratch with the 2.0 placer (S1 fixed parts, S2b sides, S3 analytical and "
              "SA-only starts, S4 legalization, S5 annealing in the Rust core; the better legal result wins), write a "
              "NEW board file, route it with the strict Freerouting check and evaluate it against the expert. "
              "side_policy 'expert' keeps every part's side (fair comparison), 'top_only' is assembly level 1. "
              "Set floorplan=true to evaluate local-block placement and pad escape reservations. "
              "Supply schematic_path to verify schematic connectivity and reset all movable PCB poses. "
              "Takes 1 to 10 minutes. Never modifies the input board."),
             {"board_path": _STRING,
              "side_policy": {"type": "string",
                              "enum": ["expert", "top_only", "level2_passives_back", "level3_two_sided"]},
              "seed": {"type": "integer", "minimum": 0, "default": 0}, "route": {"type": "boolean", "default": True},
              "s6a": {"type": "boolean", "default": True},
              "silkscreen_policy": {"type": "string",
                                    "enum": ["auto", "ipc_standard", "dense", "assembly_only", "off"],
                                    "default": "auto"},
              "fab_profile": {"type": "string",
                              "enum": ["ipc_standard", "jlcpcb", "pcbway", "high_precision_lpi"],
                              "default": "ipc_standard"},
              "keep_placement": {"type": "boolean", "default": False},
              "router_timeout_s": {"type": "integer", "minimum": 15, "maximum": 1800},
              "intent_path": _STRING, "schematic_path": _STRING, "floorplan": {"type": "boolean", "default": False}},
             ["board_path"], False),
            ("place_cluster",
             ("Optimize selected footprints in an existing KiCad board. Holds all other poses fixed and writes a new "
              "board with stale copper zone fills cleared while preserving zone definitions and rule areas. Default "
              "preserve_routing='none' clears all tracks (placement-only); preserve_routing='outside_cluster' enables "
              "Safe ECO mode, keeping healthy copper outside the moved cluster while surgically removing only tracks "
              "and vias invalidated by moved pads or dangling stubs."),
             {"board_path": _STRING, "refs": {"type": "array", "items": _STRING, "minItems": 1, "uniqueItems": True},
              "intent_path": _STRING, "seed": {"type": "integer", "minimum": 0, "default": 0},
              "preserve_routing": {"type": "string", "enum": ["none", "outside_cluster"], "default": "none"},
              "silkscreen_policy": {"type": "string",
                                    "enum": ["auto", "ipc_standard", "dense", "assembly_only", "off"],
                                    "default": "auto"},
              "fab_profile": {"type": "string",
                              "enum": ["ipc_standard", "jlcpcb", "pcbway", "high_precision_lpi"],
                              "default": "ipc_standard"}},
             ["board_path", "refs"], False),
            ("gallery",
             ("Build one side-by-side HTML gallery from run folders of placer__score or placer__place_board "
              "(run_report.json paths or their folders): placements with ratsnest, routed copper and metrics. "
              "Set include_images=true with one or two runs to also return native top and bottom PCB images "
              "in the MCP response for visual review."),
             {"runs": {"type": "array", "items": _STRING, "minItems": 1}, "title": _STRING,
              "include_images": {"type": "boolean", "default": False}}, ["runs"], True),
            ("diagnose",
             ("Review a placement or route-check run with localized findings, numeric evidence, suggested actions, "
              "and PNG crops. Reads existing artifacts only. Supports filtering by kinds and pagination via offset."),
             {"run": _STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 24},
              "kinds": {"type": "array", "items": _STRING},
              "offset": {"type": "integer", "minimum": 0, "default": 0},
              "include_images": {"type": "boolean", "default": True}}, ["run"], True),
            ("refine",
             ("Experimentally test one local placement proposal on independent copies using equal routing budgets. "
              "Use preview=true to see legal pad-relative moves and escape changes before routing. "
              "Returns the prior board on regressions or an inconclusive result. Never applies changes to the source."),
             {"run": _STRING, "change": {"type": "object", "additionalProperties": False, "properties": {
                 "ref": _STRING, "rotation_deg": {"type": "integer", "enum": [0, 90, 180, 270]},
                 "toward_ref": _STRING, "away_from_ref": _STRING, "swap_with_ref": _STRING, "target_pad": _STRING,
                 "block_refs": {"type": "array", "items": _STRING},
                 "passive_align": {"type": "boolean"},
                 "side_flip": {"type": "boolean"},
                 "allow_side_flip": {"type": "boolean"},
                 "step_mm": {"type": "number", "minimum": 0.05, "maximum": 3},
                 "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                 "required": ["ref", "confidence"],
                 "anyOf": [{"required": ["rotation_deg"]}, {"required": ["toward_ref"]},
                           {"required": ["away_from_ref"]}, {"required": ["swap_with_ref"]},
                           {"required": ["passive_align"]}, {"required": ["side_flip"]}]},
              "preview": {"type": "boolean", "default": False},
              "route_timeout_s": {"type": "integer", "minimum": 15, "maximum": 600, "default": 180}}, ["run", "change"], False),
            ("apply",
             ("Safely apply a verified placement/routing run (F7). Default adapter='kicad_file' with in_place=false "
              "writes to a new sibling file so the original board stays untouched (principle P8). Explicit "
              "in_place=true checks gates and live SHA-256 against stale edits, takes a verified backup, and swaps "
              "atomically with <2s revert via placer__revert. adapter='altium_plan' emits a Level-1 (no-flip) "
              "apply+revert delta bundle."),
             {"run": _STRING,
              "adapter": {"type": "string", "enum": ["kicad_file", "altium_plan", "kicad_ipc"], "default": "kicad_file"},
              "target_path": _STRING,
              "in_place": {"type": "boolean", "default": False},
              "use_routed": {"type": "boolean", "default": True},
              "allow_failed_gates": {"type": "boolean", "default": False},
              "force_stale": {"type": "boolean", "default": False},
              "allow_flip": {"type": "boolean", "default": False},
              "dry_run": {"type": "boolean", "default": False}}, ["run"], False),
            ("revert",
             ("Revert a previous placer__apply transaction in under 2 seconds with SHA-256 verification. "
              "Refuses to overwrite post-apply manual edits unless force=true. Pass apply_id='latest' to revert "
              "the most recent active apply."),
             {"apply_id": {"type": "string", "default": "latest"},
              "force": {"type": "boolean", "default": False}}, [], False),
        ]
        return [{"name": name, "description": description,
                 "inputSchema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False},
                 "annotations": {"readOnlyHint": readonly, "openWorldHint": False}}
                for name, description, props, required, readonly in definitions]

    # ------------------------------------------------------------------ tools
    def inspect(self, board_path: str, intent_path: str | None = None,
                mechanical_refs: list[str] | None = None) -> dict:
        from . import silkscreen as silk_mod
        from .intent.resolve import resolve
        from .place.mechanical import evidence

        board = read_board(board_path)
        summary = _inspect_summary(board)
        intent, _ = resolve(board, intent_path)
        summary["mechanical"] = evidence(board, intent, set(mechanical_refs) if mechanical_refs is not None else None)
        summary["silkscreen"] = silk_mod.audit_board(board)
        return summary

    def intent_draft(
        self,
        board_path: str,
        out_path: str | None = None,
        patches: list[dict[str, Any]] | None = None,
    ) -> dict:
        from .intent import infer, model, validate

        board = read_board(board_path)
        intent = infer.draft(board)
        applied_patches = 0
        for p in patches or ():
            kind = str(p.get("kind") or "")
            src_kind = str(p.get("source_kind") or "llm")
            src_ref = str(p.get("source_ref") or "agent patch")
            conf = float(p.get("confidence", 0.95))
            confirmed = bool(p.get("confirmed_by_user", False))
            if kind == "part_role" and p.get("ref") in intent.get("parts", {}):
                cl = model.claim(str(p["value"]), src_kind, conf, ref=src_ref)
                if confirmed:
                    cl["confirmed_by_user"] = True
                intent["parts"][p["ref"]]["role"] = cl
                applied_patches += 1
            elif kind == "part_target" and p.get("ref") in intent.get("parts", {}):
                cl = model.claim(dict(p["value"]), src_kind, conf, ref=src_ref)
                if confirmed:
                    cl["confirmed_by_user"] = True
                intent["parts"][p["ref"]]["target"] = cl
                applied_patches += 1
            elif kind == "net_class" and p.get("net") in intent.get("nets", {}):
                cl = model.claim(str(p["value"]), src_kind, conf, ref=src_ref)
                if confirmed:
                    cl["confirmed_by_user"] = True
                intent["nets"][p["net"]]["class"] = cl
                applied_patches += 1
            elif kind == "diff_pair_confirm" and p.get("positive") and p.get("negative"):
                for dp in intent.get("diff_pairs", ()):
                    val = dp.get("value") or {}
                    if val.get("positive") == p["positive"] and val.get("negative") == p["negative"]:
                        dp["confidence"] = max(float(dp.get("confidence", 0.0)), conf)
                        dp["confirmed_by_user"] = True
                        applied_patches += 1
        if applied_patches:
            v_res = validate.validate(intent, board)
            if v_res["errors"]:
                raise ValueError(f"intent_draft patches failed validation: {v_res['errors'][:3]}")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = Path(out_path) if out_path else Path(board_path).with_name(f"{board.name}.design_intent.json")
        note = None
        if target.exists():
            note = f"{target} exists and was left untouched"
            target = self.runs / f"{board.name}-intent-{stamp}" / "design_intent.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        model.dump(intent, target)
        roles: dict[str, int] = {}
        for part in intent["parts"].values():
            roles[part["role"]["value"]] = roles.get(part["role"]["value"], 0) + 1
        return {"intent_path": str(target), "note": note, "intent_sha256": model.intent_hash(intent),
                "applied_patches": applied_patches,
                "parts": len(intent["parts"]), "roles": dict(sorted(roles.items())),
                "diff_pairs": [[p["value"]["positive"], p["value"]["negative"], p["confidence"]]
                               for p in intent["diff_pairs"]],
                "questions": intent["questions"][:60], "questions_total": len(intent["questions"])}

    def intent_validate(self, board_path: str, intent_path: str) -> dict:
        from .intent import model, validate

        return validate.validate(model.load(intent_path), read_board(board_path))

    def place_board(self, board_path: str, side_policy: str | None = None, seed: int = 0, route: bool = True,
                    intent_path: str | None = None, floorplan: bool = False, schematic_path: str | None = None,
                    s6a: bool = True, silkscreen_policy: str = "auto",
                    fab_profile: str = "ipc_standard", keep_placement: bool = False,
                    router_timeout_s: int | None = None) -> dict:
        from .place.pipeline import place_board

        board_name = Path(board_path).stem
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rep = place_board(board_path, out_dir=self.runs / f"{board_name}-place-{stamp}", side_policy=side_policy,
                          seed=seed, route=route, router_timeout_s=router_timeout_s,
                          intent_path=intent_path, floorplan=floorplan,
                          schematic_path=schematic_path, s6a=s6a, silkscreen_policy=silkscreen_policy,
                          fab_profile=fab_profile, keep_placement=keep_placement)
        info = rep["stages"]["place"]
        s6a_info = rep["stages"].get("s6a")
        silk_info = info.get("silkscreen")
        pf = rep["metrics"].get("pathfinder")
        lint = rep["metrics"].get("lint")
        return {
            "passed": rep["passed"],
            "quality": _quality_summary(rep),
            "drc_signoff": _drc_signoff(rep),
            "gates": [{k: g[k] for k in ("name", "value", "op", "threshold", "passed")} for g in rep["gates"]],
            "placed_board": rep["artifacts"]["placed_board"],
            "mode": info["mode"], "candidates": info.get("candidates"), "fixed_parts": info["fixed"],
            "seconds_s0_s5": info["seconds_s0_s5"],
            "mechanical_anchors": info["mechanical_anchors"],
            "cold_input": info.get("cold_input"),
            "floorplan": {k: v for k, v in info["floorplan"].items() if k != "escape"},
            "silkscreen": ({k: silk_info.get(k) for k in ("policy", "effective_mode", "fab_profile", "moved",
                                                          "condensed_used", "dense_sized", "offloaded_to_fab",
                                                          "leader_lines_added", "unresolved", "audit")}
                           if silk_info else None),
            "s6a": ({k: s6a_info.get(k) for k in ("applied", "reason", "counts", "copper", "pruned_nets", "tap_fill_after_ses")}
                    if s6a_info else None),
            "metrics": {"hpwl_signal_mm": rep["metrics"]["hpwl"]["signal_mm"],
                        "crossings": rep["metrics"]["crossings"]["count"],
                        "fanout_escape": {k: info["floorplan"]["escape"][k] for k in ("total", "clear")},
                        "pathfinder": ({k: pf[k] for k in ("total_overflow", "max_overflow", "routability_cost")}
                                       | {"hotspot_count": len(pf.get("hotspots", ()))}
                                       if pf else None),
                        "lint": ({k: lint[k] for k in ("score", "critical_errors", "warnings", "has_copper")}
                                 if lint else None),
                        "route": ({k: rep["metrics"]["route"][k] for k in ("coverage", "unrouted", "vias",
                                                                            "track_length_mm", "router_stop")}
                                  if rep["metrics"]["route"] else None)},
            "pro_score": rep["metrics"]["pro_score"],
            "intent": rep["inputs"]["intent"],
            "run_report": str(Path(rep["artifacts"]["run_dir"]) / "run_report.json"),
            "gallery": rep["artifacts"]["gallery"],
        }

    def place_cluster(self, board_path: str, refs: list[str], intent_path: str | None = None, seed: int = 0,
                      preserve_routing: str = "none", silkscreen_policy: str = "auto",
                      fab_profile: str = "ipc_standard") -> dict:
        from .place.pipeline import place_board

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        rep = place_board(board_path, out_dir=self.runs / f"{Path(board_path).stem}-cluster-{stamp}",
                          seed=seed, route=False, intent_path=intent_path, floorplan=True, cluster_refs=refs,
                          preserve_routing=preserve_routing, silkscreen_policy=silkscreen_policy,
                          fab_profile=fab_profile)
        return {"passed": rep["passed"], "gates": rep["gates"], "refs": refs,
                "quality": _quality_summary(rep),
                "zones_require_refill": rep["stages"]["place"].get("zones_require_refill", False),
                "preserve_routing": rep["stages"]["place"].get("preserve_routing"),
                "silkscreen": rep["stages"]["place"].get("silkscreen"),
                "placed_board": rep["artifacts"]["placed_board"], "gallery": rep["artifacts"]["gallery"],
                "run_report": str(Path(rep["artifacts"]["run_dir"]) / "run_report.json")}

    def gallery(self, runs: list[str], title: str = "Placement gallery",
                include_images: bool = False) -> dict[str, Any] | types.CallToolResult:
        from . import silkscreen as silk_mod

        if include_images and len(runs) > 2:
            raise ValueError("Image review supports at most two runs per request")
        panels = []
        sources = []
        silk_qa = []
        for index, run in enumerate(runs, 1):
            path = Path(run)
            report = path / "run_report.json" if path.is_dir() else path
            rep = json.loads(report.read_text(encoding="utf-8"))
            art = rep.get("artifacts", {})
            board_file = art.get("placed_board") or rep["inputs"]["board"]["path"]
            route_stage = rep.get("stages", {}).get("route_check") or {}
            route_dir = Path(route_stage.get("run_report") or Path(art.get("run_dir", report.parent)) / "route" / "run_report.json")
            shown = art.get("routed_board") or board_file
            route_metrics = None
            if route_dir.is_file():
                rr = json.loads(route_dir.read_text(encoding="utf-8"))
                shown = rr["artifacts"].get("routed_board", board_file)
                route_metrics = rr["metrics"]
            board = read_board(shown)
            sa = silk_mod.audit_board(board)
            cnt = sa["counts"]
            silk_qa.append({
                "panel": index,
                "board": board.name,
                "board_size_mm": sa["board_size_mm"],
                "is_miniature": sa["is_miniature"],
                "overlay_mode": sa["overlay_mode"],
                "status": sa["status"],
                "dfm_score": sa["dfm_score"],
                "visible_ref_designators": sa["visible_ref_designators"],
                "visible_fab_texts": sa["visible_fab_texts"],
                "height_mm": sa["height_mm"],
                "stroke_mm": sa["stroke_mm"],
                "fab_outside_board": cnt["fab_outside_board"],
                "fab_overlaps": cnt["fab_overlaps"],
                "fab_oversized": cnt["fab_oversized"],
                "mirror_mismatches": cnt["mirror_mismatches"],
                "silk_outside_board": cnt["silk_outside_board"],
                "oversized_for_board": cnt["oversized_for_board"],
                "under_body": cnt["under_body"],
                "missing_legends": cnt["missing_legends"],
                "legend_coverage": sa["legend_coverage"],
            })
            from .intent.resolve import IntentError, resolve

            saved_intent_path = ((rep.get("inputs") or {}).get("intent") or {}).get("path")
            try:
                resolved_intent, _ = resolve(board, saved_intent_path)
            except (OSError, IntentError):
                resolved_intent = None
            copper_b = board if route_metrics is not None else None
            ev = evaluate(board, route_metrics, intent=resolved_intent, copper_board=copper_b)
            panels.append(Panel(f"{board.name} ({rep['kind']})", board, ev, route_metrics))
            sources.append((Path(shown), board.name, rep["kind"], board, sa))
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = gallery(panels, self.runs / f"gallery-{stamp}.html", title=title)
        value = {"gallery": str(out), "panels": len(panels), "silkscreen_visual_qa": silk_qa}
        if not include_images:
            return value

        content = []
        views = []
        for index, (source, name, kind, board, sa) in enumerate(sources, 1):
            cnt = sa["counts"]
            qa_tag = (
                f"overlay={sa['overlay_mode']} refs={sa['visible_ref_designators']} "
                f"fab={sa['visible_fab_texts']} fab_out={cnt['fab_outside_board']} "
                f"fab_ovl={cnt['fab_overlaps']} mir_err={cnt['mirror_mismatches']} "
                f"score={sa['dfm_score']}"
            )
            try:
                images = review_images(source)
            except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                views.append({"panel": index, "board": name, "render_status": type(exc).__name__})
                continue
            for side, data in images:
                views.append({"panel": index, "board": name, "side": side})
                content.append(types.TextContent(type="text", text=f"Panel {index} · {name} ({kind}) · {side} | {qa_tag}"))
                content.append(types.ImageContent(type="image", mimeType="image/png", data=base64.b64encode(data).decode("ascii")))
            needs_fab_view = bool(board.silkscreen_texts) and (
                sa["overlay_mode"] in ("assembly_only", "mixed_overlay_and_fab", "missing_legends")
                or cnt["fab_outside_board"] > 0
                or cnt["fab_overlaps"] > 0
                or cnt["fab_oversized"] > 0
                or cnt["mirror_mismatches"] > 0
                or cnt["silk_outside_board"] > 0
                or cnt["oversized_for_board"] > 0
                or cnt["under_body"] > 0
            )
            if needs_fab_view:
                fab_png = assembly_overlay_image(board, sa)
                views.append({"panel": index, "board": name, "side": "assembly_fab"})
                content.append(types.TextContent(
                    type="text",
                    text=f"Panel {index} · {name} ({kind}) · assembly_fab (SilkS + F.Fab/B.Fab) | {qa_tag}",
                ))
                content.append(types.ImageContent(
                    type="image",
                    mimeType="image/png",
                    data=base64.b64encode(fab_png).decode("ascii"),
                ))
        value["views"] = views
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False)), *content],
            structuredContent=value,
        )

    def score(self, board_path: str, route: bool = True, intent_path: str | None = None,
              s6a: bool = False, router_timeout_s: int | None = None) -> dict:
        from . import silkscreen as silk_mod
        from .intent.resolve import resolve
        from .place import problem

        src = Path(board_path).resolve()
        board = read_board(src)
        intent, intent_info = resolve(board, intent_path)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = (self.runs / f"{board.name}-{stamp}").resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        s6a_stage: dict | None = None
        route_input: str | Path = src
        if route and s6a:
            from .route import s6a as s6a_mod

            s6a_stage = s6a_mod.apply_to_board(src, intent, run_dir / "s6a")
            if s6a_stage["applied"] and s6a_stage["s6a_board"]:
                route_input = Path(s6a_stage["s6a_board"])
        routed = (
            route_check(route_input, out_dir=run_dir / "route", reference_board=src,
                        router_timeout_s=router_timeout_s, intent=intent)
            if route else None
        )
        s6a_summary: dict | None = None
        if s6a_stage is not None:
            s6a_summary = {k: v for k, v in s6a_stage.items() if k not in ("live_taps", "report")}
            if routed and s6a_stage["applied"] and s6a_stage["live_taps"]:
                from .route import s6a as s6a_mod

                s6a_summary["tap_fill_after_ses"] = s6a_mod.verify_taps_after_route(
                    routed["artifacts"]["routed_board"], s6a_stage["live_taps"]
                )
        native_options = {"include_fill": True} if board.source.get("eda") == "kicad" else {}
        routed_board = read_board(routed["artifacts"]["routed_board"], **native_options) if routed else None
        if native_options and routed_board is None:
            routed_board = read_board(src, include_fill=True)
        fixed_map = problem.fixed_parts(board, intent)
        evaluation = evaluate(
            board, routed["metrics"] if routed else None,
            fixed=set(fixed_map), intent=intent, copper_board=routed_board,
        )
        page = gallery([Panel(f"{board.name} (current placement)", routed_board if routed_board is not None else board,
                              evaluation, routed["metrics"] if routed else None)], run_dir / "gallery.html",
                       title=f"{board.name}: score")
        gates = [run_report.gate("legality_violations", evaluation["legality"]["strict_violations"], 0, "==")]
        if routed:
            gates += routed["gates"]
        artifacts = {"run_dir": str(run_dir), "placed_board": str(src), "gallery": str(page)}
        if routed:
            artifacts["routed_board"] = routed["artifacts"]["routed_board"]
        if s6a_summary and s6a_summary.get("s6a_board"):
            artifacts["s6a_board"] = s6a_summary["s6a_board"]
        rep = run_report.build(
            kind="score", inputs={"board": board.source, "intent": intent_info},
            config={"route": route, "s6a": s6a, "router_timeout_s": router_timeout_s},
            stages={"evaluate": {"legality": {k: v for k, v in evaluation["legality"].items() if k != "findings"},
                                 "fixed": fixed_map},
                    "s6a": s6a_summary,
                    "route_check": {"run_report": str(run_dir / "route" / "run_report.json")} if routed else None},
            metrics={**evaluation["metrics"], "pro_score": evaluation["pro_score"],
                     "route": routed["metrics"] if routed else None},
            gates=gates,
            failures=(routed["failures"] if routed else []) + [
                {"stage": "legality", "cause": f["kind"], "refs": f["refs"], "waived_by": f["waived_by"]}
                for f in evaluation["legality"]["findings"] if not f["fixed"]],
            artifacts=artifacts,
            **({"quality": routed["quality"]} if routed and routed.get("quality") else {}),
            unknowns=board.unknowns, warnings=board.warnings,
        )
        path = run_report.write(rep, run_dir / "run_report.json")
        pf = evaluation["metrics"].get("pathfinder")
        lint = evaluation["metrics"].get("lint")
        return {
            "passed": rep["passed"],
            "quality": _quality_summary(rep),
            "drc_signoff": _drc_signoff(rep),
            "gates": [{k: g[k] for k in ("name", "value", "op", "threshold", "passed")} for g in gates],
            "legality": {k: v for k, v in evaluation["legality"].items() if k != "findings"},
            "unwaived_findings": [f for f in evaluation["legality"]["findings"] if not f["waived_by"] and not f["fixed"]],
            "s6a": ({k: s6a_summary.get(k) for k in ("applied", "reason", "counts", "copper", "pruned_nets", "tap_fill_after_ses")}
                    if s6a_summary else None),
            "metrics": {"hpwl_signal_mm": evaluation["metrics"]["hpwl"]["signal_mm"],
                        "crossings": evaluation["metrics"]["crossings"],
                        "rudy": evaluation["metrics"]["rudy"],
                        "pathfinder": ({k: pf[k] for k in ("total_overflow", "max_overflow", "routability_cost")}
                                       | {"hotspot_count": len(pf.get("hotspots", ()))}
                                       if pf else None),
                        "lint": ({k: lint[k] for k in ("score", "critical_errors", "warnings", "has_copper")}
                                 if lint else None),
                        "critical_loops": evaluation["metrics"]["critical_loops"],
                        "regularity": evaluation["metrics"]["regularity"]},
            "pro_score": evaluation["pro_score"],
            "silkscreen": silk_mod.audit_board(routed_board if routed_board is not None else board),
            "route": ({k: routed["metrics"][k] for k in ("coverage", "unrouted", "vias", "track_length_mm",
                                                         "router_stop", "seconds_total")}
                      | {"unrouted_causes": [{"net": f["net"], "cause": f["cause"]} for f in routed["failures"]]}
                      if routed else None),
            "unknowns": board.unknowns,
            "warnings": board.warnings,
            "run_report": str(path),
            "gallery": str(page),
        }

    def diagnose(
        self,
        run: str,
        limit: int = 8,
        include_images: bool = True,
        kinds: list[str] | None = None,
        offset: int = 0,
    ) -> dict[str, Any] | types.CallToolResult:
        from .diagnose import diagnose

        value, boards = diagnose(run, limit, kinds=kinds, offset=offset)
        if not include_images:
            return value
        images = []
        for index, finding in enumerate(value["findings"], 1):
            if not finding["points"]:
                continue
            board = boards.get(finding["source"], boards[value["run"]])
            data = finding_image(board, finding)
            finding["image_label"] = f"Finding {index} · {finding['kind']}"
            images.extend([types.TextContent(type="text", text=finding["image_label"]),
                           types.ImageContent(type="image", mimeType="image/png",
                                              data=base64.b64encode(data).decode("ascii"))])
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False)), *images],
            structuredContent=value,
        )

    def refine(self, run: str, change: dict, route_timeout_s: int = 180, preview: bool = False) -> dict[str, Any]:
        from .refine import refine

        return refine(run, change, self.runs, route_timeout_s=route_timeout_s, preview=preview)

    def apply(
        self,
        run: str,
        adapter: str = "kicad_file",
        target_path: str | None = None,
        in_place: bool = False,
        use_routed: bool = True,
        allow_failed_gates: bool = False,
        force_stale: bool = False,
        allow_flip: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        from .apply import apply_run

        return apply_run(
            run,
            history_dir=self.runs / "apply_history",
            adapter=adapter,
            target_path=target_path,
            in_place=in_place,
            use_routed=use_routed,
            allow_failed_gates=allow_failed_gates,
            force_stale=force_stale,
            allow_flip=allow_flip,
            dry_run=dry_run,
        )

    def revert(self, apply_id: str = "latest", force: bool = False) -> dict[str, Any]:
        from .apply import revert_apply

        return revert_apply(apply_id, history_dir=self.runs / "apply_history", force=force)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | types.CallToolResult:
        functions = {"inspect": self.inspect, "score": self.score, "place_board": self.place_board,
                     "place_cluster": self.place_cluster,
                     "gallery": self.gallery, "intent_draft": self.intent_draft,
                     "intent_validate": self.intent_validate, "diagnose": self.diagnose, "refine": self.refine,
                     "apply": self.apply, "revert": self.revert}
        if name not in functions:
            raise ValueError(f"Unknown placer tool: {name}")
        return await asyncio.to_thread(functions[name], **(arguments or {}))

    async def call_tool_async(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | types.CallToolResult:
        return await self.call_tool(name, arguments)


def serve() -> None:
    """Standalone stdio server exposing the tools as ``placer__<name>`` (development use)."""
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    service = PlacerService()
    server = Server("electronics-ai-placer")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name="placer__" + t["name"], description=t["description"], inputSchema=t["inputSchema"],
                           annotations=types.ToolAnnotations(**t["annotations"])) for t in service.list_tools()]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent] | types.CallToolResult:
        if not name.startswith("placer__"):
            raise ValueError(f"Unknown tool: {name}")
        result = await service.call_tool(name.removeprefix("placer__"), arguments)
        if isinstance(result, types.CallToolResult):
            return result
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=1))]

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    serve()
