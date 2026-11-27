// SPDX-License-Identifier: MIT
//! S4 legalization and S5 legality-preserving simulated annealing.

use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;

use crate::geom::placed_rect;
use crate::problem::Problem;

/// Place movable parts, largest first, at the nearest legal position to their current pose.
///
/// Fixed parts are obstacles from the start.  Positions are searched on rings of
/// `step` around the target, up to `max_radius`; the current rotation is tried
/// first, then the others.  Rings are clamped to the board outline bounding box
/// and use a progressive stride beyond 30 rings to keep large-board retries fast
/// while staying exact near the target.  Returns the parts that found no legal
/// position (left at their target).  Caches are rebuilt at the end.
pub fn legalize(p: &mut Problem, step: f64, max_radius: f64, first: &[usize]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..p.parts.len()).filter(|&i| !p.parts[i].fixed).collect();
    order.sort_by(|&a, &b| {
        let area = |i: usize| p.parts[i].local.area();
        let pa = !first.contains(&a);
        let pb = !first.contains(&b);
        pa.cmp(&pb).then(area(b).partial_cmp(&area(a)).unwrap()).then(a.cmp(&b))
    });
    for &i in &order {
        p.set_placed(i, false);
    }
    let k_max = (max_radius / step).ceil() as i64;
    let bounds = p.outline_bounds();
    let has_bounds = bounds.x0.is_finite() && bounds.x1 > bounds.x0 && bounds.y1 > bounds.y0;
    let mut ring_buf: Vec<(i64, i64)> = Vec::new();
    let mut failed = Vec::new();
    for &i in &order {
        let (tx, ty, tr) = (p.parts[i].x, p.parts[i].y, p.parts[i].r);
        let mut rotations = vec![tr];
        if p.parts[i].can_rotate {
            rotations.extend((1..4).map(|d| (tr + d) & 3));
        }
        let mut found = false;
        'rot: for &r in &rotations {
            p.parts[i].r = r;
            let (min_x, max_x, min_y, max_y, k_start, k_limit) = if has_bounds {
                let lr = placed_rect(&p.parts[i].local, 0.0, 0.0, r);
                let min_x = bounds.x0 - lr.x0;
                let max_x = bounds.x1 - lr.x1;
                let min_y = bounds.y0 - lr.y0;
                let max_y = bounds.y1 - lr.y1;
                if min_x > max_x + 1e-6 || min_y > max_y + 1e-6 {
                    continue;
                }
                let out_d = 0.0_f64
                    .max(min_x - tx)
                    .max(tx - max_x)
                    .max(min_y - ty)
                    .max(ty - max_y);
                let far_d = (tx - min_x)
                    .abs()
                    .max((max_x - tx).abs())
                    .max((ty - min_y).abs())
                    .max((max_y - ty).abs());
                let ks = ((out_d / step).floor() as i64).max(0);
                let kl = k_max.min((far_d / step).ceil() as i64 + 1);
                (min_x - 1e-3, max_x + 1e-3, min_y - 1e-3, max_y + 1e-3, ks, kl)
            } else {
                (f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, 0, k_max)
            };
            let mut k = k_start;
            while k <= k_limit {
                let dk = ring_stride(k);
                fill_clamped_ring(
                    &mut ring_buf,
                    k,
                    dk,
                    tx,
                    ty,
                    step,
                    min_x,
                    max_x,
                    min_y,
                    max_y,
                );
                for &(a, b) in &ring_buf {
                    p.parts[i].x = round3(tx + a as f64 * step);
                    p.parts[i].y = round3(ty + b as f64 * step);
                    if p.legal_at(i, None) {
                        p.set_placed(i, true);
                        found = true;
                        break 'rot;
                    }
                }
                k += dk;
            }
        }
        if !found {
            p.parts[i].x = tx;
            p.parts[i].y = ty;
            p.parts[i].r = tr;
            p.set_placed(i, true);
            failed.push(i);
        }
    }
    p.rebuild();
    failed
}

#[inline]
fn ring_stride(k: i64) -> i64 {
    if k <= 200 {
        1
    } else if k <= 400 {
        2
    } else if k <= 800 {
        4
    } else {
        8
    }
}

