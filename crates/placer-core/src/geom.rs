// SPDX-License-Identifier: MIT
//! Geometry in KiCad's frame: millimetres, +Y down, positive angle counter-clockwise on screen.
//!
//! Rotations are restricted to multiples of 90 degrees (index 0..=3), so every
//! transform is exact and rectangles stay axis-aligned.

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
}

impl Rect {
    pub fn area(&self) -> f64 {
        (self.x1 - self.x0).max(0.0) * (self.y1 - self.y0).max(0.0)
    }

    pub fn expand(&self, d: f64) -> Rect {
        Rect {
            x0: self.x0 - d,
            y0: self.y0 - d,
            x1: self.x1 + d,
            y1: self.y1 + d,
        }
    }

    /// Area shared with `o`; touching rectangles share nothing.
    pub fn overlap(&self, o: &Rect) -> f64 {
        let w = self.x1.min(o.x1) - self.x0.max(o.x0);
        let h = self.y1.min(o.y1) - self.y0.max(o.y0);
        if w > 0.0 && h > 0.0 { w * h } else { 0.0 }
    }

    pub fn corners(&self) -> [(f64, f64); 4] {
        [
            (self.x0, self.y0),
            (self.x1, self.y0),
            (self.x1, self.y1),
            (self.x0, self.y1),
        ]
    }
}

/// Rotate a footprint-local vector by `r` quarter turns (geometry.rotate in Python).
#[inline]
pub fn rot90(x: f64, y: f64, r: u8) -> (f64, f64) {
    match r & 3 {
        0 => (x, y),
        1 => (y, -x),
        2 => (-x, -y),
        _ => (-y, x),
    }
}

/// World rectangle of a local box placed at (cx, cy) with `r` quarter turns.
pub fn placed_rect(local: &Rect, cx: f64, cy: f64, r: u8) -> Rect {
    let mut out = Rect {
        x0: f64::INFINITY,
        y0: f64::INFINITY,
        x1: f64::NEG_INFINITY,
        y1: f64::NEG_INFINITY,
    };
    for (x, y) in local.corners() {
        let (dx, dy) = rot90(x, y, r);
        out.x0 = out.x0.min(cx + dx);
        out.y0 = out.y0.min(cy + dy);
        out.x1 = out.x1.max(cx + dx);
        out.y1 = out.y1.max(cy + dy);
    }
    out
}

/// Even-odd point in polygon.
pub fn point_in_ring(px: f64, py: f64, ring: &[(f64, f64)]) -> bool {
    let n = ring.len();
    if n < 3 {
        return false;
    }
    let mut inside = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi) = ring[i];
        let (xj, yj) = ring[j];
        if (yi > py) != (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi {
            inside = !inside;
        }
        j = i;
    }
    inside
}

#[inline]
fn orient(a: (f64, f64), b: (f64, f64), c: (f64, f64)) -> f64 {
    (b.0 - a.0) * (c.1 - a.1) - (b.1 - a.1) * (c.0 - a.0)
}

/// Proper crossing of two segments: interiors cross; shared endpoints and touching do not count.
pub fn segments_cross(p1: (f64, f64), p2: (f64, f64), q1: (f64, f64), q2: (f64, f64)) -> bool {
    const EPS: f64 = 1e-9;
    let d1 = orient(q1, q2, p1);
    let d2 = orient(q1, q2, p2);
    let d3 = orient(p1, p2, q1);
    let d4 = orient(p1, p2, q2);
    ((d1 > EPS && d2 < -EPS) || (d1 < -EPS && d2 > EPS))
        && ((d3 > EPS && d4 < -EPS) || (d3 < -EPS && d4 > EPS))
}

/// True if the segment passes through the open interior of `r`.
pub fn segment_hits_rect_interior(a: (f64, f64), b: (f64, f64), r: &Rect) -> bool {
    // Liang-Barsky clipping against the slightly shrunk rectangle.
    const E: f64 = 1e-9;
    let (x0, y0, x1, y1) = (r.x0 + E, r.y0 + E, r.x1 - E, r.y1 - E);
    if x0 >= x1 || y0 >= y1 {
        return false;
    }
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let mut t0 = 0.0_f64;
    let mut t1 = 1.0_f64;
    for (p, q) in [
        (-dx, a.0 - x0),
        (dx, x1 - a.0),
        (-dy, a.1 - y0),
        (dy, y1 - a.1),
    ] {
        if p == 0.0 {
            if q < 0.0 {
                return false;
            }
        } else {
            let t = q / p;
            if p < 0.0 {
                if t > t1 {
                    return false;
                }
                if t > t0 {
                    t0 = t;
                }
            } else {
                if t < t0 {
                    return false;
                }
                if t < t1 {
                    t1 = t;
                }
            }
        }
    }
    t1 - t0 > E
}

