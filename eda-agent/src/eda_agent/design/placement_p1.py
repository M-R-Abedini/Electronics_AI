# SPDX-License-Identifier: MIT
"""P1 engineering envelopes and analytic, per-copper-layer clearance.

Derived envelopes are placement policy, not certified package or fabrication
models. Preserve the raw snapshot and state every assumption in the evidence.
P0 remains available by selecting its policy explicitly.
"""
from __future__ import annotations

import copy
import math
from collections import defaultdict

from . import copper_geometry as geom

POLICY_VERSION = 'placement-evidence-p1-v1'
P0_VERSION = 'placement-evidence-p0-v4'
SUPPORTED_POLICIES = (P0_VERSION, POLICY_VERSION)


def spike_policy(context):
    """Explicit user engineering allowances bound to one board, not defaults."""
    from .placement_evidence import _qualified, _number
    profile=(context.get('assembly_profile') or {}).get('placement_spike')
    if profile is None:return {}
    if not isinstance(profile,dict) or profile.get('scope')!=context.get('board_path'):
        raise ValueError('Placement spike policy must name this exact board')
    source=profile.get('source')
    if not _qualified(source) or source.get('kind')!='user_intent':
        raise ValueError('Placement spike allowances require explicit user intent')
    allowance=_number(profile.get('critical_allowance_mm',0),'spike critical allowance',minimum=0)
    target=profile.get('target_proximity_mm')
    if target is not None:target=_number(target,'spike target proximity',positive=True)
    if not isinstance(profile.get('placement_only',False),bool):
        raise ValueError('placement_only must be boolean')
    return dict(critical_allowance_mm=allowance,target_proximity_mm=target,
                placement_only=profile.get('placement_only',False),source=copy.deepcopy(source),scope=profile['scope'])


def quantization_budget(context, acceptance=None):
    """No grid origin/proven native measurement => no positive allowance.

    The allowance is bounded by the measured error and half the smallest
    captured pitch. A computational search pitch is never a board grid proof.
    """
    result=dict(epsilon_quant_mm=0.0, reason='Native grid origin/on-grid measurement not proven')
    grid=context.get('native_grid') or {}
    proof=acceptance or {}
    if (proof.get('status')!='PASS' or proof.get('target')!=context.get('board_path')
            or proof.get('native_apply') is not True or proof.get('restore')!='PASS'
            or grid.get('source')!='native_capture' or not grid.get('on_grid_verified')):
        return result
    try:
        pitch,origin=grid['pitch_mm'],grid['origin_mm']
        moves=proof.get('exact_moves',[]);readback=proof.get('readback_coords',[])
        if not moves or len(moves)!=len(readback):
            return result
        expected={m['component_id']:m for m in moves};actual={m['component_id']:m for m in readback}
        if len(expected)!=len(moves) or expected.keys()!=actual.keys():return result
        error=max(math.hypot(actual[cid]['x_mm']-m['x_mm'],actual[cid]['y_mm']-m['y_mm']) for cid,m in expected.items())
        values=[*pitch,*origin,error]
        if len(pitch)!=2 or len(origin)!=2 or any(isinstance(v,bool) or not isinstance(v,(float,int))
                or not math.isfinite(v) for v in values) or min(pitch)<=0 or error<0:
            return result
        # A caller's on_grid_verified flag alone is insufficient. Check the
        # captured poses and both sides of the actual native readback witness.
        for point in [*context['components'],*moves,*readback]:
            for i,axis in enumerate(('x_mm','y_mm')):
                value=point[axis]
                nearest=origin[i]+round((value-origin[i])/pitch[i])*pitch[i]
                if not math.isfinite(value) or abs(value-nearest)>error:
                    return result
        result.update(epsilon_quant_mm=min(error,min(pitch)/2),reason='Measured native readback error bounded by captured grid',
            source_snapshot=context.get('snapshot_id'),scope=context['board_path'],native_grid=copy.deepcopy(grid))
    except (KeyError,TypeError):
        pass
    return result


