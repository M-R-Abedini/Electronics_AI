# SPDX-License-Identifier: MIT
"""F3 impedance: the field solver agrees with the closed forms where both apply; mask and coupling behave."""

from __future__ import annotations

import csv

import pytest
from eda_agent.placer import impedance_golden
from eda_agent.placer.impedance import Geometry, closed, solve


@pytest.mark.parametrize("g", [
    Geometry("surface_microstrip", H1=0.2, W1=0.3),
    Geometry("surface_microstrip", H1=1.51, W1=0.15),
    Geometry("offset_stripline", H1=0.2, W1=0.15, H2=0.235),
])
def test_field_solver_matches_closed_forms(g):
    z, c = solve(g)["z0"], closed(g)
    assert abs(z - c) / c < 0.02


def test_mask_lowers_and_coupling_splits_modes():
    bare = solve(Geometry("surface_microstrip", H1=0.2, W1=0.3))["z0"]
    coated = solve(Geometry("coated_microstrip", H1=0.2, W1=0.3, C1=0.025, C2=0.025))["z0"]
    assert coated < bare
    pair = solve(Geometry("edge_coupled_surface_microstrip", H1=0.1, W1=0.125, S1=0.2032))
    assert pair["z_odd"] < pair["z_even"] and pair["zdiff"] == pytest.approx(2 * pair["z_odd"], abs=0.02)


def test_golden_compare_scores_only_filled_rows(tmp_path):
    path = impedance_golden.write_cases(tmp_path / "cases.csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert len(rows) == len(impedance_golden.cases()) and all(not r["polar_z0"] for r in rows)
    rows[2]["polar_z0"] = "54.0"  # SM03, the ~50 ohm case
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=impedance_golden.FIELDS)
        w.writeheader()
        w.writerows(rows)
    rep = impedance_golden.compare(path)
    assert rep["compared"] == 1 and abs(rep["rows"][0]["error"]) < 0.02
    assert not rep["gates"][0]["passed"] and len(rep["missing_cases"]) == rep["of"] - 1


@pytest.mark.parametrize(("column", "value", "message"), [
    ("polar_z0", "nan", "finite and positive"),
    ("H1", "2", "measured geometry differs"),
    ("id", "SM02", "duplicate Polar case"),
])
def test_golden_compare_rejects_invalid_reference_rows(tmp_path, column, value, message):
    path = impedance_golden.write_cases(tmp_path / "cases.csv")
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    rows[0].update(polar_z0="50")
    if column == "id":
        rows[1][column] = rows[0][column]
    else:
        rows[0][column] = value
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=impedance_golden.FIELDS)
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(ValueError, match=message):
        impedance_golden.compare(path)