/// A board outline: outer rings and holes (cut-outs).
#[derive(Clone, Debug, Default)]
pub struct Outline {
    pub outers: Vec<Vec<(f64, f64)>>,
    pub holes: Vec<Vec<(f64, f64)>>,
}

impl Outline {
    pub fn contains_point(&self, x: f64, y: f64) -> bool {
        self.outers.iter().any(|r| point_in_ring(x, y, r))
            && !self.holes.iter().any(|r| point_in_ring(x, y, r))
    }

    /// The rectangle lies inside the board: corners inside, no outline edge cutting through it.
    pub fn contains_rect(&self, r: &Rect) -> bool {
        if !r.corners().iter().all(|&(x, y)| self.contains_point(x, y)) {
            return false;
        }
        for ring in self.outers.iter().chain(self.holes.iter()) {
            let n = ring.len();
            for i in 0..n {
                if segment_hits_rect_interior(ring[i], ring[(i + 1) % n], r) {
                    return false;
                }
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn rotations_match_python_convention() {
        assert_eq!(rot90(1.0, 0.0, 1), (0.0, -1.0));
        assert_eq!(rot90(1.0, 0.0, 3), (-0.0, 1.0));
        assert_eq!(rot90(1.0, 2.0, 2), (-1.0, -2.0));
    }

    #[test]
    fn concave_outline_rejects_rect_across_notch() {
        // U-shaped board: the notch is x 4..6, y 0..5
        let ring = vec![
            (0.0, 0.0),
            (4.0, 0.0),
            (4.0, 5.0),
            (6.0, 5.0),
            (6.0, 0.0),
            (10.0, 0.0),
            (10.0, 10.0),
            (0.0, 10.0),
        ];
        let o = Outline {
            outers: vec![ring],
            holes: vec![],
        };
        assert!(o.contains_rect(&Rect {
            x0: 1.0,
            y0: 1.0,
            x1: 3.0,
            y1: 3.0
        }));
        assert!(!o.contains_rect(&Rect {
            x0: 3.0,
            y0: 1.0,
            x1: 7.0,
            y1: 3.0
        }));
        assert!(o.contains_rect(&Rect {
            x0: 3.0,
            y0: 6.0,
            x1: 7.0,
            y1: 9.0
        }));
    }

    proptest! {
        #[test]
        fn four_quarter_turns_are_identity(x in -50.0f64..50.0, y in -50.0f64..50.0) {
            let (mut a, mut b) = (x, y);
            for _ in 0..4 { let t = rot90(a, b, 1); a = t.0; b = t.1; }
            prop_assert!((a - x).abs() < 1e-12 && (b - y).abs() < 1e-12);
        }

        #[test]
        fn overlap_is_symmetric_and_bounded(ax in -5.0f64..5.0, ay in -5.0f64..5.0, bx in -5.0f64..5.0, by in -5.0f64..5.0,
                                            w in 0.1f64..4.0, h in 0.1f64..4.0) {
            let a = Rect { x0: ax, y0: ay, x1: ax + w, y1: ay + h };
            let b = Rect { x0: bx, y0: by, x1: bx + h, y1: by + w };
            let o = a.overlap(&b);
            prop_assert!((o - b.overlap(&a)).abs() < 1e-12);
            prop_assert!(o <= a.area().min(b.area()) + 1e-12 && o >= 0.0);
        }

        #[test]
        fn crossing_is_symmetric(a in prop::array::uniform4(-5.0f64..5.0), b in prop::array::uniform4(-5.0f64..5.0)) {
            let (p1, p2, q1, q2) = ((a[0], a[1]), (a[2], a[3]), (b[0], b[1]), (b[2], b[3]));
            prop_assert_eq!(segments_cross(p1, p2, q1, q2), segments_cross(q1, q2, p1, p2));
        }
    }
}
