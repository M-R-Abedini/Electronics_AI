// SPDX-License-Identifier: MIT
//! Electronics AI placement core (architecture 6, plan F1).
//!
//! - `geom`: exact 90-degree transforms, rectangles, outline containment, segment crossing
//! - `problem`: placement state with incremental weighted HPWL and pin-pair crossings
//! - `sa`: S4 legalization and S5 legality-preserving simulated annealing
//!
//! Python sees one class, `PlacementProblem`, plus the `hpwl` kernel kept from the F0b spike.

pub mod geom;
pub mod grid_route;
pub mod pathfinder;
pub mod problem;
pub mod sa;

use std::collections::HashMap;
use std::time::Instant;

use pyo3::prelude::*;

use geom::{Outline, Rect};
use problem::{Net, Part, Pin, Problem};

/// Half-perimeter wire length of each net, summed.
///
/// `nets[i]` lists indices into `xs`/`ys`. Nets with fewer than two pins add 0.
pub fn hpwl_sum(xs: &[f64], ys: &[f64], nets: &[Vec<usize>]) -> Result<f64, String> {
    let mut total = 0.0;
    for net in nets {
        if net.len() < 2 {
            continue;
        }
        let (mut x0, mut x1, mut y0, mut y1) = (
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
        );
        for &i in net {
            let (x, y) = match (xs.get(i), ys.get(i)) {
                (Some(x), Some(y)) => (*x, *y),
                _ => return Err(format!("pin index {i} out of range")),
            };
            x0 = x0.min(x);
            x1 = x1.max(x);
            y0 = y0.min(y);
            y1 = y1.max(y);
        }
        total += (x1 - x0) + (y1 - y0);
    }
    Ok(total)
}

#[pyfunction]
fn hpwl(xs: Vec<f64>, ys: Vec<f64>, nets: Vec<Vec<usize>>) -> PyResult<f64> {
    if xs.len() != ys.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "xs and ys differ in length",
        ));
    }
    hpwl_sum(&xs, &ys, &nets).map_err(pyo3::exceptions::PyIndexError::new_err)
}

type PartRow = (f64, f64, f64, f64, u8, bool, bool, f64, f64, u8);
type SubRectRow = (f64, f64, f64, f64);
type SideObstacleRow = (f64, f64, f64, f64, u8);

/// A placement problem: parts (local courtyard box, side, fixed, rotatable, pose), pins, nets, outline.
#[pyclass]
struct PlacementProblem {
    inner: Problem,
}

