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

import hashlib
import json
import math
import os
import re
import sys
import tempfile
from uuid import NAMESPACE_URL, uuid5

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

    # Unconnected/NPTH pads with local clearance, large solder-mask expansion, or NPTH hole clearance
    # (e.g. fiducials FID* and NPTH mounting holes MH*): KiCad's DSN exporter omits per-pad local
    # clearance and NPTH hole-to-copper rules, causing Freerouting to violate them.
    ds = board.GetDesignSettings()
    def_clr = int(ds.m_NetSettings.GetDefaultNetclass().GetClearance())
    hole_clr = int(getattr(ds, "m_HoleClearance", 0))
    board_mask = int(getattr(ds, "m_SolderMaskExpansion", 0))
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            net_name = pad.GetNetname() or ""
            is_npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
            is_unconnected = (not net_name) or net_name.startswith("unconnected-(")
            if not is_npth and not is_unconnected:
                continue
            raw_clr = pad.GetLocalClearance("") if hasattr(pad, "GetLocalClearance") else None
            loc_clr = int(raw_clr) if raw_clr is not None else 0
            raw_mask = pad.GetLocalSolderMaskMargin() if hasattr(pad, "GetLocalSolderMaskMargin") else None
            loc_mask = int(raw_mask) if raw_mask is not None else 0
            if is_npth:
                grow = max(loc_clr, hole_clr, def_clr)
                target_layers = list(copper_layers)
            else:
                if loc_clr <= def_clr + pcbnew.FromMM(0.02) and loc_mask <= board_mask + pcbnew.FromMM(0.05):
                    continue
                grow = max(loc_clr, loc_mask)
                target_layers = [lyr for lyr in copper_layers if pad.IsOnLayer(lyr)]
            for cu_lyr in target_layers:
                keep = _item_poly(pad, cu_lyr, max_error, grow)
                if keep.OutlineCount() == 0 and is_npth:
                    keep = _item_poly(pad, pcbnew.F_Cu, max_error, grow)
                if keep.OutlineCount() == 0:
                    continue
                blocked = [
                    other.GetParentFootprint().GetReference() + "-" + other.GetNumber()
                    for other in pads
                    if other is not pad
                    and other.GetParentFootprint() is not fp
                    and other.GetNetCode() > 0
                    and not (other.GetNetname() or "").startswith("unconnected-(")
                    and other.IsOnLayer(cu_lyr)
                    and _overlaps(keep, _item_poly(other, cu_lyr, max_error))
                ]
                out.append({
                    "uuid": pad.m_Uuid.AsString(),
                    "kind": "pad_keepout",
                    "source": "footprint:" + ref,
                    "class": "PAD",
                    "layer": board.GetLayerName(cu_lyr),
                    "copper": board.GetLayerName(cu_lyr),
                    "grown_mm": _mm(grow),
                    "outlines": _poly_points(keep),
                    "net": None,
                    "overlapping_pads": sorted(blocked),
                })

    # Interior Edge.Cuts holes/cutouts enlarged by copper_edge_clearance: KiCad's DSN exporter
    # emits raw cutout shapes without edge clearance, so Freerouting can route inside edge clearance.
    edge_clr = int(getattr(ds, "m_CopperEdgeClearance", 0))
    if edge_clr > 0:
        outline_poly = pcbnew.SHAPE_POLY_SET()
        if board.GetBoardPolygonOutlines(outline_poly, False):
            for oi in range(outline_poly.OutlineCount()):
                for hi in range(outline_poly.HoleCount(oi)):
                    hole_chain = outline_poly.Hole(oi, hi)
                    if hole_chain.PointCount() < 3:
                        continue
                    hp = pcbnew.SHAPE_POLY_SET()
                    hp.AddOutline(hole_chain)
                    hp.Inflate(edge_clr, 16, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS)
                    rings = _poly_points(hp)
                    if not rings:
                        continue
                    for cu_lyr in copper_layers:
                        blocked = [
                            other.GetParentFootprint().GetReference() + "-" + other.GetNumber()
                            for other in pads
                            if other.IsOnLayer(cu_lyr)
                            and _overlaps(hp, _item_poly(other, cu_lyr, max_error))
                        ]
                        out.append({
                            "uuid": f"edge_cutout_{oi}_{hi}_{int(cu_lyr)}",
                            "kind": "edge_cutout",
                            "source": "board:Edge.Cuts",
                            "class": "EDGE_CUTOUT",
                            "layer": "Edge.Cuts",
                            "copper": board.GetLayerName(cu_lyr),
                            "grown_mm": _mm(edge_clr),
                            "outlines": rings,
                            "net": None,
                            "overlapping_pads": sorted(blocked),
                        })
    return out


def _min_track_rule(board):
    return _mm(board.GetDesignSettings().m_TrackMinWidth)


def _restrict_layers(board, zone, names):
    wanted = pcbnew.LSET()
    for layer in zone.GetLayerSet().CuStack():
        if board.GetLayerName(layer) in names:
            wanted.AddLayer(layer)
    zone.SetLayerSet(wanted)


def _clamp_illegal_netclass_vias(board):
    """Clamp netclass through-via drill/diameter on the temporary DSN-export board copy when
    the project's own netclass violates board min_through_hole_diameter or min_via_diameter.

    Prevents KiCad ExportSpecctraDSN from handing Freerouting an un-manufacturable default via
    padstack (e.g. 0.40 mm drill when board min_through_hole_diameter is 0.508 mm).
    """
    ds = board.GetDesignSettings()
    min_drill = int(getattr(ds, "m_MinThroughDrill", 0))
    min_dia = int(getattr(ds, "m_ViasMinSize", 0))
    min_ann = int(getattr(ds, "m_ViasMinAnnularWidth", 0))
    if min_drill <= 0 and min_dia <= 0:
        return {}
    ns = getattr(ds, "m_NetSettings", None)
    if ns is None:
        return {}
    clamped = {}
    classes = list(ns.GetNetClasses().values()) if hasattr(ns, "GetNetClasses") else []
    if hasattr(ns, "GetDefaultNetclass"):
        classes.insert(0, ns.GetDefaultNetclass())
    seen = set()
    for nc in classes:
        name = nc.GetName()
        if name in seen:
            continue
        seen.add(name)
        old_d = int(nc.GetViaDiameter())
        old_h = int(nc.GetViaDrill())
        new_h = max(old_h, min_drill) if old_h > 0 else min_drill
        new_d = max(old_d, min_dia, new_h + 2 * min_ann) if new_h > 0 else max(old_d, min_dia)
        if new_h != old_h or new_d != old_d:
            nc.SetViaDrill(int(new_h))
            nc.SetViaDiameter(int(new_d))
            clamped[name] = {
                "from_mm": [round(old_d / 1e6, 4), round(old_h / 1e6, 4)],
                "to_mm": [round(new_d / 1e6, 4), round(new_h / 1e6, 4)],
            }
    return clamped


def prepare(board_path, keep_json, out_board, out_dsn, out_json):
    board = pcbnew.LoadBoard(board_path)
    with open(keep_json, encoding="utf-8") as fh:
        keep = json.load(fh)
    tracks = list(board.GetTracks())
    fixed = [t for t in tracks if t.IsLocked()]  # macro and S6a copper: exported to the DSN as fixed wiring
    zones = list(board.Zones())
    zone_rows = [_zone_info(board, z, keep) for z in zones]
    copper = [z for z, row in zip(zones, zone_rows) if row["role"] != "rule_area"]
    planes = [z for z, row in zip(zones, zone_rows) if row["role"] == "plane"]
    for item in tracks + copper:
        board.Remove(item)
    denominator = _unconnected(board)  # pads only: the fixed netlist denominator
    for item in fixed:
        board.Add(item)
    for zone in planes:
        _restrict_layers(board, zone, keep[zone.m_Uuid.AsString()])
        board.Add(zone)
    board.BuildConnectivity()
    clamped_vias = _clamp_illegal_netclass_vias(board)
    obstacles = _obstacles(board)
    pcbnew.SaveBoard(out_board, board)
    exported = bool(pcbnew.ExportSpecctraDSN(board, out_dsn))
    report = {
        "denominator": denominator,
        "footprints": len(board.GetFootprints()),
        "pads": sum(len(fp.Pads()) for fp in board.GetFootprints()),
        "copper_layers": board.GetCopperLayerCount(),
        "removed_routing_items": len(tracks) - len(fixed),
        "fixed_routing_items": len(fixed),
        "zones": zone_rows,
        "plane_zones_kept": len(planes),
        "obstacles": obstacles,
        "netclass_vias_clamped": clamped_vias,
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


_VIA_TYPE_NAMES = {
    pcbnew.VIATYPE_MICROVIA: "micro",
    pcbnew.VIATYPE_BLIND: "blind",
    pcbnew.VIATYPE_BURIED: "buried",
    pcbnew.VIATYPE_THROUGH: "through",
}
_VIA_TYPE_BY_NAME = {v: k for k, v in _VIA_TYPE_NAMES.items()}
_SES_VIA_PADSTACK_RE = re.compile(
    r'\(padstack\s+"?(Via\[[^\]]*\]_(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)_um)"?'
)


def _via_width_nm(via):
    try:
        return int(via.GetWidth(via.TopLayer()))
    except TypeError:
        return int(via.GetWidth())


def _signature(item):
    cls = item.GetClass()
    if cls == "PCB_VIA":
        p = item.GetPosition()
        return (
            cls,
            p.x,
            p.y,
            _via_width_nm(item),
            int(item.GetDrillValue()),
            item.GetNetname(),
            int(item.GetViaType()),
            int(item.TopLayer()),
            int(item.BottomLayer()),
        )
    mid = (item.GetMid().x, item.GetMid().y) if cls == "PCB_ARC" else None
    a, b = item.GetStart(), item.GetEnd()
    ends = tuple(sorted(((a.x, a.y), (b.x, b.y))))
    return (cls, item.GetLayer(), ends, mid, item.GetWidth(), item.GetNetname())


def _sanitize_ses_placement(ses_text, board):
    """Drop SES (place <ref> ...) entries whose ref does not exist uniquely on board.

    Prevents KiCad's ImportSpecctraSES from aborting the whole import when DSN export
    renamed duplicate REF** footprints to REF**_1 / REF**_2.
    """
    ref_counts = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        ref_counts[ref] = ref_counts.get(ref, 0) + 1
    unique_refs = {ref for ref, cnt in ref_counts.items() if cnt == 1}

    p_idx = ses_text.find("(placement")
    if p_idx < 0:
        return ses_text, []
    r_idx = ses_text.find("(routes", p_idx)
    if r_idx < 0:
        return ses_text, []
    block = ses_text[p_idx:r_idx]
    skipped = []

    def _keep_or_drop(m):
        raw_ref = m.group(1)
        ref = raw_ref[1:-1] if len(raw_ref) >= 2 and raw_ref[0] == '"' and raw_ref[-1] == '"' else raw_ref
        if ref in unique_refs:
            return m.group(0)
        skipped.append(ref)
        return ""

    cleaned = re.sub(r'\(place\s+("[^"]+"|[^\s()]+)[^()]*\)', _keep_or_drop, block)
    if not skipped:
        return ses_text, []
    return ses_text[:p_idx] + cleaned + ses_text[r_idx:], sorted(set(skipped))


def _restore_via_drills(board, ses_text, before_ids):
    """Restore exact via drills from SES Via[..]_<D>:<d>_um padstack names on newly imported vias.

    Works around KiCad 10.0.8 specctra_import.cpp clearing a drill that equals Default's drill
    so it resolves through the net's own class (shrinking annular ring on custom-class nets).
    """
    drills = {}
    for _name, dia, drill in _SES_VIA_PADSTACK_RE.findall(ses_text or ""):
        d_nm = round(float(dia) * 1000.0)
        h_nm = round(float(drill) * 1000.0)
        if d_nm in drills and drills[d_nm] != h_nm:
            drills[d_nm] = None
        else:
            drills[d_nm] = h_nm
    if not drills:
        return {"count": 0, "changes": {}}
    changes = {}
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.m_Uuid.AsString() in before_ids:
            continue
        dia = _via_width_nm(t)
        want = drills.get(dia)
        have = int(t.GetDrillValue())
        if want and want != have and want < dia:
            t.SetDrill(int(want))
            key = f"{dia / 1e6:g}/{have / 1e6:g} -> {dia / 1e6:g}/{want / 1e6:g} mm"
            changes[key] = changes.get(key, 0) + 1
    return {"count": sum(changes.values()), "changes": changes}


def _reconcile_fixed(board, fixed):
    """Put the original fixed (locked) copper back after an SES import.

    The session carries the fixed wiring Freerouting received, re-created as new
    items; those copies are removed and the original objects (same UUIDs, still
    locked) are added, so macro and S6a copper are the very same items.
    """
    originals = {_signature(t) for t in fixed}
    copies = [t for t in board.GetTracks() if _signature(t) in originals]
    matched = {_signature(t) for t in copies}
    for t in copies:
        board.Delete(t)
    for t in fixed:
        board.Add(t)
    return {"count": len(fixed), "session_copies_replaced": len(copies),
            "missing_from_session": len(originals - matched)}


def finish(board_path, ses_path, out_board, out_json):
    board = pcbnew.LoadBoard(board_path)
    tracks = list(board.GetTracks())
    fixed = [t for t in tracks if t.IsLocked()]
    zones = list(board.Zones())
    copper = [z for z in zones if not z.GetIsRuleArea()]
    # teardrops belong to the routing they were made for; the old routing is gone, so they are not restored
    stale = [z for z in copper if hasattr(z, "IsTeardropArea") and z.IsTeardropArea()]
    for item in tracks + copper:
        board.Remove(item)
    copper = [z for z in copper if not (hasattr(z, "IsTeardropArea") and z.IsTeardropArea())]
    denominator = _unconnected(board)
    with open(ses_path, encoding="utf-8", errors="replace") as fh:
        ses_text = fh.read()
    clean_ses, placement_skipped = _sanitize_ses_placement(ses_text, board)
    import_path = ses_path
    tmp_ses = None
    if placement_skipped:
        fd, tmp_ses = tempfile.mkstemp(prefix="eai_ses_", suffix=".ses")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(clean_ses)
        import_path = tmp_ses
    before_via_ids = {t.m_Uuid.AsString() for t in fixed if t.GetClass() == "PCB_VIA"}
    try:
        # ImportSpecctraSES deletes every track on the board, so the fixed copper stays out
        # (and alive in ``fixed``) until the import is done, then replaces its session copy.
        imported = bool(pcbnew.ImportSpecctraSES(board, import_path))
    finally:
        if tmp_ses and os.path.exists(tmp_ses):
            os.unlink(tmp_ses)
    fixed_report = _reconcile_fixed(board, fixed)
    drills_restored = _restore_via_drills(board, clean_ses, before_via_ids)
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
        "stale_teardrops_dropped": len(stale),
        "ses_placement_skipped": placement_skipped,
        "via_drills_restored": drills_restored,
        "fixed": fixed_report,
        "denominator": denominator,
        "unrouted_tracks_only": unrouted_tracks_only,
        "unrouted_final": unrouted_final,
        "routing": stats,
        "min_track_width_mm": _min_track_rule(board),
        "pads": pads,
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    del tracks, zones, copper, stale


def lock_routing(board_path, out_board, out_json):
    """Lock all surviving tracks and vias on a copy so a guarded re-route pass preserves clean copper."""
    board = pcbnew.LoadBoard(board_path)
    locked = 0
    for t in board.GetTracks():
        if not t.IsLocked():
            t.SetLocked(True)
            locked += 1
    pcbnew.SaveBoard(out_board, board)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"newly_locked": locked, "total_items": len(list(board.GetTracks()))}, fh, indent=1)


