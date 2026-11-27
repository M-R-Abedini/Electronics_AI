# SPDX-License-Identifier: Apache-2.0
"""First-principles electrical safety, stress & topology audit on a DesignSnapshot.

All checks are board-agnostic and operate purely on normalized netlist topology,
pin functions, component values, and footprint geometry hints so the same
engineering rules catch high-impact hardware bugs across any KiCad or Altium
design:

1. ``regulator_en_overvoltage``       -- Regulator EN/SHDN tied directly to high VIN
2. ``hv_undersized_passive``          -- Undersized SMD R/C on mains/HV or snubber nodes
3. ``mains_fuse_voltage_rating``      -- <=250 V fuse on 230-277 VAC industrial mains
4. ``bidirectional_tvs_on_unipolar_ic`` -- Bidirectional TVS protecting a unipolar IC
5. ``hv_rectifier_bridge_voltage_margin`` -- <=600 V bridge on 230+ VAC mains (MOV clamp >710 Vpk)
6. ``flyback_fwd_highside_polarity``  -- Flyback FWD sensing high-side rectified secondary (negative swing)
7. ``sense_outside_series_resistor``  -- Series protection R outside Kelvin +VSENSE feedback loop
8. ``switcher_vout_headroom``         -- Flyback controller VOUT pin directly on >=22 V near 27 V Abs Max
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .snapshot import DesignSnapshot, SnapPart, SnapPin

# ---------------------------------------------------------------------------
# Generic electrical classification patterns (naming/board-agnostic)
# ---------------------------------------------------------------------------

_HV_NET_RE = re.compile(
    r"(?:^|[/_-])("
    r"AC_?L(?:INE)?|AC_?N(?:EUT(?:RAL)?)?|AC_?L_?FUSED|AC_?L_?RECT|"
    r"MAINS|LINE_?IN|NEUTRAL|HV_?BUS|HV_?RECT|HV_?DC|HV_?IN|HV_?RAIL|"
    r"FLY_?DRAIN|RCD_?CLAMP|SNUB(?:BER)?|PRI_?DRAIN|BULK_?HV|"
    r"(?:110|120|220|230|240|277|325|380|390|400|480)V(?:AC|DC)?"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_MAINS_AC_NET_RE = re.compile(
    r"(?:^|[/_-])("
    r"AC_?L(?:INE)?|AC_?N(?:EUT(?:RAL)?)?|AC_?L_?FUSED|AC_?L_?RECT|"
    r"MAINS|LINE_?IN|NEUTRAL|(?:220|230|240|277)V(?:AC)?"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_SNUBBER_NET_RE = re.compile(
    r"(?:^|[/_-])(RCD_?CLAMP|SNUB(?:BER)?|FLY_?DRAIN|CLAMP_?HV|INRUSH|AC_?L_?RECT)(?:$|[/_-])",
    re.IGNORECASE,
)

_NEG_RAIL_RE = re.compile(
    r"(?:^|[/_-])(?:-\d+(?:[._V]\d+)?V?|VEE|VNEG|VSS_?NEG|AVSS_?NEG|NEG_?\d+V?)(?:$|[/_-])",
    re.IGNORECASE,
)

_RAIL_VOLTS_RE = re.compile(
    r"(?:^|[/_+-])(?:V(?:CC|DD|BUS|IN|OUT|RAIL|BAT)?_?)?"
    r"(\d+(?:[._]\d+)?)\s*V(?:AC|DC|_[A-Z0-9]+|$|[/_-])|"
    r"(?:^|[/_-])V(\d+)(?:V(\d+))?(?:_[A-Z0-9]+|$)",
    re.IGNORECASE,
)

_SMALL_SMD_RE = re.compile(r"(?:^|[_:\-])(0201|0402|0603)(?:$|[_:\-])")
_MED_SMD_RE = re.compile(r"(?:^|[_:\-])(0805)(?:$|[_:\-])")
_UPTO_1206_RE = re.compile(r"(?:^|[_:\-])(0201|0402|0603|0805|1206)(?:$|[_:\-])")

_LOW_EN_LDO_RE = re.compile(r"\b(TPS709\w*)\b", re.IGNORECASE)
_REGULATOR_VALUE_RE = re.compile(
    r"\b(TPS7\w+|TLV7\w+|LP59\w+|LD\d{4,5}|LM\d{3,4}|AMS1117|NCP1117|AP21\d\d|"
    r"MIC5\d+|ADP1\d+|LT30\d\d|MCP17\d\d|XC62\d\d|LDO)\b",
    re.IGNORECASE,
)

_BIDIR_TVS_RE = re.compile(
    r"\b(SM[ABC]J\d+CA|P[46]KE\d+CA|1\.5KE\d+CA|SA\d+CA|TVS.*CA|PESD\w*B\w*|BI[-_ ]?DIR)\b",
    re.IGNORECASE,
)

_FUSE_250V_RE = re.compile(
    r"(?:(?:^|[^0-9A-Za-z])250\s*V(?:AC)?(?:$|[^0-9A-Za-z])|"
    r"(?:^|[^0-9])372(?:$|[^0-9])|TR5.*250)",
    re.IGNORECASE,
)

_BRIDGE_600V_RE = re.compile(
    r"\b(MB[246]S|DF0[246]S?|DB10[1-5]|KBP20[246]|ABS[246]|B[246]S|600\s*V)\b",
    re.IGNORECASE,
)

_SWITCHER_CTRL_RE = re.compile(
    r"\b(INN3\w*|INNOSWITCH\w*|LNK\d+|TOP\d+|TNY\d+|UCC287\w*)\b",
    re.IGNORECASE,
)

_OHM_VALUE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*(?:[RΩ]|ohm|OHM)?\s*$|^\s*(\d+)[rR](\d+)\s*$"
)


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    refs: list[str] | None = None,
    net: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "dimension": "electrical_safety",
        "message": message,
        "refs": refs or [],
        "net": net,
    }


def _net_base(net_name: str) -> str:
    return (net_name or "").rsplit("/", 1)[-1].strip()


def _infer_rail_voltage(net_name: str) -> float | None:
    """Conservatively infer nominal rail voltage (V) from a net name, or None."""
    base = _net_base(net_name)
    if not base:
        return None
    upper = base.upper()
    if re.search(r"(?:^|[_-])(?:230|240)V(?:AC)?(?:$|[_-])", upper):
        return 230.0
    if re.search(r"(?:^|[_-])(?:380|390|400)V(?:DC)?(?:$|[_-])", upper):
        return 390.0
    # Match explicit forms like +24V, 24V, V24_OUT, 12V, 3V3, V3V3_OUT, V5_IN, 48V
    m = re.search(r"(?:^|[_+\-])(\d+)V(\d+)(?:$|[_-])", upper)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(?:^|[_+\-])V(\d+)V(\d+)(?:$|[_-])", upper)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(?:^|[_+\-])(\d+(?:\.\d+)?)V(?:AC|DC)?(?:$|[_-])", upper)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:^|[_+\-])V(\d+(?:\.\d+)?)(?:$|[_-])", upper)
    if m:
        return float(m.group(1))
    return None


def _is_hv_net(net_name: str) -> bool:
    if _HV_NET_RE.search(net_name or ""):
        return True
    v = _infer_rail_voltage(net_name)
    return v is not None and v >= 100.0


def _is_mains_ac_net(net_name: str) -> bool:
    return bool(_MAINS_AC_NET_RE.search(net_name or ""))


def _parse_ohms(value: str) -> float | None:
    s = (value or "").strip().split()[0] if (value or "").strip() else ""
    if not s:
        return None
    m = _OHM_VALUE_RE.match(s)
    if not m:
        return None
    if m.group(3) is not None:
        return float(f"{m.group(3)}.{m.group(4)}")
    num = float(m.group(1))
    mult = {"": 1.0, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}.get(m.group(2), 1.0)
    return num * mult


def _norm_fn(pin: SnapPin) -> str:
    raw = (pin.pinfunction or "").strip().upper()
    num = str(pin.pin or "").strip().upper()
    if num and raw.endswith("_" + num):
        raw = raw[: -len(num) - 1]
    return re.sub(r"[^A-Z0-9+_-]", "", raw)


def electrical_safety_findings(snap: DesignSnapshot) -> list[dict[str, Any]]:
    """Run all first-principles electrical safety & stress checks on ``snap``."""
    out: list[dict[str, Any]] = []
    part_by_ref: dict[str, SnapPart] = {p.refdes: p for p in snap.parts if p.refdes}
    pins_by_part: dict[str, list[SnapPin]] = defaultdict(list)
    pins_by_net: dict[str, list[SnapPin]] = defaultdict(list)
    net_by_name = {n.name: n for n in snap.nets}

    for net in snap.nets:
        for p in net.pins:
            pins_by_part[p.refdes].append(p)
            pins_by_net[net.name].append(p)

    # ------------------------------------------------------------------
    # 1) Regulator EN/SHDN tied directly to high-voltage VIN rail
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.kind != "ic" or part.dnp:
            continue
        ppins = pins_by_part.get(ref, [])
        if not ppins:
            continue
        vin_pins = [p for p in ppins if _norm_fn(p) in ("IN", "VIN", "VI", "PVIN", "V_IN")]
        en_pins = [p for p in ppins if _norm_fn(p) in ("EN", "ENABLE", "SHDN", "SHDN_N", "CE")]
        if not vin_pins or not en_pins:
            continue
        val_blob = f"{part.value} {part.lib_ref}"
        is_low_en_family = bool(_LOW_EN_LDO_RE.search(val_blob))
        is_regulator = is_low_en_family or bool(_REGULATOR_VALUE_RE.search(val_blob)) or any(
            _norm_fn(p) in ("OUT", "VOUT", "VO", "SW") for p in ppins
        )
        if not is_regulator:
            continue

        for en_p in en_pins:
            matching_vin = [vp for vp in vin_pins if vp.net and vp.net == en_p.net]
            if not matching_vin:
                continue
            vin_p = matching_vin[0]
            volts = _infer_rail_voltage(vin_p.net)
            is_raw_unreg = bool(re.search(r"(?:^|[/_-])(VRAW|RAW|UNREG|HV)", vin_p.net, re.IGNORECASE))
            if is_low_en_family and (volts is None or volts > 6.5 or is_raw_unreg):
                out.append(_finding(
                    "regulator_en_overvoltage",
                    "error",
                    f"{ref} ({part.value or part.lib_ref}) ties EN pin {en_p.pin} directly to "
                    f"VIN pin {vin_p.pin} on net '{vin_p.net}'"
                    f"{f' (~{volts:g} V)' if volts else ''}; this regulator family has a low-voltage "
                    f"clamped EN pin (Abs Max ~7 V / internal Zener) and will fail on power-up ; "
                    f"leave EN floating if internally pulled up or add a clamped divider",
                    refs=[ref],
                    net=vin_p.net,
                ))
            elif (volts is not None and volts >= 10.0) or is_raw_unreg:
                out.append(_finding(
                    "regulator_en_overvoltage",
                    "warning",
                    f"{ref} ({part.value or part.lib_ref}) ties EN pin {en_p.pin} directly to "
                    f"high-voltage VIN pin {vin_p.pin} on net '{vin_p.net}'"
                    f"{f' (~{volts:g} V)' if volts else ''}; verify EN Absolute Maximum rating "
                    f"against maximum input/surge voltage (many LDOs clamp EN at 6-7 V)",
                    refs=[ref],
                    net=vin_p.net,
                ))

    # ------------------------------------------------------------------
    # 2) Undersized SMD passives (R/C) on HV/Mains or RCD/snubber nodes
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.kind not in ("resistor", "capacitor") or part.dnp:
            continue
        fp = part.footprint or ""
        if not fp:
            continue
        ppins = pins_by_part.get(ref, [])
        touched_nets = [p.net for p in ppins if p.net]
        hv_nets = [n for n in touched_nets if _is_hv_net(n)]
        if not hv_nets:
            continue
        hv_net = min(hv_nets)
        if _SMALL_SMD_RE.search(fp):
            out.append(_finding(
                "hv_undersized_passive",
                "error",
                f"{ref} ({part.value or part.kind}, footprint '{fp}') touches high-voltage/mains "
                f"net '{hv_net}'; 0201/0402/0603 packages are rated <=50-75 V with <0.8 mm pad "
                f"creepage ; use series >=1206 HV-rated parts or HV axial/disc THT packages",
                refs=[ref],
                net=hv_net,
            ))
            continue
        snub_nets = [n for n in touched_nets if _SNUBBER_NET_RE.search(n)]
        if part.kind == "resistor" and snub_nets and _UPTO_1206_RE.search(fp):
            out.append(_finding(
                "hv_undersized_passive",
                "warning",
                f"{ref} ({part.value or 'R'}, footprint '{fp}') sits on high-voltage surge/snubber "
                f"net '{min(snub_nets)}'; <=1206 SMD resistors have limited pulse power and "
                f"creepage ; verify steady/overload wattage and consider >=2010 surge-rated SMD "
                f"or 1W-2W flameproof axial THT",
                refs=[ref],
                net=min(snub_nets),
            ))
        elif _MED_SMD_RE.search(fp) and any(_is_mains_ac_net(n) or (_infer_rail_voltage(n) or 0) >= 200.0 for n in hv_nets):
            out.append(_finding(
                "hv_undersized_passive",
                "warning",
                f"{ref} ({part.value or part.kind}, footprint '{fp}') touches mains/HV net "
                f"'{hv_net}'; standard 0805 parts are rated <=150 V working voltage ; verify "
                f"voltage/creepage derating or upgrade to >=1206 HV / THT",
                refs=[ref],
                net=hv_net,
            ))

    # ------------------------------------------------------------------
    # 3) Mains fuse voltage rating (<=250 V on 230-277 VAC mains)
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.kind != "fuse" or part.dnp:
            continue
        ppins = pins_by_part.get(ref, [])
        mains_nets = [p.net for p in ppins if _is_mains_ac_net(p.net)]
        if not mains_nets:
            continue
        blob = f"{part.value} {part.lib_ref} {part.footprint}"
        if _FUSE_250V_RE.search(blob) and not re.search(r"\b(?:300|350|400|500)\s*V", blob, re.IGNORECASE):
            out.append(_finding(
                "mains_fuse_voltage_rating",
                "warning",
                f"Mains fuse {ref} ({part.value or part.lib_ref}) on '{min(mains_nets)}' "
                f"appears rated <=250 VAC; industrial 230 VAC (+15-20%) or 277 VAC lines reach "
                f"265-277 VAC continuous ; upgrade to a >=300 VAC HBC time-lag fuse",
                refs=[ref],
                net=min(mains_nets),
            ))

    # ------------------------------------------------------------------
    # 4) Bidirectional TVS protecting a unipolar IC (no negative supply)
    # ------------------------------------------------------------------
    ic_has_neg_rail: dict[str, bool] = {}
    for ref, part in part_by_ref.items():
        if part.kind != "ic":
            continue
        ppins = pins_by_part.get(ref, [])
        ic_has_neg_rail[ref] = any(_NEG_RAIL_RE.search(p.net) for p in ppins if p.net)

    for ref, part in sorted(part_by_ref.items()):
        if part.dnp or part.kind not in ("diode", "other"):
            continue
        blob = f"{part.value} {part.lib_ref}"
        if not _BIDIR_TVS_RE.search(blob):
            continue
        ppins = pins_by_part.get(ref, [])
        sig_nets = [
            p.net for p in ppins
            if p.net and not (net_by_name.get(p.net) and net_by_name[p.net].is_ground)
        ]
        # Walk up to 1 series resistor hop from TVS signal net to reach protected IC
        protected_ics: set[tuple[str, str]] = set()
        for snet in sig_nets:
            candidate_nets = {snet}
            for peer in pins_by_net.get(snet, []):
                pp = part_by_ref.get(peer.refdes)
                if pp and pp.kind == "resistor" and not pp.dnp:
                    for rp in pins_by_part.get(peer.refdes, []):
                        if rp.net and not (net_by_name.get(rp.net) and net_by_name[rp.net].is_ground):
                            candidate_nets.add(rp.net)
            for cnet in candidate_nets:
                for peer in pins_by_net.get(cnet, []):
                    pp = part_by_ref.get(peer.refdes)
                    if (
                        pp
                        and pp.kind == "ic"
                        and len(pins_by_part.get(peer.refdes, [])) >= 6
                        and not ic_has_neg_rail.get(peer.refdes, False)
                    ):
                        protected_ics.add((peer.refdes, snet))
        if protected_ics:
            ic_refs = sorted({r for r, _ in protected_ics})
            net_ref = min({n for _, n in protected_ics})
            out.append(_finding(
                "bidirectional_tvs_on_unipolar_ic",
                "warning",
                f"Bidirectional TVS {ref} ({part.value}) on net '{net_ref}' protects unipolar "
                f"IC(s) {', '.join(ic_refs)} (no negative supply rail); negative surges will "
                f"clamp below 0 V (-V_C) and exceed IC negative Abs Max ; use a unidirectional "
                f"TVS ('...A' suffix) or rail-steering clamp diodes",
                refs=[ref, *ic_refs],
                net=net_ref,
            ))

    # ------------------------------------------------------------------
    # 5) <=600 V bridge rectifier on 230+ VAC mains (MOV clamp margin)
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.dnp:
            continue
        blob = f"{part.value} {part.lib_ref}"
        is_bridge = ref.upper().startswith(("BR", "DB")) or "BRIDGE" in blob.upper()
        if not is_bridge or not _BRIDGE_600V_RE.search(blob):
            continue
        mains_nets = [p.net for p in pins_by_part.get(ref, []) if _is_mains_ac_net(p.net)]
        if mains_nets:
            out.append(_finding(
                "hv_rectifier_bridge_voltage_margin",
                "warning",
                f"Bridge rectifier {ref} ({part.value or part.lib_ref}) on '{min(mains_nets)}' "
                f"is rated <=600 V V_RRM; a 275-300 VAC mains MOV clamps surges at 710-840 Vpk ; "
                f"upgrade to a >=1000 V bridge (e.g. MB10S / DF10S / DB107)",
                refs=[ref],
                net=min(mains_nets),
            ))

    # ------------------------------------------------------------------
    # 6) Isolated flyback FWD pin sensing High-Side rectified secondary
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.kind != "ic" or part.dnp:
            continue
        ppins = pins_by_part.get(ref, [])
        fwd_pins = [p for p in ppins if _norm_fn(p) in ("FWD", "SR_FWD")]
        if not fwd_pins:
            continue
        for fp in fwd_pins:
            # Reach winding node directly or through 1 series resistor
            candidate_nets = {fp.net}
            for peer in pins_by_net.get(fp.net, []):
                pp = part_by_ref.get(peer.refdes)
                if pp and pp.kind == "resistor" and not pp.dnp:
                    for rp in pins_by_part.get(peer.refdes, []):
                        if rp.net:
                            candidate_nets.add(rp.net)
            for wnet in sorted(candidate_nets):
                peers = pins_by_net.get(wnet, [])
                has_xfmr = any(part_by_ref.get(p.refdes) and part_by_ref[p.refdes].kind == "transformer" for p in peers)
                if not has_xfmr:
                    continue
                for p in peers:
                    dp = part_by_ref.get(p.refdes)
                    if not dp or dp.kind != "diode" or dp.dnp:
                        continue
                    pfn = _norm_fn(p)
                    # Require explicit Anode function so we never guess pin polarity!
                    if pfn in ("A", "ANODE"):
                        # Check diode's Cathode goes to a positive power rail (High-Side rectification)
                        other_pins = [op for op in pins_by_part.get(p.refdes, []) if op.pin != p.pin]
                        if any(
                            op.net
                            and not (net_by_name.get(op.net) and net_by_name[op.net].is_ground)
                            and (
                                (net_by_name.get(op.net) and net_by_name[op.net].is_power)
                                or _infer_rail_voltage(op.net) is not None
                            )
                            for op in other_pins
                        ):
                            out.append(_finding(
                                "flyback_fwd_highside_polarity",
                                "error",
                                f"{ref} ({part.value or part.lib_ref}) FWD pin {fp.pin} senses "
                                f"secondary winding net '{wnet}' on the Anode of high-side rectifier "
                                f"{p.refdes}; during primary ON-time this node swings deeply negative "
                                f"(-V_IN/N), violating FWD negative Abs Max ; rewire secondary to "
                                f"low-side rectification (diode Anode on GND, Cathode on winding/FWD)",
                                refs=[ref, p.refdes],
                                net=wnet,
                            ))

    # ------------------------------------------------------------------
    # 7) Series protection resistor outside Kelvin +VSENSE feedback tap
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.kind != "ic" or part.dnp:
            continue
        ppins = pins_by_part.get(ref, [])
        vout_pins = [p for p in ppins if _norm_fn(p) in ("VOUT", "VO", "OUT", "VOUT_P")]
        sense_pins = [p for p in ppins if _norm_fn(p) in ("+VSENSE", "VSENSE", "VSENSE+", "SENSE+", "VOUT_SENSE")]
        if not vout_pins or not sense_pins:
            continue
        for vp in vout_pins:
            if not vp.net:
                continue
            if not any(sp.net == vp.net for sp in sense_pins):
                continue
            # Check if a series resistor connects vp.net to an external connector
            for peer in pins_by_net.get(vp.net, []):
                rp = part_by_ref.get(peer.refdes)
                if not rp or rp.kind != "resistor" or rp.dnp:
                    continue
                ohms = _parse_ohms(rp.value)
                if ohms is None or ohms < 1.0 or ohms > 500.0:
                    continue
                other_nets = [x.net for x in pins_by_part.get(rp.refdes, []) if x.net and x.net != vp.net]
                for onet in other_nets:
                    conns = [
                        x.refdes for x in pins_by_net.get(onet, [])
                        if part_by_ref.get(x.refdes) and part_by_ref[x.refdes].kind == "connector"
                    ]
                    if conns:
                        out.append(_finding(
                            "sense_outside_series_resistor",
                            "warning",
                            f"{ref} ({part.value}) ties +VSENSE directly to VOUT on '{vp.net}' "
                            f"BEFORE series resistor {rp.refdes} ({rp.value}) feeding connector "
                            f"{', '.join(sorted(set(conns)))}; {rp.refdes} sits outside the Kelvin "
                            f"sense loop and adds R_series/R_load output error ; tap +VSENSE after "
                            f"{rp.refdes} (with a small stability cap/resistor per datasheet)",
                            refs=[ref, rp.refdes, *sorted(set(conns))],
                            net=vp.net,
                        ))

    # ------------------------------------------------------------------
    # 8) Integrated flyback controller VOUT pin headroom (>=22 V vs ~27 V Abs Max)
    # ------------------------------------------------------------------
    for ref, part in sorted(part_by_ref.items()):
        if part.kind != "ic" or part.dnp:
            continue
        ppins = pins_by_part.get(ref, [])
        blob = f"{part.value} {part.lib_ref}"
        is_switcher = bool(_SWITCHER_CTRL_RE.search(blob)) or (
            any(_norm_fn(p) == "FWD" for p in ppins) and any(_norm_fn(p) in ("BPS", "BPP") for p in ppins)
        )
        if not is_switcher:
            continue
        for vp in ppins:
            if _norm_fn(vp) == "VOUT" and vp.net:
                volts = _infer_rail_voltage(vp.net)
                if volts is not None and volts >= 22.0:
                    out.append(_finding(
                        "switcher_vout_headroom",
                        "warning",
                        f"Switcher controller {ref} ({part.value or part.lib_ref}) connects VOUT "
                        f"pin {vp.pin} directly to '{vp.net}' (~{volts:g} V); verify VOUT Abs Max "
                        f"(typically 27 V, below +20% OVP at {volts * 1.2:.1f} V) ; add an external "
                        f"Zener OVP clamp or lower the regulated rail for headroom",
                        refs=[ref],
                        net=vp.net,
                    ))

    return out
