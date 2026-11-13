// SPDX-License-Identifier: MIT
//! S4 legalization and S5 legality-preserving simulated annealing.

use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;

use crate::problem::Problem;

/// Place movable parts, largest first, at the nearest legal position to their current pose.
///
/// Fixed parts are obstacles from the start.  Positions are searched on rings of
/// `step` around the target, up to `max_radius`; the current rotation is tried
/// first, then the others.  Returns the parts that found no legal position (left
/// at their target).  Caches are rebuilt at the end.
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
    // All parts and rotations visit the same offsets. Cache each ring on demand.
    let mut rings = Vec::new();
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
            for k in 0..=k_max {
                if rings.len() == k as usize {
                    rings.push(legalize_ring(k));
                }
                for &(a, b) in &rings[k as usize] {
                    p.parts[i].x = round3(tx + a as f64 * step);
                    p.parts[i].y = round3(ty + b as f64 * step);
                    if p.legal_at(i, None) {
                        p.set_placed(i, true);
                        found = true;
                        break 'rot;
                    }
                }
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

fn legalize_ring(k: i64) -> Vec<(i64, i64)> {
    if k == 0 {
        return vec![(0, 0)];
    }
    let mut ring = Vec::new();
    for d in -k..=k {
        ring.extend([(d, -k), (d, k)]);
        if d != -k && d != k {
            ring.extend([(-k, d), (k, d)]);
        }
    }
    ring.sort_unstable_by_key(|&(a, b)| (a * a + b * b, a, b));
    ring
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
    for k in 0..iters {
        let f = k as f64 / iters as f64;
        let t = t0 * (t_end / t0).powf(f);
        let radius = r0 * (r1 / r0).powf(f);
        let i = movable[rng.random_range(0..movable.len())];
        let before = p.cost();
        let kind = rng.random_range(0..10u8);
        let (x, y, r) = (p.parts[i].x, p.parts[i].y, p.parts[i].r);
        let mut undos = Vec::with_capacity(2);
        let mut moved = vec![i];
        if kind < 6 || (kind < 8 && !p.parts[i].can_rotate) || (kind >= 8 && !swap) {
            let nx = round3(x + rng.random_range(-radius..=radius));
            let ny = round3(y + rng.random_range(-radius..=radius));
            undos.push(p.apply_pose(i, nx, ny, r));
        } else if kind < 8 {
            let nr = (r + rng.random_range(1..4u8)) & 3;
            undos.push(p.apply_pose(i, x, y, nr));
        } else {
            let side = p.parts[i].side;
            let peers: Vec<usize> = movable
                .iter()
                .copied()
                .filter(|&j| j != i && p.parts[j].side == side)
                .collect();
            if peers.is_empty() {
                continue;
            }
            let j = peers[rng.random_range(0..peers.len())];
            let (xj, yj, rj) = (p.parts[j].x, p.parts[j].y, p.parts[j].r);
            undos.push(p.apply_pose(i, xj, yj, r));
            undos.push(p.apply_pose(j, x, y, rj));
            moved.push(j);
        }
        stats.iterations += 1;
        let legal = moved.iter().all(|&m| p.legal_at(m, None));
        let delta = p.cost() - before;
        let accept = legal && (delta <= 0.0 || rng.random::<f64>() < (-delta / t).exp());
        if accept {
            stats.accepted += 1;
            if p.cost() < stats.best_cost - 1e-12 {
                stats.best_cost = p.cost();
                for &m in &moved {
                    best[m] = (p.parts[m].x, p.parts[m].y, p.parts[m].r);
                }
                // keep the whole snapshot coherent
                for (q, b) in p.parts.iter().zip(best.iter_mut()) {
                    *b = (q.x, q.y, q.r);
                }
            }
        } else {
            if !legal {
                stats.illegal += 1;
            }
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
