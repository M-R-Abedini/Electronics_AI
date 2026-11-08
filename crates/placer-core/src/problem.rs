// SPDX-License-Identifier: MIT
//! Placement state with incremental cost: weighted HPWL and pin-pair (MST) crossings.
//!
//! Legality (rectangle inside the outline, no same-side overlap with the
//! configured gap) is a hard predicate, not a cost term: the annealer and the
//! legalizer only ever keep legal states.  Courtyards are represented by their
//! local bounding boxes, which contain the real polygons, so a state legal here
//! is legal for the exact Python check too.

use crate::geom::{Outline, Rect, placed_rect, rot90, segments_cross};

#[derive(Clone, Debug)]
pub struct Part {
    pub local: Rect,
    pub side: u8,
    pub fixed: bool,
    pub can_rotate: bool,
    pub x: f64,
    pub y: f64,
    pub r: u8,
}

#[derive(Clone, Copy, Debug)]
pub struct Pin {
    pub part: usize,
    pub lx: f64,
    pub ly: f64,
}

#[derive(Clone, Debug)]
pub struct Net {
    pub pins: Vec<usize>,
    pub weight: f64,
    pub crossing: bool,
}

type Seg = ((f64, f64), (f64, f64));

/// Uniform grid of MST edges for crossing queries.
#[derive(Clone, Debug)]
struct EdgeIndex {
    x0: f64,
    y0: f64,
    cell: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
    edges: Vec<(Seg, u32)>, // (segment, net); net == u32::MAX marks a free slot
    free: Vec<u32>,
    net_edges: Vec<Vec<u32>>,
    stamp: Vec<u32>,
    epoch: u32,
}

impl EdgeIndex {
    fn new(bounds: Rect, cell: f64, nets: usize) -> Self {
        let nx = (((bounds.x1 - bounds.x0) / cell).ceil() as usize).max(1);
        let ny = (((bounds.y1 - bounds.y0) / cell).ceil() as usize).max(1);
        EdgeIndex {
            x0: bounds.x0,
            y0: bounds.y0,
            cell,
            nx,
            ny,
            cells: vec![Vec::new(); nx * ny],
            edges: Vec::new(),
            free: Vec::new(),
            net_edges: vec![Vec::new(); nets],
            stamp: Vec::new(),
            epoch: 0,
        }
    }

    fn span(&self, s: &Seg) -> (usize, usize, usize, usize) {
        let c =
            |v: f64, o: f64, n: usize| (((v - o) / self.cell).floor().max(0.0) as usize).min(n - 1);
        let (ax, bx) = (s.0.0.min(s.1.0), s.0.0.max(s.1.0));
        let (ay, by) = (s.0.1.min(s.1.1), s.0.1.max(s.1.1));
        (
            c(ax, self.x0, self.nx),
            c(bx, self.x0, self.nx),
            c(ay, self.y0, self.ny),
            c(by, self.y0, self.ny),
        )
    }

    fn insert(&mut self, s: Seg, net: u32) {
        let id = match self.free.pop() {
            Some(id) => {
                self.edges[id as usize] = (s, net);
                id
            }
            None => {
                self.edges.push((s, net));
                self.stamp.push(0);
                (self.edges.len() - 1) as u32
            }
        };
        let (i0, i1, j0, j1) = self.span(&s);
        for j in j0..=j1 {
            for i in i0..=i1 {
                self.cells[j * self.nx + i].push(id);
            }
        }
        self.net_edges[net as usize].push(id);
    }

    fn remove_net(&mut self, net: usize) {
        let ids = std::mem::take(&mut self.net_edges[net]);
        for id in ids {
            let s = self.edges[id as usize].0;
            let (i0, i1, j0, j1) = self.span(&s);
            for j in j0..=j1 {
                for i in i0..=i1 {
                    let cell = &mut self.cells[j * self.nx + i];
                    if let Some(k) = cell.iter().position(|&e| e == id) {
                        cell.swap_remove(k);
                    }
                }
            }
            self.edges[id as usize].1 = u32::MAX;
            self.free.push(id);
        }
    }

