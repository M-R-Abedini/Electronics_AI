# SPDX-License-Identifier: MIT
"""Deterministic P0 proxies; no inductance, routing or fabrication claims."""
import math


def transformed_pad_aabb_manhattan_mm(a, b, via_transition_penalty_mm=None):
    """Manhattan gap between transformed pad AABBs plus declared side penalty."""
    if not a.get('outline') or not b.get('outline'):
        return None
    def bounds(pad):
        shape = str(pad.get('shape','')).lower()
        w,h = pad.get('width_mm'),pad.get('height_mm')
        if shape in {'circle','round','rounded','oval','oblong'} and isinstance(w,(int,float)) and isinstance(h,(int,float)):
            radius = min(w,h)/2
            straight = abs(w-h)/2
            angle = math.radians(pad.get('rotation_deg',0) + (90 if h>w else 0))
            hx,hy = radius+straight*abs(math.cos(angle)), radius+straight*abs(math.sin(angle))
            return pad['x_mm']-hx,pad['y_mm']-hy,pad['x_mm']+hx,pad['y_mm']+hy
        points = pad['outline']
        return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)
    ax1, ay1, ax2, ay2 = bounds(a); bx1, by1, bx2, by2 = bounds(b)
    result = max(0, ax1-bx2, bx1-ax2) + max(0, ay1-by2, by1-ay2)
    # Pad layer, rather than package center side, is the connection evidence.
    layer_a, layer_b = str(a.get('layer', '')).lower(), str(b.get('layer', '')).lower()
    def side(layer):
        if layer in {'top', 'top layer', 'toplayer'}: return 'top'
        if layer in {'bottom', 'bottom layer', 'bottomlayer'}: return 'bottom'
        if layer in {'multi', 'multi-layer', 'multilayer', 'multi layer'}: return 'both'
    sa, sb = side(layer_a), side(layer_b)
    if sa is None or sb is None:
        return None
    if sa != sb and 'both' not in (sa, sb):
        value = via_transition_penalty_mm
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            return None
        result += value
    return result


def crossing_proxy(pads, excluded):
    """Reuse the existing MST crossing diagnostic on exact pad centers."""
    from collections import defaultdict
    from eda_agent.placement.autoplace import PlaceComp, PlaceNet
    from .pcb_placement import ratsnest_crossings
    nets = defaultdict(list)
    for p in pads.values():
        if p.get('net_name') not in excluded and p.get('net_name'):
            if p.get('x_mm') is None: return None
            nets[p['net_name']].append(p)
    points = {p['id']: p for values in nets.values() if len(values) > 1 for p in values}
    if len(points) > 512:
        return None  # Explicit bounded diagnostic, not an invented zero.
    comps = [PlaceComp(pid, 0, 0, p['x_mm'], p['y_mm']) for pid,p in points.items()]
    pos = {pid: [p['x_mm'],p['y_mm']] for pid,p in points.items()}
    return ratsnest_crossings(comps, pos, {p:0 for p in points}, {p:0 for p in points},
        [PlaceNet(tuple(p['id'] for p in values), name=name) for name,values in nets.items() if len(values)>1])


def alignment_proxy(context, intent, poses):
    pitch = intent.get('grid_mm')
    if isinstance(pitch, bool) or not isinstance(pitch, (float,int)) or not math.isfinite(pitch) or pitch <= 0:
        return None
    origin = context.get('origin_mm')
    if not origin or len(origin) != 2: return None
    members = set(); aligned = set()
    for group in intent.get('passive_families', []):
        ids = [cid for cid in group.get('component_ids', []) if cid in poses]
        if len(ids)<2: continue
        members.update(ids)
        for cid in ids:
            p = poses[cid]
            grid = all(abs((p[k]-origin[i])/pitch-round((p[k]-origin[i])/pitch))<1e-8 for i,k in enumerate(('x_mm','y_mm')))
            if grid and any(other!=cid and (abs(p['x_mm']-poses[other]['x_mm'])<1e-8 or
                                            abs(p['y_mm']-poses[other]['y_mm'])<1e-8) for other in ids):
                aligned.add(cid)
    return len(aligned)/len(members) if members else None
