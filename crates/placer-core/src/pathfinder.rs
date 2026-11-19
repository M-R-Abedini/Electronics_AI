// SPDX-License-Identifier: MIT
//! Coarse-grid PathFinder negotiated-congestion estimator (plan F5).
//!
//! Deterministic single-threaded McMurchie-Ebeling global router on a 2D coarse
//! grid (`nx * ny`). Decomposes each multi-pin net via deterministic Prim's MST
//! and runs bounded rip-up/reroute iterations with present congestion + history
//! cost. Produces exact edge overflow metrics and a per-cell overflow map for
//! cell inflation.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

type Cell = (usize, usize);
type Trunk = (Cell, Cell);
type Neighbor = (Option<Cell>, usize, u8);

#[derive(Clone, Copy, Debug)]
struct State {
    f: f64,
    g: f64,
    node: usize,
    dir: u8, // 0 = start, 1 = horiz, 2 = vert
}

impl PartialEq for State {
    fn eq(&self, other: &Self) -> bool {
        self.f.to_bits() == other.f.to_bits() && self.node == other.node && self.dir == other.dir
    }
}
impl Eq for State {}

impl Ord for State {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .f
            .total_cmp(&self.f)
            .then_with(|| other.g.total_cmp(&self.g))
            .then_with(|| other.node.cmp(&self.node))
            .then_with(|| other.dir.cmp(&self.dir))
    }
}

