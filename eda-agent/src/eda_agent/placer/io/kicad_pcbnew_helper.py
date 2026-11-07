# SPDX-License-Identifier: MIT
"""Helper executed by KiCad's bundled Python (``import pcbnew``).

It is standalone (stdlib + pcbnew only) because it runs in a different
interpreter from the Electronics AI runtime.  All SWIG calls of the placer
live here so a KiCad upgrade is checked in one file (plan, risk register).

Commands::

    prepare <board> <out_board> <out_dsn> <out_json>
        Strip routing and pour zones, keep plane zones and rule areas, record
        the connection denominator, list mask openings, export Specctra DSN.
    finish  <board> <session.ses> <out_board> <out_json>
        Load the original board, strip routing, import the session, restore
        and refill every copper zone, and account for every connection.

Ownership rule: every item removed from a board stays referenced by a Python
list until the command returns.  Querying a SWIG proxy after ``Remove`` without
that reference is what broke the previous helper.
"""

import json
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


def _zone_role(board, zone):
    """Provisional F0a role rule; F0b replaces it with area-fraction inference.

    plane: a copper zone lying only on inner layers of a >2-layer board, kept
    for routing and exported by KiCad as a DSN plane.  pour: every other copper
    zone, removed for routing and refilled after import (architecture 7.3).
    """
    if zone.GetIsRuleArea():
        return "rule_area"
    outer = {pcbnew.F_Cu, pcbnew.B_Cu}
    layers = list(zone.GetLayerSet().CuStack())
    if board.GetCopperLayerCount() > 2 and layers and not any(layer in outer for layer in layers):
        return "plane"
    return "pour"


def _zone_info(board, zone):
    info = {
        "uuid": zone.m_Uuid.AsString(),
        "net": zone.GetNetname(),
        "layers": _copper_layer_names(board, zone) or [zone.GetLayerName()],
        "role": _zone_role(board, zone),
    }
    if zone.GetIsRuleArea():
        info["keepout"] = {"tracks": bool(zone.GetDoNotAllowTracks()), "vias": bool(zone.GetDoNotAllowVias()),
                           "pads": bool(zone.GetDoNotAllowPads()), "zones": bool(zone.GetDoNotAllowZoneFills())}
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


def prepare(board_path, out_board, out_dsn, out_json):
    board = pcbnew.LoadBoard(board_path)
    tracks = list(board.GetTracks())
    zones = list(board.Zones())
    zone_rows = [_zone_info(board, z) for z in zones]
    copper = [z for z, row in zip(zones, zone_rows) if row["role"] != "rule_area"]
    planes = [z for z, row in zip(zones, zone_rows) if row["role"] == "plane"]
    for item in tracks + copper:
        board.Remove(item)
    denominator = _unconnected(board)  # pads only: the fixed netlist denominator
    for zone in planes:
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


if __name__ == "__main__":
    _cmd, _args = sys.argv[1], sys.argv[2:]
    {"prepare": prepare, "finish": finish}[_cmd](*_args)