    /// Crossings between `segs` (of nets in `skip`) and indexed edges of nets not in `skip`.
    fn crossings_against(&mut self, segs: &[(Seg, u32)], skip: &[usize]) -> usize {
        let mut count = 0;
        for (s, net) in segs {
            self.epoch = self.epoch.wrapping_add(1);
            if self.epoch == 0 {
                self.stamp.iter_mut().for_each(|v| *v = 0);
                self.epoch = 1;
            }
            let (i0, i1, j0, j1) = self.span(s);
            for j in j0..=j1 {
                for i in i0..=i1 {
                    for &id in &self.cells[j * self.nx + i] {
                        if self.stamp[id as usize] == self.epoch {
                            continue;
                        }
                        self.stamp[id as usize] = self.epoch;
                        let (f, fnet) = self.edges[id as usize];
                        if fnet == *net || skip.contains(&(fnet as usize)) {
                            continue;
                        }
                        if segments_cross(s.0, s.1, f.0, f.1) {
                            count += 1;
                        }
                    }
                }
            }
        }
        count
    }
}

fn crossings_within(segs: &[(Seg, u32)]) -> usize {
    let mut c = 0;
    for i in 0..segs.len() {
        for j in i + 1..segs.len() {
            if segs[i].1 != segs[j].1
                && segments_cross(segs[i].0.0, segs[i].0.1, segs[j].0.0, segs[j].0.1)
            {
                c += 1;
            }
        }
    }
    c
}

/// Uniform grid of parts per side for overlap queries.
#[derive(Clone, Debug)]
struct PartIndex {
    x0: f64,
    y0: f64,
    cell: f64,
    nx: usize,
    ny: usize,
    cells: [Vec<Vec<u32>>; 2],
}

impl PartIndex {
    fn new(bounds: Rect, cell: f64) -> Self {
        let nx = (((bounds.x1 - bounds.x0) / cell).ceil() as usize).max(1);
        let ny = (((bounds.y1 - bounds.y0) / cell).ceil() as usize).max(1);
        PartIndex {
            x0: bounds.x0,
            y0: bounds.y0,
            cell,
            nx,
            ny,
            cells: [vec![Vec::new(); nx * ny], vec![Vec::new(); nx * ny]],
        }
    }

    fn span(&self, r: &Rect) -> (usize, usize, usize, usize) {
        let c =
            |v: f64, o: f64, n: usize| (((v - o) / self.cell).floor().max(0.0) as usize).min(n - 1);
        (
            c(r.x0, self.x0, self.nx),
            c(r.x1, self.x0, self.nx),
            c(r.y0, self.y0, self.ny),
            c(r.y1, self.y0, self.ny),
        )
    }

    fn insert(&mut self, side: u8, r: &Rect, id: u32) {
        let (i0, i1, j0, j1) = self.span(r);
        for j in j0..=j1 {
            for i in i0..=i1 {
                self.cells[side as usize][j * self.nx + i].push(id);
            }
        }
    }

    fn remove(&mut self, side: u8, r: &Rect, id: u32) {
        let (i0, i1, j0, j1) = self.span(r);
        for j in j0..=j1 {
            for i in i0..=i1 {
                let cell = &mut self.cells[side as usize][j * self.nx + i];
                if let Some(k) = cell.iter().position(|&e| e == id) {
                    cell.swap_remove(k);
                }
            }
        }
    }

    fn candidates(&self, side: u8, r: &Rect, out: &mut Vec<u32>) {
        out.clear();
        let (i0, i1, j0, j1) = self.span(r);
        for j in j0..=j1 {
            for i in i0..=i1 {
                out.extend_from_slice(&self.cells[side as usize][j * self.nx + i]);
            }
        }
        out.sort_unstable();
        out.dedup();
    }
}

