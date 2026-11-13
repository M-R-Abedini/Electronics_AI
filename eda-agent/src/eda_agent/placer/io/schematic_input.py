# SPDX-License-Identifier: MIT
"""Cold placement input using a KiCad schematic and a PCB geometry template."""

from __future__ import annotations

import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree

from shapely.geometry import Point

from ..eval.legality import board_polygon
from ..ir import Board, derive
from ..place.problem import fixed_parts
from ..toolchain import kicad, sha256_file
from .kicad_sch import read_pins


def read_netlist(path: Path) -> ElementTree.Element:
    with tempfile.TemporaryDirectory(prefix="eai_netlist_") as work:
        out = Path(work) / "netlist.xml"
        subprocess.run([str(kicad().cli), "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(path)],
                       capture_output=True, text=True, check=True, timeout=60)
        root = ElementTree.parse(out).getroot()
        if not root.findall("nets/net/node"):
            raise ValueError("KiCad exported no schematic net endpoints; check the schematic format and project libraries")
        return root


def cold_input(reference: Board, schematic: Path) -> tuple[Board, dict]:
    """Use schematic connectivity after verifying the original PCB is an equivalent template."""
    root = read_netlist(schematic)
    board = reference.copy()
    by_ref = {fp.ref: fp for fp in board.footprints}
    counts = Counter(fp.ref for fp in board.footprints)
    if any(counts[fp.ref] > 1 and fp.nets for fp in board.footprints):
        raise ValueError("PCB template contains duplicate electrically connected footprint references")
    nodes = {(fp.ref, str(pad.number)) for fp in board.footprints for pad in fp.pads if pad.net}
    pcb_groups = defaultdict(set)
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net:
                pcb_groups[pad.net].add((fp.ref, str(pad.number)))
    by_group = {frozenset(group): net for net, group in pcb_groups.items()}
    if sum(map(len, pcb_groups.values())) != len(nodes):
        raise ValueError("PCB template has pad numbers assigned to multiple nets")
    pad_metadata = {}
    verified = set()
    for net in root.findall("nets/net"):
        group = frozenset((n.attrib["ref"], n.attrib["pin"]) for n in net.findall("node"))
        if group not in by_group:
            if len(group) == 1 and group.isdisjoint(nodes) and all(ref in by_ref for ref, _ in group):
                # KiCad schematic export names dangling pins that the PCB leaves unconnected.
                continue
            raise ValueError(f"schematic net {net.attrib['name']} does not match PCB endpoints ({len(group)} pins)")
        pcb_net = by_group[group]
        if pcb_net in verified:
            raise ValueError(f"multiple schematic nets map to PCB net {pcb_net}")
        verified.add(pcb_net)
        for n in net.findall("node"):
            pad_metadata[n.attrib["ref"], n.attrib["pin"]] = (pcb_net, n.get("pinfunction"), n.get("pintype"))
    missing = set(pcb_groups) - verified
    if missing:
        raise ValueError(f"{len(missing)} PCB nets absent from equivalent schematic connectivity")
    components = {c.attrib["ref"]: c for c in root.findall("components/comp")}
    if missing_refs := set(components) - set(by_ref):
        raise ValueError(f"{len(missing_refs)} schematic components absent from PCB template")
    if reference.outline_box is None or not reference.outline_valid:
        raise ValueError("valid board outline required for cold placement")
    x0, y0, x1, y1 = reference.outline_box
    anchors = fixed_parts(reference)
    board.placement_anchors = anchors.copy()
    outline = board_polygon(reference)
    center = Point((x0 + x1) / 2, (y0 + y1) / 2)
    if outline is None or outline.is_empty:
        raise ValueError("board outline has no usable area")
    if not outline.covers(center):
        center = outline.representative_point()
    origins: dict = {}
    sources = {schematic}
    for sheet in root.findall("design/sheet/title_block/source"):
        if sheet.text:
            path = schematic.parent / sheet.text
            if path.is_file():
                sources.add(path.resolve())
    # Keep sheet instances distinct via the netlist's hierarchy; use a symbol
    # origin only when its reference has one origin within the project.
    for path in sorted(sources):
        if path.suffix.lower() == ".kicad_sch":
            read_pins(path, positions=origins)
    for fp in board.footprints:
        fp.schematic_position = None
        if c := components.get(fp.ref):
            fp.value = c.findtext("value") or fp.value
            sheet = c.find("sheetpath")
            if sheet is not None:
                fp.sheet = sheet.get("names")
            positions = origins.get(fp.ref, set())
            if len(positions) == 1:
                _, x, y = next(iter(positions))
                fp.schematic_position = (x, y)
        for pad in fp.pads:
            if data := pad_metadata.get((fp.ref, str(pad.number))):
                pad.net, name, pin_type = data
                pad.function = name if name not in (None, "", "~", str(pad.number)) else None
                pad.pin_type = pin_type
        if fp.ref in anchors:
            fp.locked = True  # freeze exactly the constraints supplied by the geometry template
        else:
            fp.x, fp.y, fp.rotation = center.x, center.y, 0.0
    # Keep declared construction constraints and assembly sides. Discard copper-derived placement evidence.
    board.routing, board.routing_items, board.teardrops = {}, 0, 0
    board.zones, board.zone_fill_by_layer_mm2 = [], {}
    board.obstacles = []
    board.source = {**board.source, "schematic": {"path": str(schematic), "sha256": sha256_file(schematic),
                                                  "sheets": {str(p): sha256_file(p) for p in sorted(sources)}}}
    return derive(board), {"verified_nets": len(verified), "verified_pad_endpoints": len(pad_metadata),
                           "schematic_components": len(components), "anchors": anchors,
                           "reset_poses": len(board.footprints) - len(anchors),
                           "anchor_reasons": dict(Counter(anchors.values())), "assembly_sides": "reference"}
