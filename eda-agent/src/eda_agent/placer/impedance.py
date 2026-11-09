# SPDX-License-Identifier: MIT
"""Characteristic impedance from the stackup (plan F3, task 6; architecture 7.1).

Two independent methods:

field   a 2D quasi-static cross-section solver: finite differences with box
        integration on a non-uniform rectilinear grid, one Laplace solve with
        the dielectrics and one in air per mode.  Z0 = 1 / (c * sqrt(C * C_air)).
        It handles what closed forms do not: solder mask over and between
        traces, offset stripline, edge-coupled pairs (odd/even modes).
closed  Hammerstad-Jensen microstrip with the Hammerstad thickness correction,
        and Cohn's stripline with finite thickness.  Used to cross-check the
        solver on the structures they cover.

Geometry follows Polar SI9000's field names so the golden set can be typed
straight in: H1, H2, W1 (rectangular trace, W2 = W1), T1, S1, Er1, Er2, C1
(mask over substrate), C2 (mask over trace), C3 (mask between the traces of a
pair), CEr.  For stripline, H2 is measured from the trace bottom to the upper
plane (includes T1).  Lengths in millimetres, impedance in ohms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

C0 = 299_792_458.0
ETA0 = 376.730313668
STRUCTURES = ("surface_microstrip", "coated_microstrip", "offset_stripline",
              "edge_coupled_surface_microstrip", "edge_coupled_coated_microstrip", "edge_coupled_offset_stripline")


@dataclass
class Geometry:
    structure: str
    H1: float
    W1: float
    T1: float = 0.035
    Er1: float = 4.5
    H2: float | None = None  # stripline: trace bottom to upper plane
    Er2: float | None = None  # stripline: upper dielectric
    S1: float | None = None  # pair: edge-to-edge gap
    C1: float = 0.0
    C2: float = 0.0
    C3: float | None = None
    CEr: float = 3.8

    @property
    def coupled(self) -> bool:
        return self.structure.startswith("edge_coupled")

    @property
    def stripline(self) -> bool:
        return self.structure.endswith("stripline")

    @property
    def coated(self) -> bool:
        return "coated" in self.structure


# --------------------------------------------------------------------------- closed forms
def microstrip_closed(W: float, H: float, T: float, er: float) -> float:
    """Hammerstad & Jensen (1980), thickness by Hammerstad's width correction."""

    def z_air(u: float) -> float:
        f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
        return ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1 + 4 / u ** 2))

    def e_eff(u: float) -> float:
        a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
        b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
        return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)

    u = W / H
    if T > 0:
        t = T / H
        d1 = t / math.pi * math.log(1 + 4 * math.e / (t * (1 / math.tanh(math.sqrt(6.517 * u))) ** 2))
        dr = 0.5 * (1 + 1 / math.cosh(math.sqrt(er - 1))) * d1
        u1, ur = u + d1, u + dr
    else:
        u1 = ur = u
    eff = e_eff(ur) * (z_air(u1) / z_air(ur)) ** 2
    return z_air(u1) / math.sqrt(eff)


def stripline_closed(W: float, B: float, T: float, er: float) -> float:
    """Cohn's symmetric stripline with finite thickness (Wadell 3.5.1); B = plane spacing."""
    x = T / B
    m = 6 / (3 + 2 * x / (1 - x))
    dw = x / (math.pi * (1 - x)) * (1 - 0.5 * math.log((x / (2 - x)) ** 2 + (0.0796 * x / (W / B + 1.1 * x)) ** m))
    w = W / (B - T) + dw
    return 30 / math.sqrt(er) * math.log(1 + 4 / (math.pi * w) * (8 / (math.pi * w)
                                                                    + math.sqrt((8 / (math.pi * w)) ** 2 + 6.27)))


# --------------------------------------------------------------------------- field solver
@dataclass
class _Scene:
    xs: np.ndarray
    ys: np.ndarray
    dielectrics: list[tuple[float, float, float, float, float]] = field(default_factory=list)  # x0,x1,y0,y1,er
    conductors: list[tuple[float, float, float, float]] = field(default_factory=list)  # x0,x1,y0,y1 (signal)
    top_plane: bool = False


