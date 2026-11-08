# SPDX-License-Identifier: MIT
"""Deterministic role inference from the netlist (plan F2, task 2: "motifs").

Only netlist facts are used: reference prefix, footprint library name, value,
pin functions and electrical types, net names, schematic sheet and topology a
few hops out.  The placement is never read, so the same intent comes out of
any placement of the same design (placing from scratch must not see the
expert's answer, P2).

Each claim carries a confidence.  Where the netlist cannot settle a fact (the
rail of a decap is shared by many ICs, a 4.7 uF cap on a rail that is both a
regulator output and an IC supply), the claim says so: the target keeps its
candidates and the confidence stays below ``UNCERTAIN_CONFIDENCE``.  Those
items become questions for the client model (with datasheets) or the user.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..ir import Board, Footprint, Pad, is_ground_net, is_power_net
from ..toolchain import sha256_file
from . import model
from .model import claim, natural_key

MOTIF = "motif"

# --------------------------------------------------------------------------- facts
POWER_FN = re.compile(r"^(VCC|VDD|VAA|AVDD|DVDD|PVDD|VDDA|VCCA|VCCB|V_\{CC[AB]?\}|VIN|VI|VBAT|VSYS|VBUS|VDDH|VDDIO|"
                      r"VPP|VS)\d*$")
GROUND_FN = re.compile(r"^(GND|VSS|AVSS|PVSS|DVSS|AGND|DGND|PGND|GNDA|GNDD|VSS_PA|EP|PAD)\w*$")
SW_FN = re.compile(r"^(SW|LX|VSW|SWITCH|PH)\d*$")
CONTROL_FN = re.compile(r"^(FB|FB\+|FB-|COMP|VC|SS|SS/TR|VSET|VSET\d|RT|ILIM|TRK|NR|ADJ)\d*$")
REF_FN = re.compile(r"(REXT|RSET|IREF|ISET|RREF|RBIAS|RCAL|REF\+?|VREF|CREF\+?|IOUT\d*)$")
OUT_FN = re.compile(r"^(VO|VOUT|OUT|S|VOUT\d|SW_OUT)$")
IN_FN = re.compile(r"^(VI|VIN|IN|D|PVDD|VBUS)$")
ESD_VALUE = re.compile(r"(TVS|ESD|TPD\d|PESD|USBLC|SP0\d|RCLAMP|IP42|PRTR|SMAJ|SMBJ|ZENER)", re.IGNORECASE)
LDO_VALUE = re.compile(r"(^78\d\d|^79\d\d|LM317|LM1117|AMS1117|LDO|AP21\d\d|MCP17\d\d|LP59\d\d|TLV7\d|XC62|RT9\d\d\d|"
                       r"NCP1117|LD1117)", re.IGNORECASE)
SOCKET_VALUE = re.compile(r"(SIMM|DIMM|SIM\d|SIM72|SODIMM)", re.IGNORECASE)
POLARIZED_LIB = re.compile(r"(^CP_|CP_Elec|CP_EIA|Tantal|Elec_|CP_Axial|CP_Radial)", re.IGNORECASE)

GROUND_NAMES = frozenset({"GND", "VSS", "AVSS", "PVSS", "PVSS1", "PVSS2", "DVSS", "AGND", "DGND", "PGND", "GNDA",
                          "VSS_PA"})
# a supply connector's nets are rails
POWER_CONNECTOR = re.compile(r"(POWER|PWR|SUPPLY|^VIN$|^DC|BATT?)", re.IGNORECASE)

CONNECTOR_PREFIXES = ("J", "P", "CN", "CON", "BUS", "USB", "X")
# anchored: "DSUB-9_..._MountingHolesOffset" is a connector, not a hole
MECH_LIB = re.compile(r"^(MountingHole|Plain_Hole|Hole_|Logo|Fiducial|Creative_Commons|.*_Logo)", re.IGNORECASE)


def fn(pad: Pad) -> str:
    """Pin function, upper case, without the ``_<pad number>`` suffix some libraries append."""
    f = (pad.function or "").strip().upper()
    num = str(pad.number).upper()
    if num and f.endswith("_" + num):
        f = f[: -len(num) - 1]
    return f


def pin_type(pad: Pad) -> str:
    return (pad.pin_type or "").split("+")[0]


def parse_farads(value: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([pnuµμm]?)\s*F?", value.replace("µ", "u"))
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1e-6 if num < 1000 else 1e-12}
    if m.group(2) == "" and not re.search(r"[pnuµμm]", value, re.IGNORECASE):
        return num * 1e-6 if num < 1 else None  # "0.1" is microfarads by convention; a bare integer is unknown
    return num * unit.get(m.group(2), 1.0)


@dataclass
class Facts:
    board: Board
    fp: dict[str, Footprint] = field(default_factory=dict)
    kind: dict[str, str] = field(default_factory=dict)
    on_net: dict[str, list[tuple[str, Pad]]] = field(default_factory=lambda: defaultdict(list))
    rails: set[str] = field(default_factory=set)
    grounds: set[str] = field(default_factory=set)
    sheets: int = 1

    def parts_on(self, net: str, kinds: tuple[str, ...] | None = None, exclude: str | None = None) -> list[str]:
        seen = []
        for ref, _pad in self.on_net.get(net, ()):
            if ref != exclude and ref not in seen and (kinds is None or self.kind[ref] in kinds):
                seen.append(ref)
        return seen

    def is_rail(self, net: str | None) -> bool:
        return bool(net) and (net in self.rails or net in self.grounds)

    def signal_nets(self, ref: str) -> list[str]:
        return [n for n in dict.fromkeys(p.net for p in self.fp[ref].pads if p.net) if not self.is_rail(n)]


def _family(fp: Footprint) -> str:
    lib = fp.lib_id.split(":")[-1]
    p, v = fp.prefix, fp.value
    if not fp.pads or MECH_LIB.search(lib) or p in ("H", "MH", "LOGO", "FID", "HOLE"):
        return "mechanical"
    if "TestPoint" in lib or p == "TP":
        return "test_point"
    if "Jumper" in lib or p in ("JP", "W", "SJ"):
        return "jumper"
    if "Crystal" in lib or p in ("Y", "XTAL") or (p == "X" and "Crystal" in lib):
        return "crystal"
    if "Oscillator" in lib or p == "OSC":
        return "oscillator"
    if p in ("C", "CP", "CV"):
        return "cap"
    if p in ("RV", "POT", "VR"):
        return "pot"
    if p in ("R", "RN", "RR"):
        return "res"
    if p in ("L", "FB"):
        return "ind"
    if p in ("D", "LED", "TVS", "Z", "ZD"):
        return "led" if ("LED" in lib.upper() or "LED" in v.upper() or p == "LED") else "diode"
    if p in ("Q", "T"):
        return "transistor"
    if p in ("SW", "S", "BTN", "K"):
        return "switch"
    if "Valve" in lib or "Tube" in lib:
        return "transistor"
    if p in ("U", "IC"):
        return "connector" if SOCKET_VALUE.search(v) or SOCKET_VALUE.search(lib) else "ic"
    if p in CONNECTOR_PREFIXES or "Conn" in lib or "Connector" in lib:
        return "connector"
    return "unknown"


def facts(board: Board) -> Facts:
    f = Facts(board)
    for fp in board.footprints:
        f.fp[fp.ref] = fp
        f.kind[fp.ref] = _family(fp)
        for pad in fp.pads:
            if pad.net:
                f.on_net[pad.net].append((fp.ref, pad))
    f.sheets = len({fp.sheet for fp in board.footprints if fp.sheet})
    for net, pads in f.on_net.items():
        if is_ground_net(net) or any(f.kind[r] == "ic" and fn(p) in GROUND_NAMES for r, p in pads):
            f.grounds.add(net)
        elif is_power_net(net) or any(f.kind[r] in ("ic", "connector") and (
                pin_type(p) in ("power_in", "power_out") or POWER_FN.match(fn(p))) for r, p in pads) or any(
                f.kind[r] == "connector" and POWER_CONNECTOR.search(f.fp[r].value) for r, _p in pads):
            f.rails.add(net)
    return f


# --------------------------------------------------------------------------- regulators
@dataclass
class Regulator:
    ref: str
    role: str
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    sw: set[str] = field(default_factory=set)
    control: set[str] = field(default_factory=set)


def regulators(f: Facts) -> dict[str, Regulator]:
    out: dict[str, Regulator] = {}
    for ref, kind in f.kind.items():
        if kind != "ic":
            continue
        fp = f.fp[ref]
        names = {fn(p): p for p in fp.pads}
        sw = {p.net for p in fp.pads if p.net and SW_FN.match(fn(p))
              and any(f.kind[r] == "ind" for r in f.parts_on(p.net, exclude=ref))}
        if sw:
            reg = Regulator(ref, "converter_ic", sw=sw)
        elif ("ON" in names or "EN" in names) and ({"D", "S"} <= set(names) or "CAP" in names):
            reg = Regulator(ref, "power_switch")
        elif LDO_VALUE.search(fp.value) or ({"VI", "VO"} <= set(names)) or (
                {"VIN", "VOUT"} <= set(names) and len(fp.pads) <= 6):
            reg = Regulator(ref, "linear_regulator")
        else:
            continue
        for p in fp.pads:
            if not p.net or p.net in f.grounds:
                continue
            name, typ = fn(p), pin_type(p)
            if CONTROL_FN.match(name):
                reg.control.add(p.net)
            elif typ == "power_out" or OUT_FN.match(name):
                if p.net in f.rails or reg.role != "converter_ic":
                    reg.outputs.add(p.net)
            elif IN_FN.match(name) or (typ == "power_in" and reg.role != "converter_ic"):
                reg.inputs.add(p.net)  # a PMIC's VDDIO or VBAT is not the converter's input
        for net in sw:  # buck: SW -> L -> out;  boost: in -> L -> SW -> D -> out
            for r in f.parts_on(net, ("ind", "diode"), exclude=ref):
                for p in f.fp[r].pads:
                    if p.net and p.net != net and p.net not in f.grounds:
                        (reg.inputs if p.net in reg.inputs else reg.outputs).add(p.net)
        out[ref] = reg
    return out


# --------------------------------------------------------------------------- inference
class Inference:
    def __init__(self, board: Board):
        self.f = facts(board)
        self.regs = regulators(self.f)
        self.parts: dict[str, dict] = {}
        self.questions: list[dict] = []
        self.sw_nets = {n for r in self.regs.values() for n in r.sw}
        self.control_nets = {n for r in self.regs.values() for n in r.control}
        for reg in self.regs.values():  # a converter's input and output are rails whatever their names (pic: VPP)
            self.f.rails |= {n for n in reg.inputs | reg.outputs if n not in self.f.grounds}
        # every part that drives a rail from a power output pin (regulators, and ICs with an internal LDO)
        self.sources: dict[str, list[str]] = defaultdict(list)
        for ref, kind in self.f.kind.items():
            if kind != "ic":
                continue
            outs = set(self.regs[ref].outputs) if ref in self.regs else set()
            outs |= {p.net for p in self.f.fp[ref].pads if p.net and pin_type(p) == "power_out"}
            for net in sorted(outs, key=natural_key):
                self.sources[net].append(ref)

    # ---- helpers
    def set(self, ref: str, role: str, conf: float, why: str, target: dict | None = None,
            tconf: float | None = None, twhy: str | None = None) -> None:
        part = {"role": claim(role, MOTIF, conf, ref=why)}
        if target is not None:
            part["target"] = claim(target, MOTIF, conf if tconf is None else tconf, ref=twhy or why)
        self.parts[ref] = part

    def reg_of_net(self, net: str, which: str) -> list[str]:
        return sorted((r.ref for r in self.regs.values() if net in getattr(r, which)), key=natural_key)

    def crystal_nets(self) -> dict[str, str]:
        out = {}
        for ref, kind in self.f.kind.items():
            if kind == "crystal":
                for p in self.f.fp[ref].pads:
                    if p.net and p.net not in self.f.grounds:
                        out[p.net] = ref
        return out

    def power_pins(self, ref: str, net: str) -> list[str]:
        return sorted({str(p.number) for p in self.f.fp[ref].pads if p.net == net}, key=natural_key)

    def supply_pins(self, ref: str, net: str) -> int:
        """Pins of ``ref`` that draw supply from ``net`` (power-input type or a supply pin name)."""
        return sum(1 for p in self.f.fp[ref].pads if p.net == net and pin_type(p) != "power_out"
                   and (pin_type(p) == "power_in" or POWER_FN.match(fn(p))))

    def loads(self, net: str) -> list[str]:
        """ICs that draw supply from ``net`` (ESD arrays only clamp it)."""
        return [r for r in self.f.parts_on(net, ("ic",)) if self.supply_pins(r, net)
                and not ESD_VALUE.search(self.f.fp[r].value)]

    def local(self, refs: list[str], near: str) -> list[str]:
        """On a multi-sheet design, the refs on ``near``'s schematic sheet."""
        if self.f.sheets <= 1:
            return list(refs)
        return [r for r in refs if self.f.fp[r].sheet == self.f.fp[near].sheet]

    # ---- passes
    def run(self) -> None:
        crystal_nets = self.crystal_nets()
        for ref in sorted(self.f.fp, key=natural_key):
            kind = self.f.kind[ref]
            getattr(self, f"_{kind}", self._other)(ref, crystal_nets)

    def _other(self, ref: str, _c) -> None:
        self.set(ref, "unknown", 0.0, "no rule matched")

    def _mechanical(self, ref: str, _c) -> None:
        self.set(ref, "mechanical", 0.95, "hole, logo or padless footprint")

    def _test_point(self, ref: str, _c) -> None:
        self.set(ref, "test_point", 0.95, "test point footprint")

    def _jumper(self, ref: str, _c) -> None:
        self.set(ref, "jumper", 0.9, "jumper footprint or prefix")

    def _switch(self, ref: str, _c) -> None:
        self.set(ref, "switch", 0.9, "switch prefix")

    def _transistor(self, ref: str, _c) -> None:
        self.set(ref, "transistor", 0.9, "discrete active device (prefix Q/T or valve)")

    def _oscillator(self, ref: str, _c) -> None:
        self.set(ref, "oscillator", 0.85, "oscillator footprint")

    def _connector(self, ref: str, _c) -> None:
        fp = self.f.fp[ref]
        lib = fp.lib_id.split(":")[-1]
        nets = {p.net for p in fp.pads if p.net}
        if "PinHeader_1x01" in lib and len(fp.pads) == 1:
            self.set(ref, "test_point", 0.7, "single-pin header (probe point)")
        else:
            self.set(ref, "connector", 0.9 if nets else 0.6, "connector prefix or footprint")

    def _ic(self, ref: str, _c) -> None:
        fp = self.f.fp[ref]
        if ref in self.regs:
            reg = self.regs[ref]
            self.set(ref, reg.role, 0.9 if reg.role == "converter_ic" else 0.8,
                     {"converter_ic": "switch-node pin to an inductor", "power_switch": "ON/EN with D/S or CAP pins",
                      "linear_regulator": "VI/VO pins or regulator part number"}[reg.role])
            return
        if ESD_VALUE.search(fp.value) or ESD_VALUE.search(fp.description):
            conns = Counter(r for p in fp.pads if p.net and p.net not in self.f.grounds
                            for r in self.f.parts_on(p.net, ("connector",)))
            target = self._best(conns)
            self.set(ref, "esd", 0.9, "ESD part number", target, 0.9 if target["ref"] else 0.4, "connector on its nets")
            return
        self.set(ref, "ic", 0.9, "active multi-pin part")

    @staticmethod
    def _best(counts: Counter) -> dict:
        if not counts:
            return {"ref": None, "candidates": []}
        top = max(counts.values())
        best = sorted((r for r, c in counts.items() if c == top), key=natural_key)
        return {"ref": best[0]} if len(best) == 1 else {"ref": None, "candidates": best}

    def _crystal(self, ref: str, _c) -> None:
        ics = Counter()
        for p in self.f.fp[ref].pads:
            if not p.net or p.net in self.f.grounds:
                continue
            for r in self.f.parts_on(p.net, ("ic", "transistor"), exclude=ref):
                ics[r] += 2
            for r in self.f.parts_on(p.net, ("res",), exclude=ref):  # Pierce oscillator through a resistor
                for q in self.f.fp[r].pads:
                    if q.net and q.net != p.net:
                        for s in self.f.parts_on(q.net, ("ic",)):
                            ics[s] += 1
        target = self._best(ics)
        self.set(ref, "crystal", 0.95, "crystal footprint", target, 0.9 if target["ref"] else 0.4,
                 "IC on the crystal nets")

    def _led(self, ref: str, _c) -> None:
        self.set(ref, "led", 0.95, "LED footprint or value")

    def _diode(self, ref: str, crystal_nets) -> None:
        fp = self.f.fp[ref]
        nets = [p.net for p in fp.pads if p.net]
        if any(n in self.sw_nets for n in nets):
            reg = next(r.ref for r in self.regs.values() if set(nets) & r.sw)
            self.set(ref, "converter_diode", 0.9, "diode on a converter switch node", {"ref": reg})
            return
        ground = any(n in self.f.grounds for n in nets)
        if ground and (ESD_VALUE.search(fp.value) or ESD_VALUE.search(fp.description) or re.fullmatch(r"\d+V\d*", fp.value)):
            conns = Counter()
            for n in nets:
                if n in self.f.grounds:
                    continue
                for r in self.f.parts_on(n, ("connector",)):
                    conns[r] += 1
                for j in self.f.parts_on(n, ("jumper",)):  # through a jumper to its connector
                    for q in self.f.fp[j].pads:
                        if q.net and q.net != n:
                            for r in self.f.parts_on(q.net, ("connector",)):
                                conns[r] += 1
            target = self._best(conns)
            self.set(ref, "esd", 0.9, "TVS/ESD diode to ground", target, 0.9 if target["ref"] else 0.4,
                     "connector on its signal net")
            return
        self.set(ref, "diode", 0.8, "discrete diode")

    def _pot(self, ref: str, crystal_nets) -> None:
        if self._converter_control(ref):
            return
        self.set(ref, "signal_passive", 0.6, "potentiometer")

    def _converter_control(self, ref: str) -> bool:
        """The part sits on a converter control net, directly or through one resistor or pot."""
        nets = {p.net for p in self.f.fp[ref].pads if p.net}
        hit = nets & self.control_nets
        if not hit:
            for n in nets - self.f.grounds - self.f.rails:
                for r in self.f.parts_on(n, ("res", "pot", "cap"), exclude=ref):
                    hit |= {p.net for p in self.f.fp[r].pads if p.net} & self.control_nets
        if not hit:
            return False
        reg = next(r.ref for r in self.regs.values() if hit & r.control)
        self.set(ref, "converter_control", 0.85, "on a converter feedback/compensation net", {"ref": reg})
        return True

    def _ind(self, ref: str, _c) -> None:
        nets = [p.net for p in self.f.fp[ref].pads if p.net]
        if any(n in self.sw_nets for n in nets):
            reg = next(r.ref for r in self.regs.values() if set(nets) & r.sw)
            self.set(ref, "converter_inductor", 0.95, "inductor on a converter switch node", {"ref": reg})
        elif len(set(nets)) == 2 and all(n in self.f.rails for n in nets):
            self.set(ref, "rail_filter", 0.85, "inductor between two rails")
        else:
            self.set(ref, "signal_passive", 0.6, "inductor in a signal path")

    # ---- resistors
    def _res(self, ref: str, crystal_nets) -> None:
        fp = self.f.fp[ref]
        nets = [p.net for p in fp.pads if p.net]
        if self._converter_control(ref):
            return
        xtal = [crystal_nets[n] for n in nets if n in crystal_nets]
        if xtal:
            self.set(ref, "crystal_aux", 0.85, "resistor on a crystal net", {"ref": xtal[0]})
            return
        leds = [r for n in nets if not self.f.is_rail(n) for r in self.f.parts_on(n, ("led",))]
        if leds:
            self.set(ref, "led_resistor", 0.9, "resistor in series with an LED", {"ref": leds[0]})
            return
        if len(fp.pads) > 3:  # resistor array: common pin on a rail
            rail = Counter(n for n in nets if n in self.f.rails).most_common(1)
            gnd = Counter(n for n in nets if n in self.f.grounds).most_common(1)
            role = "pull_up" if rail else "pull_down" if gnd else "series_resistor"
            self.set(ref, role, 0.75, "resistor array with a common pin")
            return
        signal = [n for n in nets if not self.f.is_rail(n)]
        rails = [n for n in nets if n in self.f.rails]
        grounds = [n for n in nets if n in self.f.grounds]
        ref_pins = [(r, fn(p)) for n in signal for r, p in self.f.on_net[n]
                    if self.f.kind[r] == "ic" and REF_FN.search(fn(p))]
        if ref_pins and (rails or grounds or len(signal) == 1):
            self.set(ref, "bias_resistor", 0.8, f"on reference pin {ref_pins[0][1]}", {"ref": ref_pins[0][0]})
            return
        divider = self._divider(ref, signal)
        if divider:
            self.set(ref, "divider_resistor", 0.7, f"divider tap {divider}")
            return
        transistor_pins = [(r, fn(p)) for n in signal for r, p in self.f.on_net[n] if self.f.kind[r] == "transistor"]
        if len(signal) == 1 and (rails or grounds):
            base = any(name.startswith(("B", "G")) for _r, name in transistor_pins)
            if transistor_pins and not base:
                self.set(ref, "signal_passive", 0.6, "emitter/collector/cathode resistor")
            elif grounds and self._termination(ref, signal[0]):
                self.set(ref, "termination", 0.75, "low-value resistor from a connector net to ground")
            else:
                self.set(ref, "pull_up" if rails else "pull_down", 0.75, "resistor from a signal net to a rail")
            return
        if len(signal) == 2:
            ics = [{r for r, _p in self.f.on_net[n] if self.f.kind[r] == "ic"} for n in signal]
            if transistor_pins and not any(ics):
                base = any(name.startswith(("B", "G")) for _r, name in transistor_pins)
                to_conn = any(self.f.parts_on(n, ("connector",)) for n in signal)  # back-termination to a jack
                self.set(ref, "series_resistor" if base or to_conn else "signal_passive", 0.6,
                         "resistor at a transistor" + (" toward a connector" if to_conn else ""))
            elif ics[0] and ics[0] == ics[1] and len(ics[0]) == 1:
                self.set(ref, "bias_resistor", 0.6, "resistor between two pins of one IC", {"ref": next(iter(ics[0]))})
            else:
                self.set(ref, "series_resistor", 0.75, "resistor between two signal nets")
            return
        if len(set(rails)) == 2 or (rails and grounds):
            if rails and grounds:
                self.set(ref, "signal_passive", 0.5, "resistor across a rail (load or bleeder)")
            else:
                self.set(ref, "rail_filter", 0.7, "resistor between two rails")
            return
        self.set(ref, "unknown", 0.0, "no resistor rule matched")

    def _termination(self, ref: str, net: str) -> bool:
        ohms = _parse_ohms(self.f.fp[ref].value)
        return ohms is not None and ohms <= 150 and bool(self.f.parts_on(net, ("connector",)))

    def _divider(self, ref: str, signal: list[str]) -> str | None:
        for n in signal:
            res = self.f.parts_on(n, ("res",))
            others = [r for r in self.f.parts_on(n) if self.f.kind[r] not in ("res",)]
            if len(res) != 2 or any(self.f.kind[r] == "transistor" for r in others):
                continue
            if any(self._termination(r, n) and any(p.net in self.f.grounds for p in self.f.fp[r].pads) for r in res):
                continue  # a 75-ohm load at a jack with its series resistor is a termination, not a divider
            ends = []
            for r in res:
                ends += [p.net for p in self.f.fp[r].pads if p.net and p.net != n]
            if any(e in self.f.grounds for e in ends) and len(set(ends)) == 2 and others:
                return n
        return None

    # ---- capacitors
    def _cap(self, ref: str, crystal_nets) -> None:
        fp = self.f.fp[ref]
        nets = [p.net for p in fp.pads if p.net]
        if len(set(nets)) < 2:
            self.set(ref, "unknown", 0.0, "capacitor with fewer than two nets")
            return
        if self._converter_control(ref):
            return
        xtal = [crystal_nets[n] for n in nets if n in crystal_nets]
        grounds = [n for n in nets if n in self.f.grounds]
        signal = [n for n in nets if not self.f.is_rail(n)]
        if xtal and grounds:
            self.set(ref, "crystal_load_cap", 0.95, "capacitor from a crystal net to ground", {"ref": xtal[0]})
            return
        if len(signal) == 2:
            self.set(ref, "coupling_cap", 0.75, "capacitor in series between two signal nets")
            return
        if len(signal) == 1:
            net = signal[0]
            dec = [(r, p) for r, p in self.f.on_net[net] if self.f.kind[r] == "ic" and fn(p).startswith("DEC")]
            if dec:
                r = dec[0][0]
                self.set(ref, "decap", 0.9, f"on decoupling pin {fn(dec[0][1])}",
                         {"ref": r, "net": net, "pins": self.power_pins(r, net)})
                return
            active = [r for r in self.f.parts_on(net, exclude=ref) if self.f.kind[r] not in ("cap", "res", "pot")]
            ics = [r for r in active if self.f.kind[r] == "ic"]
            if len(active) == 1 and ics:
                pin = next(fn(p) for p in self.f.fp[ics[0]].pads if p.net == net)
                self.set(ref, "pin_cap", 0.8, f"capacitor on pin {pin}", {"ref": ics[0], "pins": self.power_pins(ics[0], net)})
            else:
                self.set(ref, "signal_passive", 0.6, "capacitor on a signal node")
            return
        rail = next((n for n in nets if n in self.f.rails), None)
        if rail is None or not grounds:
            self.set(ref, "signal_passive", 0.5, "capacitor between two rails")
            return
        self._rail_cap(ref, rail)

    def _rail_cap(self, ref: str, rail: str) -> None:
        fp = self.f.fp[ref]
        farads = parse_farads(fp.value)
        polar = bool(POLARIZED_LIB.search(fp.lib_id.split(":")[-1]))
        srcs, ins = self.sources.get(rail, []), self.reg_of_net(rail, "inputs")
        loads = self.loads(rail)
        if self._small(ref):
            self._decap(ref, rail, loads, srcs, ins)
            return
        if not loads and not srcs and not ins:
            self.set(ref, "bulk_cap", 0.8, "large capacitor on a rail with no IC load", {"net": rail})
            return
        big = [r for r in self.f.parts_on(rail, ("cap",))
               if not self._small(r) and any(p.net in self.f.grounds for p in self.f.fp[r].pads)]
        if len(big) > 3 or polar and not (srcs or ins):
            self.set(ref, "bulk_cap", 0.8, "one of many large capacitors on the rail" if len(big) > 3
                     else "polarized capacitor on a rail", {"net": rail})
            return
        other_loads = [r for r in loads if r not in srcs and r not in self.regs]
        if self.f.sheets > 1 and not polar and (farads is None or farads < 10e-6):
            near_loads, near_regs = self.local(other_loads, ref), self.local(srcs + ins, ref)
            if near_loads and not near_regs:  # drawn next to its load, away from the regulator
                self._decap(ref, rail, near_loads, [], [], conf=0.6)
                return
        if srcs or ins:
            shared = [r for r in srcs if r in ins]
            if shared or not srcs:
                role, regs = "regulator_input_cap", shared or ins
            else:
                role, regs = "regulator_output_cap", srcs
            conf = 0.75 if polar or not other_loads else 0.5
            if shared:  # one regulator's input and output pins share the rail: which pin it serves is not known
                conf = 0.5
            target = {"ref": regs[0], "net": rail} if len(regs) == 1 else {"ref": None, "net": rail, "candidates": regs}
            self.set(ref, role, conf, "large capacitor on a regulator rail", target, conf if len(regs) == 1 else 0.4)
            return
        if farads is not None and farads >= 10e-6:
            self.set(ref, "bulk_cap", 0.65, "10 uF or more on a rail", {"net": rail})
            return
        self._decap(ref, rail, loads, srcs, ins, conf=0.7)

    def _small(self, ref: str) -> bool:
        fp = self.f.fp[ref]
        farads = parse_farads(fp.value)
        return farads is not None and farads <= 1.0e-6 and not POLARIZED_LIB.search(fp.lib_id.split(":")[-1])

    def _decap(self, ref: str, rail: str, loads: list[str], srcs: list[str], ins: list[str], conf: float = 0.9) -> None:
        group = self._connector_group(ref, rail)
        if group:
            self.set(ref, "decap", conf, "one of a set of identical capacitors, one per identical connector",
                     {"ref": None, "net": rail, "candidates": group}, 0.4, "which connector is a placement choice")
            return
        near, near_srcs = self.local(loads, ref), self.local(srcs, ref)
        if self.f.sheets > 1 and not near and near_srcs:  # drawn next to the regulator that drives the rail
            self.set(ref, "regulator_output_cap", 0.7, "capacitor on the regulator's sheet",
                     {"ref": near_srcs[0], "net": rail})
            return
        cands = near or loads
        if not cands:
            if ins or srcs:
                role = "regulator_input_cap" if ins else "regulator_output_cap"
                self.set(ref, role, 0.7, "small capacitor on a rail that only a regulator touches",
                         {"ref": (ins or srcs)[0], "net": rail})
                return
            conns = self.f.parts_on(rail, ("connector",))
            target = {"ref": conns[0], "net": rail} if len(conns) == 1 else {"ref": None, "net": rail, "candidates": conns}
            self.set(ref, "decap", conf * 0.8, "small capacitor on a rail with no IC load", target,
                     0.7 if len(conns) == 1 else 0.3)
            return
        if all(r in self.regs for r in cands) and len(cands) == 1 and rail in self.regs[cands[0]].inputs:
            self.set(ref, "regulator_input_cap", 0.75, "small capacitor on the regulator's input",
                     {"ref": cands[0], "net": rail})
            return
        pins = sorted(((self.supply_pins(r, rail), r) for r in cands), key=lambda t: (-t[0], natural_key(t[1])))
        sheet = " of the sheet" if self.f.sheets > 1 and near else ""
        if len(cands) == 1:
            r = cands[0]
            self.set(ref, "decap", conf, "small capacitor on an IC supply rail",
                     {"ref": r, "net": rail, "pins": self.power_pins(r, rail)}, conf, "only IC on that rail" + sheet)
        elif pins[0][0] >= 4 and pins[0][0] >= 3 * pins[1][0] and self._decap_count(ref, rail) <= pins[0][0]:
            r = pins[0][1]
            self.set(ref, "decap", conf, "small capacitor on an IC supply rail",
                     {"ref": r, "net": rail, "pins": self.power_pins(r, rail)}, 0.65,
                     f"{r} has {pins[0][0]} of the supply pins on that rail{sheet}")
        else:
            self.set(ref, "decap", conf, "small capacitor on an IC supply rail",
                     {"ref": None, "net": rail, "candidates": sorted(cands, key=natural_key)}, 0.3,
                     "several ICs share the rail" + sheet + "; the netlist does not say which one")

    def _decap_count(self, ref: str, rail: str) -> int:
        """Small rail-to-ground capacitors on ``rail`` in ``ref``'s scope (its sheet on a multi-sheet design)."""
        rail_caps = [r for r in self.f.parts_on(rail, ("cap",)) if any(p.net in self.f.grounds for p in self.f.fp[r].pads)]
        big = [r for r in rail_caps if not self._small(r)]
        medium = [r for r in big if (parse_farads(self.f.fp[r].value) or 1.0) < 10e-6
                  and not POLARIZED_LIB.search(self.f.fp[r].lib_id.split(":")[-1])]
        caps = [r for r in rail_caps if self._small(r)] + (medium if len(big) <= 3 else [])  # medium ones are decaps too
        return len(self.local(caps, ref))

    def _connector_group(self, ref: str, rail: str) -> list[str] | None:
        fp = self.f.fp[ref]
        twins = [r for r in self.f.parts_on(rail, ("cap",)) if self.f.fp[r].value == fp.value
                 and self.f.fp[r].lib_id == fp.lib_id and any(p.net in self.f.grounds for p in self.f.fp[r].pads)]
        conns = self.f.parts_on(rail, ("connector",))
        by_lib = Counter(self.f.fp[c].lib_id for c in conns)
        for lib, n in by_lib.items():
            if n >= 2 and n == len(twins):
                return sorted((c for c in conns if self.f.fp[c].lib_id == lib), key=natural_key)
        return None

    # ---- nets, pairs, blocks
    def nets(self) -> dict:
        out = {}
        xtal = self.crystal_nets()
        for net in sorted(self.f.on_net, key=natural_key):
            rail = "ground" if net in self.f.grounds else "power" if net in self.f.rails else "signal"
            if net in self.sw_nets:
                cls, conf, why = "SW_NODE", 0.95, "converter switch node"
            elif net in self.control_nets or net in xtal:
                cls, conf, why = "SENSITIVE_ANALOG", 0.85, "converter control or crystal net"
            elif any(net in (r.inputs | r.outputs) for r in self.regs.values() if r.role == "converter_ic"):
                cls, conf, why = "POWER_HIGH_CURRENT", 0.6, "converter input or output (current not known)"
            else:
                cls, conf, why = "GENERAL", 0.5, "no rule"
            out[net] = {"class": claim(cls, MOTIF, conf, ref=why), "rail": claim(rail, MOTIF, 0.85, ref="net facts")}
        return out

    def pairs(self, board: Board) -> list[dict]:
        out = []
        for dp in board.diff_pairs:
            pins = [fn(p) for n in (dp.positive, dp.negative) for _r, p in self.f.on_net.get(n, ())]
            libs = [self.f.fp[r].lib_id.upper() for n in (dp.positive, dp.negative) for r, _p in self.f.on_net.get(n, ())]
            usb = any(re.search(r"(D[+-]|DP|DM|USB)", p) for p in pins) or any("USB" in lib for lib in libs)
            conf = 0.95 if usb else 0.5
            out.append(claim({"positive": dp.positive, "negative": dp.negative, "kind": "usb" if usb else "other"},
                             MOTIF, conf, ref="net name pattern" + (" + USB pins" if usb else "")))
        return out

    def blocks(self) -> list[dict]:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        anchors: dict[tuple[str, str], str | None] = {}
        for ref, part in self.parts.items():
            role = part["role"]["value"]
            tgt = part.get("target", {}).get("value") or {}
            t = tgt.get("ref")
            kind = {"decap": "decap_group", "crystal_load_cap": "crystal", "crystal_aux": "crystal",
                    "converter_inductor": "converter", "converter_diode": "converter", "converter_control": "converter",
                    "esd": "esd_port", "led_resistor": "led"}.get(role)
            if role in ("regulator_input_cap", "regulator_output_cap") and t in self.regs:
                kind = "converter" if self.regs[t].role == "converter_ic" else "linear_regulator"
            if not kind or not t:
                continue
            groups[(kind, t)].append(ref)
            anchors[(kind, t)] = t
        out = []
        for (kind, t), members in sorted(groups.items(), key=lambda kv: (kv[0][0], natural_key(kv[0][1]))):
            out.append(claim({"name": f"{kind}:{t}", "kind": kind, "anchor": anchors[(kind, t)],
                              "members": sorted({t, *members}, key=natural_key)}, MOTIF, 0.8, ref="grouped by target"))
        return out