#[allow(clippy::too_many_arguments)]
fn fill_clamped_ring(
    out: &mut Vec<(i64, i64)>,
    k: i64,
    dk: i64,
    tx: f64,
    ty: f64,
    step: f64,
    min_x: f64,
    max_x: f64,
    min_y: f64,
    max_y: f64,
) {
    out.clear();
    if k == 0 {
        if tx >= min_x && tx <= max_x && ty >= min_y && ty <= max_y {
            out.push((0, 0));
        }
        return;
    }
    let a_lo = if min_x.is_finite() {
        ((min_x - tx) / step).ceil() as i64
    } else {
        -k
    }
    .max(-k);
    let a_hi = if max_x.is_finite() {
        ((max_x - tx) / step).floor() as i64
    } else {
        k
    }
    .min(k);
    let b_lo = if min_y.is_finite() {
        ((min_y - ty) / step).ceil() as i64
    } else {
        -k
    }
    .max(-k);
    let b_hi = if max_y.is_finite() {
        ((max_y - ty) / step).floor() as i64
    } else {
        k
    }
    .min(k);

    if a_lo <= a_hi {
        for &b in &[-k, k] {
            let y = ty + b as f64 * step;
            if y >= min_y && y <= max_y {
                let mut a = a_lo;
                while a <= a_hi {
                    out.push((a, b));
                    a += dk;
                }
            }
        }
    }
    let inner_b_lo = b_lo.max(-k + 1);
    let inner_b_hi = b_hi.min(k - 1);
    if inner_b_lo <= inner_b_hi {
        for &a in &[-k, k] {
            let x = tx + a as f64 * step;
            if x >= min_x && x <= max_x {
                let mut b = inner_b_lo;
                while b <= inner_b_hi {
                    out.push((a, b));
                    b += dk;
                }
            }
        }
    }
    out.sort_unstable_by_key(|&(a, b)| (a * a + b * b, a, b));
}

#[inline]
fn round3(v: f64) -> f64 {
    (v * 1000.0).round() / 1000.0
}

#[derive(Clone, Debug, Default)]
pub struct AnnealStats {
    pub iterations: usize,
    pub accepted: usize,
    pub illegal: usize,
    pub start_cost: f64,
    pub best_cost: f64,
    pub t0: f64,
}

#[inline]
fn body_center(part: &crate::problem::Part) -> (f64, f64) {
    let r = crate::geom::placed_rect(&part.local, part.x, part.y, part.r);
    (0.5 * (r.x0 + r.x1), 0.5 * (r.y0 + r.y1))
}

#[inline]
fn pose_for_center(part: &crate::problem::Part, cx: f64, cy: f64, rot: u8) -> (f64, f64) {
    let l = crate::geom::placed_rect(&part.local, 0.0, 0.0, rot);
    (
        round3(cx - 0.5 * (l.x0 + l.x1)),
        round3(cy - 0.5 * (l.y0 + l.y1)),
    )
}