#[pymethods]
impl PlacementProblem {
    #[new]
    #[pyo3(signature = (parts, pins, nets, outers, holes, gap, w_hpwl, w_cross, affinities=None, sub_rects=None, cavity_eligible=None, clusters=None, repulsions=None, outline_locals=None, both_sides=None, side_obstacles=None, obstacle_locals=None, opp_locals=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        parts: Vec<PartRow>,
        pins: Vec<(usize, f64, f64)>,
        nets: Vec<(Vec<usize>, f64, bool)>,
        outers: Vec<Vec<(f64, f64)>>,
        holes: Vec<Vec<(f64, f64)>>,
        gap: f64,
        w_hpwl: f64,
        w_cross: f64,
        affinities: Option<Vec<(Vec<usize>, f64)>>,
        sub_rects: Option<Vec<Vec<SubRectRow>>>,
        cavity_eligible: Option<Vec<bool>>,
        clusters: Option<Vec<Vec<usize>>>,
        repulsions: Option<Vec<(usize, usize, f64, f64)>>,
        outline_locals: Option<Vec<SubRectRow>>,
        both_sides: Option<Vec<bool>>,
        side_obstacles: Option<Vec<SideObstacleRow>>,
        obstacle_locals: Option<Vec<SubRectRow>>,
        opp_locals: Option<Vec<Option<SubRectRow>>>,
    ) -> PyResult<Self> {
        let n = parts.len();
        if let Some(bad) = pins.iter().find(|p| p.0 >= n) {
            return Err(pyo3::exceptions::PyIndexError::new_err(format!(
                "pin of unknown part {}",
                bad.0
            )));
        }
        if let Some(bad) = nets
            .iter()
            .flat_map(|n| n.0.iter())
            .find(|&&p| p >= pins.len())
        {
            return Err(pyo3::exceptions::PyIndexError::new_err(format!(
                "net of unknown pin {bad}"
            )));
        }
        let affinities = affinities.unwrap_or_default();
        if affinities.iter().any(|(group, weight)| {
            group.len() < 2
                || group.iter().any(|&p| p >= pins.len())
                || !weight.is_finite()
                || *weight < 0.0
        }) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "invalid pin affinity",
            ));
        }
        let sub_vec = sub_rects.unwrap_or_default();
        let elig_vec = cavity_eligible.unwrap_or_default();
        let out_vec = outline_locals.unwrap_or_default();
        let obs_vec = obstacle_locals.unwrap_or_default();
        let both_vec = both_sides.unwrap_or_default();
        let opp_vec = opp_locals.unwrap_or_default();
        let parts = parts
            .into_iter()
            .enumerate()
            .map(|(idx, (x0, y0, x1, y1, side, fixed, can_rotate, x, y, r))| {
                let srects = sub_vec
                    .get(idx)
                    .map(|rs| {
                        rs.iter()
                            .map(|&(sx0, sy0, sx1, sy1)| Rect {
                                x0: sx0,
                                y0: sy0,
                                x1: sx1,
                                y1: sy1,
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let eligible = elig_vec.get(idx).copied().unwrap_or(true);
                let local = Rect { x0, y0, x1, y1 };
                let outline_local = out_vec
                    .get(idx)
                    .map(|&(ox0, oy0, ox1, oy1)| Rect {
                        x0: ox0,
                        y0: oy0,
                        x1: ox1,
                        y1: oy1,
                    })
                    .unwrap_or(local);
                let obstacle_local = obs_vec
                    .get(idx)
                    .map(|&(ox0, oy0, ox1, oy1)| Rect {
                        x0: ox0,
                        y0: oy0,
                        x1: ox1,
                        y1: oy1,
                    })
                    .unwrap_or(local);
                let opp_local = opp_vec
                    .get(idx)
                    .and_then(|opt| {
                        opt.map(|(ox0, oy0, ox1, oy1)| Rect {
                            x0: ox0,
                            y0: oy0,
                            x1: ox1,
                            y1: oy1,
                        })
                    })
                    .or_else(|| both_vec.get(idx).copied().unwrap_or(false).then_some(local));
                Part {
                    local,
                    outline_local,
                    obstacle_local,
                    opp_local,
                    sub_rects: srects,
                    cavity_eligible: eligible,
                    side,
                    fixed,
                    can_rotate,
                    x,
                    y,
                    r: r & 3,
                }
            })
            .collect();
        let pins = pins
            .into_iter()
            .map(|(part, lx, ly)| Pin { part, lx, ly })
            .collect();
        let nets = nets
            .into_iter()
            .map(|(pins, weight, crossing)| Net {
                pins,
                weight,
                crossing,
                nearest: false,
            })
            .chain(affinities.into_iter().map(|(pins, weight)| Net {
                pins,
                weight,
                crossing: false,
                nearest: true,
            }))
            .collect();
        let mut inner = Problem::new(
            parts,
            pins,
            nets,
            Outline { outers, holes },
            gap,
            w_hpwl,
            w_cross,
        );
        if let Some(cl) = clusters {
            inner.set_clusters(cl);
        }
        if let Some(rep) = repulsions {
            inner.set_repulsions(rep);
        }
        if let Some(obs) = side_obstacles {
            inner.set_side_obstacles(
                obs.into_iter()
                    .map(|(x0, y0, x1, y1, side)| (Rect { x0, y0, x1, y1 }, side))
                    .collect(),
            );
        }
        Ok(PlacementProblem { inner })
    }

    fn positions(&self) -> Vec<(f64, f64, u8)> {
        self.inner.parts.iter().map(|p| (p.x, p.y, p.r)).collect()
    }

    fn set_positions(&mut self, poses: Vec<(f64, f64, u8)>) -> PyResult<()> {
        if poses.len() != self.inner.parts.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("one pose per part"));
        }
        for (p, (x, y, r)) in self.inner.parts.iter_mut().zip(poses) {
            p.x = x;
            p.y = y;
            p.r = r & 3;
        }
        self.inner.rebuild();
        Ok(())
    }

    fn cost(&self) -> HashMap<String, f64> {
        HashMap::from([
            ("hpwl".to_string(), self.inner.hpwl()),
            ("crossings".to_string(), self.inner.crossings() as f64),
            ("total".to_string(), self.inner.cost()),
        ])
    }

    fn illegal_parts(&mut self) -> Vec<usize> {
        self.inner.illegal_parts()
    }

    /// S4. `first` lists parts to place before the rest (a retry for parts that found no room).
    #[pyo3(signature = (step=0.25, max_radius=50.0, first=Vec::new()))]
    fn legalize(&mut self, py: Python<'_>, step: f64, max_radius: f64, first: Vec<usize>) -> Vec<usize> {
        py.detach(|| sa::legalize(&mut self.inner, step, max_radius, &first))
    }

    #[pyo3(signature = (iterations, seed, r0, r1, t0=0.0, swap=true))]
    #[allow(clippy::too_many_arguments)]
    fn anneal(
        &mut self,
        py: Python<'_>,
        iterations: usize,
        seed: u64,
        r0: f64,
        r1: f64,
        t0: f64,
        swap: bool,
    ) -> HashMap<String, f64> {
        let s = py.detach(|| sa::anneal(&mut self.inner, iterations, seed, r0, r1, t0, swap));
        HashMap::from([
            ("iterations".to_string(), s.iterations as f64),
            ("accepted".to_string(), s.accepted as f64),
            ("illegal".to_string(), s.illegal as f64),
            ("start_cost".to_string(), s.start_cost),
            ("best_cost".to_string(), s.best_cost),
            ("t0".to_string(), s.t0),
        ])
    }

    /// Seconds per full cost evaluation (rebuild of every cache), averaged over `repeats` (plan F1 speed gate).
    fn bench_full_cost(&mut self, repeats: usize) -> f64 {
        let t = Instant::now();
        for _ in 0..repeats.max(1) {
            self.inner.rebuild();
        }
        t.elapsed().as_secs_f64() / repeats.max(1) as f64
    }

    /// MST edges of net `n` at the current poses (parity tests with the Python reference).
    fn mst(&self, n: usize) -> Vec<((f64, f64), (f64, f64))> {
        self.inner.mst(n)
    }
}