def _axis(keys: list[float], lo: float, hi: float, h_min: float, h_max: float, growth: float = 1.18) -> np.ndarray:
    """Grid lines on every key coordinate, fine near them and growing geometrically away."""
    keys = sorted({round(k, 9) for k in keys if lo <= k <= hi} | {lo, hi})
    pts = set(keys)
    for k in keys:
        for direction in (1, -1):
            step, pos = h_min, k
            while True:
                pos += direction * step
                if pos <= lo or pos >= hi:
                    break
                pts.add(round(pos, 9))
                step = min(step * growth, h_max)
    xs = np.array(sorted(pts))
    # merge lines closer than a tenth of the finest step
    keep = [xs[0]]
    for v in xs[1:]:
        if v - keep[-1] > h_min * 0.1 or v in keys:
            keep.append(v)
    return np.array(keep)


def _build(g: Geometry) -> _Scene:
    W, T = g.W1, g.T1
    S = g.S1 or 0.0
    traces = [(-S / 2 - W, -S / 2), (S / 2, S / 2 + W)] if g.coupled else [(-W / 2, W / 2)]
    if g.stripline:
        H2 = g.H2 if g.H2 is not None else g.H1 + T
        top = g.H1 + H2
        y_tr0 = g.H1
    else:
        y_tr0 = g.H1
        top = g.H1 + T + max(20 * g.H1, 6 * (W + S + W))
    y_tr1 = y_tr0 + T
    span = (traces[-1][1] - traces[0][0]) / 2 + 12 * max(g.H1, (g.H2 or 0), W)
    h_min = max(min(T, W, S or W, g.H1, (g.C1 or T), (g.C2 or T)) / 6, 1e-4)
    h_max = max(g.H1, W) * 1.5
    xkeys = [t for tr in traces for t in tr]
    if g.coated:
        xkeys += [traces[0][0] - g.C2, traces[-1][1] + g.C2]
    ykeys = [0.0, y_tr0, y_tr1]
    if g.coated:
        ykeys += [y_tr0 + g.C1, y_tr1 + g.C2]
    if g.stripline:
        ykeys.append(top)
    xs = _axis(xkeys, -span, span, h_min, h_max)
    ys = _axis(ykeys, 0.0, top, h_min, max(h_max, (top - y_tr1) / 25 if not g.stripline else h_max))
    sc = _Scene(xs, ys, top_plane=True)
    if g.stripline:
        sc.dielectrics.append((-span, span, 0.0, y_tr0, g.Er1))
        sc.dielectrics.append((-span, span, y_tr0, top, g.Er2 or g.Er1))
    else:
        sc.dielectrics.append((-span, span, 0.0, y_tr0, g.Er1))
        if g.coated:
            sc.dielectrics.append((-span, span, y_tr0, y_tr0 + g.C1, g.CEr))  # mask on the substrate
            for x0, x1 in traces:  # conformal mask over and beside each trace
                sc.dielectrics.append((x0 - g.C2, x1 + g.C2, y_tr0, y_tr1 + g.C2, g.CEr))
            if g.coupled and g.C3 is not None:
                sc.dielectrics.append((traces[0][1], traces[1][0], y_tr0, y_tr0 + g.C3, g.CEr))
    sc.conductors = [(x0, x1, y_tr0, y_tr1) for x0, x1 in traces]
    return sc