def sanitize(board_path, remove_json, out_board, out_json):
    """Deterministically remove unlocked routing items implicated in router DRC violations (plan F0a).

    Locked copper (macro / S6a), pads, footprints, and zones are never deleted.
    Recomputes exact tracks-only and zone-filled connectivity so accounting stays exact.
    """
    board = pcbnew.LoadBoard(board_path)
    with open(remove_json, encoding="utf-8") as fh:
        remove_uuids = set(json.load(fh))
    removed_tracks = removed_vias = 0
    to_remove = []
    affected_nets = set()
    for t in list(board.GetTracks()):
        if t.IsLocked():
            continue
        if t.m_Uuid.AsString() in remove_uuids:
            to_remove.append(t)
            affected_nets.add(t.GetNetCode())
            if t.GetClass() == "PCB_VIA":
                removed_vias += 1
            else:
                removed_tracks += 1
    for t in to_remove:
        board.Remove(t)

    # Iteratively prune resulting dead-end dangling tracks and orphan vias on affected nets
    # (never removing locked S6a/macro items) so sanitize never leaves track_dangling warnings!
    pruned_dangling_tracks = 0
    pruned_dangling_vias = 0
    if affected_nets:
        pads_by_net = {}
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                nc = pad.GetNetCode()
                if nc in affected_nets:
                    pads_by_net.setdefault(nc, []).append(pad)
        zone_layers_by_net = {}
        for z in board.Zones():
            if z.GetIsRuleArea():
                continue
            nc = z.GetNetCode()
            if nc in affected_nets:
                zone_layers_by_net.setdefault(nc, set()).update(z.GetLayerSet().CuStack())
        all_tracks = list(board.GetTracks())
        by_net_alive = {}
        for idx, item in enumerate(all_tracks):
            nc = item.GetNetCode()
            if nc in affected_nets:
                by_net_alive.setdefault(nc, set()).add(idx)

        def _san_hits_pad(nc, lyr, pt):
            for pad in pads_by_net.get(nc, ()):
                if pad.IsOnLayer(lyr) and pad.HitTest(pt, 0):
                    return True
            return False

        def _san_hits_other(nc, lyr, pt, self_idx):
            for other_idx in by_net_alive.get(nc, ()):
                if other_idx == self_idx:
                    continue
                other = all_tracks[other_idx]
                if other.GetClass() == "PCB_VIA":
                    if lyr in other.GetLayerSet().CuStack() and other.HitTest(pt, 0):
                        return True
                else:
                    if other.GetLayer() == lyr and other.HitTest(pt, 0):
                        return True
            return False

        pruned_idxs = set()
        changed = True
        while changed:
            changed = False
            for nc, alive in list(by_net_alive.items()):
                drop = set()
                z_lyrs = zone_layers_by_net.get(nc, set())
                for idx in alive:
                    item = all_tracks[idx]
                    if item.IsLocked():
                        continue
                    if item.GetClass() == "PCB_VIA":
                        pos = item.GetPosition()
                        v_lyrs = list(item.GetLayerSet().CuStack())
                        if any(_san_hits_pad(nc, lyr, pos) for lyr in v_lyrs):
                            continue
                        track_layers = {lyr for lyr in v_lyrs if _san_hits_other(nc, lyr, pos, idx)}
                        if not track_layers:
                            drop.add(idx)
                            continue
                        connected_layers = track_layers | (set(v_lyrs) & z_lyrs)
                        if len(connected_layers) < 2:
                            drop.add(idx)
                    else:
                        lyr = item.GetLayer()
                        for pt in (item.GetStart(), item.GetEnd()):
                            if not _san_hits_pad(nc, lyr, pt) and not _san_hits_other(nc, lyr, pt, idx):
                                drop.add(idx)
                                break
                if drop:
                    alive -= drop
                    pruned_idxs |= drop
                    changed = True
        for idx in sorted(pruned_idxs):
            item = all_tracks[idx]
            if item.GetClass() == "PCB_VIA":
                pruned_dangling_vias += 1
            else:
                pruned_dangling_tracks += 1
            board.Remove(item)

    zones = list(board.Zones())
    copper = [z for z in zones if not z.GetIsRuleArea()]
    for z in copper:
        board.Remove(z)
    unrouted_tracks_only = _unconnected(board)
    stats = _routing_stats(board)
    pads = _pad_rows(board)
    for z in copper:
        board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    unrouted_final = _unconnected(board)
    pcbnew.SaveBoard(out_board, board)
    report = {
        "removed_tracks": removed_tracks,
        "removed_vias": removed_vias,
        "pruned_dangling_tracks": pruned_dangling_tracks,
        "pruned_dangling_vias": pruned_dangling_vias,
        "removed_total": removed_tracks + removed_vias + pruned_dangling_tracks + pruned_dangling_vias,
        "unrouted_tracks_only": unrouted_tracks_only,
        "unrouted_final": unrouted_final,
        "routing": stats,
        "pads": pads,
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    del to_remove, zones, copper


def heal_thermals(board_path, spec_json, out_board, out_json):
    """Apply targeted per-pad thermal relief overrides to DRC-starved pads and refill zones.

    Modes:
      - 'relief': rotate spokes to 45 deg and tighten local thermal spoke/gap to fab-legal
        values so orthogonal tracks no longer pinch off thermal spokes.
      - 'solid': fall back to ZONE_CONNECTION_FULL on pads that remain starved after relief.
    """
    board = pcbnew.LoadBoard(board_path)
    with open(spec_json, encoding="utf-8") as fh:
        spec = json.load(fh)
    mode = spec.get("mode", "relief")
    target_uuids = set(spec.get("pad_uuids") or [])

    ds = board.GetDesignSettings()
    def_clr = int(ds.m_NetSettings.GetDefaultNetclass().GetClearance())
    min_gap = max(int(ds.m_MinClearance), def_clr, pcbnew.FromMM(0.20))

    modified = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            uid = pad.m_Uuid.AsString()
            if uid not in target_uuids:
                continue
            if mode == "relief":
                pad.SetThermalSpokeAngleDegrees(45.0)
                pad.SetThermalGap(min_gap)
            elif mode == "solid":
                pad.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)
            else:
                continue
            modified.append({"uuid": uid, "ref": ref, "pad": pad.GetNumber(), "mode": mode})

    zones = list(board.Zones())
    copper = [z for z in zones if not z.GetIsRuleArea()]
    for z in copper:
        board.Remove(z)
    unrouted_tracks_only = _unconnected(board)
    stats = _routing_stats(board)
    pads = _pad_rows(board)
    for z in copper:
        board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    unrouted_final = _unconnected(board)
    pcbnew.SaveBoard(out_board, board)
    report = {
        "mode": mode,
        "modified_count": len(modified),
        "modified": modified,
        "unrouted_tracks_only": unrouted_tracks_only,
        "unrouted_final": unrouted_final,
        "routing": stats,
        "pads": pads,
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    del zones, copper


def heal_mask_bridges(board_path, spec_json, out_board, out_json):
    """Prevent lower-priority background copper pours from intruding into free-standing solder-mask
    apertures that already expose a higher-priority zone or pad (resolving new solder_mask_bridge DRC).

    Adds surgical zone-fill-only rule areas (SetDoNotAllowZoneFills=True) outside the dominant
    zone's outline on the affected copper layer, never deleting tracks, vias, pads, or zones.
    """
    board = pcbnew.LoadBoard(board_path)
    with open(spec_json, encoding="utf-8") as fh:
        spec = json.load(fh)
    target_mask_uuids = list(spec.get("mask_uuids") or [])

    ds = board.GetDesignSettings()
    max_error = ds.m_MaxError
    def_clr = int(ds.m_NetSettings.GetDefaultNetclass().GetClearance())
    grow = max(def_clr, pcbnew.FromMM(0.15))

    mask_items = {}
    for _src, item in _graphic_sources(board):
        if item.GetLayer() in (pcbnew.F_Mask, pcbnew.B_Mask):
            mask_items[item.m_Uuid.AsString()] = item

    copper_zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    added_rule_areas = []
    for uid in target_mask_uuids:
        item = mask_items.get(uid)
        if item is None:
            continue
        mask_layer = item.GetLayer()
        cu_layer = pcbnew.F_Cu if mask_layer == pcbnew.F_Mask else pcbnew.B_Cu
        mask_poly = _item_poly(item, mask_layer, max_error, 0)
        if mask_poly.OutlineCount() == 0:
            continue

        # Identify filled copper zones overlapping this solder-mask aperture on cu_layer.
        overlapping = []
        for z in copper_zones:
            if not z.IsOnLayer(cu_layer) or not z.HasFilledPolysForLayer(cu_layer):
                continue
            filled = z.GetFilledPolysList(cu_layer)
            if filled.OutlineCount() > 0 and _overlaps(mask_poly, filled):
                overlapping.append(z)
        if not overlapping:
            continue

        keep_poly = _item_poly(item, mask_layer, max_error, grow)
        distinct_nets = {z.GetNetCode() for z in overlapping}
        if len(distinct_nets) >= 2:
            # Preserve the highest-priority (local) zone; block lower-priority pours outside it.
            overlapping.sort(key=lambda z: (int(z.GetAssignedPriority()), -float(z.GetOutlineArea())), reverse=True)
            owner = overlapping[0]
            keep_poly.BooleanSubtract(owner.Outline())
        rings = _poly_points(keep_poly)
        for idx, ring in enumerate(rings):
            if len(ring) < 3:
                continue
            ra = pcbnew.ZONE(board)
            ra.SetIsRuleArea(True)
            lset = pcbnew.LSET()
            lset.AddLayer(cu_layer)
            ra.SetLayerSet(lset)
            ra.SetDoNotAllowZoneFills(True)
            ra.SetDoNotAllowTracks(False)
            ra.SetDoNotAllowVias(False)
            ra.SetDoNotAllowPads(False)
            ra.SetDoNotAllowFootprints(False)
            ra.SetZoneName(f"eai_mask_guard_{uid[:8]}_{idx}")
            outline = ra.Outline()
            outline.NewOutline()
            for x, y in ring:
                outline.Append(pcbnew.FromMM(float(x)), pcbnew.FromMM(float(y)))
            ident = uuid5(NAMESPACE_URL, f"electronics-ai/mask-guard/{uid}/{idx}")
            ra.SetUuidDirect(pcbnew.KIID(str(ident)))
            board.Add(ra)
            added_rule_areas.append({"mask_uuid": uid, "layer": board.GetLayerName(cu_layer), "index": idx})

    zones = list(board.Zones())
    copper = [z for z in zones if not z.GetIsRuleArea()]
    for z in copper:
        board.Remove(z)
    unrouted_tracks_only = _unconnected(board)
    stats = _routing_stats(board)
    pads = _pad_rows(board)
    for z in copper:
        board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    unrouted_final = _unconnected(board)
    pcbnew.SaveBoard(out_board, board)
    report = {
        "added_count": len(added_rule_areas),
        "added": added_rule_areas,
        "unrouted_tracks_only": unrouted_tracks_only,
        "unrouted_final": unrouted_final,
        "routing": stats,
        "pads": pads,
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    del zones, copper


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
    sx, sy = size.x, size.y
    shape_layer = pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
    for lyr in (pcbnew.F_Cu, pcbnew.B_Cu):
        if pad.IsOnLayer(lyr):
            try:
                lsz = pad.GetSize(lyr)
            except TypeError:
                break
            if lsz.x * lsz.y > sx * sy or lsz.x > sx or lsz.y > sy:
                shape_layer = lyr
            sx = max(sx, lsz.x)
            sy = max(sy, lsz.y)
    if hasattr(pad, "GetFPRelativeOrientation"):
        prot = _norm(pad.GetFPRelativeOrientation().AsDegrees())
    else:
        prot = _norm(pad.GetOrientationDegrees() - rot)
    if hasattr(pcbnew, "PAD_SHAPE_CUSTOM") and pad.GetShape() == pcbnew.PAD_SHAPE_CUSTOM:
        px_mm, py_mm = pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)
        pad_deg = _norm(rot + prot)
        max_error = board.GetDesignSettings().m_MaxError
        pts = [pt for ring in _world_outlines(_item_poly(pad, shape_layer, max_error)) for pt in ring]
        if pts:
            local_pts = [_to_local(x, y, px_mm, py_mm, pad_deg) for x, y in pts]
            us = [u for u, _ in local_pts]
            vs = [v for _, v in local_pts]
            sx = max(sx, pcbnew.FromMM(max(us) - min(us)))
            sy = max(sy, pcbnew.FromMM(max(vs) - min(vs)))
    return {
        "number": pad.GetNumber(),
        "net": pad.GetNetname() or None,
        "local": [lx, ly],
        "rotation": prot,
        "size": [_mm(sx), _mm(sy)],
        "shape": pad.ShowPadShape(shape_layer) if hasattr(pad, "ShowPadShape") else str(pad.GetShape()),
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
        rings = []
        holes = []
        for i in range(poly.OutlineCount()):
            chain = poly.Outline(i)
            rings.append([
                list(_to_local(pcbnew.ToMM(chain.CPoint(j).x), pcbnew.ToMM(chain.CPoint(j).y), ox, oy, rot))
                for j in range(chain.PointCount())
            ])
            poly_holes = []
            for h in range(poly.HoleCount(i)):
                hchain = poly.Hole(i, h)
                poly_holes.append([
                    list(_to_local(pcbnew.ToMM(hchain.CPoint(j).x), pcbnew.ToMM(hchain.CPoint(j).y), ox, oy, rot))
                    for j in range(hchain.PointCount())
                ])
            holes.append(poly_holes)
        out = {"source": "file", "polygons": rings}
        if any(holes):
            out["holes"] = holes
        return out
    pts = []
    pad_boxes = []
    for pad in fp.Pads():
        stack = list(pad.GetLayerSet().CuStack())
        ppts = []
        for ring in _world_outlines(_item_poly(pad, stack[0] if stack else pcbnew.F_Cu, max_error)):
            ppts += [_to_local(x, y, ox, oy, rot) for x, y in ring]
        if ppts:
            pts += ppts
            net = pad.GetNetname() or ""
            pad_boxes.append((
                min(p[0] for p in ppts), min(p[1] for p in ppts),
                max(p[0] for p in ppts), max(p[1] for p in ppts),
                net,
            ))
    if not pts:
        return {"source": "none", "polygons": []}
    m = DERIVED_COURTYARD_MARGIN_MM
    x0, y0 = min(p[0] for p in pts) - m, min(p[1] for p in pts) - m
    x1, y1 = max(p[0] for p in pts) + m, max(p[1] for p in pts) + m
    rect = [[round(a, 6), round(b, 6)] for a, b in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    bw, bh = x1 - x0, y1 - y0
    bbox_area = bw * bh
    if bw >= 6.0 and bh >= 6.0 and len(pad_boxes) >= 3 and bbox_area > 0:
        pad_area = sum((px1 - px0 + 2 * m) * (py1 - py0 + 2 * m) for px0, py0, px1, py1, _ in pad_boxes)
        max_pad_span = max(max(px1 - px0, py1 - py0) for px0, py0, px1, py1, _ in pad_boxes)
        pad_limit = min(max_pad_span + m, 0.22 * min(bw, bh)) + 0.02
        lx0, ly0, lx1, ly1 = x0 + pad_limit, y0 + pad_limit, x1 - pad_limit, y1 - pad_limit
        wall = min(max(max_pad_span, 0.8) + 2.0 * m, 0.24 * min(bw, bh))
        ix0, iy0, ix1, iy1 = x0 + wall, y0 + wall, x1 - wall, y1 - wall
        if ix1 - ix0 >= 2.0 and iy1 - iy0 >= 2.0 and pad_area < 0.28 * bbox_area:
            interior_clear = not any(
                px1 > lx0 and px0 < lx1 and py1 > ly0 and py0 < ly1
                for px0, py0, px1, py1, _ in pad_boxes
            )
            all_gnd_or_nc = all(
                net == "" or net.lower().startswith("unconnected-") or "gnd" in net.lower() or "vss" in net.lower()
                for *_, net in pad_boxes
            )
            desc = (fp.GetReference() + " " + fp.GetValue() + " " + str(fp.GetFPID().GetUniStringLibId())).lower()
            shield_hint = "shield" in desc
            if interior_clear and (shield_hint or all_gnd_or_nc):
                hole = [[round(a, 6), round(b, 6)] for a, b in ((ix0, iy0), (ix0, iy1), (ix1, iy1), (ix1, iy0))]
                return {"source": "derived", "margin_mm": m, "polygons": [rect], "holes": [[hole]]}
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
                "microvia_diameter": (
                    _mm(nc.GetuViaDiameter())
                    if hasattr(nc, "GetuViaDiameter")
                    else (_mm(nc.GetMicroViaDiameter()) if hasattr(nc, "GetMicroViaDiameter") else None)
                ),
                "microvia_drill": (
                    _mm(nc.GetuViaDrill())
                    if hasattr(nc, "GetuViaDrill")
                    else (_mm(nc.GetMicroViaDrill()) if hasattr(nc, "GetMicroViaDrill") else None)
                ),
            }
        nets.append({"name": name, "class": cname})
    return sorted(classes.values(), key=lambda c: c["name"]), nets


def _project_rules(board):
    fname = board.GetFileName() if hasattr(board, "GetFileName") else ""
    if not fname:
        return {}
    pro_path = os.path.splitext(fname)[0] + ".kicad_pro"
    if not os.path.isfile(pro_path):
        return {}
    try:
        with open(pro_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    except (OSError, ValueError):
        return {}


def _rules(board):
    ds = board.GetDesignSettings()
    pro_r = _project_rules(board)
    return {
        "min_track_width": _mm(ds.m_TrackMinWidth),
        "min_clearance": _mm(ds.m_MinClearance),
        "min_via_diameter": _mm(ds.m_ViasMinSize),
        "min_through_drill": _mm(ds.m_MinThroughDrill),
        "min_via_annular_width": _mm(int(getattr(ds, "m_ViasMinAnnularWidth", 0))),
        "allow_microvias": bool(pro_r.get("allow_microvias", False)),
        "allow_blind_buried_vias": bool(pro_r.get("allow_blind_buried_vias", False)),
        "min_microvia_diameter": _mm(int(getattr(ds, "m_MicroViasMinSize", 0))),
        "min_microvia_drill": _mm(int(getattr(ds, "m_MicroViasMinDrill", 0))),
        "hole_clearance": _mm(ds.m_HoleClearance),
        "copper_edge_clearance": _mm(ds.m_CopperEdgeClearance),
        "hole_to_hole": _mm(ds.m_HoleToHoleMin),
        "board_thickness": _mm(ds.GetBoardThickness()),
        "min_silk_text_height": _mm(int(getattr(ds, "m_MinSilkTextHeight", 0))),
        "min_silk_text_thickness": _mm(int(getattr(ds, "m_MinSilkTextThickness", 0))),
        "silk_clearance": _mm(int(getattr(ds, "m_SilkClearance", 0))),
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
                     "filled_area_mm2": areas, "outline_area_mm2": _area_mm2(zone.Outline()),
                     "name": zone.GetZoneName(), "locked": bool(zone.IsLocked()),
                     "outline": [[[round(x, 6), round(y, 6)] for x, y in ring]
                                 for ring in _world_outlines(zone.Outline())]})
    return rows, rule_areas, fill, teardrops


def _routing_geometry(board):
    """Existing copper routing: segments and arcs (3 points), vias with drill and layer span, lock state."""
    tracks, vias = [], []
    for t in board.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            p = t.GetPosition()
            top, bottom = t.TopLayer(), t.BottomLayer()
            vias.append({"x": _mm(p.x), "y": _mm(p.y), "diameter": _mm(_via_width_nm(t)),
                         "drill": _mm(t.GetDrillValue()), "net": t.GetNetname(), "locked": bool(t.IsLocked()),
                         "via_type": _VIA_TYPE_NAMES.get(t.GetViaType(), "through"),
                         "layers": [pcbnew.BOARD.GetStandardLayerName(top), pcbnew.BOARD.GetStandardLayerName(bottom)]})
            continue
        pts = [t.GetStart(), t.GetMid(), t.GetEnd()] if cls == "PCB_ARC" else [t.GetStart(), t.GetEnd()]
        tracks.append({"layer": pcbnew.BOARD.GetStandardLayerName(t.GetLayer()), "width": _mm(t.GetWidth()),
                       "net": t.GetNetname(), "locked": bool(t.IsLocked()),
                       "points": [[_mm(q.x), _mm(q.y)] for q in pts]})
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
        "silkscreen_texts": _extract_silkscreen_texts(board),
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _boxes_overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _box_overlap_area(a, b):
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0 and dy > 0 else 0


def _box_distance_nm(a, b):
    dx = max(0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0, max(a[1] - b[3], b[1] - a[3]))
    return math.hypot(dx, dy)


def _box_from_bbox(bb, pad=0):
    x0 = bb.GetX()
    y0 = bb.GetY()
    return (x0 - pad, y0 - pad, x0 + bb.GetWidth() + pad, y0 + bb.GetHeight() + pad)


def _text_box(item):
    if hasattr(item, "ClearBoundingBoxCache"):
        item.ClearBoundingBoxCache()
    bb = item.GetBoundingBox()
    return (bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight())


def _box_inside_outline(box, outline_poly, edge_margin=0):
    x0, y0, x1, y1 = box[0] - edge_margin, box[1] - edge_margin, box[2] + edge_margin, box[3] + edge_margin
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    for px, py in (
        (x0, y0), (x1, y0), (x1, y1), (x0, y1),
        (cx, cy), (cx, y0), (cx, y1), (x0, cy), (x1, cy),
    ):
        if not outline_poly.Contains(pcbnew.VECTOR2I(int(px), int(py))):
            return False
    return True


def _footprint_body_box(fp):
    """Estimate physical component body bbox (nm) from Fab -> Courtyard -> Pads."""
    fab_lyr = pcbnew.B_Fab if fp.IsFlipped() else pcbnew.F_Fab
    cy_lyr = pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd
    for target_lyr in (fab_lyr, cy_lyr):
        xs0, ys0, xs1, ys1 = [], [], [], []
        for g in fp.GraphicalItems():
            if g.GetLayer() != target_lyr:
                continue
            if g.GetClass() in _TEXT_CLASSES:
                continue
            bb = g.GetBoundingBox()
            if bb.GetWidth() > 0 or bb.GetHeight() > 0:
                xs0.append(bb.GetX())
                ys0.append(bb.GetY())
                xs1.append(bb.GetX() + bb.GetWidth())
                ys1.append(bb.GetY() + bb.GetHeight())
        if xs0:
            return (min(xs0), min(ys0), max(xs1), max(ys1))
    pbb = fp.GetBoundingBox(False, False)
    return (pbb.GetX(), pbb.GetY(), pbb.GetX() + pbb.GetWidth(), pbb.GetY() + pbb.GetHeight())


def _item_text(item):
    if hasattr(item, "GetShownText"):
        try:
            txt = item.GetShownText(False)
        except TypeError:
            txt = item.GetShownText()
        if txt:
            return str(txt).strip()
    return str(item.GetText() if hasattr(item, "GetText") else "").strip()


def _extract_silkscreen_texts(board):
    """Extract visible silkscreen and assembly-layer texts with DFM geometry metrics."""
    silk_layers = (pcbnew.F_SilkS, pcbnew.B_SilkS)
    fab_layers = (pcbnew.F_Fab, pcbnew.B_Fab)
    all_layers = silk_layers + fab_layers

    outline_poly = pcbnew.SHAPE_POLY_SET()
    has_outline = bool(board.GetBoardPolygonOutlines(outline_poly, False))

    pad_boxes = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    body_boxes = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    fp_body_by_ref = {}
    footprints = sorted(board.GetFootprints(), key=lambda f: (f.GetReference(), f.m_Uuid.AsString()))
    for fp in footprints:
        silk_lyr = pcbnew.B_SilkS if fp.IsFlipped() else pcbnew.F_SilkS
        bb_fp = _footprint_body_box(fp)
        body_boxes[silk_lyr].append(bb_fp)
        fp_body_by_ref[fp.GetReference()] = bb_fp
        for pad in fp.Pads():
            pbb = pad.GetBoundingBox()
            box = _box_from_bbox(pbb, 0)
            if pad.IsOnLayer(pcbnew.F_Cu) or pad.IsOnLayer(pcbnew.F_Mask):
                pad_boxes[pcbnew.F_SilkS].append(box)
            if pad.IsOnLayer(pcbnew.B_Cu) or pad.IsOnLayer(pcbnew.B_Mask):
                pad_boxes[pcbnew.B_SilkS].append(box)

    raw_items = []
    for fp in footprints:
        ref = fp.GetReference()
        for fld in fp.GetFields():
            lyr = fld.GetLayer()
            if lyr in all_layers and fld.IsVisible() and _item_text(fld):
                fld_name = fld.GetName() if hasattr(fld, "GetName") else ""
                kind = "field" if fld_name == "Reference" else "value_field"
                raw_items.append((ref, kind, fld, lyr))
        for g in fp.GraphicalItems():
            lyr = g.GetLayer()
            if lyr in all_layers and g.GetClass() in _TEXT_CLASSES and g.IsVisible() and _item_text(g):
                raw_items.append((ref, "graphic_text", g, lyr))
    for d in board.GetDrawings():
        lyr = d.GetLayer()
        if lyr in all_layers and d.GetClass() in _TEXT_CLASSES and d.IsVisible() and _item_text(d):
            raw_items.append(("board", "board_text", d, lyr))

    # Precompute visible text bboxes to detect silk-to-silk and fab-to-fab overlaps.
    silk_boxes = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    fab_boxes = {pcbnew.F_Fab: [], pcbnew.B_Fab: []}
    entries = []
    for ref, kind, item, lyr in raw_items:
        sz = item.GetTextSize()
        thick = int(item.GetTextThickness())
        box = _text_box(item)
        if lyr in silk_layers:
            silk_boxes[lyr].append((item.m_Uuid.AsString(), box))
        elif lyr in fab_layers:
            fab_boxes[lyr].append((item.m_Uuid.AsString(), box))
        entries.append((ref, kind, item, lyr, sz, thick, box))

    out = []
    for ref, kind, item, lyr, sz, thick, box in entries:
        uid = item.m_Uuid.AsString()
        txt = _item_text(item)
        h_mm = _mm(sz.y)
        w_mm = _mm(sz.x)
        # KiCad renders 0 stroke thickness at height/8 internally, but report actual configured stroke.
        s_mm = _mm(thick) if thick > 0 else round(h_mm / 8.0, 4)
        pad_clr_mm = None
        under_body = False
        overlaps_silk = False
        overlaps_fab = False
        exceeds_body = False
        is_back = lyr in (pcbnew.B_SilkS, pcbnew.B_Fab)
        mirrored = bool(item.IsMirrored()) if hasattr(item, "IsMirrored") else is_back
        mirror_mismatch = bool(mirrored != is_back)
        outside_board = bool(has_outline and not _box_inside_outline(box, outline_poly, 0))
        if lyr in silk_layers:
            if pad_boxes[lyr]:
                min_d = min(_box_distance_nm(box, pb) for pb in pad_boxes[lyr])
                pad_clr_mm = _mm(min_d)
            if kind == "field":
                t_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
                for bb in body_boxes[lyr]:
                    if _box_overlap_area(box, bb) / t_area >= 0.25:
                        under_body = True
                        break
            for o_uid, o_box in silk_boxes[lyr]:
                if o_uid != uid and _boxes_overlap(box, o_box):
                    overlaps_silk = True
                    break
        elif lyr in fab_layers:
            t_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
            for o_uid, o_box in fab_boxes[lyr]:
                if o_uid != uid and _boxes_overlap(box, o_box):
                    o_area = max(1, (o_box[2] - o_box[0]) * (o_box[3] - o_box[1]))
                    if _box_overlap_area(box, o_box) / min(t_area, o_area) >= 0.15:
                        overlaps_fab = True
                        break
            if ref in fp_body_by_ref:
                bb_fp = fp_body_by_ref[ref]
                body_long = max(bb_fp[2] - bb_fp[0], bb_fp[3] - bb_fp[1])
                txt_long = max(box[2] - box[0], box[3] - box[1])
                if txt_long > max(int(body_long * 1.5), pcbnew.FromMM(2.0)):
                    exceeds_body = True
        out.append({
            "uuid": uid,
            "ref": ref,
            "kind": kind,
            "text": txt,
            "layer": pcbnew.BOARD.GetStandardLayerName(lyr),
            "visible": True,
            "height_mm": h_mm,
            "width_mm": w_mm,
            "stroke_mm": s_mm,
            "pad_clearance_mm": pad_clr_mm,
            "under_body": under_body,
            "overlaps_silk": overlaps_silk,
            "overlaps_fab": overlaps_fab,
            "exceeds_body": exceeds_body,
            "mirror_mismatch": mirror_mismatch,
            "outside_board": outside_board,
            "pos_mm": [_mm((box[0] + box[2]) // 2), _mm((box[1] + box[3]) // 2)],
            "bbox_mm": [_mm(box[2] - box[0]), _mm(box[3] - box[1])],
        })
    out.sort(key=lambda r: (r["layer"], r["ref"], r["text"], r["uuid"]))
    return out


def _ensure_fab_reference(board, fp):
    """Ensure a visible assembly-drawing reference exists on F.Fab / B.Fab when hidden from Silkscreen."""
    fab_lyr = pcbnew.B_Fab if fp.IsFlipped() else pcbnew.F_Fab
    ref_str = fp.GetReference()
    for g in fp.GraphicalItems():
        if g.GetLayer() == fab_lyr and g.GetClass() in _TEXT_CLASSES and g.IsVisible():
            shown = _item_text(g)
            if shown in (ref_str, "${REFERENCE}", "%R"):
                return False
    for fld in fp.GetFields():
        if fld.GetLayer() == fab_lyr and fld.IsVisible() and fld.GetText().strip() == ref_str:
            return False

    body = _footprint_body_box(fp)
    bw = max(pcbnew.FromMM(0.4), body[2] - body[0])
    bh = max(pcbnew.FromMM(0.4), body[3] - body[1])
    cx = (body[0] + body[2]) // 2
    cy = (body[1] + body[3]) // 2
    tall = bh > bw * 1.25
    long_side = max(bw, bh)
    short_side = min(bw, bh)
    # Size fab text cleanly to fit inside the component body outline for Assembly Drawings.
    char_count = max(2, len(ref_str))
    fit_h = min(pcbnew.FromMM(0.85), int(short_side * 0.65), int(long_side / (char_count * 0.88)))
    fab_h = max(pcbnew.FromMM(0.16), fit_h)
    fab_w = max(pcbnew.FromMM(0.14), int(fab_h * 0.82))
    fab_s = max(pcbnew.FromMM(0.03), min(pcbnew.FromMM(0.14), int(fab_h / 7.0)))

    txt = pcbnew.PCB_TEXT(fp)
    txt.SetText(ref_str)
    txt.SetLayer(fab_lyr)
    txt.SetPosition(pcbnew.VECTOR2I(int(cx), int(cy)))
    txt.SetTextSize(pcbnew.VECTOR2I(int(fab_w), int(fab_h)))
    txt.SetTextThickness(int(fab_s))
    txt.SetTextAngleDegrees(90.0 if tall else 0.0)
    txt.SetVisible(True)
    if hasattr(txt, "SetMirrored"):
        txt.SetMirrored(bool(fp.IsFlipped()))
    ident = uuid5(NAMESPACE_URL, f"electronics-ai/fab-ref/{fp.m_Uuid.AsString()}/{ref_str}")
    txt.SetUuidDirect(pcbnew.KIID(str(ident)))
    fp.Add(txt)
    return True


def _normalize_fab_texts(board, outline_poly, has_outline, fp_body_map, is_miniature, *, ref_only_fab=False):
    """Enforce IPC-A-610 Assembly Drawing typography on F.Fab/B.Fab and fix mirror mismatches.

    1. Fixes mirrored_text_on_front_layer / nonmirrored_text_on_back_layer on all visible texts.
    2. On dense miniature or assembly-only boards (ref_only_fab or (is_miniature and > 12 footprints)),
       hides cluttering F.Fab/B.Fab Value strings (keeping crisp body-contained Reference designators
       only, matching real IPC-A-610 Assembly PDFs). Never touches Silkscreen Value port labels.
    3. Sizes every F.Fab/B.Fab Reference strictly inside its component body along the long axis.
    """
    fab_layers = (pcbnew.F_Fab, pcbnew.B_Fab)
    silk_layers = (pcbnew.F_SilkS, pcbnew.B_SilkS)
    normalized = 0
    values_hidden = 0
    mirrors_fixed = 0
    footprints = sorted(board.GetFootprints(), key=lambda f: (f.GetReference(), f.m_Uuid.AsString()))
    dense_miniature_fab = bool(ref_only_fab or (is_miniature and len(footprints) > 12))

    for fp in footprints:
        # Fix mirror mismatch on ALL visible silkscreen and fab items of this footprint.
        for item in list(fp.GetFields()) + [
            g for g in fp.GraphicalItems() if g.GetClass() in _TEXT_CLASSES
        ]:
            lyr = item.GetLayer()
            if lyr not in fab_layers and lyr not in silk_layers:
                continue
            if not item.IsVisible() or not hasattr(item, "SetMirrored"):
                continue
            want_mirror = lyr in (pcbnew.B_Fab, pcbnew.B_SilkS)
            if bool(item.IsMirrored()) != want_mirror:
                item.SetMirrored(want_mirror)
                mirrors_fixed += 1

        body = fp_body_map.get(fp.m_Uuid.AsString()) or _footprint_body_box(fp)
        bw = max(pcbnew.FromMM(0.35), body[2] - body[0])
        bh = max(pcbnew.FromMM(0.35), body[3] - body[1])
        cx = (body[0] + body[2]) // 2
        cy = (body[1] + body[3]) // 2
        long_side = max(bw, bh)
        short_side = min(bw, bh)
        preferred_ang = 90.0 if bh > bw * 1.25 else 0.0

        fab_items = []
        for fld in fp.GetFields():
            if fld.GetLayer() in fab_layers and fld.IsVisible() and _item_text(fld):
                fld_name = fld.GetName() if hasattr(fld, "GetName") else ""
                fab_items.append((fld, fld_name != "Reference"))
        for g in fp.GraphicalItems():
            if g.GetLayer() in fab_layers and g.GetClass() in _TEXT_CLASSES and g.IsVisible() and _item_text(g):
                fab_items.append((g, False))

        for item, is_value in fab_items:
            if is_value and dense_miniature_fab:
                # Dense miniature boards (e.g. 0201 wearables): keep F.Fab/B.Fab Ref-only per IPC-A-610.
                item.SetVisible(False)
                values_hidden += 1
                continue

            txt = _item_text(item)
            box = _text_box(item)
            tw = box[2] - box[0]
            th = box[3] - box[1]
            outside = bool(has_outline and not _box_inside_outline(box, outline_poly, 0))
            exceeds_own_body = (
                max(tw, th) > int(long_side * 1.08)
                or min(tw, th) > int(short_side * 0.92)
            )
            oversized = bool(
                dense_miniature_fab
                or (is_miniature and exceeds_own_body)
                or (max(tw, th) > max(int(long_side * 1.45), pcbnew.FromMM(2.5)))
            )
            if not outside and not oversized:
                continue

            chars = max(2, len(txt))
            base_h = min(
                pcbnew.FromMM(0.85),
                int(short_side * 0.62),
                int(long_side / (chars * 0.88)),
            )
            min_fab_h = pcbnew.FromMM(0.14 if not is_value else 0.18)
            trial_heights = []
            for h_cand in (
                base_h,
                int(base_h * 0.80),
                pcbnew.FromMM(0.50),
                pcbnew.FromMM(0.35),
                pcbnew.FromMM(0.25),
                pcbnew.FromMM(0.18),
                min_fab_h,
            ):
                clamped = max(min_fab_h, min(pcbnew.FromMM(0.85), int(h_cand)))
                if clamped not in trial_heights:
                    trial_heights.append(clamped)
            trial_heights.sort(reverse=True)

            item.SetPosition(pcbnew.VECTOR2I(int(cx), int(cy)))
            if hasattr(item, "SetMirrored"):
                item.SetMirrored(item.GetLayer() == pcbnew.B_Fab)

            fitted = False
            angles = (preferred_ang, 90.0 - preferred_ang)
            max_allow_long = int(long_side * 1.05)
            max_allow_short = int(short_side * 0.95)
            for h_nm in trial_heights:
                s_nm = max(pcbnew.FromMM(0.03), min(pcbnew.FromMM(0.12), int(h_nm / 7.0)))
                for w_ratio in (0.82, 0.68):
                    w_nm = max(pcbnew.FromMM(0.11), int(h_nm * w_ratio))
                    item.SetTextSize(pcbnew.VECTOR2I(w_nm, h_nm))
                    item.SetTextThickness(s_nm)
                    for ang in angles:
                        item.SetTextAngleDegrees(ang)
                        nbox = _text_box(item)
                        nw = nbox[2] - nbox[0]
                        nh = nbox[3] - nbox[1]
                        in_outline = not has_outline or _box_inside_outline(nbox, outline_poly, 0)
                        in_body = max(nw, nh) <= max_allow_long and min(nw, nh) <= max_allow_short
                        if in_outline and (in_body or h_nm == trial_heights[-1]):
                            fitted = True
                            break
                    if fitted:
                        break
                if fitted:
                    break

            if fitted:
                normalized += 1
            elif is_value and has_outline:
                # Never leave an oversized Fab Value string sticking outside the board outline;
                # the component's F.Fab/B.Fab Reference remains visible for assembly drawings.
                item.SetVisible(False)
                values_hidden += 1
            else:
                normalized += 1

    return normalized, values_hidden, mirrors_fixed


def _segment_hits_any_box(p0, p1, boxes):
    """Conservative AABB-sampled check for a leader line segment against obstacle boxes."""
    x0, y0 = p0
    x1, y1 = p1
    sbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    near = [b for b in boxes if _boxes_overlap(sbox, b)]
    if not near:
        return False
    steps = 16
    for i in range(steps + 1):
        t = i / steps
        px = x0 + (x1 - x0) * t
        py = y0 + (y1 - y0) * t
        for bx0, by0, bx1, by1 in near:
            if bx0 <= px <= bx1 and by0 <= py <= by1:
                return True
    return False


def _try_add_leader_line(board, fp, lyr, text_box, body_box, stroke_nm, obstacle_boxes, placed_boxes):
    """Draw a DRC-clean silkscreen leader line (straight or 1-bend L) from a displaced critical ref."""
    tcx = (text_box[0] + text_box[2]) / 2.0
    tcy = (text_box[1] + text_box[3]) / 2.0
    bcx = (body_box[0] + body_box[2]) / 2.0
    bcy = (body_box[1] + body_box[3]) / 2.0

    # Clamp endpoints onto the facing edges of text_box and body_box.
    tx = max(text_box[0], min(text_box[2], bcx))
    ty = max(text_box[1], min(text_box[3], bcy))
    bx = max(body_box[0], min(body_box[2], tcx))
    by = max(body_box[1], min(body_box[3], tcy))
    dist = math.hypot(bx - tx, by - ty)
    if dist < pcbnew.FromMM(1.5) or dist > pcbnew.FromMM(12.0):
        return False

    ux, uy = (bx - tx) / dist, (by - ty) / dist
    pull_text = pcbnew.FromMM(0.28)
    pull_body = pcbnew.FromMM(0.28)
    if dist <= pull_text + pull_body + pcbnew.FromMM(0.40):
        return False

    p0 = (round(tx + ux * pull_text), round(ty + uy * pull_text))
    p1 = (round(bx - ux * pull_body), round(by - uy * pull_body))
    half = stroke_nm // 2 + pcbnew.FromMM(0.06)
    inflated = [(b[0] - half, b[1] - half, b[2] + half, b[3] + half) for b in obstacle_boxes]

    # Try straight segment first, then two orthogonal L-bend alternatives.
    paths = [
        [(p0, p1)],
        [(p0, (p1[0], p0[1])), ((p1[0], p0[1]), p1)],
        [(p0, (p0[0], p1[1])), ((p0[0], p1[1]), p1)],
    ]
    chosen = None
    for segs in paths:
        if all(
            math.hypot(b[0] - a[0], b[1] - a[1]) >= pcbnew.FromMM(0.15)
            and not _segment_hits_any_box(a, b, inflated)
            for a, b in segs
        ):
            chosen = segs
            break
    if chosen is None:
        return False

    for idx, (a, b) in enumerate(chosen):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(lyr)
        seg.SetStart(pcbnew.VECTOR2I(a[0], a[1]))
        seg.SetEnd(pcbnew.VECTOR2I(b[0], b[1]))
        seg.SetWidth(int(stroke_nm))
        ident = uuid5(NAMESPACE_URL, f"electronics-ai/silk-leader/{fp.m_Uuid.AsString()}/{a[0]}/{a[1]}/{idx}")
        seg.SetUuidDirect(pcbnew.KIID(str(ident)))
        board.Add(seg)
        lbox = (min(a[0], b[0]) - half, min(a[1], b[1]) - half, max(a[0], b[0]) + half, max(a[1], b[1]) + half)
        placed_boxes[lyr].append(lbox)
    return True


def _clean_silkscreen(board, cfg=None):
    """F6 DFA + IPC-7351 Silkscreen & Assembly Legend Engine.

    Enforces printable height/stroke/ratio bounds, prefers Condensed width before shrinking
    height, keeps >= 0.20 mm clearance from pads, avoids placing Reference Designators under
    component bodies, attaches DRC-safe leader lines for displaced critical parts, and offloads
    unplaceable or dense non-critical passive designators to F.Fab / B.Fab Assembly layers.
    """
    cfg = dict(cfg or {})
    policy = cfg.get("policy", "auto")
    if policy == "off":
        return {"policy": "off", "skipped": True}

    ds = board.GetDesignSettings()
    silk_clear = max(int(getattr(ds, "m_SilkClearance", 0)), pcbnew.FromMM(0.15))
    mask_exp = max(int(getattr(ds, "m_SolderMaskExpansion", 0)), 0)
    mask_to_cu = max(int(getattr(ds, "m_SolderMaskToCopperClearance", 0)), 0)
    cfg_pad_clr = pcbnew.FromMM(float(cfg.get("min_pad_clearance_mm", 0.20)))
    pad_margin = max(silk_clear + pcbnew.FromMM(0.12), mask_exp + pcbnew.FromMM(0.12), mask_to_cu + pcbnew.FromMM(0.12), cfg_pad_clr)
    silk_margin = silk_clear + pcbnew.FromMM(0.06)
    edge_margin = pcbnew.FromMM(0.20)

    min_h = max(int(getattr(ds, "m_MinSilkTextHeight", 0)), pcbnew.FromMM(float(cfg.get("min_height_mm", 0.80))))
    dense_h = max(min_h, pcbnew.FromMM(float(cfg.get("dense_height_mm", 0.80))))
    target_h = max(dense_h, pcbnew.FromMM(float(cfg.get("target_height_mm", 1.00))))
    max_h = max(target_h, pcbnew.FromMM(float(cfg.get("max_height_mm", 1.27))))
    min_w = max(int(getattr(ds, "m_MinSilkTextThickness", 0)), pcbnew.FromMM(float(cfg.get("min_stroke_mm", 0.15))))
    max_w = max(min_w, pcbnew.FromMM(float(cfg.get("max_stroke_mm", 0.18))))
    min_ratio = float(cfg.get("min_stroke_ratio", 0.10))
    max_ratio = float(cfg.get("max_stroke_ratio", 0.25))
    condensed_ratio = float(cfg.get("condensed_width_ratio", 0.78))
    avoid_bodies = bool(cfg.get("avoid_bodies", True))
    enable_leaders = bool(cfg.get("enable_leader_lines", policy != "assembly_only"))
    effective_mode = cfg.get("effective_mode", "ipc_standard" if policy != "dense" else "dense")
    is_miniature = bool(cfg.get("is_miniature", (cfg.get("density") or {}).get("is_miniature", False)))
    prefer_dense_size = bool(cfg.get("prefer_dense_size", (policy == "dense") or is_miniature))
    proactive_offload = bool(cfg.get("proactive_offload_non_critical", policy == "dense"))
    fallback_offload = bool(cfg.get("fallback_offload_non_critical", policy in ("auto", "ipc_standard", "dense")))
    fallback_offload_critical = bool(cfg.get("fallback_offload_critical", policy in ("auto", "ipc_standard", "dense")))
    populate_hidden_refs = bool(cfg.get("populate_hidden_refs", False))
    normalize_fab_texts = bool(cfg.get("normalize_fab_texts", policy != "off"))

    critical_refs = set(cfg.get("critical_refs") or ())
    non_critical_refs = set(cfg.get("non_critical_refs") or ())

    def _stroke_for(h_nm):
        ideal = round(h_nm / 7.0)
        floor = max(min_w, math.ceil(h_nm * min_ratio))
        ceil_s = min(max_w, math.floor(h_nm * max_ratio))
        return max(floor, min(ceil_s, ideal)) if ceil_s >= floor else floor

    outline_poly = pcbnew.SHAPE_POLY_SET()
    has_outline = bool(board.GetBoardPolygonOutlines(outline_poly, False))
    silk_layers = (pcbnew.F_SilkS, pcbnew.B_SilkS)
    static_boxes = {lyr: [] for lyr in silk_layers}
    body_boxes = {lyr: [] for lyr in silk_layers}
    placed_boxes = {lyr: [] for lyr in silk_layers}

    def add_shape_boxes(g, lyr):
        bb = g.GetBoundingBox()
        w, h = bb.GetWidth(), bb.GetHeight()
        if w <= 0 and h <= 0:
            return
        shape_str = g.GetShapeStr() if hasattr(g, "GetShapeStr") else ""
        is_filled = bool(g.IsFilled()) if hasattr(g, "IsFilled") else False
        stroke = int(g.GetWidth()) if hasattr(g, "GetWidth") else pcbnew.FromMM(0.12)
        m = stroke // 2 + silk_margin
        if shape_str == "Rectangle" and not is_filled and min(w, h) > pcbnew.FromMM(1.5):
            x0, y0, x1, y1 = bb.GetX(), bb.GetY(), bb.GetX() + w, bb.GetY() + h
            static_boxes[lyr].extend([
                (x0 - m, y0 - m, x1 + m, y0 + m),
                (x0 - m, y1 - m, x1 + m, y1 + m),
                (x0 - m, y0 - m, x0 + m, y1 + m),
                (x1 - m, y0 - m, x1 + m, y1 + m),
            ])
        elif shape_str == "Circle" and not is_filled and min(w, h) > pcbnew.FromMM(2.0):
            cx, cy = bb.GetX() + w // 2, bb.GetY() + h // 2
            rad = max(w, h) / 2.0
            for k in range(12):
                a = 2.0 * math.pi * k / 12.0
                px = round(cx + rad * math.cos(a))
                py = round(cy + rad * math.sin(a))
                seg = round(rad * 0.28) + m
                static_boxes[lyr].append((px - seg, py - seg, px + seg, py + seg))
        else:
            static_boxes[lyr].append(_box_from_bbox(bb, silk_margin))

    for d in board.GetDrawings():
        lyr = d.GetLayer()
        if lyr not in silk_layers:
            continue
        if d.GetClass() in ("PCB_TEXT", "PCB_TEXTBOX"):
            static_boxes[lyr].append(_box_from_bbox(d.GetBoundingBox(), silk_margin))
        else:
            add_shape_boxes(d, lyr)

    movable_texts = []
    hidden_ref_candidates = []
    visible_ref_count = 0
    offloaded_refs = []
    fab_created = 0
    fp_body_map = {}
    footprints = sorted(board.GetFootprints(), key=lambda f: (f.GetReference(), f.m_Uuid.AsString()))

    for fp in footprints:
        ref = fp.GetReference()
        silk_lyr = pcbnew.B_SilkS if fp.IsFlipped() else pcbnew.F_SilkS
        b_box = _footprint_body_box(fp)
        fp_body_map[fp.m_Uuid.AsString()] = b_box
        body_boxes[silk_lyr].append(b_box)

        for pad in fp.Pads():
            pbb = pad.GetBoundingBox()
            if pad.IsOnLayer(pcbnew.F_Cu) or pad.IsOnLayer(pcbnew.F_Mask):
                static_boxes[pcbnew.F_SilkS].append(_box_from_bbox(pbb, pad_margin))
            if pad.IsOnLayer(pcbnew.B_Cu) or pad.IsOnLayer(pcbnew.B_Mask):
                static_boxes[pcbnew.B_SilkS].append(_box_from_bbox(pbb, pad_margin))
        for g in fp.GraphicalItems():
            lyr = g.GetLayer()
            if lyr not in silk_layers:
                continue
            if g.GetClass() in _TEXT_CLASSES:
                if g.IsVisible():
                    movable_texts.append((fp, g, False, False))
            else:
                add_shape_boxes(g, lyr)
        for fld in fp.GetFields():
            if fld.GetLayer() not in silk_layers:
                continue
            fld_name = fld.GetName() if hasattr(fld, "GetName") else ""
            is_ref_field = fld_name == "Reference"
            if not fld.IsVisible():
                if is_ref_field and _item_text(fld):
                    if policy == "assembly_only":
                        if _ensure_fab_reference(board, fp):
                            fab_created += 1
                    elif populate_hidden_refs:
                        hidden_ref_candidates.append((fp, fld, True, True))
                    else:
                        if _ensure_fab_reference(board, fp):
                            fab_created += 1
                continue
            if is_ref_field:
                visible_ref_count += 1
            if is_ref_field and (policy == "assembly_only" or (proactive_offload and ref in non_critical_refs)):
                fld.SetVisible(False)
                if _ensure_fab_reference(board, fp):
                    fab_created += 1
                offloaded_refs.append(ref)
            else:
                movable_texts.append((fp, fld, is_ref_field, False))

    # Only auto-populate hidden silkscreen references on boards where all refs start hidden
    # (or when explicitly requested via policy="ipc_standard").
    do_populate_hidden = bool(hidden_ref_candidates) and (policy == "ipc_standard" or visible_ref_count == 0)
    if not do_populate_hidden:
        for fp, _fld, _is_ref, _was_hidden in hidden_ref_candidates:
            if _ensure_fab_reference(board, fp):
                fab_created += 1
        hidden_ref_candidates = []

    def collides_hard(box, lyr):
        if has_outline and not _box_inside_outline(box, outline_poly, edge_margin):
            return True
        for obs in static_boxes[lyr]:
            if _boxes_overlap(box, obs):
                return True
        for obs in placed_boxes[lyr]:
            if _boxes_overlap(box, obs):
                return True
        return False

    def hits_body(box, lyr):
        if not avoid_bodies:
            return False
        t_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
        for bb in body_boxes[lyr]:
            if _box_overlap_area(box, bb) / t_area >= 0.20:
                return True
        return False

    def is_size_compliant(item, is_ref):
        if not is_ref:
            return True
        sz = item.GetTextSize()
        th = int(item.GetTextThickness())
        if sz.y < min_h - pcbnew.FromMM(0.01):
            return False
        if is_miniature and sz.y > dense_h + pcbnew.FromMM(0.05):
            return False
        if th < min_w - pcbnew.FromMM(0.005) or th > max_w + pcbnew.FromMM(0.02):
            return False
        ratio = th / float(max(1, sz.y))
        return (min_ratio - 0.005) <= ratio <= (max_ratio + 0.005)

    # Phase A: keep already IPC-compliant, collision-free, non-hidden texts untouched.
    pending = []
    already_clean = 0
    normalized_in_place = 0
    for fp, item, is_ref, was_hidden in movable_texts:
        lyr = item.GetLayer()
        orig_sz = item.GetTextSize()
        orig_th = int(item.GetTextThickness())
        compliant = is_size_compliant(item, is_ref)
        if not compliant and is_ref:
            # Try normalizing size/stroke in place first while preserving IPC bounds.
            nh = max(min_h, min(max_h, orig_sz.y if orig_sz.y >= min_h else target_h))
            if prefer_dense_size and nh > dense_h:
                nh = dense_h
            nw = max(round(nh * condensed_ratio), min(nh, orig_sz.x)) if orig_sz.x > 0 else nh
            ns = _stroke_for(nh)
            item.SetTextSize(pcbnew.VECTOR2I(int(nw), int(nh)))
            item.SetTextThickness(int(ns))
            box = _text_box(item)
            if not collides_hard(box, lyr) and not hits_body(box, lyr):
                normalized_in_place += 1
                placed_boxes[lyr].append((box[0] - silk_margin, box[1] - silk_margin,
                                          box[2] + silk_margin, box[3] + silk_margin))
                continue
            item.SetTextSize(orig_sz)
            item.SetTextThickness(orig_th)
            pending.append((fp, item, is_ref, was_hidden))
            continue

        box = _text_box(item)
        if not collides_hard(box, lyr) and (not is_ref or not hits_body(box, lyr)):
            already_clean += 1
            placed_boxes[lyr].append((box[0] - silk_margin, box[1] - silk_margin,
                                      box[2] + silk_margin, box[3] + silk_margin))
        else:
            pending.append((fp, item, is_ref, was_hidden))

    if hidden_ref_candidates:
        pending.extend(hidden_ref_candidates)

    # Precompute compact deterministic spiral offsets.
    def _spiral_offsets(max_ring):
        pts = [(0, 0)]
        for k in range(1, max_ring + 1):
            ring = []
            for d in range(-k, k + 1):
                ring.append((d, -k))
                ring.append((d, k))
                if d != -k and d != k:
                    ring.append((-k, d))
                    ring.append((k, d))
            ring.sort(key=lambda t: (t[0] * t[0] + t[1] * t[1], abs(t[0]), abs(t[1]), t[0], t[1]))
            pts.extend(ring)
        return pts

    offsets_near = _spiral_offsets(16)
    offsets_wide = _spiral_offsets(26)

    def _search(
        item,
        lyr,
        is_ref,
        size_trials,
        offsets_list,
        anchor_x,
        anchor_y,
        step_nm,
        angles,
        *,
        require_outside_body,
    ):
        for tag, sz, th in size_trials:
            item.SetTextSize(sz)
            item.SetTextThickness(int(th))
            for dx, dy in offsets_list:
                px = anchor_x + dx * step_nm
                py = anchor_y + dy * step_nm
                item.SetPosition(pcbnew.VECTOR2I(int(px), int(py)))
                for ang in angles:
                    item.SetTextAngleDegrees(ang)
                    box = _text_box(item)
                    if collides_hard(box, lyr):
                        continue
                    if require_outside_body and is_ref and hits_body(box, lyr):
                        continue
                    return tag, box, int(th)
        return None

    # Phase B: polarity markers first, then critical refs, then non-critical passive refs.
    def _sort_key(row):
        fp, _item, is_ref, was_hidden = row
        ref = fp.GetReference()
        tier = 0 if not is_ref else (1 if (ref in critical_refs or ref not in non_critical_refs) else 2)
        return (tier, 1 if was_hidden else 0, ref, fp.m_Uuid.AsString())

    pending.sort(key=_sort_key)
    moved_count = 0
    populated_from_hidden = 0
    condensed_used = 0
    dense_used = 0
    leader_refs = []
    unresolved = 0

    for fp, item, is_ref, was_hidden in pending:
        ref = fp.GetReference()
        is_non_crit = is_ref and (ref in non_critical_refs) and (ref not in critical_refs)
        lyr = item.GetLayer()
        orig_pos = item.GetPosition()
        orig_ang = float(item.GetTextAngleDegrees())
        orig_size = item.GetTextSize()
        orig_thick = int(item.GetTextThickness())
        if was_hidden:
            item.SetVisible(True)
        orig_box = _text_box(item)
        orig_hard_collision = collides_hard(orig_box, lyr)
        orig_under_body = hits_body(orig_box, lyr)
        b_box = fp_body_map[fp.m_Uuid.AsString()]
        fcx = (b_box[0] + b_box[2]) // 2
        fcy = (b_box[1] + b_box[3]) // 2
        anchor_x = fcx if is_ref else orig_pos.x
        anchor_y = fcy if is_ref else orig_pos.y
        angles = (0.0, 90.0) if is_ref else (orig_ang % 180.0, (orig_ang + 90.0) % 180.0)

        if is_ref:
            if prefer_dense_size:
                size_trials = [
                    ("dense", pcbnew.VECTOR2I(int(dense_h), int(dense_h)), _stroke_for(dense_h)),
                    ("dense_condensed", pcbnew.VECTOR2I(round(dense_h * 0.75), int(dense_h)), _stroke_for(dense_h)),
                ]
            else:
                # Always try IPC-7351 standard height + Condensed width (height constant!)
                # BEFORE stepping height down to the 0.80 mm dense floor.
                size_trials = [
                    ("ipc_standard", pcbnew.VECTOR2I(int(target_h), int(target_h)), _stroke_for(target_h)),
                    ("condensed", pcbnew.VECTOR2I(round(target_h * condensed_ratio), int(target_h)), _stroke_for(target_h)),
                    ("dense", pcbnew.VECTOR2I(int(dense_h), int(dense_h)), _stroke_for(dense_h)),
                    ("dense_condensed", pcbnew.VECTOR2I(round(dense_h * 0.75), int(dense_h)), _stroke_for(dense_h)),
                ]
        else:
            size_trials = [("keep", orig_size, max(min_w, orig_thick))]

        step_nm = pcbnew.FromMM(0.25 if (is_miniature or not is_ref) else 0.45)

        # 1. Preferred: outside all component bodies, near footprint.
        found = _search(
            item, lyr, is_ref, size_trials, offsets_near, anchor_x, anchor_y, step_nm, angles,
            require_outside_body=True,
        )
        used_wide = False

        # Non-critical passives must stay unambiguously adjacent to their own body;
        # if the nearest free slot is too far away, offload cleanly to F.Fab/B.Fab instead.
        if found is not None and is_non_crit and (fallback_offload or was_hidden):
            max_passive_dist = pcbnew.FromMM(1.5 if is_miniature else 2.5)
            if _box_distance_nm(found[1], b_box) > max_passive_dist:
                found = None

        # 2. If non-critical passive did not fit cleanly outside bodies -> offload to F.Fab/B.Fab!
        if found is None and is_non_crit and (fallback_offload or was_hidden):
            item.SetTextSize(orig_size)
            item.SetTextThickness(orig_thick)
            item.SetPosition(orig_pos)
            item.SetTextAngleDegrees(orig_ang)
            item.SetVisible(False)
            if _ensure_fab_reference(board, fp):
                fab_created += 1
            offloaded_refs.append(ref)
            continue

        # 3. For critical parts, search wider radius outside bodies (and attach leader line if displaced).
        if found is None and is_ref:
            found = _search(
                item, lyr, is_ref, size_trials, offsets_wide[len(offsets_near):], anchor_x, anchor_y, step_nm, angles,
                require_outside_body=True,
            )
            used_wide = found is not None

        # 3b. If a critical ref STILL has no clean outside-body slot on silkscreen -> offload cleanly
        #     to F.Fab/B.Fab instead of burying it beneath a component body or leaving a collision!
        if found is None and is_ref and (fallback_offload_critical or was_hidden):
            item.SetTextSize(orig_size)
            item.SetTextThickness(orig_thick)
            item.SetPosition(orig_pos)
            item.SetTextAngleDegrees(orig_ang)
            item.SetVisible(False)
            if _ensure_fab_reference(board, fp):
                fab_created += 1
            offloaded_refs.append(ref)
            continue

        # 4. Last resort (non-ref graphics or when fallback offload disabled): allow inside body only if colliding.
        if found is None and not was_hidden and (orig_hard_collision or orig_under_body):
            found = _search(
                item, lyr, is_ref, size_trials, offsets_near, anchor_x, anchor_y, step_nm, angles,
                require_outside_body=False,
            )

        if found is not None:
            tag, found_box, used_stroke = found
            moved_count += 1
            if was_hidden:
                populated_from_hidden += 1
            if "condensed" in tag:
                condensed_used += 1
            if "dense" in tag:
                dense_used += 1
            placed_boxes[lyr].append((found_box[0] - silk_margin, found_box[1] - silk_margin,
                                      found_box[2] + silk_margin, found_box[3] + silk_margin))
            if (
                enable_leaders
                and is_ref
                and not is_non_crit
                and (used_wide or _box_distance_nm(found_box, b_box) >= pcbnew.FromMM(1.5))
                and _try_add_leader_line(
                    board, fp, lyr, found_box, b_box, used_stroke,
                    static_boxes[lyr] + placed_boxes[lyr][:-1], placed_boxes,
                )
            ):
                leader_refs.append(ref)
        else:
            unresolved += 1
            # Keep original geometry if position could not move.
            item.SetTextSize(orig_size)
            item.SetTextThickness(orig_thick)
            item.SetPosition(orig_pos)
            item.SetTextAngleDegrees(orig_ang)
            if was_hidden:
                item.SetVisible(False)
                if _ensure_fab_reference(board, fp):
                    fab_created += 1
            else:
                box = _text_box(item)
                placed_boxes[lyr].append((box[0] - silk_margin, box[1] - silk_margin,
                                          box[2] + silk_margin, box[3] + silk_margin))

    fab_texts_normalized = 0
    fab_values_hidden = 0
    mirrors_fixed = 0
    if normalize_fab_texts:
        ref_only_fab = bool(
            policy == "assembly_only"
            or (visible_ref_count == 0 and len(board.GetFootprints()) >= 40)
        )
        fab_texts_normalized, fab_values_hidden, mirrors_fixed = _normalize_fab_texts(
            board, outline_poly, has_outline, fp_body_map, is_miniature, ref_only_fab=ref_only_fab
        )

    return {
        "policy": policy,
        "effective_mode": effective_mode,
        "fab_profile": cfg.get("fab_profile", "ipc_standard"),
        "is_miniature": is_miniature,
        "checked": len(movable_texts) + len(hidden_ref_candidates) + len(offloaded_refs),
        "already_clean": already_clean,
        "normalized_in_place": normalized_in_place,
        "populated_from_hidden": populated_from_hidden,
        "colliding": len(pending),
        "moved": moved_count,
        "condensed_used": condensed_used,
        "dense_sized": dense_used,
        "offloaded_to_fab": len(offloaded_refs),
        "offloaded_refs": sorted(set(offloaded_refs)),
        "fab_refs_created": fab_created,
        "fab_texts_normalized": fab_texts_normalized,
        "fab_values_hidden": fab_values_hidden,
        "mirrors_fixed": mirrors_fixed,
        "leader_lines_added": len(leader_refs),
        "leader_refs": sorted(set(leader_refs)),
        "unresolved": unresolved,
        "post_texts": _extract_silkscreen_texts(board),
    }


def _eco_strip_routing(board, moved_fps, old_pad_polys):
    """Surgically remove routing invalidated by moving ``moved_fps`` while keeping outside copper.

    Removes:
      1. Any track/arc/via attached to a moved footprint's OLD pad (pre-move geometry).
      2. Any track/arc/via colliding with a moved footprint's NEW pad within clearance.
      3. Iteratively prunes resulting dead-end dangling stubs and orphan vias on affected nets.
    """
    max_error = board.GetDesignSettings().m_MaxError
    ds = board.GetDesignSettings()
    def_clr = int(ds.m_NetSettings.GetDefaultNetclass().GetClearance())
    hole_clr = int(getattr(ds, "m_HoleClearance", 0))
    copper_layers = list(board.GetEnabledLayers().CuStack())
    moved_uuids = {fp.m_Uuid.AsString() for fp in moved_fps}

    # Build NEW pad clearance polygons for moved footprints (after move).
    new_pad_polys = []  # (layer, net_code, is_smd, is_npth, poly)
    for fp in moved_fps:
        for pad in fp.Pads():
            net_code = pad.GetNetCode()
            attr = pad.GetAttribute()
            is_smd = attr == pcbnew.PAD_ATTRIB_SMD
            is_npth = attr == pcbnew.PAD_ATTRIB_NPTH
            raw_clr = pad.GetLocalClearance("") if hasattr(pad, "GetLocalClearance") else None
            loc_clr = int(raw_clr) if raw_clr is not None else 0
            grow = max(loc_clr, def_clr, hole_clr if is_npth else 0) + pcbnew.FromMM(0.02)
            lyrs = copper_layers if is_npth else [lyr for lyr in copper_layers if pad.IsOnLayer(lyr)]
            for lyr in lyrs:
                poly = _item_poly(pad, lyr, max_error, grow)
                if poly.OutlineCount() == 0 and is_npth:
                    poly = _item_poly(pad, pcbnew.F_Cu, max_error, grow)
                if poly.OutlineCount() > 0:
                    new_pad_polys.append((lyr, net_code, is_smd, is_npth, poly))

    all_tracks = list(board.GetTracks())
    seed_remove = set()
    for idx, item in enumerate(all_tracks):
        is_via = item.GetClass() == "PCB_VIA"
        item_net = item.GetNetCode()
        item_lyrs = list(item.GetLayerSet().CuStack()) if is_via else [item.GetLayer()]
        hit = False
        for lyr in item_lyrs:
            item_poly = _item_poly(item, lyr, max_error, 0)
            if item_poly.OutlineCount() == 0:
                continue
            for o_lyr, o_net, o_poly in old_pad_polys:
                if o_lyr == lyr and (o_net == item_net or o_net <= 0) and _overlaps(item_poly, o_poly):
                    hit = True
                    break
            if hit:
                break
            for n_lyr, n_net, n_smd, n_npth, n_poly in new_pad_polys:
                if n_lyr != lyr:
                    continue
                if n_net == item_net and not n_npth and not (is_via and n_smd):
                    continue
                if _overlaps(item_poly, n_poly):
                    hit = True
                    break
            if hit:
                break
        if hit:
            seed_remove.add(idx)

    if not seed_remove:
        return [], len(all_tracks)

    # Fixed pads on affected nets anchor surviving copper.
    affected_nets = {all_tracks[i].GetNetCode() for i in seed_remove}
    fixed_pads_by_net = {}
    for fp in board.GetFootprints():
        if fp.m_Uuid.AsString() in moved_uuids:
            continue
        for pad in fp.Pads():
            nc = pad.GetNetCode()
            if nc in affected_nets:
                fixed_pads_by_net.setdefault(nc, []).append(pad)

    zone_layers_by_net = {}
    for z in board.Zones():
        if z.GetIsRuleArea():
            continue
        nc = z.GetNetCode()
        if nc in affected_nets:
            zone_layers_by_net.setdefault(nc, set()).update(z.GetLayerSet().CuStack())

    # Iteratively prune dangling dead-end tracks and orphan vias on affected nets.
    removed_idxs = set(seed_remove)
    by_net_alive = {}
    for idx, item in enumerate(all_tracks):
        if idx in removed_idxs:
            continue
        nc = item.GetNetCode()
        if nc in affected_nets:
            by_net_alive.setdefault(nc, set()).add(idx)

    def _pt_hits_fixed_pad(nc, lyr, pt):
        for pad in fixed_pads_by_net.get(nc, ()):
            if pad.IsOnLayer(lyr) and pad.HitTest(pt, 0):
                return True
        return False

    def _pt_hits_other_item(nc, lyr, pt, self_idx):
        for other_idx in by_net_alive.get(nc, ()):
            if other_idx == self_idx:
                continue
            other = all_tracks[other_idx]
            if other.GetClass() == "PCB_VIA":
                if lyr in other.GetLayerSet().CuStack() and other.HitTest(pt, 0):
                    return True
            else:
                if other.GetLayer() == lyr and other.HitTest(pt, 0):
                    return True
        return False

    changed = True
    while changed:
        changed = False
        for nc, alive in list(by_net_alive.items()):
            drop = set()
            z_lyrs = zone_layers_by_net.get(nc, set())
            for idx in alive:
                item = all_tracks[idx]
                if item.GetClass() == "PCB_VIA":
                    pos = item.GetPosition()
                    v_lyrs = list(item.GetLayerSet().CuStack())
                    if any(_pt_hits_fixed_pad(nc, lyr, pos) for lyr in v_lyrs):
                        continue
                    track_layers = {lyr for lyr in v_lyrs if _pt_hits_other_item(nc, lyr, pos, idx)}
                    if not track_layers:
                        drop.add(idx)
                        continue
                    connected_layers = track_layers | (set(v_lyrs) & z_lyrs)
                    if len(connected_layers) < 2:
                        drop.add(idx)
                else:
                    lyr = item.GetLayer()
                    for pt in (item.GetStart(), item.GetEnd()):
                        if not _pt_hits_fixed_pad(nc, lyr, pt) and not _pt_hits_other_item(nc, lyr, pt, idx):
                            drop.add(idx)
                            break
            if drop:
                alive -= drop
                removed_idxs |= drop
                changed = True

    removed = [all_tracks[i] for i in sorted(removed_idxs)]
    preserved = len(all_tracks) - len(removed)
    return removed, preserved


def place(board_path, placement_json, out_board, strip_routing, out_json, clean_silk="0"):
    """Write a placement ``{uuid: {x, y, rotation, side}}`` into a new board file."""
    board = pcbnew.LoadBoard(board_path)
    with open(placement_json, encoding="utf-8") as fh:
        placement = json.load(fh)
    by_uuid = {fp.m_Uuid.AsString(): fp for fp in board.GetFootprints()}

    # For Safe ECO mode ('4'), snapshot old pad polygons of footprints that will actually move.
    moved_fps = []
    old_pad_polys = []
    if strip_routing == "4":
        max_error = board.GetDesignSettings().m_MaxError
        copper_layers = list(board.GetEnabledLayers().CuStack())
        for uuid, pose in placement.items():
            fp = by_uuid.get(uuid)
            if fp is None:
                continue
            cur_pos = fp.GetPosition()
            cur_side = "B" if fp.IsFlipped() else "F"
            cur_rot = float(fp.GetOrientationDegrees()) % 360.0
            new_rot = float(pose["rotation"]) % 360.0
            rot_diff = min(abs(cur_rot - new_rot), 360.0 - abs(cur_rot - new_rot))
            if (
                abs(_mm(cur_pos.x) - float(pose["x"])) > 1e-4
                or abs(_mm(cur_pos.y) - float(pose["y"])) > 1e-4
                or rot_diff > 1e-3
                or cur_side != pose["side"]
            ):
                moved_fps.append(fp)
                for pad in fp.Pads():
                    nc = pad.GetNetCode()
                    lyrs = (
                        copper_layers if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                        else [lyr for lyr in copper_layers if pad.IsOnLayer(lyr)]
                    )
                    for lyr in lyrs:
                        poly = _item_poly(pad, lyr, max_error, 0)
                        if poly.OutlineCount() > 0:
                            old_pad_polys.append((lyr, nc, poly))

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
        if "locked" in pose:
            fp.SetLocked(bool(pose["locked"]))
        moved += 1
    preserved_routing = None
    if strip_routing == "4":
        removed, preserved_routing = _eco_strip_routing(board, moved_fps, old_pad_polys)
    else:
        removed = list(board.GetTracks()) if strip_routing in ("1", "2", "3") else []
    unfilled = 0
    if strip_routing == "2":
        removed.extend(zone for zone in board.Zones() if not zone.GetIsRuleArea())
    elif strip_routing in ("3", "4"):
        for zone in list(board.Zones()):
            if zone.GetIsRuleArea():
                continue
            if hasattr(zone, "IsTeardropArea") and zone.IsTeardropArea():
                removed.append(zone)
            else:
                zone.UnFill()
                unfilled += 1
    for item in removed:
        board.Remove(item)
    silk_report = None
    if clean_silk == "1":
        silk_report = _clean_silkscreen(board, None)
    elif clean_silk not in ("0", "", None):
        with open(clean_silk, encoding="utf-8") as fh:
            silk_cfg = json.load(fh)
        silk_report = _clean_silkscreen(board, silk_cfg)
    pcbnew.SaveBoard(out_board, board)
    with open(out_json, "w", encoding="utf-8") as fh:  # not stdout: SWIG writes leak notices there
        json.dump({"placed": moved, "flipped": flipped, "missing": sorted(set(placement) - set(by_uuid)),
                   "removed_routing": len(removed), "preserved_routing": preserved_routing,
                   "moved_footprints": len(moved_fps) if strip_routing == "4" else None,
                   "unfilled_zones": unfilled, "silkscreen": silk_report}, fh)
    del removed


# --------------------------------------------------------------------------
# Macro instances (F0c)
# --------------------------------------------------------------------------
def _vec(pt):
    return pcbnew.VECTOR2I(pcbnew.FromMM(float(pt[0])), pcbnew.FromMM(float(pt[1])))


def _add_copper(board, spec, lock, seed):
    def identify(item, kind, index):
        identity = uuid5(NAMESPACE_URL, f"electronics-ai/copper/{seed}/{kind}/{index}")
        item.SetUuidDirect(pcbnew.KIID(str(identity)))

    added = {"tracks": 0, "vias": 0, "zones": 0}
    for t in spec.get("tracks", []):
        item = pcbnew.PCB_ARC(board) if len(t["points"]) == 3 else pcbnew.PCB_TRACK(board)
        item.SetStart(_vec(t["points"][0]))
        if len(t["points"]) == 3:
            item.SetMid(_vec(t["points"][1]))
        item.SetEnd(_vec(t["points"][-1]))
        item.SetWidth(pcbnew.FromMM(float(t["width"])))
        item.SetLayer(board.GetLayerID(t["layer"]))
        item.SetNet(board.FindNet(t["net"]))
        item.SetLocked(lock)
        identify(item, "tracks", added["tracks"])
        board.Add(item)
        added["tracks"] += 1
    for v in spec.get("vias", []):
        via = pcbnew.PCB_VIA(board)
        vtype = _VIA_TYPE_BY_NAME.get(v.get("via_type", "through"), pcbnew.VIATYPE_THROUGH)
        via.SetViaType(vtype)
        via.SetPosition(_vec((v["x"], v["y"])))
        via.SetLayerPair(board.GetLayerID(v["layers"][0]), board.GetLayerID(v["layers"][1]))
        via.SetWidth(pcbnew.FromMM(float(v["diameter"])))
        via.SetDrill(pcbnew.FromMM(float(v["drill"])))
        if v.get("filled") and hasattr(via, "SetPrimaryDrillFilledFlag"):
            via.SetPrimaryDrillFilledFlag(True)
        if v.get("capped") and hasattr(via, "SetPrimaryDrillCappedFlag"):
            via.SetPrimaryDrillCappedFlag(True)
        via.SetNet(board.FindNet(v["net"]))
        via.SetLocked(lock)
        identify(via, "vias", added["vias"])
        board.Add(via)
        added["vias"] += 1
    for z in spec.get("zones", []):
        zone = pcbnew.ZONE(board)
        layers = pcbnew.LSET()
        for name in z["layers"]:
            layers.AddLayer(board.GetLayerID(name))
        zone.SetLayerSet(layers)
        zone.SetNet(board.FindNet(z["net"]))
        outline = zone.Outline()
        for ring in z["outline"]:
            outline.NewOutline()
            for x, y in ring:
                outline.Append(pcbnew.FromMM(float(x)), pcbnew.FromMM(float(y)))
        if z.get("pad_connection", "solid") == "solid":
            zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        zone.SetZoneName(z.get("name", ""))
        zone.SetAssignedPriority(int(z.get("priority", 0)))
        zone.SetLocked(lock)
        identify(zone, "zones", added["zones"])
        board.Add(zone)
        added["zones"] += 1
    return added


def macro_apply(board_path, spec_json, out_board, out_json):
    """Write a macro instance: keep only some footprints (optional), strip copper (optional),
    pose the parts (flipping when the side changes) and add the macro copper, locked."""
    board = pcbnew.LoadBoard(board_path)
    with open(spec_json, encoding="utf-8") as fh:
        spec = json.load(fh)
    removed = []
    keep = spec.get("keep_only")
    if keep is not None:
        keep = set(keep)
        removed += [fp for fp in board.GetFootprints() if fp.m_Uuid.AsString() not in keep]
    strip = spec.get("strip", "none")  # none | tracks | all
    if strip in ("tracks", "all"):
        removed += list(board.GetTracks())
    if strip == "all":
        removed += [z for z in board.Zones() if not z.GetIsRuleArea()]
    for item in removed:
        board.Remove(item)
    by_uuid = {fp.m_Uuid.AsString(): fp for fp in board.GetFootprints()}
    flipped = 0
    for uuid, pose in spec.get("poses", {}).items():
        fp = by_uuid[uuid]
        if (pose["side"] == "B") != fp.IsFlipped():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            flipped += 1
        fp.SetOrientationDegrees(float(pose["rotation"]))
        fp.SetPosition(_vec((pose["x"], pose["y"])))
    with open(board_path, "rb") as source:
        identity = hashlib.file_digest(source, "sha256")
    identity.update(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    added = _add_copper(board, spec, bool(spec.get("lock", True)), identity.hexdigest())
    if spec.get("fill", True):
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(out_board, board)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"removed": len(removed), "flipped": flipped, **added}, fh)
    del removed


def filled_hits(board_path, points_json, out_json):
    """For each {x, y, radius, net, layer}: is the whole disc on filled copper of that net on that layer?

    The fill is rebuilt first, so the answer is about the copper a fab would get (S6a tap check)."""
    board = pcbnew.LoadBoard(board_path)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    with open(points_json, encoding="utf-8") as fh:
        points = json.load(fh)
    names = {board.GetLayerName(layer): layer for layer in board.GetEnabledLayers().CuStack()}
    names.update({pcbnew.BOARD.GetStandardLayerName(layer): layer for layer in board.GetEnabledLayers().CuStack()})
    out = []
    for p in points:
        layer = names.get(p["layer"])
        hit = False
        if layer is not None:
            r = float(p.get("radius", 0.0))
            probes = [(p["x"], p["y"])] + [(p["x"] + dx * r, p["y"] + dy * r)
                                             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            for zone in board.Zones():
                if zone.GetIsRuleArea() or zone.GetNetname() != p["net"] or not zone.IsOnLayer(layer):
                    continue
                if all(zone.HitTestFilledArea(layer, _vec(q), 0) for q in probes):
                    hit = True
                    break
        out.append(hit)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(out, fh)


if __name__ == "__main__":
    _cmd, _args = sys.argv[1], sys.argv[2:]
    {"prepare": prepare, "finish": finish, "sanitize": sanitize, "lock_routing": lock_routing,
     "heal_thermals": heal_thermals, "heal_mask_bridges": heal_mask_bridges, "dump": dump,
     "place": place, "macro_apply": macro_apply, "filled_hits": filled_hits}[_cmd](*_args)
