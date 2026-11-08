// SPDX-License-Identifier: MIT
//! Electronics AI placement core.
//!
//! F0b toolchain spike: one real kernel (half-perimeter wire length) proves
//! the PyO3 + maturin + MSVC path. The full core arrives in F1.

use pyo3::prelude::*;

/// Half-perimeter wire length of each net, summed.
///
/// `nets[i]` lists indices into `xs`/`ys`. Nets with fewer than two pins add 0.
pub fn hpwl_sum(xs: &[f64], ys: &[f64], nets: &[Vec<usize>]) -> Result<f64, String> {
    let mut total = 0.0;
    for net in nets {
        if net.len() < 2 {
            continue;
        }
        let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
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
        return Err(pyo3::exceptions::PyValueError::new_err("xs and ys differ in length"));
    }
    hpwl_sum(&xs, &ys, &nets).map_err(pyo3::exceptions::PyIndexError::new_err)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(hpwl, m)?)?;
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
