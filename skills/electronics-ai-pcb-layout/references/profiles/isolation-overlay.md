# Isolation overlay

Compose this overlay with analog, digital or power profiles where a verified
barrier separates domains. Establish the applicable product standard, working
voltage, transient/impulse conditions, isolation category, pollution/material
conditions, altitude and assembly construction for the actual scope. A test
voltage alone does not establish the required distances.

TI's [SSZTBV8 motor-drive isolation article](https://www.ti.com/document-viewer/lit/html/SSZTBV8/GUID-07C2F756-E174-4D11-BF2D-3E5BE1BDA530)
(November 2015, steps 1-7) illustrates why isolation type, transient/system
conditions and working voltage feed different insulation requirements. It is
application context, not a substitute for the applicable current standard or
a source of universal PCB distances.

Model the actual layer-aware barrier region, slots/cutouts and explicitly
permitted component crossings. A component whitelist is not permission for
arbitrary copper to cross. Check conductors, pads, returns, shields, stitching,
test points, thermal copper and mounting hardware for unintended bridges.

Distinguish air clearance, surface creepage and internal insulation. A 2D
distance checker cannot certify a 3D/surface path it does not model. Require
coverage for each applicable check. Missing safety inputs block that dependent
scope; an independently authorized low-voltage section can still progress.
Do not relax a barrier to improve wirelength, congestion or appearance.

## Engine-Enforced Barrier & DRU Checks (`dru.py` + `emc_safety.py`)

- **Compound `.kicad_dru` compilation:** Custom KiCad rules using `hasNetclass`,
  `A.NetName`, and compound `A.NetName && B.NetName` expressions (e.g., `2.5 mm`
  mains L-N clearance or `8.0 mm` primary-to-secondary reinforced creepage) are
  compiled directly into deduplicated Specctra DSN `(class_class ...)` rules
  (`route/dru.py`) and into placement repulsions (`place/problem.py`).
- **All-layer vertical moat & mounting-hole keepouts:** Verify zero vertical
  primary-to-secondary overlap across `< 0.40 mm` prepreg (Rule 18
  `hv_vertical_prepreg_overlap`), `>= 6.0 mm` HV copper clearance around NPTH
  mounting holes (Rule 10 `mounting_hole_copper_clearance`), zero copper pour
  under unfused mains EMI input stages (Rule 11), and geometric HV pad creepage
  (Rule 12).
