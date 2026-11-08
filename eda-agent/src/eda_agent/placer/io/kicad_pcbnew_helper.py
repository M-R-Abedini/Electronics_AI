# SPDX-License-Identifier: MIT
"""Helper executed by KiCad's bundled Python (``import pcbnew``).

It is standalone (stdlib + pcbnew only) because it runs in a different
interpreter from the Electronics AI runtime.  All SWIG calls of the placer
live here so a KiCad upgrade is checked in one file (plan, risk register).

Commands::

    prepare <board> <keep.json> <out_board> <out_dsn> <out_json>
        Strip routing and pour zones, keep plane zones (only on the layers
        keep.json names) and rule areas, record the connection denominator,
        list obstacles, export Specctra DSN.
    finish  <board> <session.ses> <out_board> <out_json>
        Load the original board, strip routing, import the session, restore
        and refill every copper zone, and account for every connection.

Ownership rule: every item removed from a board stays referenced by a Python
list until the command returns.  Querying a SWIG proxy after ``Remove`` without
that reference is what broke the previous helper.
"""

import json
import math
import sys

import pcbnew

try:
    import wx

    _LOG_NULL = wx.LogNull()  # keep KiCad's wx log dialogs out of headless runs
except Exception:  # noqa: BLE001  # pragma: no cover - wx always ships with KiCad
    _LOG_NULL = None


def _mm(v):
    return round(pcbnew.ToMM(v), 6)


def _unconnected(board):
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    try:
        return int(conn.GetUnconnectedCount(False))
    except TypeError:  # older signature without aVisibleOnly
        return int(conn.GetUnconnectedCount())


def _copper_layer_names(board, item):
    return [board.GetLayerName(layer) for layer in item.GetLayerSet().CuStack()]


def _zone_role(zone, keep):
    """rule_area, plane (kept for routing on the layers ``keep`` names) or pour.

    ``keep`` maps zone UUID -> copper layer names; it comes from the Board IR's
    layer roles (architecture 4.2 and 7.3), so the rule lives in one place.
    """
    if zone.GetIsRuleArea():
        return "rule_area"
    return "plane" if keep.get(zone.m_Uuid.AsString()) else "pour"


def _zone_info(board, zone, keep=None):
    info = {
        "uuid": zone.m_Uuid.AsString(),
        "net": zone.GetNetname(),
        "layers": _copper_layer_names(board, zone) or [zone.GetLayerName()],
        "role": _zone_role(zone, keep or {}),
    }
    if zone.GetIsRuleArea():
        info["keepout"] = {"tracks": bool(zone.GetDoNotAllowTracks()), "vias": bool(zone.GetDoNotAllowVias()),
                           "pads": bool(zone.GetDoNotAllowPads()), "zones": bool(zone.GetDoNotAllowZoneFills()),
                           "footprints": bool(zone.GetDoNotAllowFootprints())}
    return info


def _poly_points(poly):
    outlines = []
    for i in range(poly.OutlineCount()):
        chain = poly.Outline(i)
        outlines.append([[_mm(chain.CPoint(j).x), _mm(chain.CPoint(j).y)] for j in range(chain.PointCount())])
    return outlines


def _item_poly(item, layer, max_error, clearance=0):
    poly = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(poly, layer, clearance, max_error, pcbnew.ERROR_OUTSIDE if clearance
                                 else pcbnew.ERROR_INSIDE)
    return poly


def _overlaps(a, b):
    """True when two SHAPE_POLY_SETs share area (bounding boxes if SWIG lacks the boolean)."""
    if not a.BBox().Intersects(b.BBox()):
        return False
    try:
        inter = pcbnew.SHAPE_POLY_SET(a)
        inter.BooleanIntersection(b)
        return inter.OutlineCount() > 0
    except Exception:  # noqa: BLE001  # SWIG builds without the boolean op: fall back to the bbox answer
        return True


_TEXT_CLASSES = ("PCB_TEXT", "PCB_TEXTBOX", "PCB_FIELD")
_GRAPHIC_CLASSES = ("PCB_SHAPE", "FP_SHAPE") + _TEXT_CLASSES


