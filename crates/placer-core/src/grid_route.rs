// SPDX-License-Identifier: MIT
//! Grid A* for S6a (architecture 7.1): one centreline on a multi-layer grid.
//!
//! The caller inflates every obstacle by the half-width of what is being
//! routed (for a pair: one trace, the gap and the other trace) plus the
//! clearance, so a free cell means the whole object fits there. Moves are
//! octilinear (cost 1 or sqrt 2); a change of direction costs extra (45 deg
//! less than 90 deg, sharper turns are not allowed), which keeps the line
//! straight and the pair's legs equal. A layer change is allowed only where
//! the caller marked a via site (`via_ok`), at `via_cost`.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

const DIRS: [(i32, i32); 8] = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)];
const NO_DIR: usize = 8;

#[derive(Copy, Clone, PartialEq)]
struct Node {
    f: f64,
    g: f64,
    state: usize,
}

impl Eq for Node {}

impl Ord for Node {
    fn cmp(&self, other: &Self) -> Ordering {
        // min-heap on f, ties broken toward larger g (deeper nodes) for determinism and speed
        other.f.partial_cmp(&self.f).unwrap_or(Ordering::Equal)
            .then_with(|| self.g.partial_cmp(&other.g).unwrap_or(Ordering::Equal))
            .then_with(|| other.state.cmp(&self.state))
    }
}

impl PartialOrd for Node {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

pub struct Grid<'a> {
    pub nx: usize,
    pub ny: usize,
    pub nl: usize,
    pub blocked: &'a [u8], // index (l * ny + y) * nx + x; nonzero = blocked
    pub via_ok: &'a [u8],  // index y * nx + x; nonzero = a via (pair) fits here on every layer
}

pub struct Costs {
    pub via: f64,
    pub turn45: f64,
    pub turn90: f64,
}

fn octile(dx: i64, dy: i64) -> f64 {
    let (a, b) = (dx.unsigned_abs() as f64, dy.unsigned_abs() as f64);
    let (lo, hi) = if a < b { (a, b) } else { (b, a) };
    hi - lo + lo * std::f64::consts::SQRT_2
}

/// Shortest path from any start to any goal; cells as (x, y, layer). None if unreachable.
pub fn astar(grid: &Grid, starts: &[(usize, usize, usize)], goals: &[(usize, usize, usize)], c: &Costs,
             max_expansions: usize) -> Option<Vec<(usize, usize, usize)>> {
    let (nx, ny, nl) = (grid.nx, grid.ny, grid.nl);
    let cells = nx * ny * nl;
    let n_states = cells * 9;
    let cell = |x: usize, y: usize, l: usize| (l * ny + y) * nx + x;
    let free = |x: i64, y: i64, l: usize| -> bool {
        x >= 0 && y >= 0 && (x as usize) < nx && (y as usize) < ny && grid.blocked[cell(x as usize, y as usize, l)] == 0
    };
    let mut is_goal = vec![false; cells];
    for &(x, y, l) in goals {
        if x < nx && y < ny && l < nl {
            is_goal[cell(x, y, l)] = true;
        }
    }
    let goal_xy: Vec<(i64, i64)> = goals.iter().map(|&(x, y, _)| (x as i64, y as i64)).collect();
    let h = |x: usize, y: usize| -> f64 {
        goal_xy.iter().map(|&(gx, gy)| octile(gx - x as i64, gy - y as i64)).fold(f64::INFINITY, f64::min)
    };
    let mut g_best = vec![f64::INFINITY; n_states];
    let mut parent = vec![usize::MAX; n_states];
    let mut heap = BinaryHeap::new();
    for &(x, y, l) in starts {
        if x < nx && y < ny && l < nl {
            let s = cell(x, y, l) * 9 + NO_DIR;
            g_best[s] = 0.0;
            heap.push(Node { f: h(x, y), g: 0.0, state: s });
        }
    }
    let mut expansions = 0usize;
    while let Some(Node { g, state, .. }) = heap.pop() {
        if g > g_best[state] {
            continue;
        }
        let ci = state / 9;
        let dir = state % 9;
        let (x, rest) = (ci % nx, ci / nx);
        let (y, l) = (rest % ny, rest / ny);
        if is_goal[ci] {
            let mut path = Vec::new();
            let mut s = state;
            loop {
                let c2 = s / 9;
                let p = (c2 % nx, (c2 / nx) % ny, c2 / (nx * ny));
                if path.last() != Some(&p) {
                    path.push(p);
                }
                if parent[s] == usize::MAX {
                    break;
                }
                s = parent[s];
            }
            path.reverse();
            return Some(path);
        }
        expansions += 1;
        if expansions > max_expansions {
            return None;
        }
        for (d, &(dx, dy)) in DIRS.iter().enumerate() {
            let turn = if dir == NO_DIR { 0 } else { let t = (d as i32 - dir as i32).rem_euclid(8); t.min(8 - t) };
            if turn > 2 {
                continue; // no sharper than 90 deg
            }
            let (x2, y2) = (x as i64 + dx as i64, y as i64 + dy as i64);
            if !free(x2, y2, l) {
                continue;
            }
            if dx != 0 && dy != 0 && !(free(x as i64 + dx as i64, y as i64, l) && free(x as i64, y as i64 + dy as i64, l)) {
                continue; // no corner cutting
            }
            let step = if dx != 0 && dy != 0 { std::f64::consts::SQRT_2 } else { 1.0 };
            let g2 = g + step + match turn { 1 => c.turn45, 2 => c.turn90, _ => 0.0 };
            let s2 = cell(x2 as usize, y2 as usize, l) * 9 + d;
            if g2 < g_best[s2] {
                g_best[s2] = g2;
                parent[s2] = state;
                heap.push(Node { f: g2 + h(x2 as usize, y2 as usize), g: g2, state: s2 });
            }
        }
        if grid.via_ok[y * nx + x] != 0 {
            for l2 in 0..nl {
                if l2 == l || grid.blocked[cell(x, y, l2)] != 0 {
                    continue;
                }
                let g2 = g + c.via;
                let s2 = cell(x, y, l2) * 9 + NO_DIR; // direction free after a via
                if g2 < g_best[s2] {
                    g_best[s2] = g2;
                    parent[s2] = state;
                    heap.push(Node { f: g2 + h(x, y), g: g2, state: s2 });
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn straight_line_and_wall_with_via() {
        let (nx, ny, nl) = (20usize, 5usize, 2usize);
        let mut blocked = vec![0u8; nx * ny * nl];
        for y in 0..ny {
            blocked[(0 * ny + y) * nx + 10] = 1; // wall on layer 0 at x = 10
        }
        let mut via_ok = vec![0u8; nx * ny];
        via_ok[2 * nx + 5] = 1;
        let g = Grid { nx, ny, nl, blocked: &blocked, via_ok: &via_ok };
        let c = Costs { via: 5.0, turn45: 0.2, turn90: 0.5 };
        let p = astar(&g, &[(0, 2, 0)], &[(19, 2, 0)], &c, 1_000_000);
        assert!(p.is_none(), "no way back to layer 0 after the wall");
        let p = astar(&g, &[(0, 2, 0)], &[(19, 2, 1)], &c, 1_000_000).unwrap();
        assert_eq!(p.first(), Some(&(0, 2, 0)));
        assert_eq!(p.last(), Some(&(19, 2, 1)));
        assert!(p.iter().any(|&(x, _, l)| x == 5 && l == 1), "changes layer at the via site");
    }
}
