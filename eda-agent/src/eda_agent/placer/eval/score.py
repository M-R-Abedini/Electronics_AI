# SPDX-License-Identifier: MIT
"""``evaluate``: legality gate, metrics and Pro Score for one placement.

Pro Score ranks legal candidates only; it never replaces a gate (architecture
8.3).  Weights 35/25/15/15/10; each component maps to 0..100 with the
declared reference functions below.  Lint arrives in F6: until then its weight
is left out and the others are renormalised, which ``weights_used`` states.

Version 1 (F2): critical loops are measured from the design intent, and the
weights were calibrated once against blind rankings of the dev candidates by
two raters (2026-11-09, ``pixi run calibrate``): the declared weights agreed
with 72.7 % of the 44 stated preferences, the raters with each other on 71 %,
and the best grid weights gained only one pair (75.0 %), below the declared
5-point adoption threshold, so the declared weights stand (decision log).
"""

from __future__ import annotations

from ..ir import Board
from . import legality, metrics

WEIGHTS = {"routability": 35, "critical_loops": 25, "ratsnest": 15, "regularity": 15, "lint": 10}
PRO_SCORE_VERSION = 1
CALIBRATION = {"date": "2026-11-09", "raters": 2, "pairs": 44, "agreement": 0.7273, "inter_rater": 0.71,
               "adopted": "declared weights (fit gain below threshold)"}
DECAP_GOOD_MM, DECAP_BAD_MM = 1.5, 6.0  # reference: median decap pad -> target supply pin


def _clip(v: float) -> float:
    return max(0.0, min(100.0, v))


def components(m: dict, route: dict | None) -> dict[str, float | None]:
    if route is not None:
        routability = 100.0 * route["coverage"]
    elif m["rudy"].get("available"):
        routability = 100.0 * (1.0 - m["rudy"]["over_capacity_fraction"])
    else:
        routability = None
    d = m["critical_loops"]["decap_pin_distance"]
    loops = (_clip(100.0 * (DECAP_BAD_MM - d["median_mm"]) / (DECAP_BAD_MM - DECAP_GOOD_MM))
             if d.get("n") else None)
    ratsnest = 100.0 / (1.0 + 4.0 * m["crossings"]["per_edge"])
    reg = m["regularity"]
    regularity = (100.0 * (0.5 * reg["aligned_fraction"] + 0.3 * reg["rotation_on_90_grid"]
                           + 0.2 * reg["dominant_axis_fraction"]) if reg.get("n") else None)
    return {"routability": routability, "critical_loops": loops, "ratsnest": round(ratsnest, 2),
            "regularity": regularity, "lint": None}


def pro_score(parts: dict[str, float | None]) -> dict:
    used = {k: w for k, w in WEIGHTS.items() if parts.get(k) is not None}
    total = sum(used.values())
    score = sum(parts[k] * w for k, w in used.items()) / total if total else None
    return {"version": PRO_SCORE_VERSION, "calibrated": True, "calibration": CALIBRATION,
            "score": round(score, 2) if score is not None else None,
            "components": {k: (round(v, 2) if v is not None else None) for k, v in parts.items()},
            "weights_used": used}


def evaluate(board: Board, route: dict | None = None, fixed: set[str] | None = None,
             intent: dict | None = None) -> dict:
    """``route``: metrics of a route check on the same placement; ``fixed``: the S1 parts; ``intent``: the
    design intent (drafted from the netlist when not given, which is placement-independent)."""
    from ..intent import infer, model

    if intent is None:
        intent = infer.draft(board)
    findings = legality.check(board, fixed=fixed)
    legal = legality.summary(findings)
    loops = metrics.critical_loops(board, intent)
    loops["intent_sha256"] = model.intent_hash(intent)
    m = {"hpwl": metrics.hpwl(board), "crossings": metrics.crossings(board), "rudy": metrics.rudy(board),
         "critical_loops": loops, "regularity": metrics.regularity(board)}
    return {
        "legality": legal,
        "metrics": m,
        "pro_score": pro_score(components(m, route)),
        "capacity_warnings": board.warnings,
        "unknowns": board.unknowns,
    }