def transformed_pad_aabb_manhattan_mm(a,b,via_transition_penalty_mm=None):
    """Physical 2D gap of the union of captured layer bounds; via is soft only."""
    try:
        def bounds(p):
            rows=p.get('layer_geometries') or [p]
            boxes=[analytic_bounds(r) for r in rows]
            return min(r[0] for r in boxes),min(r[1] for r in boxes),max(r[2] for r in boxes),max(r[3] for r in boxes)
        aa,bb=bounds(a),bounds(b)
        return max(0,aa[0]-bb[2],bb[0]-aa[2])+max(0,aa[1]-bb[3],bb[1]-aa[3])
    except (ValueError,KeyError,TypeError):
        return None


def policy_version(context):
    version = context.get('policy_version', P0_VERSION)
    if version not in SUPPORTED_POLICIES:
        raise ValueError('Unsupported placement policy; recapture under a supported policy')
    return version


def analytic_shape(pad):
    """A point/segment/rectangle core Minkowski-summed with an exact disk.

    Supports rectangles, circles, capsules and uniform rounded rectangles.
    No inscribed polygon participates in a copper-clearance decision.
    """
    x, y = pad['x_mm'], pad['y_mm']
    w, h = pad['width_mm'], pad['height_mm']
    angle = pad.get('rotation_deg', 0)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (x, y, w, h, angle)) or min(w, h) <= 0:
        raise ValueError('Invalid analytic pad dimensions')
    kind = str(pad.get('shape', '')).lower()
    if kind in {'rectangle', 'rect', '2'}:
        radius = 0
    elif kind in {'round', 'circle', '1'} and w == h:
        radius = w / 2
    elif kind in {'oval', 'capsule'}:
        radius = min(w, h) / 2
    elif kind in {'rounded_rectangle', 'roundrect', 'rounded_rect'}:
        radius = pad['corner_radius_mm']
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(radius) or not 0 <= radius <= min(w, h) / 2:
            raise ValueError('Unknown rounded rectangle corner radius')
    else:
        raise ValueError('Unsupported analytic copper shape')
    hx, hy = w / 2 - radius, h / 2 - radius
    local = [(0, 0)] if hx == hy == 0 else ([(-hx, -hy), (hx, hy)] if not hx or not hy
              else [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)])
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    core = [(x + a*ca - b*sa, y + a*sa + b*ca) for a, b in local]
    return core, radius


def analytic_bounds(pad):
    core, radius = analytic_shape(pad)
    return (min(x for x, y in core)-radius, min(y for x, y in core)-radius,
            max(x for x, y in core)+radius, max(y for x, y in core)+radius)


def analytic_distance(a, b):
    ac, ar = analytic_shape(a); bc, br = analytic_shape(b)
    if (len(ac) > 2 and any(geom.point_in_ring(p, ac) for p in bc)
            or len(bc) > 2 and any(geom.point_in_ring(p, bc) for p in ac)):
        return 0.0
    def edges(points):
        if len(points) == 1:
            return [(points[0], points[0])]
        return list(geom.ring_edges(points))
    return max(0.0, min(geom.segment_distance(p, q, r, s)
               for p, q in edges(ac) for r, s in edges(bc)) - ar - br)


def hole_shape(hole):
    raw = hole.get('hole_geometry', hole)
    kind, d = raw.get('shape', 'circle'), raw.get('diameter_mm')
    if kind not in {'circle', 'square', 'capsule'} or not d or d <= 0:
        raise ValueError('Unsupported analytic hole')
    length = max(d, raw.get('slot_length_mm', d)) if kind == 'capsule' else d
    return dict(x_mm=raw['x_mm'], y_mm=raw['y_mm'], width_mm=length, height_mm=d,
                rotation_deg=raw.get('pad_rotation_deg', 0)+raw.get('rotation_within_pad_deg', 0),
                shape={'circle':'circle', 'square':'rectangle', 'capsule':'capsule'}[kind])


def expand_layers(pads):
    """Explicit stacks only; a missing layer cannot become an invented circle."""
    rows = []
    for pad in pads.values():
        layers = pad.get('layer_geometries')
        if not layers:
            if str(pad.get('layer', '')).lower() in {'multilayer', 'multi-layer'}:
                raise ValueError('Multilayer pad requires captured layer geometries')
            layers = [pad]
        seen = set()
        for layer in layers:
            if layer.get('geometry_complete') is False or not layer.get('layer') or layer['layer'] in seen:
                raise ValueError('Partial or ambiguous copper layer geometry')
            seen.add(layer['layer'])
            row = dict(pad, **layer)
            analytic_shape(row)
            rows.append(row)
    return rows