def _signal_clearance(board):
    """Largest effective clearance of any net on the board (net classes and rules).

    ``GetBiggestClearanceValue`` also counts unrelated constraints (1 mm on
    pic_programmer while every class is 0.25 mm), which would wall off routing
    space around copper text for no DRC reason.
    """
    best = 0
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                for layer in pad.GetLayerSet().CuStack():
                    best = max(best, int(pad.GetOwnClearance(layer)))
    return best or int(board.GetDesignSettings().m_NetSettings.GetDefaultNetclass().GetClearance())


def _graphic_sources(board):
    sources = [("board", d) for d in board.GetDrawings()]
    for fp in board.GetFootprints():
        ref = "footprint:" + fp.GetReference()
        sources += [(ref, g) for g in fp.GraphicalItems()]
        fields = fp.GetFields() if hasattr(fp, "GetFields") else [fp.Reference(), fp.Value()]
        sources += [(ref, f) for f in fields]
    return [(s, item) for s, item in sources if item.GetClass() in _GRAPHIC_CLASSES]


def _obstacles(board):
    """Graphics KiCad's DSN exporter leaves out, as candidate DSN keepouts.

    mask_aperture   a shape on F/B.Mask.  A track crossing an aperture that also
                    exposes another net is a ``solder_mask_bridge`` error.
    copper_graphic  text or a net-less shape on a copper layer, grown by the
                    biggest board clearance.  Crossing it is a short or a
                    clearance error (seen on pic_programmer copper text).
    An obstacle that overlaps a pad, or a copper shape that carries a net, is not
    exported: a keepout there would also block a legitimate connection.  Both
    are reported with the reason.
    """
    max_error = board.GetDesignSettings().m_MaxError
    clearance = _signal_clearance(board)
    copper_layers = list(board.GetEnabledLayers().CuStack())
    pads = [p for fp in board.GetFootprints() for p in fp.Pads()]
    out = []
    for source, item in _graphic_sources(board):
        layer = item.GetLayer()
        if item.GetClass() in _TEXT_CLASSES and not item.IsVisible():
            continue
        if layer in (pcbnew.F_Mask, pcbnew.B_Mask):
            kind, copper = "mask_aperture", pcbnew.F_Cu if layer == pcbnew.F_Mask else pcbnew.B_Cu
            grow = 0
        elif layer in copper_layers:
            kind, copper, grow = "copper_graphic", layer, clearance
        else:
            continue
        shape = _item_poly(item, layer, max_error)
        blocked = [pad.GetParentFootprint().GetReference() + "-" + pad.GetNumber() for pad in pads
                   if pad.IsOnLayer(copper) and _overlaps(shape, _item_poly(pad, copper, max_error))]
        net = item.GetNetname() if hasattr(item, "GetNetCode") and item.GetNetCode() > 0 else None
        keep = _item_poly(item, layer, max_error, grow) if grow else shape
        out.append({"uuid": item.m_Uuid.AsString(), "kind": kind, "source": source, "class": item.GetClass(),
                    "layer": board.GetLayerName(layer), "copper": board.GetLayerName(copper),
                    "grown_mm": _mm(grow), "outlines": _poly_points(keep), "net": net,
                    "overlapping_pads": sorted(blocked)})
    return out


def _min_track_rule(board):
    return _mm(board.GetDesignSettings().m_TrackMinWidth)


def _restrict_layers(board, zone, names):
    wanted = pcbnew.LSET()
    for layer in zone.GetLayerSet().CuStack():
        if board.GetLayerName(layer) in names:
            wanted.AddLayer(layer)
    zone.SetLayerSet(wanted)