def _capacitance(sc: _Scene, volts: list[float], air: bool) -> list[float]:
    """Charge per unit length on each signal conductor (F/m with eps0 = 1 units), planes at 0 V."""
    xs, ys = sc.xs, sc.ys
    nx, ny = len(xs), len(ys)
    dx, dy = np.diff(xs), np.diff(ys)
    xc, yc = (xs[:-1] + xs[1:]) / 2, (ys[:-1] + ys[1:]) / 2
    eps = np.ones((nx - 1, ny - 1))
    if not air:
        for x0, x1, y0, y1, er in sc.dielectrics:  # later regions overwrite earlier ones
            mx = (xc >= x0) & (xc <= x1)
            my = (yc >= y0) & (yc <= y1)
            eps[np.ix_(mx, my)] = er
    idx = np.arange(nx * ny).reshape(nx, ny)
    fixed = np.full((nx, ny), np.nan)
    fixed[:, 0] = 0.0
    if sc.top_plane:
        fixed[:, -1] = 0.0
    tol = 1e-9
    for k, (x0, x1, y0, y1) in enumerate(sc.conductors):
        mx = (xs >= x0 - tol) & (xs <= x1 + tol)
        my = (ys >= y0 - tol) & (ys <= y1 + tol)
        fixed[np.ix_(mx, my)] = volts[k]
    rows, cols, vals = [], [], []

    def eps_at(i: int, j: int) -> float:
        return eps[i, j] if 0 <= i < nx - 1 and 0 <= j < ny - 1 else 0.0

    # edge conductances: horizontal edge (i,j)-(i+1,j), vertical edge (i,j)-(i,j+1)
    gx = np.zeros((nx - 1, ny))
    for j in range(ny):
        lo = dy[j - 1] / 2 if j > 0 else 0.0
        hi = dy[j] / 2 if j < ny - 1 else 0.0
        for i in range(nx - 1):
            gx[i, j] = (eps_at(i, j - 1) * lo + eps_at(i, j) * hi) / dx[i]
    gy = np.zeros((nx, ny - 1))
    for i in range(nx):
        lo = dx[i - 1] / 2 if i > 0 else 0.0
        hi = dx[i] / 2 if i < nx - 1 else 0.0
        for j in range(ny - 1):
            gy[i, j] = (eps_at(i - 1, j) * lo + eps_at(i, j) * hi) / dy[j]
    edges = [(idx[:-1, :].ravel(), idx[1:, :].ravel(), gx.ravel()),
             (idx[:, :-1].ravel(), idx[:, 1:].ravel(), gy.ravel())]
    n = nx * ny
    diag = np.zeros(n)
    for a, b, g in edges:
        rows += [a, b]
        cols += [b, a]
        vals += [-g, -g]
        np.add.at(diag, a, g)
        np.add.at(diag, b, g)
    rows.append(np.arange(n))
    cols.append(np.arange(n))
    vals.append(diag)
    A = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)).tocsr()
    known = ~np.isnan(fixed.ravel())
    free = ~known
    v = np.where(known, np.nan_to_num(fixed.ravel()), 0.0)
    rhs = -A[free][:, known] @ v[known]
    v[free] = spsolve(A[free][:, free].tocsc(), rhs)
    charge = A @ v  # net flux out of each node
    out = []
    for x0, x1, y0, y1 in sc.conductors:
        mx = (xs >= x0 - tol) & (xs <= x1 + tol)
        my = (ys >= y0 - tol) & (ys <= y1 + tol)
        out.append(float(charge[idx[np.ix_(mx, my)].ravel()].sum()))
    return out


def _z_from(sc: _Scene, volts: list[float]) -> float:
    q = _capacitance(sc, volts, air=False)[0]
    q0 = _capacitance(sc, volts, air=True)[0]
    eps0 = 8.8541878128e-12
    c, c_air = q * eps0, q0 * eps0  # per metre (lengths cancel in the 2D ratio)
    return 1 / (C0 * math.sqrt(c * c_air))


def solve(g: Geometry) -> dict:
    """Z0 for a single trace; Z_odd, Z_even, Zdiff and Zcommon for a pair."""
    sc = _build(g)
    if not g.coupled:
        return {"z0": round(_z_from(sc, [1.0]), 2), "method": "field"}
    z_odd = _z_from(sc, [1.0, -1.0])
    z_even = _z_from(sc, [1.0, 1.0])
    return {"z_odd": round(z_odd, 2), "z_even": round(z_even, 2), "zdiff": round(2 * z_odd, 2),
            "zcommon": round(z_even / 2, 2), "method": "field"}


def closed(g: Geometry) -> float | None:
    if g.structure == "surface_microstrip":
        return round(microstrip_closed(g.W1, g.H1, g.T1, g.Er1), 2)
    if g.structure == "offset_stripline" and g.H2 is not None and abs(g.H2 - (g.H1 + g.T1)) < 1e-9 \
            and (g.Er2 is None or g.Er2 == g.Er1):
        return round(stripline_closed(g.W1, g.H1 + g.H2, g.T1, g.Er1), 2)
    return None