pub const MAX_CROSSING_PINS: usize = 16;

#[derive(Clone, Debug)]
pub struct Problem {
    pub parts: Vec<Part>,
    pub pins: Vec<Pin>,
    pub nets: Vec<Net>,
    pub outline: Outline,
    pub gap: f64,
    pub w_hpwl: f64,
    pub w_cross: f64,
    pub part_nets: Vec<Vec<usize>>,
    pub placed: Vec<bool>, // parts present in the overlap index (all, except during legalization)
    bounds: Rect,
    index: PartIndex,
    edges: EdgeIndex,
    net_hpwl: Vec<f64>,
    hpwl_total: f64,
    crossings: usize,
    scratch: Vec<u32>,
}

impl Problem {
    pub fn new(
        parts: Vec<Part>,
        pins: Vec<Pin>,
        nets: Vec<Net>,
        outline: Outline,
        gap: f64,
        w_hpwl: f64,
        w_cross: f64,
    ) -> Self {
        let mut bounds = Rect {
            x0: f64::INFINITY,
            y0: f64::INFINITY,
            x1: f64::NEG_INFINITY,
            y1: f64::NEG_INFINITY,
        };
        for ring in &outline.outers {
            for &(x, y) in ring {
                bounds.x0 = bounds.x0.min(x);
                bounds.y0 = bounds.y0.min(y);
                bounds.x1 = bounds.x1.max(x);
                bounds.y1 = bounds.y1.max(y);
            }
        }
        for p in &parts {
            let r = placed_rect(&p.local, p.x, p.y, p.r);
            bounds.x0 = bounds.x0.min(r.x0);
            bounds.y0 = bounds.y0.min(r.y0);
            bounds.x1 = bounds.x1.max(r.x1);
            bounds.y1 = bounds.y1.max(r.y1);
        }
        let bounds = bounds.expand(5.0);
        let mut part_nets = vec![Vec::new(); parts.len()];
        for (n, net) in nets.iter().enumerate() {
            for &pin in &net.pins {
                let p = pins[pin].part;
                if !part_nets[p].contains(&n) {
                    part_nets[p].push(n);
                }
            }
        }
        let mut dims: Vec<f64> = parts
            .iter()
            .map(|p| (p.local.x1 - p.local.x0).max(p.local.y1 - p.local.y0))
            .collect();
        dims.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let cell = dims
            .get(dims.len() / 2)
            .copied()
            .unwrap_or(2.0)
            .clamp(1.0, 10.0)
            * 2.0;
        let n_nets = nets.len();
        let n_parts = parts.len();
        let mut pb = Problem {
            parts,
            pins,
            nets,
            outline,
            gap,
            w_hpwl,
            w_cross,
            part_nets,
            placed: vec![true; n_parts],
            bounds,
            index: PartIndex::new(bounds, cell),
            edges: EdgeIndex::new(bounds, edge_cell(&bounds), n_nets),
            net_hpwl: vec![0.0; n_nets],
            hpwl_total: 0.0,
            crossings: 0,
            scratch: Vec::new(),
        };
        pb.rebuild();
        pb
    }

    /// Recompute every cached quantity from the part poses.
    pub fn rebuild(&mut self) {
        self.index = PartIndex::new(self.bounds, self.index.cell);
        for i in 0..self.parts.len() {
            if self.placed[i] {
                let r = self.rect(i);
                self.index.insert(self.parts[i].side, &r, i as u32);
            }
        }
        self.hpwl_total = 0.0;
        for n in 0..self.nets.len() {
            self.net_hpwl[n] = self.compute_net_hpwl(n);
            self.hpwl_total += self.nets[n].weight * self.net_hpwl[n];
        }
        let nets = self.nets.len();
        self.edges = EdgeIndex::new(self.bounds, self.edges.cell, nets);
        let mut all = Vec::new();
        for n in 0..nets {
            if self.crossing_net(n) {
                for s in self.mst(n) {
                    self.edges.insert(s, n as u32);
                    all.push((s, n as u32));
                }
            }
        }
        // each crossing seen from both edges: count against the index, then halve
        let skip: Vec<usize> = Vec::new();
        self.crossings = self.edges.crossings_against(&all, &skip) / 2;
    }

