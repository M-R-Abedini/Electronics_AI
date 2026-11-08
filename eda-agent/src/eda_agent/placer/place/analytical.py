# SPDX-License-Identifier: MIT
"""S3 global placement: analytical, in the style of ePlace / Cypress (architecture 6).

Minimises weighted-average (WA) wirelength plus an electrostatic density
penalty per side: part areas are charges on a bin grid, the potential comes from
a DCT Poisson solve, and the field pushes parts apart.  Bins outside the board
outline and fixed parts are fixed charge, so parts are pushed into the board.
Rotations are re-chosen every ``ROTATE_EVERY`` iterations by trying the four
quarter turns of each part against its nets.

Pin-pair crossings are not in the analytical objective yet; S5 handles them
(decision log, F1).  Deterministic: no randomness; the start spreads parts on a
golden-angle spiral by index.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.fft import dctn, idctn
from shapely.geometry import Point
from shapely.prepared import prep

from .problem import PlaceProblem

BINS = 64  # declared: density grid resolution per side (bins on the longer board axis)
TARGET_DENSITY = 0.85  # declared: bin fill the density term aims below
STOP_OVERFLOW = 0.10
MAX_ITERS = 900
ROTATE_EVERY = 120


@dataclass
class S3Result:
    poses: list[tuple[float, float, int]]
    iterations: int
    overflow: float
    wirelength: float


def _rot(x: np.ndarray, y: np.ndarray, r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Quarter turns, KiCad convention: r=1 maps (x, y) -> (y, -x)."""
    xs = np.select([r == 0, r == 1, r == 2, r == 3], [x, y, -x, -y])
    ys = np.select([r == 0, r == 1, r == 2, r == 3], [y, -x, -y, x])
    return xs, ys


