# SPDX-License-Identifier: MIT
"""IPC-7351 and DFM/DFA silkscreen (Top/Bottom Overlay) legend policy, sizing, and audit.

Implements general-purpose silkscreen legend engineering rules without board-specific hacks:
  - IPC-7351 recommended text height 1.00..1.27 mm (40..50 mil), dense practical floor 0.80 mm (32 mil).
  - Stroke width 0.15..0.18 mm (6..7 mil), dense floor 0.12..0.15 mm, never thinner than 1:10 ratio
    (ideal stroke-to-height ratio 1:8..1:6, avoiding both faint print and ink blotches).
  - Condensed-width preference (narrowing character width at constant height before reducing height).
  - Minimum silkscreen-to-pad clearance >= 0.20 mm and component-body avoidance so designators stay
    readable after assembly.
  - Dense-board triage: keep critical parts (ICs, connectors, polarized parts, crystals, switches,
    jumpers, test points, polarity markers) on silkscreen (with leader lines when displaced) and
    offload crowded non-critical passive designators to F.Fab / B.Fab (Assembly Drawing) layers.
  - Explicit pre-fab reminder distinguishing CAD stroke width from fab-controlled cured ink
    thickness (15..25 um, minimum 10 um; screen-print vs LPI vs inkjet).
"""

from __future__ import annotations

import math
import re
from typing import Any

from .ir import Board, Footprint

SILKSCREEN_POLICIES = ("auto", "ipc_standard", "dense", "assembly_only", "off")

FAB_PROFILES: dict[str, dict[str, float | str]] = {
    "ipc_standard": {
        "name": "ipc_standard",
        "target_height_mm": 1.00,
        "max_height_mm": 1.27,
        "dense_height_mm": 0.80,
        "min_height_mm": 0.80,
        "target_stroke_mm": 0.15,
        "max_stroke_mm": 0.18,
        "min_stroke_mm": 0.15,
        "min_stroke_ratio": 0.10,  # 1:10 hard floor
        "ideal_stroke_ratio_min": 0.125,  # 1:8
        "ideal_stroke_ratio_max": 0.167,  # 1:6
        "max_stroke_ratio": 0.25,  # 1:4 blotch ceiling
        "condensed_width_ratio": 0.78,
        "min_pad_clearance_mm": 0.20,
    },
    "jlcpcb": {
        "name": "jlcpcb",
        "target_height_mm": 1.00,
        "max_height_mm": 1.27,
        "dense_height_mm": 0.80,
        "min_height_mm": 0.80,
        "target_stroke_mm": 0.15,
        "max_stroke_mm": 0.18,
        "min_stroke_mm": 0.15,
        "min_stroke_ratio": 0.10,
        "ideal_stroke_ratio_min": 0.125,
        "ideal_stroke_ratio_max": 0.167,
        "max_stroke_ratio": 0.25,
        "condensed_width_ratio": 0.78,
        "min_pad_clearance_mm": 0.20,
    },
    "pcbway": {
        "name": "pcbway",
        "target_height_mm": 1.00,
        "max_height_mm": 1.27,
        "dense_height_mm": 0.80,
        "min_height_mm": 0.80,
        "target_stroke_mm": 0.15,
        "max_stroke_mm": 0.18,
        "min_stroke_mm": 0.15,
        "min_stroke_ratio": 0.10,
        "ideal_stroke_ratio_min": 0.125,
        "ideal_stroke_ratio_max": 0.167,
        "max_stroke_ratio": 0.25,
        "condensed_width_ratio": 0.78,
        "min_pad_clearance_mm": 0.20,
    },
    "high_precision_lpi": {
        "name": "high_precision_lpi",
        "target_height_mm": 1.00,
        "max_height_mm": 1.27,
        "dense_height_mm": 0.80,
        "min_height_mm": 0.75,
        "target_stroke_mm": 0.15,
        "max_stroke_mm": 0.18,
        "min_stroke_mm": 0.12,
        "min_stroke_ratio": 0.10,
        "ideal_stroke_ratio_min": 0.125,
        "ideal_stroke_ratio_max": 0.167,
        "max_stroke_ratio": 0.25,
        "condensed_width_ratio": 0.75,
        "min_pad_clearance_mm": 0.15,
    },
}