    fn crossing_net(&self, n: usize) -> bool {
        self.nets[n].crossing
            && self.nets[n].pins.len() >= 2
            && self.nets[n].pins.len() <= MAX_CROSSING_PINS
    }

    #[inline]
    pub fn pin_xy(&self, pin: usize) -> (f64, f64) {
        let p = &self.pins[pin];
        let part = &self.parts[p.part];
        let (dx, dy) = rot90(p.lx, p.ly, part.r);
        (part.x + dx, part.y + dy)
    }

    /// Courtyard box of part `i`, grown by half the gap on every side.
    #[inline]
    pub fn rect(&self, i: usize) -> Rect {
        let p = &self.parts[i];
        placed_rect(&p.local, p.x, p.y, p.r).expand(self.gap / 2.0)
    }

    fn compute_net_hpwl(&self, n: usize) -> f64 {
        let pins = &self.nets[n].pins;
        if pins.len() < 2 {
            return 0.0;
        }
        let (mut x0, mut x1, mut y0, mut y1) = (
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
        );
        for &pin in pins {
            let (x, y) = self.pin_xy(pin);
            x0 = x0.min(x);
            x1 = x1.max(x);
            y0 = y0.min(y);
            y1 = y1.max(y);
        }
        (x1 - x0) + (y1 - y0)
    }

    /// Prim's MST over the net's pins, deterministic for a fixed pin order.
    pub fn mst(&self, n: usize) -> Vec<Seg> {
        let pts: Vec<(f64, f64)> = self.nets[n].pins.iter().map(|&p| self.pin_xy(p)).collect();
        let k = pts.len();
        if k < 2 {
            return Vec::new();
        }
        let mut inside = vec![false; k];
        let mut best = vec![f64::INFINITY; k];
        let mut parent = vec![usize::MAX; k];
        best[0] = 0.0;
        let mut out = Vec::with_capacity(k - 1);
        for _ in 0..k {
            let mut u = usize::MAX;
            for i in 0..k {
                if !inside[i] && (u == usize::MAX || best[i] < best[u]) {
                    u = i;
                }
            }
            inside[u] = true;
            if parent[u] != usize::MAX && pts[parent[u]] != pts[u] {
                out.push((pts[parent[u]], pts[u]));
            }
            for v in 0..k {
                if !inside[v] {
                    let d = ((pts[v].0 - pts[u].0).powi(2) + (pts[v].1 - pts[u].1).powi(2)).sqrt();
                    if d < best[v] {
                        best[v] = d;
                        parent[v] = u;
                    }
                }
            }
        }
        out
    }

    pub fn hpwl(&self) -> f64 {
        self.hpwl_total
    }

    pub fn crossings(&self) -> usize {
        self.crossings
    }

    pub fn cost(&self) -> f64 {
        self.w_hpwl * self.hpwl_total + self.w_cross * self.crossings as f64
    }

    // ------------------------------------------------------------- legality
    /// Is part `i` legal at its current pose (inside the board, no same-side overlap), ignoring `skip`?
    pub fn legal_at(&mut self, i: usize, skip: Option<usize>) -> bool {
        let r = self.rect(i);
        let inner = Rect {
            x0: r.x0 + self.gap / 2.0,
            y0: r.y0 + self.gap / 2.0,
            x1: r.x1 - self.gap / 2.0,
            y1: r.y1 - self.gap / 2.0,
        };
        if !self.parts[i].fixed && !self.outline.contains_rect(&inner) {
            return false;
        }
        let side = self.parts[i].side;
        let mut cand = std::mem::take(&mut self.scratch);
        self.index.candidates(side, &r, &mut cand);
        let mut ok = true;
        for &j in &cand {
            let j = j as usize;
            if j == i || Some(j) == skip || !self.placed[j] {
                continue;
            }
            if self.parts[j].fixed && self.parts[i].fixed {
                continue; // fixed parts are the designer's: never blame them on each other
            }
            if r.overlap(&self.rect(j)) > 1e-9 {
                ok = false;
                break;
            }
        }
        self.scratch = cand;
        ok
    }

