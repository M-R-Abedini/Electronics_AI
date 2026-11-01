# Modified 2026-11-01 by M-R-Abedini; see root MODIFICATIONS.md.
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Net-class assignment from net roles.

Before routing a board you set up net classes (Power, Ground, Differential,
Clock, Analog, ...) and hang the width / clearance / impedance rules off them.
The plan's ``Net.role`` (and the power/ground flags) already carry that intent,
so this maps each net to a class deterministically -- the membership the
planner would otherwise assign by hand. Naming-agnostic (roles + flags, not net
names). Composes with the differential-pair detector and the mixed-signal
domain inference, which read the same roles. Pure offline.
"""

from __future__ import annotations

from dataclasses import dataclass

from eda_agent.design.plan import DesignPlan

# role -> class for the well-known roles (power/ground come from flags first).
_ROLE_CLASS = {
    "differential": "differential",
    "clock": "clock",
    "analog_sensitive": "analog",
    "feedback": "analog",
    "switch": "switch",
    "high_current": "high_current",
    "control": "control",
    "power": "power",
    "ground": "ground",
}


@dataclass(frozen=True)
class NetClassReport:
    """Per-net class plus the inverse grouping."""

    by_net: dict[str, str]                 # net name -> class
    groups: dict[str, tuple[str, ...]]     # class -> sorted net names


def net_class_of(net) -> str:
    """Classify one net from its flags / role. Power and ground flags win over
    a role; otherwise a known role maps to its class; default ``signal``."""
    role = (net.role or "").strip().lower()
    if net.is_ground or role == "ground":
        return "ground"
    if net.is_power or role == "power":
        return "power"
    return _ROLE_CLASS.get(role, "signal")


def classify_nets(plan: DesignPlan) -> NetClassReport:
    """Assign every net in ``plan`` to a class (see module docstring).

    Returns a :class:`NetClassReport` with the per-net mapping and the inverse
    grouping (class -> sorted nets), both deterministic.
    """
    by_net: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for net in plan.nets:
        cls = net_class_of(net)
        by_net[net.name] = cls
        groups.setdefault(cls, []).append(net.name)
    return NetClassReport(
        by_net=dict(sorted(by_net.items())),
        groups={c: tuple(sorted(n)) for c, n in sorted(groups.items())})


def classify_copper_intent(plan: DesignPlan | dict | None, topology: dict | None = None) -> dict:
    """Evidence-bearing physical intent derived from the existing DesignPlan.

    Uppercase physical tags augment, rather than replace, ``classify_nets``.
    Unidentified nets stay UNKNOWN; names and IC model strings do not confer
    electrical intent or datasheet pin functions. Numeric constraints are
    passed through only when supplied explicitly by the caller.
    """
    from eda_agent.design.mixed_signal import ANALOG_ROLES
    from eda_agent.design.motifs import recognize_copper_motifs
    topology = topology or {}
    if plan is None:
        from types import SimpleNamespace
        snapshot = topology.get("board_snapshot", {})
        metadata = snapshot.get("net_metadata", {})
        names = set()
        for entry in snapshot.get("nets", []):
            if isinstance(entry, str):
                names.add(entry)
            elif isinstance(entry, dict) and entry.get("name"):
                names.add(entry["name"]); metadata = {**metadata, entry["name"]: entry}
        names.update(p.get("net_name") for p in snapshot.get("pads", []) if p.get("net_name"))
        names.update(topology.get("net_roles", {}))
        nets = []
        for name in sorted(names):
            facts = metadata.get(name, {})
            role = topology.get("net_roles", {}).get(name, facts.get("role"))
            nets.append(SimpleNamespace(name=name, role=role, is_power=facts.get("is_power", False),
                                        is_ground=facts.get("is_ground", False), pins=[
                                            SimpleNamespace(refdes=p.get("component"), pin=p.get("pad_name"))
                                            for p in snapshot.get("pads", []) if p.get("net_name") == name]))
        plan = SimpleNamespace(nets=nets)
        motifs = [{**m, "kind": "current_shunt"} for m in topology.get("shunts", [])]
        motifs += [{**m, "kind": "decoupling"} for m in topology.get("decoupling", [])]
        motifs += [{**m, "kind": "switching_loop"} for m in topology.get("switching_loops", [])]
        differential_pairs = topology.get("differential_pairs", [])
    elif isinstance(plan, dict):
        plan = DesignPlan.model_validate(plan)
        motifs = recognize_copper_motifs(plan, topology)
        from eda_agent.design.diffpairs import detect_diff_pairs
        differential_pairs = [{"nets": list(p.nets), "endpoints": list(p.endpoints)} for p in detect_diff_pairs(plan)]
    else:
        motifs = recognize_copper_motifs(plan, topology)
        from eda_agent.design.diffpairs import detect_diff_pairs
        differential_pairs = [{"nets": list(p.nets), "endpoints": list(p.endpoints)} for p in detect_diff_pairs(plan)]
    base = classify_nets(plan)
    by_net, evidence, unknowns = {}, [], []
    sensitive_motif_nets = {n for m in motifs if m["kind"] in {"opamp_inverting", "crystal_load", "fb_divider", "rc_compensation"} for n in m.get("nets", [])}
    role_tags = {"differential": "HIGH_SPEED_DIFF", "clock": "HIGH_SPEED_SINGLE",
                 "high_speed": "HIGH_SPEED_SINGLE", "switch": "AGGRESSIVE_SWITCHING",
                 "high_current": "HIGH_CURRENT_POWER", "high_speed_diff": "HIGH_SPEED_DIFF",
                 "high_speed_single": "HIGH_SPEED_SINGLE", "aggressive_switching": "AGGRESSIVE_SWITCHING",
                 "high_current_power": "HIGH_CURRENT_POWER", "control": "CONTROL",
                 "signal": "SIGNAL", "digital": "DIGITAL", "analog": "ANALOG"}
    for net in plan.nets:
        role = (net.role or "").strip().lower()
        tags = []
        why, confidence = [], 1.0
        if net.is_ground or role == "ground":
            tags.append("GROUND")
            why.append("declared ground net")
        if net.is_power or role == "power":
            tags.append("POWER")
            why.append("declared power net")
        if role in role_tags:
            tags.append(role_tags[role]); why.append(f"Net.role={role}")
        if role in ANALOG_ROLES or role == "sensitive_analog":
            tags.append("SENSITIVE_ANALOG"); why.append(f"Net.role={role}")
        if net.name in sensitive_motif_nets and not (net.is_power or net.is_ground or role in {"power", "ground"}):
            tags.append("SENSITIVE_ANALOG"); why.append("existing analog/crystal circuit motif"); confidence = 0.85
        for pin in net.pins:
            key = f"{pin.refdes}.{pin.pin}"
            verified_role = str(topology.get("pin_roles", {}).get(key, "")).lower()
            if verified_role in ANALOG_ROLES:
                tags.append("SENSITIVE_ANALOG"); why.append(f"supplied pin role {key}={verified_role}")
        if not tags:
            tags = ["UNKNOWN"]
            confidence = 0.0
            unknowns.append(f"{net.name}: physical electrical intent unclassified")
        by_net[net.name] = sorted(set(tags))
        evidence.append({"net_name": net.name, "base_class": base.by_net[net.name],
                         "confidence": confidence, "sources": why, "pin_roles_inferred": False})
    for motif in motifs:
        if motif["kind"] == "current_shunt" and not (motif.get("kelvin_mapping_known") or motif.get("kelvin_paths")):
            unknowns.append(f"{motif['component']}: separate load/Kelvin terminal assignment unavailable")
    return {"net_classes": by_net, "constraints": topology.get("constraints", {}),
            "motifs": motifs, "differential_pairs": differential_pairs, "evidence": evidence,
            "unknowns": unknowns or ([] if by_net else ["DesignPlan or explicit PCB electrical roles unavailable"])}


__all__ = ["NetClassReport", "net_class_of", "classify_nets", "classify_copper_intent"]
