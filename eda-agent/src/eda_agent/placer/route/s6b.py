# SPDX-License-Identifier: MIT
"""S6b: Opt-in reference plane synthesis strictly gated behind ``stackup_policy="propose"``.

By default (``stackup_policy="locked"``), the engine NEVER synthesizes or mutates copper
planes on user boards. Only when the caller explicitly passes ``stackup_policy="propose"``
does S6b propose solid board-outline-inset reference plane zones (e.g. ``In1.Cu = GND`` on
4L boards or dual ``In1.Cu`` / ``In4.Cu`` GND planes on 6L boards) for designs lacking
reference planes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..eval.legality import board_polygon
from ..ir import Board, is_ground_net, is_power_net, plane_zone_layers
from ..macro import Instance, MZone
from ..macro_proof import apply_instance

STACKUP_POLICIES = ("locked", "propose")


def _primary_gnd_net(board: Board) -> str | None:
    gnds = [n for n in board.nets if is_ground_net(n.name)]
    if not gnds:
        return None
    gnds.sort(key=lambda n: (-n.pads, n.name))
    return gnds[0].name


def _primary_power_net(board: Board) -> str | None:
    pwrs = [n for n in board.nets if is_power_net(n.name) and not is_ground_net(n.name)]
    if not pwrs:
        return None
    pwrs.sort(key=lambda n: (-n.pads, n.name))
    return pwrs[0].name


def propose_planes(
    board: Board,
    intent: dict | None = None,
    *,
    stackup_policy: str = "locked",
) -> dict[str, Any]:
    """Propose solid reference plane zones ONLY when ``stackup_policy == "propose"``."""
    if stackup_policy not in STACKUP_POLICIES:
        raise ValueError(f"unsupported stackup_policy {stackup_policy!r}; valid: {STACKUP_POLICIES}")
    if stackup_policy != "propose":
        return {"applied": False, "reason": "stackup_policy_locked", "zones": []}

    ordered = [cl for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    n_layers = len(ordered)
    if n_layers < 2:
        return {"applied": False, "reason": "single_layer_board", "zones": []}

    outline = board_polygon(board)
    if outline is None or outline.is_empty:
        return {"applied": False, "reason": "missing_outline", "zones": []}
    edge = max(float(board.rules.get("copper_edge_clearance") or 0.0), 0.30)
    inset = outline.buffer(-edge)
    if inset.is_empty:
        return {"applied": False, "reason": "outline_too_small", "zones": []}

    polys = [inset] if inset.geom_type == "Polygon" else [g for g in getattr(inset, "geoms", ()) if g.geom_type == "Polygon"]
    rings = [
        [(round(float(x), 4), round(float(y), 4)) for x, y in list(p.exterior.coords)[:-1]]
        for p in polys
        if not p.is_empty and p.area > 1.0
    ]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return {"applied": False, "reason": "no_valid_inset_polygon", "zones": []}

    by_canon = {cl.name: cl.canonical for cl in ordered}
    name_by_canon = {cl.canonical: cl.name for cl in ordered}
    existing = plane_zone_layers(board)
    covered_layers = {by_canon.get(n, n) for lyrs in existing.values() for n in lyrs}
    gnd_net = _primary_gnd_net(board)
    pwr_net = _primary_power_net(board)
    if gnd_net is None:
        return {"applied": False, "reason": "no_ground_net", "zones": []}

    proposed: list[dict[str, Any]] = []
    if n_layers == 2:
        if "B.Cu" not in covered_layers:
            proposed.append({
                "name": "eai_s6b_gnd_bot",
                "net": gnd_net,
                "layers": [name_by_canon["B.Cu"]],
                "outline": rings,
                "pad_connection": "thermal",
                "priority": 0,
            })
    elif n_layers == 4:
        in1 = ordered[1].canonical
        in2 = ordered[2].canonical
        if in1 not in covered_layers:
            proposed.append({
                "name": "eai_s6b_gnd_l2",
                "net": gnd_net,
                "layers": [name_by_canon[in1]],
                "outline": rings,
                "pad_connection": "thermal",
                "priority": 1,
            })
        if in2 not in covered_layers and pwr_net is not None:
            proposed.append({
                "name": "eai_s6b_pwr_l3",
                "net": pwr_net,
                "layers": [name_by_canon[in2]],
                "outline": rings,
                "pad_connection": "thermal",
                "priority": 1,
            })
    else:
        # 6L+ Hartley dual-GND shielding: L2 and L(N-1) solid GND planes.
        gnd_targets = (ordered[1].canonical, ordered[-2].canonical)
        for idx, canon in enumerate(gnd_targets, start=1):
            if canon not in covered_layers:
                proposed.append({
                    "name": f"eai_s6b_gnd_{idx}",
                    "net": gnd_net,
                    "layers": [name_by_canon[canon]],
                    "outline": rings,
                    "pad_connection": "thermal",
                    "priority": 1,
                })

    if not proposed:
        return {"applied": False, "reason": "planes_already_present", "zones": []}
    return {"applied": True, "reason": "proposed", "zones": proposed}


def apply_proposed_planes(
    board_path: str | Path,
    out_board: str | Path,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Write proposed S6b planes onto a board copy (P8-safe, unlocked so zones refill cleanly)."""
    zones_spec = proposal.get("zones") or []
    if not proposal.get("applied") or not zones_spec:
        return {"applied": False, "zones_added": 0, "board": None}
    src = Path(board_path).resolve()
    dst = Path(out_board).resolve()
    mzones = [
        MZone(
            layers=tuple(z["layers"]),
            net=str(z["net"]),
            outline=[list(ring) for ring in z["outline"]],
            pad_connection=str(z.get("pad_connection", "thermal")),
            name=str(z.get("name", "")),
            priority=int(z.get("priority", 0)),
        )
        for z in zones_spec
    ]
    apply_instance(src, Instance({}, [], [], mzones, [], False), dst, keep_only=None, strip="none", lock=False)
    return {"applied": True, "zones_added": len(mzones), "board": str(dst)}