    pub fn illegal_parts(&mut self) -> Vec<usize> {
        (0..self.parts.len())
            .filter(|&i| !self.parts[i].fixed && !self.legal_at(i, None))
            .collect()
    }

    // --------------------------------------------------------------- moves
    /// Move part `i` (pose already changed by the caller from `old`) and return the cost delta,
    /// updating all caches.  `undo` restores the previous caches exactly.
    pub fn apply_pose(&mut self, i: usize, x: f64, y: f64, r: u8) -> Undo {
        let old = (self.parts[i].x, self.parts[i].y, self.parts[i].r);
        let old_rect = self.rect(i);
        if self.placed[i] {
            self.index.remove(self.parts[i].side, &old_rect, i as u32);
        }
        self.parts[i].x = x;
        self.parts[i].y = y;
        self.parts[i].r = r;
        if self.placed[i] {
            let new_rect = self.rect(i);
            self.index.insert(self.parts[i].side, &new_rect, i as u32);
        }
        let nets = self.part_nets[i].clone();
        let mut net_old = Vec::with_capacity(nets.len());
        for &n in &nets {
            let h = self.compute_net_hpwl(n);
            net_old.push((n, self.net_hpwl[n]));
            self.hpwl_total += self.nets[n].weight * (h - self.net_hpwl[n]);
            self.net_hpwl[n] = h;
        }
        let cross_nets: Vec<usize> = nets
            .iter()
            .copied()
            .filter(|&n| self.crossing_net(n))
            .collect();
        let mut old_edges = Vec::new();
        let mut cross_delta: isize = 0;
        if !cross_nets.is_empty() {
            for &n in &cross_nets {
                for &id in &self.edges.net_edges[n] {
                    old_edges.push(self.edges.edges[id as usize]);
                }
            }
            let before = self.edges.crossings_against(&old_edges, &cross_nets)
                + crossings_within(&old_edges);
            for &n in &cross_nets {
                self.edges.remove_net(n);
            }
            let mut new_edges = Vec::new();
            for &n in &cross_nets {
                for s in self.mst(n) {
                    new_edges.push((s, n as u32));
                }
            }
            let after = self.edges.crossings_against(&new_edges, &cross_nets)
                + crossings_within(&new_edges);
            for (s, n) in &new_edges {
                self.edges.insert(*s, *n);
            }
            cross_delta = after as isize - before as isize;
            self.crossings = (self.crossings as isize + cross_delta) as usize;
        }
        Undo {
            part: i,
            old,
            net_old,
            cross_nets,
            old_edges,
            cross_delta,
        }
    }

    pub fn undo(&mut self, u: Undo) {
        let i = u.part;
        let cur_rect = self.rect(i);
        if self.placed[i] {
            self.index.remove(self.parts[i].side, &cur_rect, i as u32);
        }
        self.parts[i].x = u.old.0;
        self.parts[i].y = u.old.1;
        self.parts[i].r = u.old.2;
        if self.placed[i] {
            let r = self.rect(i);
            self.index.insert(self.parts[i].side, &r, i as u32);
        }
        for (n, h) in u.net_old {
            self.hpwl_total += self.nets[n].weight * (h - self.net_hpwl[n]);
            self.net_hpwl[n] = h;
        }
        if !u.cross_nets.is_empty() {
            for &n in &u.cross_nets {
                self.edges.remove_net(n);
            }
            for (s, n) in u.old_edges {
                self.edges.insert(s, n);
            }
            self.crossings = (self.crossings as isize - u.cross_delta) as usize;
        }
    }