CURED_INK_SPEC_UM = {
    "recommended_min_um": 15,
    "recommended_max_um": 25,
    "min_readable_um": 10,
    "note": (
        "CAD controls stroke width (mm); cured ink thickness (15-25 um, min 10 um) depends on "
        "fab printing process (silkscreen, LPI, or inkjet). Confirm minimum printable height and "
        "stroke width with the target PCB manufacturer before fabrication."
    ),
}

CRITICAL_SILK_ROLES = frozenset({
    "ic",
    "converter_ic",
    "linear_regulator",
    "power_switch",
    "esd",
    "connector",
    "crystal",
    "oscillator",
    "diode",
    "converter_diode",
    "led",
    "transistor",
    "switch",
    "jumper",
    "test_point",
    "battery",
    "bulk_cap",
})

NON_CRITICAL_SILK_ROLES = frozenset({
    "decap",
    "pin_cap",
    "coupling_cap",
    "crystal_load_cap",
    "crystal_aux",
    "regulator_input_cap",
    "regulator_output_cap",
    "series_resistor",
    "pull_up",
    "pull_down",
    "termination",
    "bias_resistor",
    "divider_resistor",
    "led_resistor",
    "rail_filter",
    "signal_passive",
})

_POLARIZED_HINTS = re.compile(
    r"(electrolytic|tantalum|cp_|pol|diode|led|zener|schottky|tvs|usb|jst|header|switch|button|batt)",
    re.IGNORECASE,
)
_CRITICAL_PREFIXES = ("U", "IC", "J", "CN", "CON", "P", "USB", "D", "LED", "Q", "Y", "X", "XTAL", "SW", "S", "JP", "TP", "BT", "F")
_NON_CRITICAL_PREFIXES = ("R", "RN", "C", "L", "FB")


def _ref_prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return m.group(1).upper() if m else ""


def is_critical_silkscreen_part(
    fp: Footprint,
    role: str | None = None,
) -> bool:
    """Return True if this part's Reference Designator should stay prioritized on Silkscreen.

    Critical: ICs, connectors, polarized parts (diodes, LEDs, polarized caps, batteries),
    transistors, crystals/oscillators, switches, jumpers, test points, ESD devices.
    Non-critical: unpolarized general passives (resistors, ceramic decaps/load caps, beads).
    """
    lib_val = f"{fp.lib_id} {fp.value} {fp.description}"
    if _POLARIZED_HINTS.search(lib_val):
        return True
    if role:
        if role in CRITICAL_SILK_ROLES:
            return True
        if role in NON_CRITICAL_SILK_ROLES:
            return False
    prefix = _ref_prefix(fp.ref)
    if prefix in _CRITICAL_PREFIXES:
        return True
    return prefix not in _NON_CRITICAL_PREFIXES


def _box_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return math.hypot(dx, dy)


