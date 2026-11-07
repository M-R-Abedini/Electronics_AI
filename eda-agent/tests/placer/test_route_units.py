# SPDX-License-Identifier: MIT
"""Unit tests of the F0a route-check pieces that need no KiCad or Java."""

from __future__ import annotations

from eda_agent.placer.route import drc, dsn
from eda_agent.placer.route.check import classify_unrouted
from eda_agent.placer.route.freerouting import RouterConfig, parse_log

DSN = """(pcb x.dsn
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu (type signal))
    (layer B.Cu (type signal))
    (boundary (path pcb 0 0 0 1000 0))
    (via "Via[0-1]_500:300_um")
    (rule (width 150))
  )
)
"""


def test_keepout_inserted_before_via_with_y_negated():
    k = dsn.Keepout("mask_a", "F.Cu", ((1.0, 2.0), (1.5, 2.0), (1.5, 2.25)))
    out = dsn.add_keepouts(DSN, [k])
    assert '(keepout "mask_a" (polygon F.Cu 0 1000 -2000 1500 -2000 1500 -2250))' in out
    assert out.index("(keepout") < out.index('(via "Via')
    assert out.replace(dsn.keepout_text(k, 1000.0), "") == DSN


def test_keepout_quotes_unusual_layer_names_and_noop_when_empty():
    k = dsn.Keepout("m", "Top Layer", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)))
    assert '(polygon "Top Layer" 0' in dsn.keepout_text(k, 1000.0)
    assert dsn.add_keepouts(DSN, []) == DSN
    assert dsn.layers(DSN) == ["F.Cu", "B.Cu"]


def test_router_flags_are_the_architecture_configuration():
    flags = RouterConfig(neck_width_um=150).flags()
    assert "--router.automatic_neckdown=false" in flags
    assert "--router.fanout.enabled=false" in flags
    assert "--router.neck_width_um=150" in flags
    assert "--router.strict_drc=true" in flags
    assert "--router.optimizer.max_threads=1" in flags


def test_parse_log_reads_passes_stop_and_pin_violations():
    log = (
        "  - 1 unfixable violation(s):\n      * 1 pin-to-pin clearance violations:\n"
        "          - JP1.2 [net VIN] <-> JP1.1 [net Net-(J1-Pin_1)]\n"
        "Auto-routing pass #1 on board 'a' was completed\nAuto-routing pass #19 on board 'b'\n"
        "The router's best score (940.94) has not improved by more than 0.5 points since pass #9.\n"
        "Auto-routing stage completed: started with 226 unrouted nets, final score: 940.94 (10 unrouted and 1 violation)\n"
    )
    got = parse_log(log)
    assert got["passes"] == 19 and got["stop_reason"] == "converged" and got["log_unrouted"] == 10
    assert got["pre_existing_violations"] == ["JP1.2 [net VIN] <-> JP1.1 [net Net-(J1-Pin_1)]"]


def _v(kind, *descs, severity="error"):
    return {"type": kind, "severity": severity, "items": [{"description": d} for d in descs]}


def test_drc_attribution_separates_router_preexisting_and_new():
    reference = {"violations": [_v("clearance", "Pad 1 [VIN] of JP1", "Pad 2 [X] of JP1")]}
    routed = {"violations": [
        _v("clearance", "Pad 1 [VIN] of JP1", "Pad 2 [X] of JP1"),        # pre-existing
        _v("track_width", "Track [GND] on F.Cu, length 1 mm"),             # router
        _v("solder_mask_bridge", "Polygon on F.Mask", "Via [GND] on F.Cu - B.Cu"),  # router
        _v("clearance", "Pad 1 [A] of R1", "Zone [GND] on F.Cu"),          # new, not routing
        _v("lib_footprint_issues", "Footprint R1", severity="warning"),
    ]}
    a = drc.attribute(routed, reference)
    c = a.counts()
    assert (c["router_caused"], c["pre_existing"], c["new_non_routing"], c["warnings"]) == (2, 1, 1, 1)
    assert c["failing_by_type"] == {"clearance": 1, "solder_mask_bridge": 1, "track_width": 1}


def test_unrouted_causes_use_net_facts_not_reported_items():
    def pad(uuid, net, ref, layers, attached):
        return {"uuid": uuid, "net": net, "ref": ref, "pad": "1", "layers": layers, "attached": attached}

    finished = {
        "routing": {"vias_by_net": {"N2": 1}},
        "pads": [
            pad("a1", "N1", "U1", ["B.Cu"], False), pad("a2", "N1", "J1", ["F.Cu"], True),
            pad("b1", "N2", "U1", ["B.Cu"], False), pad("b2", "N2", "J2", ["F.Cu"], True),
            pad("c1", "N3", "R1", ["F.Cu"], True), pad("c2", "N3", "J3", ["F.Cu", "B.Cu"], True),
        ],
    }

    def entry(net, first):
        return {"items": [{"description": first, "uuid": "x"},
                          {"description": f"Pad 2 [{net}] of Y", "uuid": "y"}]}

    # The same net reported through a track or through a pad gets the same cause.
    for first in ("Track [{}] on F.Cu, length 1.4 mm", "Pad 1 [{}] of U1 on B.Cu"):
        rows = classify_unrouted([entry(n, first.format(n)) for n in ("N3", "N1", "N2")], finished, "converged")
        assert [(r["net"], r["cause"]) for r in rows] == [
            ("N1", "via_blocked"), ("N2", "escape_blocked"), ("N3", "path_blocked")]
        assert rows[1]["unescaped_pads"] == ["U1-1"]
        assert all(r["run_stop"] == "converged" and r["stage"] == "S6c" for r in rows)


def test_keepouts_skip_pad_overlap_and_net_copper_with_reason():
    from eda_agent.placer.route.check import keepouts_from

    square = [[0, 0], [1, 0], [1, 1]]
    rows = [
        {"uuid": "a" * 36, "kind": "mask_aperture", "source": "board", "copper": "F.Cu",
         "outlines": [square], "overlapping_pads": [], "net": None},
        {"uuid": "b" * 36, "kind": "mask_aperture", "source": "footprint:JP1", "copper": "B.Cu",
         "outlines": [square], "overlapping_pads": ["JP1-1"], "net": None},
        {"uuid": "c" * 36, "kind": "copper_graphic", "source": "board", "copper": "F.Cu",
         "outlines": [square], "overlapping_pads": [], "net": "GND"},
    ]
    keep, skipped = keepouts_from(rows)
    assert [k.name for k in keep] == ["mask_aperture_aaaaaaaa_0"]
    assert [s["reason"] for s in skipped] == ["overlaps pads JP1-1", "carries net GND"]