/// Legality-preserving annealing: every accepted state is legal (requires a legal start).
pub fn anneal(
    p: &mut Problem,
    iters: usize,
    seed: u64,
    r0: f64,
    r1: f64,
    t0: f64,
    swap: bool,
) -> AnnealStats {
    let movable: Vec<usize> = (0..p.parts.len()).filter(|&i| !p.parts[i].fixed).collect();
    let mut stats = AnnealStats {
        start_cost: p.cost(),
        best_cost: p.cost(),
        ..Default::default()
    };
    if movable.is_empty() || iters == 0 {
        return stats;
    }
    let mut rng = ChaCha8Rng::seed_from_u64(seed);
    let t0 = if t0 > 0.0 {
        t0
    } else {
        estimate_t0(p, &movable, &mut rng, r0)
    };
    stats.t0 = t0;
    let t_end = t0 * 1e-4;
    let mut best: Vec<(f64, f64, u8)> = p.parts.iter().map(|q| (q.x, q.y, q.r)).collect();
    let mut movable_by_side: [Vec<usize>; 2] = [Vec::new(), Vec::new()];
    for &m in &movable {
        let s = (p.parts[m].side as usize).min(1);
        movable_by_side[s].push(m);
    }
    let n_parts = p.parts.len();
    let mut affinity_nets: Vec<Vec<usize>> = vec![Vec::new(); n_parts];
    for (ni, net) in p.nets.iter().enumerate() {
        if net.nearest && net.pins.len() >= 2 {
            let owner = p.pins[net.pins[0]].part;
            if !p.parts[owner].fixed {
                affinity_nets[owner].push(ni);
            }
        }
    }
    let mut part_cluster: Vec<Option<usize>> = vec![None; n_parts];
    for (ci, cl) in p.clusters.iter().enumerate() {
        if let Some(&anchor) = cl.first()
            && anchor < n_parts
            && !p.parts[anchor].fixed
            && cl.len() >= 2
        {
            part_cluster[anchor] = Some(ci);
        }
    }
    let mut group_moves: Vec<(usize, f64, f64, u8)> = Vec::with_capacity(16);
    let mut undos = Vec::with_capacity(16);
    for k in 0..iters {
        let f = k as f64 / iters as f64;
        let t = t0 * (t_end / t0).powf(f);
        let radius = r0 * (r1 / r0).powf(f);
        let i = movable[rng.random_range(0..movable.len())];
        let before = p.cost();
        let kind = rng.random_range(0..10u8);
        let (x, y, r) = (p.parts[i].x, p.parts[i].y, p.parts[i].r);
        undos.clear();
        if kind == 4 && part_cluster[i].is_some() {
            // Soft cluster move: translate or rigid-rotate anchor + tightly coupled movable children atomically.
            let cid = part_cluster[i].unwrap();
            group_moves.clear();
            let do_rotate = p.parts[i].can_rotate && rng.random_bool(0.5);
            if do_rotate {
                let dr = rng.random_range(1..4u8);
                let (acx, acy) = body_center(&p.parts[i]);
                let mut valid_rot = true;
                for &m in &p.clusters[cid] {
                    if p.parts[m].fixed {
                        continue;
                    }
                    if !p.parts[m].can_rotate {
                        valid_rot = false;
                        break;
                    }
                    let (mcx, mcy) = body_center(&p.parts[m]);
                    let (rdx, rdy) = crate::geom::rot90(mcx - acx, mcy - acy, dr);
                    let nr = (p.parts[m].r + dr) & 3;
                    let (nx, ny) = pose_for_center(&p.parts[m], acx + rdx, acy + rdy, nr);
                    group_moves.push((m, nx, ny, nr));
                }
                if !valid_rot || group_moves.is_empty() {
                    continue;
                }
            } else {
                let dx = round3(rng.random_range(-radius..=radius));
                let dy = round3(rng.random_range(-radius..=radius));
                for &m in &p.clusters[cid] {
                    if !p.parts[m].fixed {
                        group_moves.push((m, round3(p.parts[m].x + dx), round3(p.parts[m].y + dy), p.parts[m].r));
                    }
                }
                if group_moves.is_empty() {
                    continue;
                }
            }
            stats.iterations += 1;
            if !p.can_place_group(&group_moves) {
                stats.illegal += 1;
                continue;
            }
            for &(m, nx, ny, nr) in &group_moves {
                undos.push(p.apply_pose(m, nx, ny, nr));
            }
        } else if kind == 5 && !affinity_nets[i].is_empty() {
            // Connected-target affinity jump: lets trapped decaps/crystals hop over IC bodies.
            let affs = &affinity_nets[i];
            let ni = affs[rng.random_range(0..affs.len())];
            let pins = &p.nets[ni].pins;
            let tpin = pins[1 + rng.random_range(0..(pins.len() - 1))];
            let (tx, ty) = p.pin_xy(tpin);
            let jr = radius.clamp(0.5, 3.0);
            let nr = if p.parts[i].can_rotate {
                rng.random_range(0..4u8)
            } else {
                r
            };
            let cx = tx + rng.random_range(-jr..=jr);
            let cy = ty + rng.random_range(-jr..=jr);
            let (nx, ny) = pose_for_center(&p.parts[i], cx, cy, nr);
            stats.iterations += 1;
            if !p.can_place_at(i, nx, ny, nr, None) {
                stats.illegal += 1;
                continue;
            }
            undos.push(p.apply_pose(i, nx, ny, nr));
        } else if (kind == 3 || (kind == 2 && f >= 0.55))
            && f >= 0.25
            && p.w_reg > 0.0
            && p.reg_groups.get(i).copied().unwrap_or(0) > 0
            && p.reg_members[p.reg_groups[i] as usize].len() >= 2
        {
            // Peer-align move: snap passive X or Y onto a nearby same-group peer (and align axis).
            let g = p.reg_groups[i] as usize;
            let members = &p.reg_members[g];
            let mut best_peer = None;
            let mut best_d2 = 64.0_f64; // within 8.0 mm ALIGN_RADIUS_MM
            for &m in members {
                if m == i {
                    continue;
                }
                let dx = p.parts[m].x - x;
                let dy = p.parts[m].y - y;
                let d2 = dx * dx + dy * dy;
                if d2 <= best_d2 && (best_peer.is_none() || rng.random_bool(0.35)) {
                    best_d2 = d2;
                    best_peer = Some(m);
                }
            }
            let j = best_peer.unwrap_or_else(|| {
                let idx = rng.random_range(0..(members.len() - 1));
                if members[idx] == i {
                    members[members.len() - 1]
                } else {
                    members[idx]
                }
            });
            let (jx, jy, jr) = (p.parts[j].x, p.parts[j].y, p.parts[j].r);
            let snap_x = if (jx - x).abs() <= (jy - y).abs() {
                rng.random_bool(0.75)
            } else {
                rng.random_bool(0.25)
            };
            let jit = if rng.random_bool(0.55) { 0.0 } else { radius.min(1.2) };
            let (nx, ny) = if snap_x {
                (jx, if jit == 0.0 { y } else { round3(y + rng.random_range(-jit..=jit)) })
            } else {
                (if jit == 0.0 { x } else { round3(x + rng.random_range(-jit..=jit)) }, jy)
            };
            let nr = if p.parts[i].can_rotate && rng.random_bool(0.70) {
                (jr & 1) | (r & 2)
            } else {
                r
            };
            stats.iterations += 1;
            if !p.can_place_at(i, nx, ny, nr, None) {
                stats.illegal += 1;
                continue;
            }
            undos.push(p.apply_pose(i, nx, ny, nr));
        } else if kind < 6 || (kind < 8 && !p.parts[i].can_rotate) || (kind >= 8 && !swap) {
            let nx = round3(x + rng.random_range(-radius..=radius));
            let ny = round3(y + rng.random_range(-radius..=radius));
            stats.iterations += 1;
            if !p.can_place_at(i, nx, ny, r, None) {
                stats.illegal += 1;
                continue;
            }
            undos.push(p.apply_pose(i, nx, ny, r));
        } else if kind < 8 {
            // Body-center-preserving rotation avoids spurious collisions on off-center footprints.
            let nr = (r + rng.random_range(1..4u8)) & 3;
            let (cx, cy) = body_center(&p.parts[i]);
            let (nx, ny) = pose_for_center(&p.parts[i], cx, cy, nr);
            stats.iterations += 1;
            if !p.can_place_at(i, nx, ny, nr, None) {
                stats.illegal += 1;
                continue;
            }
            undos.push(p.apply_pose(i, nx, ny, nr));
        } else {
            let side = (p.parts[i].side as usize).min(1);
            let peers = &movable_by_side[side];
            if peers.len() <= 1 {
                continue;
            }
            let pick = rng.random_range(0..(peers.len() - 1));
            let j = if peers[pick] == i {
                peers[peers.len() - 1]
            } else {
                peers[pick]
            };
            let (xj, yj, rj) = (p.parts[j].x, p.parts[j].y, p.parts[j].r);
            stats.iterations += 1;
            if !p.can_swap(i, xj, yj, r, j, x, y, rj) {
                stats.illegal += 1;
                continue;
            }
            undos.push(p.apply_pose(i, xj, yj, r));
            undos.push(p.apply_pose(j, x, y, rj));
        }
        let delta = p.cost() - before;
        let accept = delta <= 0.0 || rng.random::<f64>() < (-delta / t).exp();
        if accept {
            stats.accepted += 1;
            if p.cost() < stats.best_cost - 1e-12 {
                stats.best_cost = p.cost();
                for (q, b) in p.parts.iter().zip(best.iter_mut()) {
                    *b = (q.x, q.y, q.r);
                }
            }
        } else {
            while let Some(u) = undos.pop() {
                p.undo(u);
            }
        }
    }
    for (q, b) in p.parts.iter_mut().zip(best.iter()) {
        q.x = b.0;
        q.y = b.1;
        q.r = b.2;
    }
    p.rebuild();
    stats.best_cost = p.cost();
    stats
}

