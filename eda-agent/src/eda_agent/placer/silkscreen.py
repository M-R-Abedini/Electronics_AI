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
    """Estimate assembly silkscreen crowding from courtyard utilization and part-to-part spacing."""
    area = float(board.board_area_mm2 or 0.0)
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
    is_dense = (max_util >= 0.45) or (median_gap is not None and median_gap < 1.0) or (tight_fraction >= 0.60)
    return {
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
    policy: str = "auto",
    fab_profile: str = "ipc_standard",
) -> dict[str, Any]:
    """Build deterministic silkscreen cleanup config from board rules, density, and design intent."""
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

    # Respect board DRC minimums so normalized silkscreen never violates board design rules.
    board_min_h = float(board.rules.get("min_silk_text_height") or 0.0)
    board_min_w = float(board.rules.get("min_silk_text_thickness") or 0.0)
    board_silk_clr = float(board.rules.get("silk_clearance") or 0.0)

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

    return {
        "policy": policy,
        "effective_mode": effective_mode,
        "fab_profile": fab_profile,
        "density": density,
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
        # in "auto" and "ipc_standard", offload non-critical passives only when no legal silk slot fits.
        "proactive_offload_non_critical": policy == "dense",
        "fallback_offload_non_critical": policy in ("auto", "ipc_standard", "dense"),
        "critical_refs": critical_refs,
        "non_critical_refs": non_critical_refs,
        "cured_ink_spec_um": CURED_INK_SPEC_UM,
    }


def audit_texts(
    texts: list[dict[str, Any]],
    *,
    fab_profile: str = "ipc_standard",
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit extracted silkscreen and assembly-layer texts against IPC-7351 and fab DFM limits."""
    prof = FAB_PROFILES.get(fab_profile, FAB_PROFILES["ipc_standard"])
    rules = rules or {}
    min_h = max(float(prof["min_height_mm"]), float(rules.get("min_silk_text_height") or 0.0))
    rec_h = float(prof["target_height_mm"])
    min_s = max(float(prof["min_stroke_mm"]), float(rules.get("min_silk_text_thickness") or 0.0))
    hard_min_s = 0.12
    min_ratio = float(prof["min_stroke_ratio"])
    max_ratio = float(prof["max_stroke_ratio"])
    min_pad_clr = max(float(prof["min_pad_clearance_mm"]), float(rules.get("silk_clearance") or 0.0))

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

    heights: list[float] = []
    strokes: list[float] = []
    ratios: list[float] = []

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
            findings.append({
                "severity": "error",
                "rule": "silk_text_height",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(h, 3),
                "limit_mm": round(min_h, 3),
            })
        elif h < rec_h - 1e-3:
            below_rec_h += 1

        if s < hard_min_s - 1e-3:
            below_hard_s += 1
            below_min_s += 1
            findings.append({
                "severity": "error",
                "rule": "silk_stroke_width",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(s, 3),
                "limit_mm": round(min_s, 3),
            })
        elif s < min_s - 1e-3:
            below_min_s += 1
            findings.append({
                "severity": "warning",
                "rule": "silk_stroke_width",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(s, 3),
                "limit_mm": round(min_s, 3),
            })

        if h > 1e-6 and ratio < min_ratio - 1e-3:
            below_min_ratio += 1
            findings.append({
                "severity": "error",
                "rule": "silk_stroke_ratio_too_thin",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "ratio": round(ratio, 4),
                "min_ratio": min_ratio,
            })
        elif h > 1e-6 and ratio > max_ratio + 1e-3:
            above_max_ratio += 1
            findings.append({
                "severity": "warning",
                "rule": "silk_stroke_ratio_too_thick",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "ratio": round(ratio, 4),
                "max_ratio": max_ratio,
            })

        pclr = t.get("pad_clearance_mm")
        if pclr is not None and float(pclr) < min_pad_clr - 1e-3:
            pad_clr_bad += 1
            findings.append({
                "severity": "error" if float(pclr) <= 0.05 else "warning",
                "rule": "silk_pad_clearance",
                "ref": ref,
                "text": text_val,
                "layer": layer,
                "measured_mm": round(float(pclr), 3),
                "limit_mm": round(min_pad_clr, 3),
            })

        if t.get("under_body"):
            under_body += 1
            findings.append({
                "severity": "warning",
                "rule": "silk_under_component_body",
                "ref": ref,
                "text": text_val,
                "layer": layer,
            })

        if t.get("overlaps_silk"):
            overlaps += 1
            findings.append({
                "severity": "error",
                "rule": "silk_text_overlap",
                "ref": ref,
                "text": text_val,
                "layer": layer,
            })

    error_count = sum(1 for f in findings if f["severity"] == "error")
    warn_count = sum(1 for f in findings if f["severity"] == "warning")
    ref_texts = [t for t in silk_visible if t.get("kind") == "field"]
    ref_uuids = {t.get("ref") for t in ref_texts}
    ref_findings = [f for f in findings if f.get("ref") in ref_uuids and f.get("ref") != "board"]
    ref_errors = sum(1 for f in ref_findings if f["severity"] == "error")
    ref_warnings = sum(1 for f in ref_findings if f["severity"] == "warning")
    if not silk_visible:
        score = 100.0
    else:
        penalty = (
            min(40.0, 40.0 * (below_min_h + below_hard_s + below_min_ratio) / len(silk_visible))
            + min(30.0, 30.0 * (pad_clr_bad + overlaps) / len(silk_visible))
            + min(20.0, 20.0 * under_body / len(silk_visible))
            + min(10.0, 10.0 * (below_min_s - below_hard_s + above_max_ratio) / len(silk_visible))
        )
        score = round(max(0.0, 100.0 - penalty), 2)

    status = "fail" if error_count > 0 else "warn" if warn_count > 0 else "pass"
    return {
        "fab_profile": fab_profile,
        "status": status,
        "dfm_score": score,
        "visible_silk_texts": len(silk_visible),
        "visible_ref_designators": len(ref_texts),
        "visible_fab_texts": len(fab_visible),
        "condensed_count": condensed,
        "reference_designators": {
            "count": len(ref_texts),
            "errors": ref_errors,
            "warnings": ref_warnings,
        },
        "height_mm": {
            "min": round(min(heights), 3) if heights else None,
            "max": round(max(heights), 3) if heights else None,
            "ipc_recommended": [1.00, 1.27],
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
        },
        "findings": findings[:40],
        "findings_total": len(findings),
        "cured_ink_spec_um": CURED_INK_SPEC_UM,
    }


def audit_board(board: Board, *, fab_profile: str = "ipc_standard") -> dict[str, Any]:
    """Audit a Board IR's extracted silkscreen/assembly texts."""
    return audit_texts(board.silkscreen_texts, fab_profile=fab_profile, rules=board.rules)
