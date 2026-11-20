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
import sys
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


def _signature(item):
    cls = item.GetClass()
    if cls == "PCB_VIA":
        p = item.GetPosition()
        return (cls, p.x, p.y, item.GetWidth(pcbnew.F_Cu), item.GetDrillValue(), item.GetNetname())
    mid = (item.GetMid().x, item.GetMid().y) if cls == "PCB_ARC" else None
    a, b = item.GetStart(), item.GetEnd()
    ends = tuple(sorted(((a.x, a.y), (b.x, b.y))))
    return (cls, item.GetLayer(), ends, mid, item.GetWidth(), item.GetNetname())


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
        board.Remove(t)
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
    # ImportSpecctraSES deletes every track on the board, so the fixed copper stays out
    # (and alive in ``fixed``) until the import is done, then replaces its session copy.
    imported = bool(pcbnew.ImportSpecctraSES(board, ses_path))
    fixed_report = _reconcile_fixed(board, fixed)
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
    for t in list(board.GetTracks()):
        if t.IsLocked():
            continue
        if t.m_Uuid.AsString() in remove_uuids:
            to_remove.append(t)
            if t.GetClass() == "PCB_VIA":
                removed_vias += 1
            else:
                removed_tracks += 1
    for t in to_remove:
        board.Remove(t)

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
        "removed_total": removed_tracks + removed_vias,
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
            vias.append({"x": _mm(p.x), "y": _mm(p.y), "diameter": _mm(t.GetWidth(pcbnew.F_Cu)),
                         "drill": _mm(t.GetDrillValue()), "net": t.GetNetname(), "locked": bool(t.IsLocked()),
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
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _boxes_overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _box_from_bbox(bb, pad=0):
    x0 = bb.GetX()
    y0 = bb.GetY()
    return (x0 - pad, y0 - pad, x0 + bb.GetWidth() + pad, y0 + bb.GetHeight() + pad)


def _text_box(item):
    if hasattr(item, "ClearBoundingBoxCache"):
        item.ClearBoundingBoxCache()
    bb = item.GetBoundingBox()
    return (bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight())


def _box_inside_outline(box, outline_poly):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (cx, cy)):
        if not outline_poly.Contains(pcbnew.VECTOR2I(int(px), int(py))):
            return False
    return True


def _clean_silkscreen(board):
    """F6 DFA: relocate colliding footprint silkscreen texts away from pads, graphics, and edges."""
    ds = board.GetDesignSettings()
    silk_clear = max(int(getattr(ds, "m_SilkClearance", 0)), pcbnew.FromMM(0.15))
    mask_exp = max(int(getattr(ds, "m_SolderMaskExpansion", 0)), 0)
    mask_to_cu = max(int(getattr(ds, "m_SolderMaskToCopperClearance", 0)), 0)
    pad_margin = max(silk_clear, mask_exp, mask_to_cu) + pcbnew.FromMM(0.12)
    silk_margin = silk_clear + pcbnew.FromMM(0.06)
    min_h = max(int(getattr(ds, "m_MinSilkTextHeight", 0)), pcbnew.FromMM(0.8))
    min_w = max(int(getattr(ds, "m_MinSilkTextThickness", 0)), pcbnew.FromMM(0.12))

    outline_poly = pcbnew.SHAPE_POLY_SET()
    has_outline = bool(board.GetBoardPolygonOutlines(outline_poly, False))
    silk_layers = (pcbnew.F_SilkS, pcbnew.B_SilkS)
    static_boxes = {lyr: [] for lyr in silk_layers}
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

    # Board-level drawings on silkscreen
    for d in board.GetDrawings():
        lyr = d.GetLayer()
        if lyr not in silk_layers:
            continue
        if d.GetClass() in ("PCB_TEXT", "PCB_TEXTBOX"):
            static_boxes[lyr].append(_box_from_bbox(d.GetBoundingBox(), silk_margin))
        else:
            add_shape_boxes(d, lyr)

    # Footprint pads (copper/mask) and static silkscreen graphics
    movable_texts = []
    footprints = sorted(board.GetFootprints(), key=lambda f: (f.GetReference(), f.m_Uuid.AsString()))
    for fp in footprints:
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
            cls = g.GetClass()
            if cls in ("PCB_TEXT", "FP_TEXT"):
                if g.IsVisible():
                    movable_texts.append((fp, g, False))
            else:
                add_shape_boxes(g, lyr)
        for fld in fp.GetFields():
            if fld.IsVisible() and fld.GetLayer() in silk_layers:
                movable_texts.append((fp, fld, True))

    def collides(box, lyr):
        if has_outline and not _box_inside_outline(box, outline_poly):
            return True
        for obs in static_boxes[lyr]:
            if _boxes_overlap(box, obs):
                return True
        for obs in placed_boxes[lyr]:
            if _boxes_overlap(box, obs):
                return True
        return False

    # Phase A: keep already collision-free texts untouched; defer colliding texts to Phase B.
    pending = []
    for fp, item, is_ref in movable_texts:
        lyr = item.GetLayer()
        box = _text_box(item)
        if not collides(box, lyr):
            placed_boxes[lyr].append((box[0] - silk_margin, box[1] - silk_margin,
                                      box[2] + silk_margin, box[3] + silk_margin))
        else:
            pending.append((fp, item, is_ref))

    # Phase B: place tight polarity/body markers first, then Reference/field labels.
    pending.sort(key=lambda row: (row[2], row[0].GetReference(), row[0].m_Uuid.AsString()))
    moved_count = 0
    for fp, item, is_ref in pending:
        lyr = item.GetLayer()
        orig_pos = item.GetPosition()
        orig_ang = float(item.GetTextAngleDegrees())
        orig_size = item.GetTextSize()
        orig_thick = int(item.GetTextThickness())

        fp_bb = fp.GetBoundingBox(False, False)
        fcx = fp_bb.GetX() + fp_bb.GetWidth() // 2
        fcy = fp_bb.GetY() + fp_bb.GetHeight() // 2
        anchor_x = fcx if is_ref else orig_pos.x
        anchor_y = fcy if is_ref else orig_pos.y
        angles = (0.0, 90.0) if is_ref else (orig_ang % 180.0, (orig_ang + 90.0) % 180.0)

        step_nm = pcbnew.FromMM(0.40 if is_ref else 0.25)
        max_ring = 20 if is_ref else 16
        offsets = [(0, 0)]
        for k in range(1, max_ring + 1):
            ring = []
            for d in range(-k, k + 1):
                ring.append((d, -k))
                ring.append((d, k))
                if d != -k and d != k:
                    ring.append((-k, d))
                    ring.append((k, d))
            ring.sort(key=lambda t: (t[0] * t[0] + t[1] * t[1], abs(t[0]), abs(t[1]), t[0], t[1]))
            offsets.extend(ring)

        size_trials = [(orig_size, orig_thick)]
        if orig_size.y > min_h + pcbnew.FromMM(0.02) or orig_size.x > min_h + pcbnew.FromMM(0.02):
            sh = max(min_h, round(orig_size.y * 0.8))
            sw = max(min_h, round(orig_size.x * 0.8))
            size_trials.append((pcbnew.VECTOR2I(sw, sh), max(min_w, min(orig_thick, pcbnew.FromMM(0.15)))))

        found_box = None
        for sz, th in size_trials:
            item.SetTextSize(sz)
            item.SetTextThickness(th)
            for dx, dy in offsets:
                px = anchor_x + dx * step_nm
                py = anchor_y + dy * step_nm
                item.SetPosition(pcbnew.VECTOR2I(int(px), int(py)))
                for ang in angles:
                    item.SetTextAngleDegrees(ang)
                    box = _text_box(item)
                    if not collides(box, lyr):
                        found_box = box
                        break
                if found_box is not None:
                    break
            if found_box is not None:
                break

        if found_box is not None:
            moved_count += 1
            placed_boxes[lyr].append((found_box[0] - silk_margin, found_box[1] - silk_margin,
                                      found_box[2] + silk_margin, found_box[3] + silk_margin))
        else:
            item.SetTextSize(orig_size)
            item.SetTextThickness(orig_thick)
            item.SetPosition(orig_pos)
            item.SetTextAngleDegrees(orig_ang)
            box = _text_box(item)
            placed_boxes[lyr].append((box[0] - silk_margin, box[1] - silk_margin,
                                      box[2] + silk_margin, box[3] + silk_margin))
    return {"checked": len(movable_texts), "colliding": len(pending), "moved": moved_count}


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
    silk_report = _clean_silkscreen(board) if clean_silk == "1" else None
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
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetPosition(_vec((v["x"], v["y"])))
        via.SetLayerPair(board.GetLayerID(v["layers"][0]), board.GetLayerID(v["layers"][1]))
        via.SetWidth(pcbnew.FromMM(float(v["diameter"])))
        via.SetDrill(pcbnew.FromMM(float(v["drill"])))
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
    {"prepare": prepare, "finish": finish, "sanitize": sanitize, "heal_thermals": heal_thermals,
     "heal_mask_bridges": heal_mask_bridges, "dump": dump, "place": place,
     "macro_apply": macro_apply, "filled_hits": filled_hits}[_cmd](*_args)