/// Grid A* for S6a: `blocked` is nl*ny*nx bytes (layer-major), `via_ok` ny*nx bytes; cells are (x, y, layer).
#[pyfunction]
#[pyo3(signature = (nx, ny, nl, blocked, via_ok, starts, goals, via_cost, turn45, turn90, max_expansions=20_000_000))]
#[allow(clippy::too_many_arguments)]
fn grid_astar(py: Python<'_>, nx: usize, ny: usize, nl: usize, blocked: &[u8], via_ok: &[u8],
              starts: Vec<(usize, usize, usize)>, goals: Vec<(usize, usize, usize)>,
              via_cost: f64, turn45: f64, turn90: f64, max_expansions: usize)
              -> PyResult<Option<Vec<(usize, usize, usize)>>> {
    if blocked.len() != nx * ny * nl || via_ok.len() != nx * ny {
        return Err(pyo3::exceptions::PyValueError::new_err("grid sizes do not match nx, ny, nl"));
    }
    let grid = grid_route::Grid { nx, ny, nl, blocked, via_ok };
    let costs = grid_route::Costs { via: via_cost, turn45, turn90 };
    Ok(py.detach(|| grid_route::astar(&grid, &starts, &goals, &costs, max_expansions)))
}

/// Coarse-grid PathFinder congestion estimator (plan F5).
#[pyfunction]
#[pyo3(signature = (nx, ny, cap_h, cap_v, nets, iterations=8, history_inc=0.5, present_fac=1.0, present_mult=1.5))]
#[allow(clippy::too_many_arguments)]
fn pathfinder_estimate<'py>(
    py: Python<'py>,
    nx: usize,
    ny: usize,
    cap_h: Vec<f64>,
    cap_v: Vec<f64>,
    nets: Vec<Vec<(usize, usize)>>,
    iterations: usize,
    history_inc: f64,
    present_fac: f64,
    present_mult: f64,
) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
    let res = py.detach(|| {
        pathfinder::estimate(
            nx,
            ny,
            &cap_h,
            &cap_v,
            &nets,
            iterations,
            history_inc,
            present_fac,
            present_mult,
        )
    })
    .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("total_overflow", res.total_overflow)?;
    dict.set_item("max_overflow", res.max_overflow)?;
    dict.set_item("overflowed_edges", res.overflowed_edges)?;
    dict.set_item("peak_utilization", res.peak_utilization)?;
    dict.set_item("congestion_energy", res.congestion_energy)?;
    dict.set_item("total_wirelength_cells", res.total_wirelength_cells)?;
    dict.set_item("routed_connections", res.routed_connections)?;
    dict.set_item("iterations_run", res.iterations_run)?;
    dict.set_item("cell_overflow", res.cell_overflow)?;
    dict.set_item("congestion_score", res.congestion_score)?;
    Ok(dict)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(hpwl, m)?)?;
    m.add_function(wrap_pyfunction!(grid_astar, m)?)?;
    m.add_function(wrap_pyfunction!(pathfinder_estimate, m)?)?;
    m.add_class::<PlacementProblem>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hpwl_of_two_nets() {
        let xs = [0.0, 3.0, 1.0, 1.0];
        let ys = [0.0, 4.0, 0.0, 2.0];
        let nets = vec![vec![0, 1], vec![2, 3], vec![0]];
        assert_eq!(hpwl_sum(&xs, &ys, &nets).unwrap(), 7.0 + 2.0);
    }

    #[test]
    fn out_of_range_pin_is_an_error() {
        assert!(hpwl_sum(&[0.0], &[0.0], &[vec![0, 5]]).is_err());
    }
}