    /// Take part `i` out of / back into the overlap index (legalization order).
    pub fn set_placed(&mut self, i: usize, placed: bool) {
        if self.placed[i] == placed {
            return;
        }
        let r = self.rect(i);
        if placed {
            self.index.insert(self.parts[i].side, &r, i as u32);
        } else {
            self.index.remove(self.parts[i].side, &r, i as u32);
        }
        self.placed[i] = placed;
    }

    pub fn bounds(&self) -> Rect {
        self.bounds
    }
}

pub struct Undo {
    part: usize,
    old: (f64, f64, u8),
    net_old: Vec<(usize, f64)>,
    cross_nets: Vec<usize>,
    old_edges: Vec<(Seg, u32)>,
    cross_delta: isize,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn square() -> Outline {
        Outline {
            outers: vec![vec![(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]],
            holes: vec![],
        }
    }

    fn part(x: f64, y: f64) -> Part {
        Part {
            local: Rect {
                x0: -1.0,
                y0: -0.5,
                x1: 1.0,
                y1: 0.5,
            },
            side: 0,
            fixed: false,
            can_rotate: true,
            x,
            y,
            r: 0,
        }
    }

    fn problem() -> Problem {
        // nets A: p0 -> p1, B: p2 -> p3, placed to cross once
        let parts = vec![
            part(2.0, 2.0),
            part(12.0, 12.0),
            part(2.0, 12.0),
            part(12.0, 2.0),
        ];
        let pins = vec![
            Pin {
                part: 0,
                lx: 0.5,
                ly: 0.0,
            },
            Pin {
                part: 1,
                lx: -0.5,
                ly: 0.0,
            },
            Pin {
                part: 2,
                lx: 0.5,
                ly: 0.0,
            },
            Pin {
                part: 3,
                lx: -0.5,
                ly: 0.0,
            },
        ];
        let nets = vec![
            Net {
                pins: vec![0, 1],
                weight: 1.0,
                crossing: true,
            },
            Net {
                pins: vec![2, 3],
                weight: 1.0,
                crossing: true,
            },
        ];
        Problem::new(parts, pins, nets, square(), 0.0, 1.0, 10.0)
    }

    #[test]
    fn incremental_matches_full_and_undo_restores() {
        let mut p = problem();
        assert_eq!(p.crossings(), 1);
        let base = (p.hpwl(), p.crossings());
        let u = p.apply_pose(3, 2.0, 16.0, 1);
        let inc = (p.hpwl(), p.crossings());
        let mut full = p.clone();
        full.rebuild();
        assert!((inc.0 - full.hpwl()).abs() < 1e-9);
        assert_eq!(inc.1, full.crossings());
        assert_eq!(inc.1, 0);
        p.undo(u);
        assert!((p.hpwl() - base.0).abs() < 1e-9);
        assert_eq!(p.crossings(), base.1);
    }

    #[test]
    fn legality_detects_overlap_and_outline() {
        let mut p = problem();
        assert!(p.illegal_parts().is_empty());
        let _ = p.apply_pose(1, 2.5, 2.0, 0); // onto part 0
        assert_eq!(p.illegal_parts(), vec![0, 1]);
        let _ = p.apply_pose(1, 19.5, 10.0, 0); // sticks out of the board
        assert_eq!(p.illegal_parts(), vec![1]);
    }
}

/// Edge-grid cell: about 1/40 of the board's longer side, at least 2 mm.
/// A fixed 2 mm cell made long edges on a 300 mm board walk hundreds of cells per query.
fn edge_cell(bounds: &Rect) -> f64 {
    ((bounds.x1 - bounds.x0).max(bounds.y1 - bounds.y0) / 40.0).max(2.0)
}
