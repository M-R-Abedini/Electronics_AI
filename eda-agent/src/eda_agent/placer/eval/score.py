# SPDX-License-Identifier: MIT
"""``evaluate``: legality gate, metrics and Pro Score v0 for one placement.

Pro Score ranks legal candidates only; it never replaces a gate (architecture
8.3).  Weights 35/25/15/15/10.  Version 0 maps each component to 0..100 with
the declared reference functions below; F2 calibrates them once against the
gallery and the user's judgement.  Lint arrives in F6: until then its weight
is left out and the others are renormalised, which ``weights_used`` states.
"""

from __future__ import annotations

from ..ir import Board
from . import legality, metrics

WEIGHTS = {"routability": 35, "critical_loops": 25, "ratsnest": 15, "regularity": 15, "lint": 10}
PRO_SCORE_VERSION = 0
DECAP_GOOD_MM, DECAP_BAD_MM = 1.5, 6.0  # v0 reference: median IC power pin -> decap pad


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
    return {"version": PRO_SCORE_VERSION, "calibrated": False,
            "score": round(score, 2) if score is not None else None,
            "components": {k: (round(v, 2) if v is not None else None) for k, v in parts.items()},
            "weights_used": used}


def evaluate(board: Board, route: dict | None = None) -> dict:
    """``route`` is the metrics block of a route check run on the same placement, if any."""
    findings = legality.check(board)
    legal = legality.summary(findings)
    m = {"hpwl": metrics.hpwl(board), "crossings": metrics.crossings(board), "rudy": metrics.rudy(board),
         "critical_loops": metrics.critical_loops(board), "regularity": metrics.regularity(board)}
    return {
        "legality": legal,
        "metrics": m,
        "pro_score": pro_score(components(m, route)),
        "capacity_warnings": board.warnings,
        "unknowns": board.unknowns,
    }