def _parse_ohms(value: str) -> float | None:
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*([RkKmM]?)(\d*)", value)
    if not m:
        return None
    num = float(m.group(1).replace(",", ".") + (("." + m.group(3)) if m.group(3) and m.group(2) else ""))
    return num * {"": 1, "R": 1, "k": 1e3, "K": 1e3, "m": 1e-3, "M": 1e6}[m.group(2)]


def draft(board: Board) -> dict:
    """Intent drafted from the netlist alone, with the questions it could not settle."""
    inf = Inference(board)
    inf.run()
    intent = {
        "schema": model.SCHEMA, "schema_version": model.SCHEMA_VERSION,
        "board": {"name": board.name, "sha256": board.source.get("sha256") or (
                      sha256_file(board.source["path"]) if board.source.get("path") else "0" * 64),
                  "path": board.source.get("path", "")},
        "policies": {"side_policy": claim("expert", "board", 0.9, ref="default: keep every part's side (P2)"),
                     "stackup": claim(board.stackup.policy, "board", 0.9, ref="stackup policy from the board (S0)")},
        "parts": inf.parts, "nets": inf.nets(), "diff_pairs": inf.pairs(board), "blocks": inf.blocks(), "power_w": {},
    }
    intent["questions"] = questions(intent)
    return intent


def questions(intent: dict) -> list[dict]:
    """Uncertain claims, critical ones first: what the client model (with datasheets) or the user should settle."""
    out = []
    for ref, part in intent["parts"].items():
        role = part["role"]["value"]
        target = part.get("target")
        tv = target["value"] if target else {}
        crit = model.is_critical(role, model.role_of(intent, tv.get("ref") or "") if tv else None)
        if model.is_uncertain(part["role"]):
            out.append({"ref": ref, "about": "role", "draft": role, "critical": crit,
                        "why": part["role"]["source"].get("ref", "")})
        elif target and (model.is_uncertain(target) or tv.get("ref") is None):
            out.append({"ref": ref, "about": "target", "draft": role, "critical": crit,
                        "candidates": tv.get("candidates", []), "net": tv.get("net"),
                        "why": target["source"].get("ref", "")})
    for pair in intent["diff_pairs"]:
        if model.is_uncertain(pair):
            out.append({"pair": [pair["value"]["positive"], pair["value"]["negative"]], "about": "diff_pair",
                        "critical": True, "why": pair["source"].get("ref", "")})
    return sorted(out, key=lambda q: (not q["critical"], natural_key(q.get("ref", "~"))))