class Analytical:
    def __init__(self, pb: PlaceProblem) -> None:
        self.pb = pb
        parts = pb.parts
        self.n = len(parts)
        self.box = np.array([[p[0], p[1], p[2], p[3]] for p in parts], dtype=float)
        self.side = np.array([p[4] for p in parts], dtype=int)
        self.fixed = np.array([p[5] for p in parts], dtype=bool)
        self.can_rotate = np.array([p[6] for p in parts], dtype=bool)
        self.x = np.array([p[7] for p in parts], dtype=float)
        self.y = np.array([p[8] for p in parts], dtype=float)
        self.r = np.array([p[9] for p in parts], dtype=int)
        self.movable = np.flatnonzero(~self.fixed)
        # pins, sorted by net for segmented reductions
        order, net_of, weights = [], [], []
        for k, (pins, weight, _crossing) in enumerate(pb.nets):
            order += pins
            net_of += [k] * len(pins)
            weights.append(weight)
        self.pin_part = np.array([pb.pins[i][0] for i in order], dtype=int)
        self.pin_lx = np.array([pb.pins[i][1] for i in order], dtype=float)
        self.pin_ly = np.array([pb.pins[i][2] for i in order], dtype=float)
        self.pin_net = np.array(net_of, dtype=int)
        self.net_start = np.flatnonzero(np.r_[True, self.pin_net[1:] != self.pin_net[:-1]]) if net_of else np.array([0])
        self.net_w = np.array(weights, dtype=float)
        # grid over the board's bounding box
        xs = [p[0] for ring in pb.outers for p in ring]
        ys = [p[1] for ring in pb.outers for p in ring]
        self.gx0, self.gy0 = min(xs), min(ys)
        span = max(max(xs) - self.gx0, max(ys) - self.gy0)
        self.bw = span / BINS
        self.mx = max(4, math.ceil((max(xs) - self.gx0) / self.bw))
        self.my = max(4, math.ceil((max(ys) - self.gy0) / self.bw))
        self.bin_area = self.bw * self.bw
        self.fixed_map = [self._fixed_density(s) for s in (0, 1)]
        u = np.pi * np.arange(self.mx) / self.mx
        v = np.pi * np.arange(self.my) / self.my
        self.denom = u[None, :] ** 2 + v[:, None] ** 2
        self.denom[0, 0] = 1.0

    # ------------------------------------------------------------- geometry
    def _rects(self, idx: np.ndarray, r: np.ndarray | None = None) -> np.ndarray:
        """World rects (x0, y0, x1, y1) of parts ``idx`` at their poses."""
        r = self.r[idx] if r is None else r
        b = self.box[idx]
        cx, cy = _rot(np.stack([b[:, 0], b[:, 2], b[:, 2], b[:, 0]], 1), np.stack([b[:, 1], b[:, 1], b[:, 3], b[:, 3]], 1),
                      r[:, None])
        return np.stack([self.x[idx] + cx.min(1), self.y[idx] + cy.min(1), self.x[idx] + cx.max(1), self.y[idx] + cy.max(1)], 1)

    def _window(self, rect) -> tuple[slice, slice, np.ndarray] | None:
        """Bins under ``rect`` and the overlap area with each."""
        x0, y0, x1, y1 = rect
        i0 = max(0, int((x0 - self.gx0) // self.bw))
        i1 = min(self.mx - 1, int((x1 - self.gx0) // self.bw))
        j0 = max(0, int((y0 - self.gy0) // self.bw))
        j1 = min(self.my - 1, int((y1 - self.gy0) // self.bw))
        if i1 < i0 or j1 < j0:
            return None
        ex = self.gx0 + self.bw * np.arange(i0, i1 + 2)
        ey = self.gy0 + self.bw * np.arange(j0, j1 + 2)
        ox = np.clip(np.minimum(ex[1:], x1) - np.maximum(ex[:-1], x0), 0, None)
        oy = np.clip(np.minimum(ey[1:], y1) - np.maximum(ey[:-1], y0), 0, None)
        return slice(j0, j1 + 1), slice(i0, i1 + 1), np.outer(oy, ox)

    def _raster(self, rect, grid: np.ndarray, scale: float = 1.0) -> None:
        win = self._window(rect)
        if win is not None:
            grid[win[0], win[1]] += scale * win[2]

    def _fixed_density(self, side: int) -> np.ndarray:
        grid = np.zeros((self.my, self.mx))
        board = prep(self._board_shape())
        cx = self.gx0 + self.bw * (np.arange(self.mx) + 0.5)
        cy = self.gy0 + self.bw * (np.arange(self.my) + 0.5)
        for j, yy in enumerate(cy):
            for i, xx in enumerate(cx):
                if not board.contains(Point(xx, yy)):
                    grid[j, i] = self.bin_area  # outside the board: full
        idx = np.flatnonzero(self.fixed & (self.side == side))
        for rect in self._rects(idx):
            self._raster(tuple(rect), grid)
        return np.minimum(grid, self.bin_area)

    def _board_shape(self):
        from shapely.geometry import Polygon

        from ..eval.legality import board_polygon

        shape = board_polygon(self.pb.board)
        return shape if shape is not None else Polygon(self.pb.outers[0])

    # ----------------------------------------------------------- wirelength
    def _pins_xy(self) -> tuple[np.ndarray, np.ndarray]:
        dx, dy = _rot(self.pin_lx, self.pin_ly, self.r[self.pin_part])
        return self.x[self.pin_part] + dx, self.y[self.pin_part] + dy

    def _wa(self, v: np.ndarray, gamma: float) -> tuple[float, np.ndarray]:
        """Weighted-average wirelength along one axis and its gradient per pin."""
        s = self.net_start
        vmax = np.maximum.reduceat(v, s)[self.pin_net]
        vmin = np.minimum.reduceat(v, s)[self.pin_net]
        ap = np.exp((v - vmax) / gamma)
        am = np.exp(-(v - vmin) / gamma)
        sp, sm = np.add.reduceat(ap, s), np.add.reduceat(am, s)
        xp, xm = np.add.reduceat(v * ap, s), np.add.reduceat(v * am, s)
        wap, wam = xp / sp, xm / sm
        w = self.net_w
        wl = float(np.sum(w * (wap - wam)))
        gp = ap * (1 + (v - wap[self.pin_net]) / gamma) / sp[self.pin_net]
        gm = am * (1 - (v - wam[self.pin_net]) / gamma) / sm[self.pin_net]
        return wl, w[self.pin_net] * (gp - gm)

    def wirelength(self, gamma: float) -> tuple[float, np.ndarray, np.ndarray]:
        px, py = self._pins_xy()
        wx, gx = self._wa(px, gamma)
        wy, gy = self._wa(py, gamma)
        grad_x = np.bincount(self.pin_part, weights=gx, minlength=self.n)
        grad_y = np.bincount(self.pin_part, weights=gy, minlength=self.n)
        return wx + wy, grad_x, grad_y

    # -------------------------------------------------------------- density
    def density(self) -> tuple[float, np.ndarray, np.ndarray, float]:
        """Electrostatic energy gradient per part and the overflow of movable area."""
        grad_x = np.zeros(self.n)
        grad_y = np.zeros(self.n)
        overflow_area = 0.0
        energy = 0.0
        min_w = 1.414 * self.bw
        for s in (0, 1):
            idx = np.flatnonzero(~self.fixed & (self.side == s))
            if idx.size == 0:
                continue
            rects = self._rects(idx)
            mov = np.zeros((self.my, self.mx))
            windows = []
            for rect in rects:
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
                ew, eh = max(w, min_w), max(h, min_w)
                win = self._window((cx - ew / 2, cy - eh / 2, cx + ew / 2, cy + eh / 2))
                if win is not None:
                    win = (win[0], win[1], win[2] * (w * h) / (ew * eh))
                    mov[win[0], win[1]] += win[2]
                windows.append(win)
            rho = (mov + self.fixed_map[s]) / self.bin_area
            coef = dctn(rho, type=2, norm="ortho") / self.denom
            coef[0, 0] = 0.0
            psi = idctn(coef, type=2, norm="ortho")
            ey, ex = np.gradient(-psi, self.bw)
            energy += float(np.sum(rho * psi))
            cap = np.clip(TARGET_DENSITY * self.bin_area - self.fixed_map[s], 0, None)
            overflow_area += float(np.sum(np.clip(mov - cap, 0, None)))
            for k, win in zip(idx, windows, strict=True):
                if win is None:
                    continue
                grad_x[k] = -float(np.sum(win[2] * ex[win[0], win[1]]))
                grad_y[k] = -float(np.sum(win[2] * ey[win[0], win[1]]))
        movable_area = float(np.sum((self.box[:, 2] - self.box[:, 0]) * (self.box[:, 3] - self.box[:, 1])
                                    * (~self.fixed)))
        return energy, grad_x, grad_y, overflow_area / max(movable_area, 1e-9)

    # ------------------------------------------------------------- rotation
    def choose_rotations(self, gamma: float) -> int:
        changed = 0
        for k in self.movable:
            if not self.can_rotate[k] or not np.any(self.pin_part == k):
                continue
            nets = np.unique(self.pin_net[self.pin_part == k])
            mask = np.isin(self.pin_net, nets)
            best_r, best = int(self.r[k]), None
            for r in (self.r[k], (self.r[k] + 1) % 4, (self.r[k] + 2) % 4, (self.r[k] + 3) % 4):
                self.r[k] = r
                px, py = self._pins_xy()
                cost = 0.0
                for v in (px, py):
                    sel = v[mask]
                    nn = self.pin_net[mask]
                    starts = np.flatnonzero(np.r_[True, nn[1:] != nn[:-1]])
                    cost += float(np.sum((np.maximum.reduceat(sel, starts) - np.minimum.reduceat(sel, starts))
                                         * self.net_w[nn[starts]]))
                if best is None or cost < best - 1e-9:
                    best, best_r = cost, int(r)
            if best_r != self.r[k]:
                changed += 1
            self.r[k] = best_r
        return changed

    # ------------------------------------------------------------ optimiser
    def run(self, max_iters: int = MAX_ITERS) -> S3Result:
        mov = self.movable
        if mov.size == 0:
            return S3Result(self._poses(), 0, 0.0, 0.0)
        cx = self.gx0 + self.mx * self.bw / 2
        cy = self.gy0 + self.my * self.bw / 2
        golden = math.pi * (3 - math.sqrt(5))
        for n, k in enumerate(mov):  # deterministic spiral start around the board centre
            rad = 0.5 * self.bw * math.sqrt(n + 1)
            self.x[k] = cx + rad * math.cos(n * golden)
            self.y[k] = cy + rad * math.sin(n * golden)
        vx = np.zeros(self.n)
        vy = np.zeros(self.n)
        lam = None
        gamma = 8 * self.bw
        overflow = 1.0
        wl = 0.0
        it = 0
        for it in range(1, max_iters + 1):
            wl, wgx, wgy = self.wirelength(gamma)
            _e, dgx, dgy, overflow = self.density()
            if lam is None:
                lam = (np.abs(wgx[mov]).sum() + np.abs(wgy[mov]).sum()) / max(
                    np.abs(dgx[mov]).sum() + np.abs(dgy[mov]).sum(), 1e-12)
            gx = wgx + lam * dgx
            gy = wgy + lam * dgy
            norm = np.sqrt(np.mean(gx[mov] ** 2 + gy[mov] ** 2)) or 1.0
            step = self.bw * (0.6 if it < 300 else 0.3)
            vx[mov] = 0.9 * vx[mov] - step * gx[mov] / norm
            vy[mov] = 0.9 * vy[mov] - step * gy[mov] / norm
            self.x[mov] += vx[mov]
            self.y[mov] += vy[mov]
            self._clamp()
            lam *= 1.03
            gamma = self.bw * max(0.5, 8.0 * min(overflow, 1.0))
            if it % ROTATE_EVERY == 0:
                self.choose_rotations(gamma)
            if overflow < STOP_OVERFLOW and it > 50:
                break
        self.choose_rotations(gamma)
        return S3Result(self._poses(), it, round(overflow, 4), round(wl, 3))

    def _clamp(self) -> None:
        mov = self.movable
        rects = self._rects(mov)
        x_max, y_max = self.gx0 + self.mx * self.bw, self.gy0 + self.my * self.bw
        self.x[mov] += np.clip(self.gx0 - rects[:, 0], 0, None) - np.clip(rects[:, 2] - x_max, 0, None)
        self.y[mov] += np.clip(self.gy0 - rects[:, 1], 0, None) - np.clip(rects[:, 3] - y_max, 0, None)

    def _poses(self) -> list[tuple[float, float, int]]:
        return [(float(self.x[k]), float(self.y[k]), int(self.r[k])) for k in range(self.n)]


def global_place(pb: PlaceProblem, max_iters: int = MAX_ITERS) -> S3Result:
    return Analytical(pb).run(max_iters)