fn estimate_t0(p: &mut Problem, movable: &[usize], rng: &mut ChaCha8Rng, r0: f64) -> f64 {
    let mut up = Vec::new();
    for _ in 0..400 {
        let i = movable[rng.random_range(0..movable.len())];
        let (x, y, r) = (p.parts[i].x, p.parts[i].y, p.parts[i].r);
        let before = p.cost();
        let u = p.apply_pose(
            i,
            x + rng.random_range(-r0..=r0),
            y + rng.random_range(-r0..=r0),
            r,
        );
        let d = p.cost() - before;
        p.undo(u);
        if d > 0.0 {
            up.push(d);
        }
    }
    if up.is_empty() {
        1.0
    } else {
        up.iter().sum::<f64>() / up.len() as f64
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geom::{Outline, Rect};
    use crate::problem::{Net, Part, Pin};

    fn chain(n: usize) -> Problem {
        let local = Rect {
            x0: -1.0,
            y0: -0.5,
            x1: 1.0,
            y1: 0.5,
        };
        let parts: Vec<Part> = (0..n)
            .map(|i| Part {
                local,
                outline_local: local,
                obstacle_local: local,
                sub_rects: Vec::new(),
                cavity_eligible: true,
                opp_local: None,
                side: 0,
                fixed: i == 0,
                can_rotate: true,
                x: 10.0,
                y: 10.0,
                r: 0,
            })
            .collect();
        let pins: Vec<Pin> = (0..n)
            .flat_map(|i| {
                [
                    Pin {
                        part: i,
                        lx: -0.8,
                        ly: 0.0,
                    },
                    Pin {
                        part: i,
                        lx: 0.8,
                        ly: 0.0,
                    },
                ]
            })
            .collect();
        let nets: Vec<Net> = (0..n - 1)
            .map(|i| Net {
                pins: vec![2 * i + 1, 2 * i + 2],
                weight: 1.0,
                crossing: true,
                nearest: false,
            })
            .collect();
        let outline = Outline {
            outers: vec![vec![(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (0.0, 30.0)]],
            holes: vec![],
        };
        Problem::new(parts, pins, nets, outline, 0.1, 1.0, 5.0)
    }

    #[test]
    fn legalize_then_anneal_stays_legal_and_improves() {
        let mut p = chain(12);
        let failed = legalize(&mut p, 0.25, 20.0, &[]);
        assert!(failed.is_empty());
        assert!(p.illegal_parts().is_empty());
        let start = p.cost();
        let stats = anneal(&mut p, 20_000, 7, 5.0, 0.1, 0.0, true);
        assert!(p.illegal_parts().is_empty());
        assert!(stats.best_cost <= start + 1e-9);
    }

    #[test]
    fn anneal_is_deterministic() {
        let run = || {
            let mut p = chain(10);
            legalize(&mut p, 0.25, 20.0, &[]);
            anneal(&mut p, 5_000, 42, 5.0, 0.1, 0.0, true);
            p.parts.iter().map(|q| (q.x, q.y, q.r)).collect::<Vec<_>>()
        };
        assert_eq!(run(), run());
    }
}