def prepare(board_path, keep_json, out_board, out_dsn, out_json):
    board = pcbnew.LoadBoard(board_path)
    with open(keep_json, encoding="utf-8") as fh:
        keep = json.load(fh)
    tracks = list(board.GetTracks())
    zones = list(board.Zones())
    zone_rows = [_zone_info(board, z, keep) for z in zones]
    copper = [z for z, row in zip(zones, zone_rows) if row["role"] != "rule_area"]
    planes = [z for z, row in zip(zones, zone_rows) if row["role"] == "plane"]
    for item in tracks + copper:
        board.Remove(item)
    denominator = _unconnected(board)  # pads only: the fixed netlist denominator
    for zone in planes:
        _restrict_layers(board, zone, keep[zone.m_Uuid.AsString()])
        board.Add(zone)
    board.BuildConnectivity()
    obstacles = _obstacles(board)
    pcbnew.SaveBoard(out_board, board)
    exported = bool(pcbnew.ExportSpecctraDSN(board, out_dsn))
    report = {
        "denominator": denominator,
        "footprints": len(board.GetFootprints()),
        "pads": sum(len(fp.Pads()) for fp in board.GetFootprints()),
        "copper_layers": board.GetCopperLayerCount(),
        "removed_routing_items": len(tracks),
        "zones": zone_rows,
        "plane_zones_kept": len(planes),
        "obstacles": obstacles,
        "min_track_width_mm": _min_track_rule(board),
        "dsn_exported": exported,
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    del tracks, zones, copper, planes  # removed items stay alive until here


def _routing_stats(board):
    vias = segments = arcs = 0
    length = 0.0
    widths = {}
    vias_by_net = {}
    for t in board.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            vias += 1
            vias_by_net[t.GetNetname()] = vias_by_net.get(t.GetNetname(), 0) + 1
            continue
        if cls == "PCB_ARC":
            arcs += 1
        else:
            segments += 1
        length += pcbnew.ToMM(t.GetLength())
        w = _mm(t.GetWidth())
        widths[str(w)] = widths.get(str(w), 0) + 1
    return {"vias": vias, "segments": segments, "arcs": arcs, "length_mm": round(length, 3),
            "width_histogram_mm": dict(sorted(widths.items(), key=lambda kv: float(kv[0]))),
            "vias_by_net": dict(sorted(vias_by_net.items()))}


def _pad_rows(board):
    """Every pad with whether new routing copper touches it (escape evidence)."""
    by_net = {}
    for t in board.GetTracks():
        by_net.setdefault(t.GetNetCode(), []).append(t)
    rows = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() <= 0:
                continue
            attached = False
            for t in by_net.get(pad.GetNetCode(), ()):
                if t.GetClass() == "PCB_VIA":
                    points, on_layer = [t.GetPosition()], any(pad.IsOnLayer(layer) for layer in t.GetLayerSet().CuStack())
                else:
                    points, on_layer = [t.GetStart(), t.GetEnd()], pad.IsOnLayer(t.GetLayer())
                if on_layer and any(pad.HitTest(p, 0) for p in points):
                    attached = True
                    break
            rows.append({"uuid": pad.m_Uuid.AsString(), "ref": fp.GetReference(), "pad": pad.GetNumber(),
                         "net": pad.GetNetname(), "layers": _copper_layer_names(board, pad),
                         "attached": attached})
    return rows


def finish(board_path, ses_path, out_board, out_json):
    board = pcbnew.LoadBoard(board_path)
    tracks = list(board.GetTracks())
    zones = list(board.Zones())
    copper = [z for z in zones if not z.GetIsRuleArea()]
    for item in tracks + copper:
        board.Remove(item)
    denominator = _unconnected(board)
    imported = bool(pcbnew.ImportSpecctraSES(board, ses_path))
    unrouted_tracks_only = _unconnected(board)
    stats = _routing_stats(board)
    pads = _pad_rows(board)
    for zone in copper:
        board.Add(zone)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    unrouted_final = _unconnected(board)
    pcbnew.SaveBoard(out_board, board)
    report = {
        "imported": imported,
        "denominator": denominator,
        "unrouted_tracks_only": unrouted_tracks_only,
        "unrouted_final": unrouted_final,
        "routing": stats,
        "min_track_width_mm": _min_track_rule(board),
        "pads": pads,
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    del tracks, zones, copper


# --------------------------------------------------------------------------
# Board IR dump and placement write (F0b)
# --------------------------------------------------------------------------
_PAD_KIND = {pcbnew.PAD_ATTRIB_PTH: "thru_hole", pcbnew.PAD_ATTRIB_SMD: "smd",
             pcbnew.PAD_ATTRIB_CONN: "connect", pcbnew.PAD_ATTRIB_NPTH: "np_thru_hole"}
_FP_ATTRS = (("smd", "FP_SMD"), ("through_hole", "FP_THROUGH_HOLE"), ("exclude_from_bom", "FP_EXCLUDE_FROM_BOM"),
             ("exclude_from_pos", "FP_EXCLUDE_FROM_POS_FILES"), ("board_only", "FP_BOARD_ONLY"), ("dnp", "FP_DNP"),
             ("allow_missing_courtyard", "FP_ALLOW_MISSING_COURTYARD"))
_LAYER_TYPE = {0: "signal", 1: "power", 2: "mixed", 3: "jumper"}
DERIVED_COURTYARD_MARGIN_MM = 0.25  # declared parameter (architecture 4.1)


def _to_local(x, y, ox, oy, deg):
    """Inverse of geometry.transform: board mm -> footprint frame (Y down, CCW on screen)."""
    dx, dy = x - ox, y - oy
    r = math.radians(-deg)
    c, s = math.cos(r), math.sin(r)
    return round(dx * c + dy * s, 6), round(-dx * s + dy * c, 6)


def _norm(deg):
    deg = math.fmod(deg, 360.0)
    deg = deg + 360.0 if deg < 0 else deg
    return 0.0 if abs(deg - 360.0) < 1e-9 else round(deg, 6)


def _world_outlines(poly):
    out = []
    for i in range(poly.OutlineCount()):
        chain = poly.Outline(i)
        out.append([(pcbnew.ToMM(chain.CPoint(j).x), pcbnew.ToMM(chain.CPoint(j).y))
                    for j in range(chain.PointCount())])
    return out


def _area_mm2(poly):
    return round(poly.Area() / 1e12, 4)


def _attrs(fp):
    bits = fp.GetAttributes()
    names = [name for name, const in _FP_ATTRS if hasattr(pcbnew, const) and bits & getattr(pcbnew, const)]
    if hasattr(fp, "IsDNP") and fp.IsDNP() and "dnp" not in names:
        names.append("dnp")
    return sorted(names)


def _pad_dict(board, pad, ox, oy, rot):
    p = pad.GetPosition()
    lx, ly = _to_local(pcbnew.ToMM(p.x), pcbnew.ToMM(p.y), ox, oy, rot)
    size, drill = pad.GetSize(), pad.GetDrillSize()
    if hasattr(pad, "GetFPRelativeOrientation"):
        prot = _norm(pad.GetFPRelativeOrientation().AsDegrees())
    else:
        prot = _norm(pad.GetOrientationDegrees() - rot)
    return {
        "number": pad.GetNumber(),
        "net": pad.GetNetname() or None,
        "local": [lx, ly],
        "rotation": prot,
        "size": [_mm(size.x), _mm(size.y)],
        "shape": pad.ShowPadShape(pcbnew.F_Cu) if hasattr(pad, "ShowPadShape") else str(pad.GetShape()),
        "kind": _PAD_KIND.get(pad.GetAttribute(), "smd"),
        "layers": _copper_layer_names(board, pad),
        "drill": [_mm(drill.x), _mm(drill.y)] if drill.x > 0 else None,
        "function": pad.GetPinFunction() or None,
        "pin_type": pad.GetPinType() or None,
        "uuid": pad.m_Uuid.AsString(),
    }


def _courtyard(fp, ox, oy, rot, max_error):
    """Courtyard of the footprint's own side in its frame; derived from pads if absent."""
    if hasattr(fp, "BuildCourtyardCaches"):
        fp.BuildCourtyardCaches()
    poly = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    if poly.OutlineCount():
        rings = [[list(_to_local(x, y, ox, oy, rot)) for x, y in ring] for ring in _world_outlines(poly)]
        return {"source": "file", "polygons": rings}
    pts = []
    for pad in fp.Pads():
        stack = list(pad.GetLayerSet().CuStack())
        for ring in _world_outlines(_item_poly(pad, stack[0] if stack else pcbnew.F_Cu, max_error)):
            pts += [_to_local(x, y, ox, oy, rot) for x, y in ring]
    if not pts:
        return {"source": "none", "polygons": []}
    m = DERIVED_COURTYARD_MARGIN_MM
    x0, y0 = min(p[0] for p in pts) - m, min(p[1] for p in pts) - m
    x1, y1 = max(p[0] for p in pts) + m, max(p[1] for p in pts) + m
    rect = [[round(a, 6), round(b, 6)] for a, b in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {"source": "derived", "margin_mm": m, "polygons": [rect]}


def _footprint_dict(board, fp, max_error):
    pos = fp.GetPosition()
    ox, oy, rot = pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), _norm(fp.GetOrientationDegrees())
    return {
        "ref": fp.GetReference(),
        "value": fp.GetValue(),
        "lib_id": fp.GetFPIDAsString(),
        "uuid": fp.m_Uuid.AsString(),
        "x": round(ox, 6),
        "y": round(oy, 6),
        "rotation": rot,
        "side": "B" if fp.IsFlipped() else "F",
        "locked": bool(fp.IsLocked()),
        "attributes": _attrs(fp),
        "sheet": fp.GetSheetname() if hasattr(fp, "GetSheetname") else None,
        "description": fp.GetLibDescription() if hasattr(fp, "GetLibDescription") else "",
        "courtyard": _courtyard(fp, ox, oy, rot, max_error),
        "pads": [_pad_dict(board, pad, ox, oy, rot) for pad in fp.Pads()],
    }


def _netclasses(board):
    ns = board.GetDesignSettings().m_NetSettings
    classes, nets = {}, []
    for name in sorted(str(n) for n in board.GetNetsByName().keys()):  # noqa: SIM118  (SWIG map)
        if not name:
            continue
        nc = ns.GetEffectiveNetClass(name)
        cname = nc.GetName()
        if cname not in classes:
            classes[cname] = {
                "name": cname,
                "clearance": _mm(nc.GetClearance()),
                "track_width": _mm(nc.GetTrackWidth()),
                "via_diameter": _mm(nc.GetViaDiameter()),
                "via_drill": _mm(nc.GetViaDrill()),
                "diff_pair_width": _mm(nc.GetDiffPairWidth()) if nc.HasDiffPairWidth() else None,
                "diff_pair_gap": _mm(nc.GetDiffPairGap()) if nc.HasDiffPairGap() else None,
            }
        nets.append({"name": name, "class": cname})
    return sorted(classes.values(), key=lambda c: c["name"]), nets


def _rules(board):
    ds = board.GetDesignSettings()
    return {
        "min_track_width": _mm(ds.m_TrackMinWidth),
        "min_clearance": _mm(ds.m_MinClearance),
        "min_via_diameter": _mm(ds.m_ViasMinSize),
        "min_through_drill": _mm(ds.m_MinThroughDrill),
        "hole_clearance": _mm(ds.m_HoleClearance),
        "copper_edge_clearance": _mm(ds.m_CopperEdgeClearance),
        "hole_to_hole": _mm(ds.m_HoleToHoleMin),
        "board_thickness": _mm(ds.GetBoardThickness()),
    }


def _chain_points(chain):
    return [[_mm(chain.CPoint(j).x), _mm(chain.CPoint(j).y)] for j in range(chain.PointCount())]


def _outline(board):
    poly = pcbnew.SHAPE_POLY_SET()
    ok = board.GetBoardPolygonOutlines(poly, False)
    out = [{"outer": _chain_points(poly.Outline(i)),
            "holes": [_chain_points(poly.Hole(i, h)) for h in range(poly.HoleCount(i))]}
           for i in range(poly.OutlineCount())]
    return {"valid": bool(ok), "polygons": out, "area_mm2": _area_mm2(poly)}


def _zones(board):
    rows, rule_areas, fill = [], [], {}
    teardrops = 0
    for zone in board.Zones():
        if hasattr(zone, "IsTeardropArea") and zone.IsTeardropArea():
            teardrops += 1  # regenerated by KiCad on save; ignored by the IR (architecture 4.2)
            continue
        layers = list(zone.GetLayerSet().CuStack())
        names = [board.GetLayerName(layer) for layer in layers]
        if zone.GetIsRuleArea():
            outline = [[[round(x, 6), round(y, 6)] for x, y in ring] for ring in _world_outlines(zone.Outline())]
            rule_areas.append({"uuid": zone.m_Uuid.AsString(), "layers": names, "polygons": outline,
                               "keepout": _zone_info(board, zone)["keepout"]})
            continue
        areas = {}
        for layer, name in zip(layers, names):
            area = _area_mm2(zone.GetFilledPolysList(layer)) if zone.IsFilled() else 0.0
            areas[name] = area
            by_net = fill.setdefault(name, {})
            by_net[zone.GetNetname()] = round(by_net.get(zone.GetNetname(), 0.0) + area, 4)
        rows.append({"uuid": zone.m_Uuid.AsString(), "net": zone.GetNetname() or None, "layers": names,
                     "priority": int(zone.GetAssignedPriority()), "filled": bool(zone.IsFilled()),
                     "filled_area_mm2": areas, "outline_area_mm2": _area_mm2(zone.Outline())})
    return rows, rule_areas, fill, teardrops


def _routing_geometry(board):
    """Existing copper routing for review renders: segments (arcs as 3 points) and vias."""
    tracks, vias = [], []
    for t in board.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            p = t.GetPosition()
            vias.append([_mm(p.x), _mm(p.y), _mm(t.GetWidth(pcbnew.F_Cu) if cls == "PCB_VIA" else 0), t.GetNetname()])
            continue
        pts = [t.GetStart(), t.GetMid(), t.GetEnd()] if cls == "PCB_ARC" else [t.GetStart(), t.GetEnd()]
        tracks.append({"layer": pcbnew.BOARD.GetStandardLayerName(t.GetLayer()), "width": _mm(t.GetWidth()),
                       "net": t.GetNetname(), "points": [[_mm(q.x), _mm(q.y)] for q in pts]})
    return {"tracks": tracks, "vias": vias}


def dump(board_path, out_json):
    """Everything the Board IR needs, resolved by KiCad itself (net classes, rules, courtyards)."""
    board = pcbnew.LoadBoard(board_path)
    max_error = board.GetDesignSettings().m_MaxError
    classes, nets = _netclasses(board)
    zones, rule_areas, fill, teardrops = _zones(board)
    footprints = [_footprint_dict(board, fp, max_error) for fp in board.GetFootprints()]
    counts = {}
    for fp in footprints:
        for pad in fp["pads"]:
            if pad["net"]:
                counts[pad["net"]] = counts.get(pad["net"], 0) + 1
    for net in nets:
        net["pads"] = counts.get(net["name"], 0)
    data = {
        "source": {"path": board_path, "eda": "kicad", "format_version": int(board.GetFileFormatVersionAtLoad()),
                   "eda_version": pcbnew.Version() if hasattr(pcbnew, "Version") else pcbnew.GetBuildVersion()},
        "copper_layers": [{"name": board.GetLayerName(layer), "canonical": pcbnew.BOARD.GetStandardLayerName(layer),
                           "id": int(layer), "index": i,
                           "type": _LAYER_TYPE.get(board.GetLayerType(layer), "signal")}
                          for i, layer in enumerate(board.GetEnabledLayers().CuStack())],
        "outline": _outline(board),
        "rules": _rules(board),
        "netclasses": classes,
        "nets": nets,
        "footprints": footprints,
        "zones": zones,
        "rule_areas": rule_areas,
        "zone_fill_by_layer_mm2": fill,
        "teardrops": teardrops,
        "obstacles": _obstacles(board),
        "routing_items": len(board.GetTracks()),
        "routing": _routing_geometry(board),
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def place(board_path, placement_json, out_board, strip_routing, out_json):
    """Write a placement ``{uuid: {x, y, rotation, side}}`` into a new board file."""
    board = pcbnew.LoadBoard(board_path)
    with open(placement_json, encoding="utf-8") as fh:
        placement = json.load(fh)
    by_uuid = {fp.m_Uuid.AsString(): fp for fp in board.GetFootprints()}
    moved = flipped = 0
    for uuid, pose in placement.items():
        fp = by_uuid.get(uuid)
        if fp is None:
            continue
        if (pose["side"] == "B") != fp.IsFlipped():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            flipped += 1
        fp.SetOrientationDegrees(float(pose["rotation"]))
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(float(pose["x"])), pcbnew.FromMM(float(pose["y"]))))
        moved += 1
    removed = list(board.GetTracks()) if strip_routing == "1" else []
    for item in removed:
        board.Remove(item)
    pcbnew.SaveBoard(out_board, board)
    with open(out_json, "w", encoding="utf-8") as fh:  # not stdout: SWIG writes leak notices there
        json.dump({"placed": moved, "flipped": flipped, "missing": sorted(set(placement) - set(by_uuid)),
                   "removed_routing": len(removed)}, fh)
    del removed


if __name__ == "__main__":
    _cmd, _args = sys.argv[1], sys.argv[2:]
    {"prepare": prepare, "finish": finish, "dump": dump, "place": place}[_cmd](*_args)