def estimate_board_density(board: Board) -> dict[str, Any]:
    """Estimate assembly silkscreen crowding and miniature-board sizing constraints."""
    area = float(board.board_area_mm2 or 0.0)
    outline_box = board.outline_box
    board_size_mm: list[float] | None = None
    min_dim: float | None = None
    if outline_box is not None:
        bw = max(0.0, float(outline_box[2] - outline_box[0]))
        bh = max(0.0, float(outline_box[3] - outline_box[1]))
        board_size_mm = [round(bw, 2), round(bh, 2)]
        min_dim = min(bw, bh)
    is_miniature = bool((0.0 < area < 400.0) or (min_dim is not None and 0.0 < min_dim < 18.0))

    by_side: dict[str, list[Footprint]] = {"F": [], "B": []}
    for fp in board.footprints:
        by_side.setdefault(fp.side, []).append(fp)

    max_util = 0.0
    nearest_gaps: list[float] = []
    for fps in by_side.values():
        if area > 1e-6:
            util = sum(fp.courtyard_area for fp in fps) / area
            max_util = max(max_util, util)
        boxes = [fp.box() for fp in fps]
        for i, ba in enumerate(boxes):
            best = math.inf
            for j, bb in enumerate(boxes):
                if i == j:
                    continue
                g = _box_gap(ba, bb)
                best = min(best, g)
            if math.isfinite(best):
                nearest_gaps.append(best)

    nearest_gaps.sort()
    n = len(nearest_gaps)
    median_gap = (
        nearest_gaps[n // 2]
        if n % 2
        else (0.5 * (nearest_gaps[n // 2 - 1] + nearest_gaps[n // 2]) if n else None)
    )
    tight_fraction = (sum(1 for g in nearest_gaps if g < 1.0) / n) if n else 0.0
    is_dense = (
        is_miniature
        or (max_util >= 0.45)
        or (median_gap is not None and median_gap < 1.0)
        or (tight_fraction >= 0.60)
    )
    return {
        "board_area_mm2": round(area, 2),
        "board_size_mm": board_size_mm,
        "is_miniature": is_miniature,
        "recommended_height_mm": 0.80 if is_miniature else 1.00,
        "max_side_courtyard_utilization": round(max_util, 4),
        "median_neighbor_gap_mm": round(median_gap, 3) if median_gap is not None else None,
        "tight_neighbor_fraction_lt_1mm": round(tight_fraction, 3),
        "classification": "dense" if is_dense else "standard",
    }


def recommended_stroke_mm(height_mm: float, *, min_stroke_mm: float = 0.15, max_stroke_mm: float = 0.18) -> float:
    """Compute IPC-7351 stroke width targeting 1:6..1:8 ratio and never below 1:10 or fab floor."""
    ideal = height_mm / 7.0
    floor = max(min_stroke_mm, height_mm * 0.10)
    return round(max(floor, min(max_stroke_mm, ideal)), 4)


def build_config(
    board: Board,
    intent: dict | None = None,
    *,
    policy: str = "assembly_only",
    fab_profile: str = "ipc_standard",
    dru_rules: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build deterministic silkscreen cleanup config from board rules, ``.kicad_dru``, density, and intent."""
    if policy not in SILKSCREEN_POLICIES:
        raise ValueError(f"unsupported silkscreen_policy: {policy!r}; valid: {SILKSCREEN_POLICIES}")
    if fab_profile not in FAB_PROFILES:
        raise ValueError(f"unsupported fab_profile: {fab_profile!r}; valid: {tuple(FAB_PROFILES)}")

    prof = dict(FAB_PROFILES[fab_profile])
    density = estimate_board_density(board)
    effective_mode = (
        "off"
        if policy == "off"
        else "assembly_only"
        if policy == "assembly_only"
        else "dense"
        if policy == "dense"
        else "ipc_standard"
        if policy == "ipc_standard"
        else ("dense" if density["classification"] == "dense" else "ipc_standard")
    )

    # Respect board DRC + .kicad_dru minimums so normalized silkscreen never violates fab rules.
    dru_eff = dru_rules or {}
    board_min_h = max(float(board.rules.get("min_silk_text_height") or 0.0), float(dru_eff.get("min_silk_text_height_mm") or 0.0))
    board_min_w = max(float(board.rules.get("min_silk_text_thickness") or 0.0), float(dru_eff.get("min_silk_text_thickness_mm") or 0.0))
    board_silk_clr = max(float(board.rules.get("silk_clearance") or 0.0), float(dru_eff.get("silk_clearance_mm") or 0.0))

    min_h = max(float(prof["min_height_mm"]), board_min_h)
    dense_h = max(float(prof["dense_height_mm"]), min_h)
    target_h = max(float(prof["target_height_mm"]), dense_h)
    max_h = max(float(prof["max_height_mm"]), target_h)
    min_stroke = max(float(prof["min_stroke_mm"]), board_min_w)
    target_stroke = max(float(prof["target_stroke_mm"]), min_stroke)
    max_stroke = max(float(prof["max_stroke_mm"]), target_stroke)
    min_pad_clr = max(float(prof["min_pad_clearance_mm"]), board_silk_clr)

    parts_intent = (intent or {}).get("parts") or {}
    critical_refs: list[str] = []
    non_critical_refs: list[str] = []
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        role = ((parts_intent.get(fp.ref) or {}).get("role") or {}).get("value")
        if is_critical_silkscreen_part(fp, role):
            critical_refs.append(fp.ref)
        else:
            non_critical_refs.append(fp.ref)

    is_miniature = bool(density["is_miniature"])
    prefer_dense_size = (policy == "dense") or is_miniature
    populate_hidden_refs = (
        policy == "ipc_standard"
        or (
            policy == "auto"
            and len(board.footprints) <= 16
            and float(density["max_side_courtyard_utilization"]) < 0.45
        )
    )

    return {
        "policy": policy,
        "effective_mode": effective_mode,
        "fab_profile": fab_profile,
        "density": density,
        "is_miniature": is_miniature,
        "prefer_dense_size": prefer_dense_size,
        "target_height_mm": round(target_h, 4),
        "max_height_mm": round(max_h, 4),
        "dense_height_mm": round(dense_h, 4),
        "min_height_mm": round(min_h, 4),
        "target_stroke_mm": round(target_stroke, 4),
        "max_stroke_mm": round(max_stroke, 4),
        "min_stroke_mm": round(min_stroke, 4),
        "min_stroke_ratio": float(prof["min_stroke_ratio"]),
        "max_stroke_ratio": float(prof["max_stroke_ratio"]),
        "condensed_width_ratio": float(prof["condensed_width_ratio"]),
        "min_pad_clearance_mm": round(min_pad_clr, 4),
        "avoid_bodies": True,
        "enable_leader_lines": policy != "assembly_only",
        # In explicit "dense" policy, proactively offload non-critical passives to F.Fab/B.Fab;
        # in "auto" and "ipc_standard", offload any part whose ref cannot fit cleanly outside bodies.
        "proactive_offload_non_critical": policy == "dense",
        "fallback_offload_non_critical": policy in ("auto", "ipc_standard", "dense"),
        "fallback_offload_critical": policy in ("auto", "ipc_standard", "dense"),
        "populate_hidden_refs": populate_hidden_refs,
        "hide_silk_values": policy != "off",
        "normalize_fab_texts": policy != "off",
        "critical_refs": critical_refs,
        "non_critical_refs": non_critical_refs,
        "cured_ink_spec_um": CURED_INK_SPEC_UM,
    }


def audit_texts(
    texts: list[dict[str, Any]],
    *,
    fab_profile: str = "ipc_standard",
    rules: dict[str, Any] | None = None,
    board: Board | None = None,
    dru_rules: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Audit extracted silkscreen and assembly-layer texts against IPC-7351 and fab DFM limits."""
    prof = FAB_PROFILES.get(fab_profile, FAB_PROFILES["ipc_standard"])
    rules = rules or {}
    dru_eff = dru_rules or {}
    min_h = max(float(prof["min_height_mm"]), float(rules.get("min_silk_text_height") or 0.0), float(dru_eff.get("min_silk_text_height_mm") or 0.0))
    rec_h = float(prof["target_height_mm"])
    min_s = max(float(prof["min_stroke_mm"]), float(rules.get("min_silk_text_thickness") or 0.0), float(dru_eff.get("min_silk_text_thickness_mm") or 0.0))
    hard_min_s = 0.12
    min_ratio = float(prof["min_stroke_ratio"])
    max_ratio = float(prof["max_stroke_ratio"])
    min_pad_clr = max(float(prof["min_pad_clearance_mm"]), float(rules.get("silk_clearance") or 0.0), float(dru_eff.get("silk_clearance_mm") or 0.0))

    density = estimate_board_density(board) if board is not None else None
    is_miniature = bool(density["is_miniature"]) if density is not None else False
    board_size_mm = density["board_size_mm"] if density is not None else None
    footprint_refs = {fp.ref for fp in board.footprints if fp.ref} if board is not None else set()

    silk_visible = [t for t in texts if t.get("visible") and t.get("layer") in ("F.Silkscreen", "B.Silkscreen")]
    fab_visible = [t for t in texts if t.get("visible") and t.get("layer") in ("F.Fab", "B.Fab")]

    findings: list[dict[str, Any]] = []
    below_min_h = 0
    below_rec_h = 0
    below_min_s = 0
    below_hard_s = 0
    below_min_ratio = 0
    above_max_ratio = 0
    pad_clr_bad = 0
    under_body = 0
    overlaps = 0
    condensed = 0
    silk_outside_board = 0
    fab_outside_board = 0
    fab_overlaps = 0
    fab_oversized = 0
    mirror_mismatches = 0
    oversized_for_board = 0

    heights: list[float] = []
    strokes: list[float] = []
    ratios: list[float] = []

    def _with_pos(entry: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        pos = item.get("pos_mm")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            entry["pos_mm"] = [round(float(pos[0]), 3), round(float(pos[1]), 3)]
        return entry

    for t in silk_visible:
        h = float(t.get("height_mm") or 0.0)
        w = float(t.get("width_mm") or 0.0)
        s = float(t.get("stroke_mm") or 0.0)
        if h > 0:
            heights.append(h)
        if s > 0:
            strokes.append(s)
        ratio = (s / h) if h > 1e-6 else 0.0
        if h > 1e-6:
            ratios.append(ratio)
        if h > 1e-6 and w > 0 and (w / h) <= 0.88:
            condensed += 1

        ref = t.get("ref") or "board"
        text_val = t.get("text") or ""
        layer = t.get("layer")

        if h < min_h - 1e-3:
            below_min_h += 1
            findings.append(_with_pos({
                "severity": "error",
                "rule": "silk_text_height",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(h, 3),
                "limit_mm": round(min_h, 3),
            }, t))
        elif h < rec_h - 1e-3:
            below_rec_h += 1

        if is_miniature and t.get("kind") == "field" and h > 0.90 + 1e-3:
            oversized_for_board += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "silk_oversized_for_miniature_board",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(h, 3),
                "recommended_mm": round(min_h, 3),
            }, t))

        if s < hard_min_s - 1e-3:
            below_hard_s += 1
            below_min_s += 1
            findings.append(_with_pos({
                "severity": "error",
                "rule": "silk_stroke_width",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(s, 3),
                "limit_mm": round(min_s, 3),
            }, t))
        elif s < min_s - 1e-3:
            below_min_s += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "silk_stroke_width",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(s, 3),
                "limit_mm": round(min_s, 3),
            }, t))

        if h > 1e-6 and ratio < min_ratio - 1e-3:
            below_min_ratio += 1
            findings.append(_with_pos({
                "severity": "error",
                "rule": "silk_stroke_ratio_too_thin",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "ratio": round(ratio, 4),
                "min_ratio": min_ratio,
            }, t))
        elif h > 1e-6 and ratio > max_ratio + 1e-3:
            above_max_ratio += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "silk_stroke_ratio_too_thick",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "ratio": round(ratio, 4),
                "max_ratio": max_ratio,
            }, t))

        pclr = t.get("pad_clearance_mm")
        if pclr is not None and float(pclr) < min_pad_clr - 1e-3:
            pad_clr_bad += 1
            findings.append(_with_pos({
                "severity": "error" if float(pclr) <= 0.05 else "warning",
                "rule": "silk_pad_clearance",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(float(pclr), 3),
                "limit_mm": round(min_pad_clr, 3),
            }, t))

        if t.get("under_body"):
            under_body += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "silk_under_component_body",
                "ref": ref,
                "text": text_val,
                "layer": layer,
            }, t))

        if t.get("overlaps_silk"):
            overlaps += 1
            findings.append(_with_pos({
                "severity": "error",
                "rule": "silk_text_overlap",
                "ref": ref,
                "text": text_val,
                "layer": layer,
            }, t))

        if t.get("outside_board"):
            silk_outside_board += 1
            findings.append(_with_pos({
                "severity": "error",
                "rule": "silk_text_outside_board",
                "ref": ref,
                "text": text_val,
                "layer": layer,
            }, t))

        if t.get("mirror_mismatch"):
            mirror_mismatches += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "text_mirror_mismatch",
                "ref": ref,
                "text": text_val,
                "layer": layer,
            }, t))

    for t in fab_visible:
        if t.get("outside_board"):
            fab_outside_board += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "fab_text_outside_board",
                "ref": t.get("ref") or "board",
                "text": t.get("text") or "",
                "layer": t.get("layer"),
                "bbox_mm": t.get("bbox_mm"),
            }, t))
        if t.get("overlaps_fab"):
            fab_overlaps += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "fab_text_overlap",
                "ref": t.get("ref") or "board",
                "text": t.get("text") or "",
                "layer": t.get("layer"),
                "bbox_mm": t.get("bbox_mm"),
            }, t))
        if t.get("exceeds_body"):
            fab_oversized += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "fab_text_oversized_for_body",
                "ref": t.get("ref") or "board",
                "text": t.get("text") or "",
                "layer": t.get("layer"),
                "bbox_mm": t.get("bbox_mm"),
            }, t))
        if t.get("mirror_mismatch"):
            mirror_mismatches += 1
            findings.append(_with_pos({
                "severity": "warning",
                "rule": "text_mirror_mismatch",
                "ref": t.get("ref") or "board",
                "text": t.get("text") or "",
                "layer": t.get("layer"),
            }, t))

    ref_texts = [t for t in silk_visible if t.get("kind") == "field"]
    silk_ref_set = {str(t.get("ref")) for t in ref_texts if t.get("ref")}
    fab_ref_set = {
        str(t.get("ref"))
        for t in fab_visible
        if t.get("ref") and (t.get("kind") == "field" or t.get("text") == t.get("ref"))
    }
    missing_refs: list[str] = []
    legend_coverage: dict[str, Any] | None = None
    if footprint_refs:
        on_overlay = sorted(footprint_refs & silk_ref_set)
        on_fab_only = sorted((footprint_refs - silk_ref_set) & fab_ref_set)
        missing_refs = sorted(footprint_refs - silk_ref_set - fab_ref_set)
        legend_coverage = {
            "total_footprints": len(footprint_refs),
            "on_overlay": len(on_overlay),
            "on_fab_only": len(on_fab_only),
            "missing": len(missing_refs),
            "on_overlay_refs": on_overlay[:40],
            "on_fab_only_refs": on_fab_only[:40],
            "missing_refs": missing_refs[:40],
        }
        if texts:
            fp_map = {fp.ref: fp for fp in board.footprints} if board is not None else {}
            for ref in missing_refs:
                fp = fp_map.get(ref)
                entry = {
                    "severity": "warning",
                    "rule": "missing_component_legend",
                    "ref": ref,
                    "text": ref,
                    "layer": f"{fp.side}.Silkscreen" if fp is not None else "F.Silkscreen",
                }
                if fp is not None:
                    entry["pos_mm"] = [round(fp.x, 3), round(fp.y, 3)]
                findings.append(entry)
        if on_overlay and on_fab_only:
            overlay_mode = "mixed_overlay_and_fab"
        elif on_overlay:
            overlay_mode = "populated"
        elif on_fab_only:
            overlay_mode = "assembly_only"
        else:
            overlay_mode = "missing_legends" if texts else "unextracted"
    else:
        if ref_texts and fab_ref_set - silk_ref_set:
            overlay_mode = "mixed_overlay_and_fab"
        elif ref_texts or silk_visible:
            overlay_mode = "populated"
        elif fab_visible:
            overlay_mode = "assembly_only"
        else:
            overlay_mode = "unextracted"

    error_count = sum(1 for f in findings if f["severity"] == "error")
    warn_count = sum(1 for f in findings if f["severity"] == "warning")
    ref_uuids = {t.get("ref") for t in ref_texts}
    ref_findings = [f for f in findings if f.get("ref") in ref_uuids and f.get("ref") != "board"]
    ref_errors = sum(1 for f in ref_findings if f["severity"] == "error")
    ref_warnings = sum(1 for f in ref_findings if f["severity"] == "warning")

    fab_denom = max(1, len(fab_visible))
    fab_penalty = (
        (min(30.0, 10.0 * fab_outside_board) if fab_outside_board else 0.0)
        + (min(25.0, 25.0 * fab_overlaps / fab_denom) if fab_overlaps else 0.0)
        + (min(15.0, 15.0 * fab_oversized / fab_denom) if fab_oversized else 0.0)
        + (min(15.0, 15.0 * mirror_mismatches / max(1, len(silk_visible) + len(fab_visible))) if mirror_mismatches else 0.0)
    )
    missing_penalty = (
        min(25.0, 25.0 * len(missing_refs) / max(1, len(footprint_refs)))
        if (texts and missing_refs)
        else 0.0
    )
    if not silk_visible:
        score = round(max(0.0, 100.0 - fab_penalty - missing_penalty), 2)
    else:
        penalty = (
            min(40.0, 40.0 * (below_min_h + below_hard_s + below_min_ratio + silk_outside_board) / len(silk_visible))
            + min(30.0, 30.0 * (pad_clr_bad + overlaps) / len(silk_visible))
            + min(20.0, 20.0 * under_body / len(silk_visible))
            + min(
                10.0,
                10.0 * (below_min_s - below_hard_s + above_max_ratio + oversized_for_board) / len(silk_visible),
            )
            + fab_penalty * 0.5
            + missing_penalty * 0.5
        )
        score = round(max(0.0, 100.0 - penalty), 2)

    status = "fail" if error_count > 0 else "warn" if warn_count > 0 else "pass"
    return {
        "fab_profile": fab_profile,
        "status": status,
        "dfm_score": score,
        "overlay_mode": overlay_mode,
        "is_miniature": is_miniature,
        "board_size_mm": board_size_mm,
        "visible_silk_texts": len(silk_visible),
        "visible_ref_designators": len(ref_texts),
        "visible_fab_texts": len(fab_visible),
        "condensed_count": condensed,
        "legend_coverage": legend_coverage,
        "reference_designators": {
            "count": len(ref_texts),
            "errors": ref_errors,
            "warnings": ref_warnings,
        },
        "height_mm": {
            "min": round(min(heights), 3) if heights else None,
            "max": round(max(heights), 3) if heights else None,
            "ipc_recommended": [0.80, 0.90] if is_miniature else [1.00, 1.27],
            "dense_floor": round(min_h, 3),
        },
        "stroke_mm": {
            "min": round(min(strokes), 3) if strokes else None,
            "max": round(max(strokes), 3) if strokes else None,
            "ipc_recommended": [0.15, 0.18],
            "min_limit": round(min_s, 3),
        },
        "stroke_ratio": {
            "min": round(min(ratios), 4) if ratios else None,
            "max": round(max(ratios), 4) if ratios else None,
            "ipc_ideal": [0.125, 0.167],
            "hard_floor": min_ratio,
        },
        "counts": {
            "errors": error_count,
            "warnings": warn_count,
            "below_min_height": below_min_h,
            "below_recommended_height": below_rec_h,
            "below_min_stroke": below_min_s,
            "below_min_ratio": below_min_ratio,
            "above_max_ratio": above_max_ratio,
            "pad_clearance_violations": pad_clr_bad,
            "under_body": under_body,
            "overlaps": overlaps,
            "silk_outside_board": silk_outside_board,
            "fab_outside_board": fab_outside_board,
            "fab_overlaps": fab_overlaps,
            "fab_oversized": fab_oversized,
            "mirror_mismatches": mirror_mismatches,
            "oversized_for_board": oversized_for_board,
            "missing_legends": len(missing_refs) if texts else 0,
        },
        "findings": findings[:40],
        "findings_total": len(findings),
        "cured_ink_spec_um": CURED_INK_SPEC_UM,
    }


def audit_board(board: Board, *, fab_profile: str = "ipc_standard") -> dict[str, Any]:
    """Audit a Board IR's extracted silkscreen/assembly texts."""
    return audit_texts(board.silkscreen_texts, fab_profile=fab_profile, rules=board.rules, board=board)