def transform_layer_geometry(raw, transformed, old, pose):
    from .placement_evidence import transform_point, inverse_transform_point
    out = dict(transformed)
    def move_point(x, y):
        return transform_point(inverse_transform_point((x,y), old), pose)
    def move_angle(angle):
        local = angle-old['rotation_deg']
        if old['side']=='bottom': local=-local
        return (pose['rotation_deg']+(local if pose['side']=='top' else -local))%360
    if raw.get('layer_geometries'):
        out['layer_geometries']=copy.deepcopy(raw['layer_geometries'])
        for row in out['layer_geometries']:
            row['x_mm'],row['y_mm']=move_point(row['x_mm'],row['y_mm'])
            row['rotation_deg']=move_angle(row.get('rotation_deg',0))
            row['bbox_mm']=list(analytic_bounds(row))
    if raw.get('hole_geometry'):
        h=out['hole_geometry']=copy.deepcopy(raw['hole_geometry'])
        h['x_mm'],h['y_mm']=move_point(h['x_mm'],h['y_mm'])
        h['pad_rotation_deg']=move_angle(h.get('pad_rotation_deg',0))
    return out


def qualify_unchanged_baseline(context, checks, moves):
    """Local ECO: zero moved-object Tier 0 violations; unrelated ones unchanged.

    Never waive a newly introduced check, changed measurement or a check whose
    scope touches a moved component. The whole-board defects remain reported.
    """
    from .placement_evidence import evaluate_placement
    if not moves:
        return []
    baseline=evaluate_placement(context)
    existing={c['id']:c for c in baseline['checks']}
    moved={m['component_id'] for m in moves}
    waived=[]
    for check in checks:
        old=existing.get(check['id'])
        members=set(check.get('scope',{}).get('components',[]))
        if (members and not members&moved and old==check
                and check['id'].startswith(('courtyard:','outline:','collision:','opposite_assembly:', 'manufacturing:pad_clearance:'))
                and check['status'] in {'fail','unknown'}):
            check['required']=False
            check['baseline_disposition']='unchanged_outside_moved_scope'
            waived.append(check['id'])
    return waived


def board_clearance(snapshot):
    """Read the enabled All/All electrical clearance; never infer fabrication."""
    rules = snapshot.get('native_rules') or {}
    values = snapshot.get('native_rule_values') or {}
    if not isinstance(rules,dict) or not isinstance(values,dict):
        return {}
    coord = values.get('coord_mm')
    if isinstance(coord, bool) or not isinstance(coord, (int, float)) or not math.isfinite(coord) or coord <= 0:
        return {}
    numeric = {r['name']:r for r in values.get('items', []) if 'gap_coord' in r}
    candidates = []
    for rule in rules.get('rules', []):
        if rule.get('rule_kind') != 0 or rule.get('enabled') is not True or rule.get('drc_enabled') is not True:
            continue
        scopes = [rule.get(k, '').strip().casefold() for k in ('scope_1','scope_2')]
        if scopes != ['all', 'all']:
            # These captured track-only constraints provably don't scope pads.
            if not any(s.startswith('istrack') for s in scopes):
                return {}  # An unevaluated pad-specific rule cannot be bypassed.
            continue
        if rule['name'] in numeric:
            gap = numeric[rule['name']]['gap_coord']
            if isinstance(gap, (int,float)) and not isinstance(gap,bool) and math.isfinite(gap) and gap >= 0:
                candidates.append((rule.get('priority', 10**9), rule, gap*coord))
    if not candidates:
        return {}
    _, rule, limit = min(candidates, key=lambda r:r[0])
    return {'minimum_pad_clearance_mm':limit, 'clearance_source':'board_drc_rule',
            'fabricator_verified':False, 'source':{'kind':'measured_geometry',
            'id':snapshot['board_path']+'#rule:'+rule['name'], 'rule':copy.deepcopy(rule),
            'native_value':copy.deepcopy(numeric[rule['name']]),
            'qualification':'captured design constraint; not manufacturer capability'}}