impl PartialOrd for State {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

pub struct PathFinderResult {
    pub total_overflow: f64,
    pub max_overflow: f64,
    pub overflowed_edges: usize,
    pub peak_utilization: f64,
    pub congestion_energy: f64,
    pub total_wirelength_cells: f64,
    pub routed_connections: usize,
    pub iterations_run: usize,
    pub cell_overflow: Vec<f64>,
    pub congestion_score: f64,
}

/// Edge index encoding:
/// - Horizontal edge between (x, y) and (x+1, y): `0 .. (nx-1)*ny`, idx = `y*(nx-1) + x`
/// - Vertical edge between (x, y) and (x, y+1): offset `nh`, idx = `nh + y*nx + x`
#[inline]
fn h_idx(nx: usize, x: usize, y: usize) -> usize {
    y * (nx - 1) + x
}

#[inline]
fn v_idx(nx: usize, nh: usize, x: usize, y: usize) -> usize {
    nh + y * nx + x
}

fn prim_mst_cells(pins: &[(usize, usize)]) -> Vec<((usize, usize), (usize, usize))> {
    let mut uniq: Vec<(usize, usize)> = pins.to_vec();
    uniq.sort_unstable();
    uniq.dedup();
    let n = uniq.len();
    if n < 2 {
        return Vec::new();
    }
    let mut in_tree = vec![false; n];
    let mut best_d = vec![usize::MAX; n];
    let mut parent = vec![0usize; n];
    in_tree[0] = true;
    for j in 1..n {
        let d = uniq[0].0.abs_diff(uniq[j].0) + uniq[0].1.abs_diff(uniq[j].1);
        best_d[j] = d;
        parent[j] = 0;
    }
    let mut edges = Vec::with_capacity(n - 1);
    for _ in 1..n {
        let mut pick = 0usize;
        let mut min_d = usize::MAX;
        for j in 1..n {
            if !in_tree[j] && best_d[j] < min_d {
                min_d = best_d[j];
                pick = j;
            }
        }
        if pick == 0 {
            break;
        }
        in_tree[pick] = true;
        edges.push((uniq[parent[pick]], uniq[pick]));
        let (px, py) = uniq[pick];
        for j in 1..n {
            if !in_tree[j] {
                let d = px.abs_diff(uniq[j].0) + py.abs_diff(uniq[j].1);
                if d < best_d[j] {
                    best_d[j] = d;
                    parent[j] = pick;
                }
            }
        }
    }
    edges
}

#[allow(clippy::too_many_arguments)]
pub fn estimate(
    nx: usize,
    ny: usize,
    cap_h: &[f64],
    cap_v: &[f64],
    nets: &[Vec<(usize, usize)>],
    iterations: usize,
    history_inc: f64,
    present_fac_init: f64,
    present_mult: f64,
) -> Result<PathFinderResult, String> {
    if nx == 0 || ny == 0 {
        return Err("grid dimensions must be positive".to_string());
    }
    let nh = nx.saturating_sub(1) * ny;
    let nv = nx * ny.saturating_sub(1);
    if cap_h.len() != nh || cap_v.len() != nv {
        return Err(format!(
            "capacity lengths ({}, {}) do not match grid ({nx}x{ny} -> {nh}, {nv})",
            cap_h.len(),
            cap_v.len()
        ));
    }
    let n_edges = nh + nv;
    let mut cap = Vec::with_capacity(n_edges);
    cap.extend_from_slice(cap_h);
    cap.extend_from_slice(cap_v);

    // Build deterministic 2-pin trunks per net.
    let mut net_trunks: Vec<Vec<Trunk>> = Vec::with_capacity(nets.len());
    let mut total_conns = 0usize;
    for pins in nets {
        let mut clamped: Vec<(usize, usize)> = Vec::with_capacity(pins.len());
        for &(x, y) in pins {
            if x >= nx || y >= ny {
                return Err(format!("pin cell ({x}, {y}) out of grid ({nx}x{ny})"));
            }
            clamped.push((x, y));
        }
        let trunks = prim_mst_cells(&clamped);
        total_conns += trunks.len();
        net_trunks.push(trunks);
    }

    let n_nodes = nx * ny;
    if total_conns == 0 || n_edges == 0 {
        return Ok(PathFinderResult {
            total_overflow: 0.0,
            max_overflow: 0.0,
            overflowed_edges: 0,
            peak_utilization: 0.0,
            congestion_energy: 0.0,
            total_wirelength_cells: 0.0,
            routed_connections: 0,
            iterations_run: 0,
            cell_overflow: vec![0.0; n_nodes],
            congestion_score: 100.0,
        });
    }

    let mut demand = vec![0.0f64; n_edges];
    let mut history = vec![1.0f64; n_edges];
    // Per-net edge usage counts (sorted list of edges used by each net in current iteration)
    let mut net_edges: Vec<Vec<usize>> = vec![Vec::new(); nets.len()];
    let mut net_seen_epoch = vec![0u32; n_edges];
    let mut cur_net_epoch = 0u32;

    // Reusable A* scratch buffers (3 directions per node: 0=none, 1=H, 2=V; collapse to node-level g)
    let mut g_score = vec![f64::INFINITY; n_nodes];
    let mut came_node = vec![usize::MAX; n_nodes];
    let mut came_edge = vec![usize::MAX; n_nodes];
    let mut visited_epoch = vec![0u32; n_nodes];
    let mut cur_astar_epoch = 0u32;
    let mut heap = BinaryHeap::new();

    let mut best_total_ov = f64::INFINITY;
    let mut best_max_ov = f64::INFINITY;
    let mut best_ov_edges = usize::MAX;
    let mut best_peak_util = 0.0f64;
    let mut best_energy = f64::INFINITY;
    let mut best_wl = f64::INFINITY;
    let mut best_cell_ov = vec![0.0f64; n_nodes];
    let mut iters_run = 0usize;

    let max_iters = iterations.max(1);
    let mut p_fac = present_fac_init.max(0.1);
    let bend_cost = 0.12f64;

    // Order nets deterministically: shorter/smaller nets first, then index (standard global router order).
    let mut net_order: Vec<usize> = (0..nets.len()).filter(|&i| !net_trunks[i].is_empty()).collect();
    net_order.sort_by_key(|&i| {
        let span: usize = net_trunks[i]
            .iter()
            .map(|(a, b)| a.0.abs_diff(b.0) + a.1.abs_diff(b.1))
            .sum();
        (span, i)
    });

    for iter in 0..max_iters {
        iters_run = iter + 1;

        for &ni in &net_order {
            // Rip up this net's previous edges from global demand.
            for &e in &net_edges[ni] {
                demand[e] = (demand[e] - 1.0).max(0.0);
            }
            net_edges[ni].clear();
            cur_net_epoch = cur_net_epoch.wrapping_add(1);
            if cur_net_epoch == 0 {
                net_seen_epoch.fill(0);
                cur_net_epoch = 1;
            }

            // Route each 2-pin MST trunk of this net.
            for &(src, dst) in &net_trunks[ni] {
                if src == dst {
                    continue;
                }
                let start_node = src.1 * nx + src.0;
                let goal_node = dst.1 * nx + dst.0;

                cur_astar_epoch = cur_astar_epoch.wrapping_add(1);
                if cur_astar_epoch == 0 {
                    visited_epoch.fill(0);
                    cur_astar_epoch = 1;
                }
                heap.clear();
                visited_epoch[start_node] = cur_astar_epoch;
                g_score[start_node] = 0.0;
                came_node[start_node] = usize::MAX;
                came_edge[start_node] = usize::MAX;

                let h0 = (src.0.abs_diff(dst.0) + src.1.abs_diff(dst.1)) as f64;
                heap.push(State {
                    f: h0,
                    g: 0.0,
                    node: start_node,
                    dir: 0,
                });

                let mut found = false;
                while let Some(State { g, node, dir, .. }) = heap.pop() {
                    if node == goal_node {
                        found = true;
                        break;
                    }
                    if visited_epoch[node] == cur_astar_epoch && g > g_score[node] + 1e-12 {
                        continue;
                    }
                    let x = node % nx;
                    let y = node / nx;

                    // 4-connected neighbors: Left, Right, Up, Down
                    let neighbors: [Neighbor; 4] = [
                        (if x > 0 { Some((x - 1, y)) } else { None }, if x > 0 { h_idx(nx, x - 1, y) } else { 0 }, 1),
                        (if x + 1 < nx { Some((x + 1, y)) } else { None }, if x + 1 < nx { h_idx(nx, x, y) } else { 0 }, 1),
                        (if y > 0 { Some((x, y - 1)) } else { None }, if y > 0 { v_idx(nx, nh, x, y - 1) } else { 0 }, 2),
                        (if y + 1 < ny { Some((x, y + 1)) } else { None }, if y + 1 < ny { v_idx(nx, nh, x, y) } else { 0 }, 2),
                    ];

                    for (nb_opt, e_idx, ndir) in neighbors {
                        let Some((nx_c, ny_c)) = nb_opt else { continue };
                        let nb = ny_c * nx + nx_c;
                        // Same-net edge sharing: reusing an edge already owned by this net is almost free.
                        let step_cost = if net_seen_epoch[e_idx] == cur_net_epoch {
                            0.05
                        } else {
                            let c = cap[e_idx];
                            let d_after = demand[e_idx] + 1.0;
                            let present = if c <= 1e-6 {
                                1.0 + p_fac * (d_after + 4.0)
                            } else if d_after > c {
                                1.0 + p_fac * (d_after - c) / c.max(0.25)
                            } else {
                                1.0 + 0.15 * (d_after / c)
                            };
                            let bend = if dir != 0 && dir != ndir { bend_cost } else { 0.0 };
                            history[e_idx] * present + bend
                        };
                        let ng = g + step_cost;
                        let better = visited_epoch[nb] != cur_astar_epoch || ng + 1e-12 < g_score[nb];
                        if better {
                            visited_epoch[nb] = cur_astar_epoch;
                            g_score[nb] = ng;
                            came_node[nb] = node;
                            came_edge[nb] = e_idx;
                            let heur = (nx_c.abs_diff(dst.0) + ny_c.abs_diff(dst.1)) as f64 * 0.95;
                            heap.push(State {
                                f: ng + heur,
                                g: ng,
                                node: nb,
                                dir: ndir,
                            });
                        }
                    }
                }

                if found {
                    let mut cur = goal_node;
                    while cur != start_node {
                        let e = came_edge[cur];
                        if e == usize::MAX {
                            break;
                        }
                        if net_seen_epoch[e] != cur_net_epoch {
                            net_seen_epoch[e] = cur_net_epoch;
                            net_edges[ni].push(e);
                            demand[e] += 1.0;
                        }
                        cur = came_node[cur];
                    }
                }
            }
        }

        // Measure overflow, peak utilization, smooth congestion energy, and update history penalties.
        let mut total_ov = 0.0f64;
        let mut max_ov = 0.0f64;
        let mut ov_edges = 0usize;
        let mut peak_util = 0.0f64;
        let mut energy = 0.0f64;
        for e in 0..n_edges {
            let d = demand[e];
            if d > 1e-9 {
                let c = cap[e].max(0.1);
                let u = d / c;
                if u > peak_util {
                    peak_util = u;
                }
                energy += u * u;
            }
            let ov = (d - cap[e]).max(0.0);
            if ov > 1e-9 {
                total_ov += ov;
                if ov > max_ov {
                    max_ov = ov;
                }
                ov_edges += 1;
                history[e] += history_inc * ov;
            }
        }
        let wl: f64 = net_edges.iter().map(|v| v.len() as f64).sum();

        let improved = total_ov + 1e-9 < best_total_ov
            || ((total_ov - best_total_ov).abs() <= 1e-9
                && (max_ov + 1e-9 < best_max_ov
                    || ((max_ov - best_max_ov).abs() <= 1e-9 && wl < best_wl)));

        if improved {
            best_total_ov = total_ov;
            best_max_ov = max_ov;
            best_ov_edges = ov_edges;
            best_peak_util = peak_util;
            best_energy = energy;
            best_wl = wl;
            best_cell_ov.fill(0.0);
            for y in 0..ny {
                for x in 0..nx.saturating_sub(1) {
                    let ov = (demand[h_idx(nx, x, y)] - cap[h_idx(nx, x, y)]).max(0.0);
                    if ov > 1e-9 {
                        best_cell_ov[y * nx + x] += 0.5 * ov;
                        best_cell_ov[y * nx + x + 1] += 0.5 * ov;
                    }
                }
            }
            for y in 0..ny.saturating_sub(1) {
                for x in 0..nx {
                    let ov = (demand[v_idx(nx, nh, x, y)] - cap[v_idx(nx, nh, x, y)]).max(0.0);
                    if ov > 1e-9 {
                        best_cell_ov[y * nx + x] += 0.5 * ov;
                        best_cell_ov[(y + 1) * nx + x] += 0.5 * ov;
                    }
                }
            }
        }

        if total_ov <= 1e-9 {
            break;
        }
        p_fac *= present_mult;
    }

    let denom = (total_conns as f64 * 0.25).max(1.0);
    let score = (100.0 * (-best_total_ov / denom).exp()).clamp(0.0, 100.0);
    Ok(PathFinderResult {
        total_overflow: best_total_ov,
        max_overflow: best_max_ov,
        overflowed_edges: best_ov_edges,
        peak_utilization: best_peak_util,
        congestion_energy: best_energy,
        total_wirelength_cells: best_wl,
        routed_connections: total_conns,
        iterations_run: iters_run,
        cell_overflow: best_cell_ov,
        congestion_score: (score * 100.0).round() / 100.0,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uncongested_grid_has_zero_overflow() {
        let nx = 6;
        let ny = 6;
        let cap_h = vec![4.0; (nx - 1) * ny];
        let cap_v = vec![4.0; nx * (ny - 1)];
        let nets = vec![vec![(0, 0), (5, 5)], vec![(0, 5), (5, 0)]];
        let res = estimate(nx, ny, &cap_h, &cap_v, &nets, 6, 0.5, 1.0, 1.5).unwrap();
        assert_eq!(res.total_overflow, 0.0);
        assert_eq!(res.overflowed_edges, 0);
        assert_eq!(res.congestion_score, 100.0);
        assert_eq!(res.iterations_run, 1);
    }

    #[test]
    fn bottleneck_triggers_negotiation_and_detours() {
        // 5x5 grid: middle column edges at y=2 have capacity 1, rows above/below have capacity 4.
        let nx = 5;
        let ny = 5;
        let mut cap_h = vec![4.0; (nx - 1) * ny];
        let cap_v = vec![4.0; nx * (ny - 1)];
        for x in 0..(nx - 1) {
            cap_h[h_idx(nx, x, 2)] = 1.0;
        }
        // 4 horizontal nets all want row y=2 initially, but PathFinder negotiates them onto y=1, y=3.
        let nets = vec![
            vec![(0, 2), (4, 2)],
            vec![(0, 2), (4, 2)],
            vec![(0, 2), (4, 2)],
            vec![(0, 2), (4, 2)],
        ];
        let res_1 = estimate(nx, ny, &cap_h, &cap_v, &nets, 1, 0.5, 1.0, 1.5).unwrap();
        let res_6 = estimate(nx, ny, &cap_h, &cap_v, &nets, 6, 0.5, 1.0, 1.5).unwrap();
        assert!(res_6.total_overflow <= res_1.total_overflow);
        assert_eq!(res_6.total_overflow, 0.0);
    }

    #[test]
    fn unavoidable_bottleneck_reports_positive_cell_overflow_and_is_deterministic() {
        // Wall across x=2..3 except a single bridge at y=2 with capacity 1; 4 nets must cross.
        let nx = 5;
        let ny = 5;
        let mut cap_h = vec![4.0; (nx - 1) * ny];
        let cap_v = vec![4.0; nx * (ny - 1)];
        for y in 0..ny {
            cap_h[h_idx(nx, 2, y)] = if y == 2 { 1.0 } else { 0.0 };
        }
        let nets = vec![
            vec![(0, 1), (4, 1)],
            vec![(0, 2), (4, 2)],
            vec![(0, 3), (4, 3)],
            vec![(1, 2), (3, 2)],
        ];
        let a = estimate(nx, ny, &cap_h, &cap_v, &nets, 6, 0.5, 1.0, 1.5).unwrap();
        let b = estimate(nx, ny, &cap_h, &cap_v, &nets, 6, 0.5, 1.0, 1.5).unwrap();
        assert!(a.total_overflow > 0.0);
        assert!(a.cell_overflow[2 * nx + 2] > 0.0);
        assert_eq!(a.total_overflow.to_bits(), b.total_overflow.to_bits());
        assert_eq!(a.cell_overflow, b.cell_overflow);
    }
}
