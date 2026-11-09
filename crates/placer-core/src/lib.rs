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

/// A placement problem: parts (local courtyard box, side, fixed, rotatable, pose), pins, nets, outline.
#[pyclass]
struct PlacementProblem {
    inner: Problem,
}

#[pymethods]
impl PlacementProblem {
    #[new]
    #[pyo3(signature = (parts, pins, nets, outers, holes, gap, w_hpwl, w_cross))]
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
        let parts = parts
            .into_iter()
            .map(|(x0, y0, x1, y1, side, fixed, can_rotate, x, y, r)| Part {
                local: Rect { x0, y0, x1, y1 },
                side,
                fixed,
                can_rotate,
                x,
                y,
                r: r & 3,
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
            })
            .collect();
        Ok(PlacementProblem {
            inner: Problem::new(
                parts,
                pins,
                nets,
                Outline { outers, holes },
                gap,
                w_hpwl,
                w_cross,
            ),
        })
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
    fn legalize(&mut self, step: f64, max_radius: f64, first: Vec<usize>) -> Vec<usize> {
        sa::legalize(&mut self.inner, step, max_radius, &first)
    }

    #[pyo3(signature = (iterations, seed, r0, r1, t0=0.0, swap=true))]
    fn anneal(
        &mut self,
        iterations: usize,
        seed: u64,
        r0: f64,
        r1: f64,
        t0: f64,
        swap: bool,
    ) -> HashMap<String, f64> {
        let s = sa::anneal(&mut self.inner, iterations, seed, r0, r1, t0, swap);
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
fn grid_astar(nx: usize, ny: usize, nl: usize, blocked: &[u8], via_ok: &[u8], starts: Vec<(usize, usize, usize)>,
              goals: Vec<(usize, usize, usize)>, via_cost: f64, turn45: f64, turn90: f64, max_expansions: usize)
              -> PyResult<Option<Vec<(usize, usize, usize)>>> {
    if blocked.len() != nx * ny * nl || via_ok.len() != nx * ny {
        return Err(pyo3::exceptions::PyValueError::new_err("grid sizes do not match nx, ny, nl"));
    }
    let grid = grid_route::Grid { nx, ny, nl, blocked, via_ok };
    let costs = grid_route::Costs { via: via_cost, turn45, turn90 };
    Ok(grid_route::astar(&grid, &starts, &goals, &costs, max_expansions))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(hpwl, m)?)?;
    m.add_function(wrap_pyfunction!(grid_astar, m)?)?;
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