def prepare_context(context):
    """Attach conservative placement envelopes with explicit engineering source."""
    from .placement_evidence import inverse_transform_point, _qualified, _pose, digest
    out = copy.deepcopy(context)
    out['policy_version'] = POLICY_VERSION
    snapshot = out.get('copper_snapshot', {})
    if not out.get('fabrication_profile'):
        out['fabrication_profile'] = board_clearance(snapshot)
    raw_components = {str(c['id']):c for c in snapshot.get('components', [])}
    pads = defaultdict(list)
    for p in out.get('pads', []):
        pads[p['component_id']].append(p)
    for c in out['components']:
        c.setdefault('allowed_rotations_deg',[0,90,180,270])
        if c.get('courtyard_local') is not None and _qualified(c.get('courtyard_source')):
            c.setdefault('envelope_source', 'explicit_courtyard')
            continue
        pose = _pose(c)
        points, witnesses = [], []
        try:
            if not pads[c['id']]:
                raise ValueError('No captured pads for derived envelope')
            for p in expand_layers({p['id']:p for p in pads[c['id']]}):
                box = analytic_bounds(p)
                points += [inverse_transform_point(v, pose) for v in
                           [(box[0],box[1]),(box[2],box[1]),(box[2],box[3]),(box[0],box[3])]]
            raw = raw_components.get(c['id'], {})
            box = raw.get('bbox_mm')
            if box and raw.get('bbox_source'):
                points += [inverse_transform_point(v, pose) for v in
                           [(box[0],box[1]),(box[2],box[1]),(box[2],box[3]),(box[0],box[3])]]
                witnesses.append({'kind':'native_component_extent', 'source':raw['bbox_source'], 'bbox_mm':box})
            elif c.get('body_local'):
                points += c['body_local']
                witnesses.append({'kind':'caller_body_model'})
            else:
                raise ValueError('Body/mechanical extent unavailable; pads alone are insufficient')
            # User-authorized default policy margins, NOT a claim of IPC compliance.
            # Two copper pads do not prove a passive: also require captured type.
            text = str(raw.get('source_description','')).upper()
            passive = len(pads[c['id']]) == 2 and any(k in text for k in ('RESISTOR', 'CAPACITOR', 'INDUCTOR'))
            margin = .15 if passive else .25
            xs, ys = zip(*points)
            x0,x1,y0,y1 = min(xs)-margin,max(xs)+margin,min(ys)-margin,max(ys)+margin
            ring = [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
            c.update(courtyard_local=ring, envelope_source='derived_pad_body_margin',
                     ipc_courtyard_margin_mm=margin,
                     courtyard_source={'kind':'engineering_policy', 'id':'p1-tiered-envelope:'+digest(ring),
                       'revision':POLICY_VERSION, 'margin_source':'user_authorized_default',
                       'ipc_compliance_verified':False, 'body_witnesses':witnesses,
                       'qualification':'conservative 2D placement envelope; 3D/mechanical certification not implied'})
        except (KeyError, ValueError, TypeError) as error:
            c['envelope_diagnostic'] = str(error)
    return out


def manufacturing_checks(context, pads, snapshot):
    from .placement_evidence import _check, _qualified
    profile = context.get('fabrication_profile', {})
    source, limit = profile.get('source'), profile.get('minimum_pad_clearance_mm')
    if limit is None or not _qualified(source):
        return [_check('manufacturing:pad_clearance_input','unknown',snapshot,
                       reason='No qualified pad clearance profile or supported board DRC rule')]
    if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not math.isfinite(limit) or limit < 0:
        raise ValueError('Invalid pad clearance')
    try:
        rows = expand_layers(pads)
        physical = context.get('copper_snapshot', {})
        for h in physical.get('holes', []):
            hole_shape(h)
        unresolved = set(physical.get('missing', [])) - {
            'slotted/square pad hole outline unavailable', 'non-simple pad stack geometry unavailable'}
        known_partial = bool(physical.get('missing')) and not unresolved
        if (unresolved or context.get('pad_ownership_complete') is False
                or context.get('coverage',{}).get('pads') not in {'complete',True} and not known_partial):
            raise ValueError('Pad extraction has unresolved missing geometry')
        # Non-simple stacks must cover every captured copper layer.
        copper = {r['name'] for r in physical.get('layers',[]) if r.get('kind') in {'signal','plane'}}
        for p in pads.values():
            if str(p.get('layer','')).lower() == 'multilayer' and copper != {r['layer'] for r in p.get('layer_geometries',[])}:
                raise ValueError('Pad stack does not cover the complete captured copper stack')
    except (KeyError, ValueError, TypeError) as error:
        return [_check('manufacturing:pad_extraction','unknown',snapshot,reason=str(error),source=source)]
    scope = set(context['scope_component_ids'])
    index, boxes = defaultdict(geom.SpatialIndex), []
    for i,p in enumerate(rows):
        box = analytic_bounds(p); boxes.append(box); index[p['layer'].casefold()].insert(i, box)
    checks, seen = [], set()
    for i,a in enumerate(rows):
        if a.get('component_id') not in scope:
            continue
        box=boxes[i]; search=(box[0]-limit,box[1]-limit,box[2]+limit,box[3]+limit)
        for j in index[a['layer'].casefold()].query(search):
            pair=tuple(sorted((i,j)))
            if i==j or pair in seen:
                continue
            seen.add(pair); b=rows[j]
            if a.get('net_name') and a.get('net_name')==b.get('net_name'):
                continue
            measured=analytic_distance(a,b)
            if measured+geom.EPS < limit:
                checks.append(_check('manufacturing:pad_clearance:'+a['id']+':'+b['id']+':'+a['layer'],
                    'fail',snapshot,measured=measured,limit=limit,unit='mm',source=source,
                    scope={'pads':[a['id'],b['id']], 'components':sorted({p.get('component_id') for p in (a,b) if p.get('component_id')})},
                    geometry={'layer':a['layer'],'method':'analytic_rounded_convex_cores'}))
    checks.append(_check('manufacturing:pad_clearance','pass',snapshot,
        limit=limit,unit='mm',source=source,geometry={'physical_pad_count':len(pads),'layer_geometry_count':len(rows),
            'clearance_source':profile.get('clearance_source','supplied_profile'),'analytic_holes_consumed':len(physical.get('holes',[])),
            'scope':'pad-to-pad only; not a full fabrication/copper DRC'}))
    # Drill-to-drill distance uses the captured dedicated rule, not the
    # electrical copper clearance. Slot cores and radii participate analytically.
    rules=(physical.get('native_rules') or {}).get('rules',[])
    vals=physical.get('native_rule_values') or {}
    if not isinstance(vals,dict):vals={}
    numeric={r['name']:r for r in vals.get('items',[]) if 'gap_coord' in r}
    hole_rules=[r for r in rules if r.get('rule_kind')==52 and r.get('enabled') is True
        and r.get('drc_enabled') is True and r.get('scope_1','').casefold()=='all'
        and r.get('scope_2','').casefold()=='all' and r['name'] in numeric]
    if hole_rules:
        rule=min(hole_rules,key=lambda r:r.get('priority',10**9))
        hole_limit=numeric[rule['name']]['gap_coord']*vals['coord_mm']
        holes=[]
        for raw in physical.get('holes',[]):
            owner=pads.get(str(raw.get('owner_id')), {})
            h=dict(raw)
            if owner.get('hole_geometry'):
                h['hole_geometry']=owner['hole_geometry']
            holes.append((hole_shape(h),owner.get('component_id')))
        for i,(a,ca) in enumerate(holes):
            for b,cb in holes[i+1:]:
                if not ({ca,cb}&scope): continue
                distance=analytic_distance(a,b)
                if distance+geom.EPS < hole_limit:
                    checks.append(_check('manufacturing:hole_clearance:'+str(i)+':'+str(len(checks)),
                        'fail',snapshot,measured=distance,limit=hole_limit,unit='mm',
                        scope={'components':sorted({c for c in (ca,cb) if c})},source={'kind':'measured_geometry',
                        'id':physical['board_path']+'#rule:'+rule['name']},
                        geometry={'method':'analytic_circle_square_capsule_drills'}))
    return checks
