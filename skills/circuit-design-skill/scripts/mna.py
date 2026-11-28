#!/usr/bin/env python3
"""A small Modified Nodal Analysis (MNA) solver, standard library only.

This is the skill's *independent* linear check.  It is intentionally limited
to linear elements (R, L, C, independent sources, VCVS/VCCS and an ideal
op-amp model).  Nonlinear devices (diodes, BJTs, MOSFETs) are verified with
ngspice and a vendor/generic model instead; this module refuses to pretend it
can solve them.

Conventions
-----------
* Node ``"0"`` is ground (or the node passed as ``ground=``).
* Voltage sources and VCVS elements add a branch current unknown.
* AC analysis zeroes independent DC sources: a voltage source becomes a
  short, a current source becomes an open.  Only the ``ac`` amplitude drives
  the AC solution.
* DC analysis opens capacitors and shorts inductors.

The implementation uses complex Gaussian elimination with partial pivoting.
It is fine for the small verification circuits this skill generates; it is
not a replacement for a real SPICE engine.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from typing import Iterable


class MNAError(ValueError):
    """Raised for an invalid or singular circuit."""


class UnsupportedElement(MNAError):
    """Raised when a nonlinear or unsupported element is requested."""


@dataclass
class Element:
    kind: str
    name: str
    nodes: tuple[str, ...]
    value: float
    extra: dict = field(default_factory=dict)


class Circuit:
    """Build and solve a linear circuit with MNA."""

    def __init__(self, ground: str = "0"):
        self.ground = ground
        self.elements: list[Element] = []
        self._names: set[str] = set()
        self._last_solution: dict[str, complex] | None = None

    # -- construction ------------------------------------------------------
    def _add(self, kind: str, name: str, nodes: Iterable[str], value: float = 0.0, **extra) -> "Circuit":
        if name in self._names:
            raise MNAError(f"duplicate element name {name!r}")
        self._names.add(name)
        self.elements.append(Element(kind, name, tuple(nodes), float(value), extra))
        return self

    def R(self, name: str, n1: str, n2: str, value: float) -> "Circuit":
        if value <= 0:
            raise MNAError(f"{name}: resistance must be positive")
        return self._add("R", name, (n1, n2), value)

    def C(self, name: str, n1: str, n2: str, value: float) -> "Circuit":
        if value < 0:
            raise MNAError(f"{name}: capacitance must be non-negative")
        return self._add("C", name, (n1, n2), value)

    def L(self, name: str, n1: str, n2: str, value: float) -> "Circuit":
        if value <= 0:
            raise MNAError(f"{name}: inductance must be positive")
        return self._add("L", name, (n1, n2), value)

    def V(self, name: str, npos: str, nneg: str, dc: float = 0.0, ac: float = 0.0) -> "Circuit":
        return self._add("V", name, (npos, nneg), dc, ac=ac)

    def I(self, name: str, npos: str, nneg: str, dc: float = 0.0, ac: float = 0.0) -> "Circuit":
        # Positive current flows from npos to nneg *inside* the source, so it
        # injects current into nneg and pulls it from npos.
        return self._add("I", name, (npos, nneg), dc, ac=ac)

    def E(self, name: str, npos: str, nneg: str, ncp: str, ncn: str, gain: float) -> "Circuit":
        return self._add("E", name, (npos, nneg, ncp, ncn), gain)

    def G(self, name: str, npos: str, nneg: str, ncp: str, ncn: str, gain: float) -> "Circuit":
        return self._add("G", name, (npos, nneg, ncp, ncn), gain)

    def opamp(
        self,
        name: str,
        inp: str,
        inn: str,
        out: str,
        *,
        gain: float = 1e6,
        rout: float = 50.0,
        vpos: str | None = None,
        vneg: str | None = None,
    ) -> "Circuit":
        """Ideal-ish op-amp: VCVS with finite open-loop gain and output R.

        Supply pins are accepted for interface compatibility but are not
        modelled: the output is not clipped and supply current is not drawn.
        That is acceptable for small-signal gain/frequency checks and is
        explicitly reported as an assumption.
        """
        internal = f"{name}__out"
        self.E(f"{name}__E", internal, self.ground, inp, inn, gain)
        self.R(f"{name}__ro", internal, out, rout)
        if vpos is not None:
            self._add("SUPPLY", f"{name}__vp", (vpos,), 0.0)
        if vneg is not None:
            self._add("SUPPLY", f"{name}__vn", (vneg,), 0.0)
        return self

    # -- assembly ----------------------------------------------------------
    def _nodes(self) -> list[str]:
        seen: list[str] = []
        for e in self.elements:
            for n in e.nodes:
                if n != self.ground and n not in seen:
                    seen.append(n)
        return seen

    def _assemble(self, freq: float | None) -> tuple[list[list[complex]], list[complex], dict[str, int], dict[str, int]]:
        """Return (A, z, node_index, branch_index) for AC (freq) or DC (None)."""
        nodes = self._nodes()
        node_index = {n: i for i, n in enumerate(nodes)}
        n_nodes = len(nodes)

        branches: list[Element] = []
        for e in self.elements:
            if e.kind in ("V", "E"):
                branches.append(e)
            elif e.kind == "L" and freq is None:
                branches.append(e)
        branch_index = {e.name: n_nodes + i for i, e in enumerate(branches)}

        size = n_nodes + len(branches)
        A = [[0j] * size for _ in range(size)]
        z = [0j] * size

        def gi(node: str) -> int | None:
            if node == self.ground:
                return None
            if node not in node_index:
                raise MNAError(f"unknown node {node!r}")
            return node_index[node]

        def g_stamp(n1: str, n2: str, g: complex) -> None:
            i, j = gi(n1), gi(n2)
            if i is not None:
                A[i][i] += g
            if j is not None:
                A[j][j] += g
            if i is not None and j is not None:
                A[i][j] -= g
                A[j][i] -= g

        def v_stamp(e: Element, rhs: float) -> None:
            k = branch_index[e.name]
            i, j = gi(e.nodes[0]), gi(e.nodes[1])
            if i is not None:
                A[i][k] += 1
                A[k][i] += 1
            if j is not None:
                A[j][k] -= 1
                A[k][j] -= 1
            z[k] += rhs

        for e in self.elements:
            if e.kind == "R":
                g_stamp(e.nodes[0], e.nodes[1], 1.0 / e.value)
            elif e.kind == "C":
                if freq is None or e.value == 0:
                    continue
                g_stamp(e.nodes[0], e.nodes[1], 1j * 2 * math.pi * freq * e.value)
            elif e.kind == "L":
                if freq is None:
                    v_stamp(e, 0.0)  # ideal short at DC
                else:
                    g_stamp(e.nodes[0], e.nodes[1], 1.0 / (1j * 2 * math.pi * freq * e.value))
            elif e.kind == "V":
                v_stamp(e, e.value if freq is None else e.extra.get("ac", 0.0))
            elif e.kind == "I":
                src = e.value if freq is None else e.extra.get("ac", 0.0)
                i, j = gi(e.nodes[0]), gi(e.nodes[1])
                if i is not None:
                    z[i] -= src
                if j is not None:
                    z[j] += src
            elif e.kind == "E":
                k = branch_index[e.name]
                i, j = gi(e.nodes[0]), gi(e.nodes[1])
                cp, cn = gi(e.nodes[2]), gi(e.nodes[3])
                if i is not None:
                    A[i][k] += 1
                    A[k][i] += 1
                if j is not None:
                    A[j][k] -= 1
                    A[k][j] -= 1
                if cp is not None:
                    A[k][cp] -= e.value
                if cn is not None:
                    A[k][cn] += e.value
                z[k] += 0.0
            elif e.kind == "G":
                g = e.value
                i, j = gi(e.nodes[0]), gi(e.nodes[1])
                cp, cn = gi(e.nodes[2]), gi(e.nodes[3])
                for row, sign in ((i, 1.0), (j, -1.0)):
                    if row is None:
                        continue
                    if cp is not None:
                        A[row][cp] += sign * g
                    if cn is not None:
                        A[row][cn] -= sign * g
            elif e.kind == "SUPPLY":
                continue
            else:  # pragma: no cover - construction guards this
                raise UnsupportedElement(f"unsupported element kind {e.kind!r}")

        return A, z, node_index, branch_index

    @staticmethod
    def _solve_linear(A: list[list[complex]], z: list[complex]) -> list[complex]:
        """Solve an MNA system after row/column equilibration.

        MNA matrices mix conductances, ideal-source constraints, and controlled
        source gains, so a fixed absolute pivot threshold is not meaningful.
        Equilibrating rows and columns makes the singularity test relative to
        the matrix scale while preserving the original solution.
        """
        n = len(A)
        if n == 0:
            return []
        if len(z) != n or any(len(row) != n for row in A):
            raise MNAError("linear system must have a square matrix and matching RHS")

        # Scale each equation by its largest coefficient.
        row_scale = [max(abs(value) for value in row) for row in A]
        if any(scale == 0 for scale in row_scale):
            raise MNAError(
                "singular matrix: check for floating nodes, a missing ground "
                "reference, or two ideal voltage sources in parallel"
            )
        scaled = [
            [value / row_scale[i] for value in A[i]]
            for i in range(n)
        ]
        rhs = [z[i] / row_scale[i] for i in range(n)]

        # Scale unknowns as well; y[j] = column_scale[j] * x[j].
        column_scale = [
            max(abs(scaled[i][j]) for i in range(n))
            for j in range(n)
        ]
        if any(scale == 0 for scale in column_scale):
            raise MNAError(
                "singular matrix: check for floating nodes, a missing ground "
                "reference, or two ideal voltage sources in parallel"
            )
        M = [
            [scaled[i][j] / column_scale[j] for j in range(n)] + [rhs[i]]
            for i in range(n)
        ]

        # Partial pivoting on the equilibrated matrix. This relative threshold
        # detects numerical singularity without rejecting small SI-valued
        # conductances merely because their absolute magnitude is below 1e-18.
        pivot_tolerance = 1e-14
        for col in range(n):
            pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
            if abs(M[pivot][col]) <= pivot_tolerance:
                raise MNAError(
                    "singular or ill-conditioned matrix after scaling: check "
                    "for floating nodes, missing ground, or conflicting ideal sources"
                )
            if pivot != col:
                M[col], M[pivot] = M[pivot], M[col]
            pv = M[col][col]
            for r in range(col + 1, n):
                factor = M[r][col] / pv
                if factor == 0:
                    continue
                M[r][col] = 0j
                for c in range(col + 1, n + 1):
                    M[r][c] -= factor * M[col][c]

        y = [0j] * n
        for row in range(n - 1, -1, -1):
            total = M[row][n]
            for c in range(row + 1, n):
                total -= M[row][c] * y[c]
            y[row] = total / M[row][row]

        x = [y[j] / column_scale[j] for j in range(n)]

        # Check the solution against the unscaled system. The row-wise
        # backward error is unit-independent and catches unstable solves.
        for i in range(n):
            residual = sum(A[i][j] * x[j] for j in range(n)) - z[i]
            scale = abs(z[i]) + sum(abs(A[i][j]) * abs(x[j]) for j in range(n))
            if abs(residual) > 1e-10 * max(scale, 1e-300):
                raise MNAError(
                    "numerically unstable solution: scaled linear solve failed "
                    "the normalized residual check"
                )
        return x

    def _solution_to_nodes(
        self, x: list[complex], node_index: dict[str, int], branch_index: dict[str, int]
    ) -> dict[str, complex]:
        out: dict[str, complex] = {self.ground: 0j}
        for node, i in node_index.items():
            out[node] = x[i]
        for name, i in branch_index.items():
            out[f"@{name}"] = x[i]
        return out

    # -- public solving ----------------------------------------------------
    def solve_dc(self) -> dict[str, complex]:
        A, z, ni, bi = self._assemble(None)
        x = self._solve_linear(A, z)
        self._last_solution = self._solution_to_nodes(x, ni, bi)
        return self._last_solution

    def solve_ac(self, frequencies: Iterable[float]) -> dict[float, dict[str, complex]]:
        out: dict[float, dict[str, complex]] = {}
        for f in frequencies:
            if f <= 0:
                raise MNAError("AC frequency must be positive")
            A, z, ni, bi = self._assemble(float(f))
            x = self._solve_linear(A, z)
            out[float(f)] = self._solution_to_nodes(x, ni, bi)
        return out

    def solve_op(self) -> dict[str, complex]:
        """Alias for solve_dc, for symmetry with SPICE terminology."""
        return self.solve_dc()

    # -- convenience -------------------------------------------------------
    def dc_voltage(self, node: str) -> float:
        sol = self._last_solution or self.solve_dc()
        return float(sol[node].real)

    def ac_gain(self, out_node: str, in_node: str, frequency: float) -> complex:
        sol = self.solve_ac([frequency])[frequency]
        vin = sol[in_node]
        if abs(vin) == 0:
            raise MNAError(f"input node {in_node!r} has zero AC excitation")
        return sol[out_node] / vin

    def ac_gain_db(self, out_node: str, in_node: str, frequency: float) -> float:
        g = self.ac_gain(out_node, in_node, frequency)
        return 20 * math.log10(abs(g)) if abs(g) > 0 else -math.inf

    def find_cutoff(
        self,
        out_node: str,
        in_node: str,
        *,
        passband: str = "low",
        f_min: float = 1.0,
        f_max: float = 1e9,
        tol: float = 1e-3,
    ) -> float:
        """Find the -3 dB corner by bisection on log frequency.

        ``passband='low'`` searches for the point where the gain falls to
        1/sqrt(2) of the low-frequency gain; ``'high'`` uses the high-frequency
        gain.  Returns the corner frequency in Hz.
        """
        if passband not in ("low", "high"):
            raise MNAError("passband must be 'low' or 'high'")
        ref_f = f_min if passband == "low" else f_max
        ref = abs(self.ac_gain(out_node, in_node, ref_f))
        if ref == 0:
            raise MNAError("reference gain is zero; cannot find a -3 dB point")
        target = ref / math.sqrt(2)

        def mag(f: float) -> float:
            return abs(self.ac_gain(out_node, in_node, f))

        lo, hi = math.log10(f_min), math.log10(f_max)
        # Walk the decade grid to bracket the crossing.
        steps = 400
        prev_f = f_min
        prev = mag(prev_f) - target
        for k in range(1, steps + 1):
            f = 10 ** (lo + (hi - lo) * k / steps)
            cur = mag(f) - target
            if prev == 0:
                return prev_f
            if prev * cur < 0:
                a, b = prev_f, f
                for _ in range(80):
                    mid = math.sqrt(a * b)
                    if (mag(a) - target) * (mag(mid) - target) <= 0:
                        b = mid
                    else:
                        a = mid
                    if abs(b - a) / mid < tol:
                        break
                return math.sqrt(a * b)
            prev_f, prev = f, cur
        raise MNAError("no -3 dB crossing found in the search range")


def from_components(
    components: list[dict],
    *,
    ground: str = "0",
    supplies: dict[str, float] | None = None,
) -> Circuit:
    """Build a :class:`Circuit` from the skill's component list.

    Supported types: ``R``, ``C``, ``L``, ``V``, ``I``, ``E``, ``G``, ``OPAMP``.
    Raises :class:`UnsupportedElement` for anything nonlinear so the caller can
    fall back to ngspice instead of silently linearising a diode.
    """
    c = Circuit(ground=ground)
    supplies = supplies or {}
    for comp in components:
        ctype = str(comp.get("type", "")).upper()
        name = comp.get("ref") or comp.get("name")
        if not name:
            raise MNAError("component is missing 'ref'")
        pins = comp.get("pins") or {}
        value = comp.get("value", 0.0)

        def pin(*keys: str) -> str:
            for key in keys:
                if key in pins:
                    return pins[key]
            raise MNAError(f"{name}: missing pin {keys[0]!r}")

        if ctype == "R":
            c.R(name, pin("1", "A"), pin("2", "B"), float(value))
        elif ctype == "C":
            c.C(name, pin("1", "A"), pin("2", "B"), float(value))
        elif ctype == "L":
            c.L(name, pin("1", "A"), pin("2", "B"), float(value))
        elif ctype == "V":
            dc = comp["dc"] if comp.get("dc") is not None else value
            c.V(
                name,
                pin("1", "+"),
                pin("2", "-"),
                dc=float(dc),
                ac=float(comp.get("ac", 0.0)),
            )
        elif ctype == "I":
            dc = comp["dc"] if comp.get("dc") is not None else value
            c.I(
                name,
                pin("1", "+"),
                pin("2", "-"),
                dc=float(dc),
                ac=float(comp.get("ac", 0.0)),
            )
        elif ctype == "E":
            c.E(name, pin("1", "+"), pin("2", "-"), pin("3", "CP"), pin("4", "CN"), float(value))
        elif ctype == "G":
            c.G(name, pin("1", "+"), pin("2", "-"), pin("3", "CP"), pin("4", "CN"), float(value))
        elif ctype == "OPAMP":
            c.opamp(
                name,
                pin("1", "IN+", "+"),
                pin("2", "IN-", "-"),
                pin("5", "OUT"),
                gain=float(comp.get("gain", comp.get("value", 1e6))),
                rout=float(comp.get("rout", 50.0)),
                vpos=pins.get("3", pins.get("V+")),
                vneg=pins.get("4", pins.get("V-")),
            )
        else:
            raise UnsupportedElement(
                f"{name}: MNA cannot solve a {ctype} element; use ngspice with a model"
            )
    return c
