# SPDX-License-Identifier: MIT
"""Placement copper obstacles; exact convex cores and bounded arc chords.

Unpoured definitions are not copper. Net-less copper is an obstacle to every
net. Existing routed attachments are reported and never silently ripped up.
"""
from __future__ import annotations
import math
from collections import defaultdict
from . import copper_geometry as g, placement_p1 as p1


def section_complete(snapshot,name):
    if name not in snapshot.get('coverage',{}):return snapshot.get('complete') is True
    v=snapshot.get('coverage',{}).get(name)
    return v in (True,'complete') if not isinstance(v,dict) else v.get('complete') is True or v.get('status')=='complete'


def via_layers(snapshot,via):
    layers=[l['name'] for l in sorted(snapshot.get('layers',[]),key=lambda l:l.get('order',0)) if l.get('kind') in {'signal','plane','copper'}]
    try:a,b=layers.index(via['start_layer']),layers.index(via['end_layer'])
    except (ValueError,KeyError):raise ValueError('Via copper span unavailable')
    return layers[min(a,b):max(a,b)+1]


def segment_shape(s):
    a,b=g.xy(s['start']),g.xy(s['end']);w=s['width_mm']
    if not math.isfinite(w) or w<=0:raise ValueError('Track width unavailable')
    return dict(x_mm=(a[0]+b[0])/2,y_mm=(a[1]+b[1])/2,width_mm=g.distance(a,b)+w,height_mm=w,
                rotation_deg=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0])),shape='capsule')


def obstacles(snapshot,error_mm=.0005):
    from .copper_audit import _segments
    rows=[];unknown=[]
    for section in ('tracks','arcs','vias','regions','layers'):
        if not section_complete(snapshot,section):unknown.append('Incomplete '+section+' extraction')
    extraction={'unknowns':[]}
    segments=_segments(snapshot,report=extraction,error=error_mm)
    unknown.extend(r['reason'] for r in extraction['unknowns'])
    for s in segments:
        rows.append(dict(id=s.get('arc_id',s['id']),kind='arc' if s.get('arc_id') else 'track',
            layer=s.get('layer'),net_name=s.get('net_name',''),shape=segment_shape(s),error_mm=s.get('arc_error_mm',0)))
    for v in snapshot.get('vias',[]):
        try:
            for layer in via_layers(snapshot,v):
                shape=dict(x_mm=v['x_mm'],y_mm=v['y_mm'],width_mm=v['diameter_mm'],height_mm=v['diameter_mm'],shape='circle')
                p1.analytic_shape(shape)
                rows.append(dict(id=v['id'],kind='via',layer=layer,net_name=v.get('net_name',''),shape=shape,error_mm=0))
        except (KeyError,ValueError,TypeError) as exc:unknown.append(str(exc))
    for r in snapshot.get('regions',[]):
        if r.get('poured') is not True or r.get('kind') in {'polygon_definition','cutout','keepout'}:continue
        if r.get('geometry_complete') is False or not g.region_geometry_valid(r):
            unknown.append('Incomplete poured region '+str(r.get('id')));continue
        rows.append(dict(id=r['id'],kind='region',layer=r.get('layer'),net_name=r.get('net_name',''),region=r,error_mm=0))
    return rows,sorted(set(unknown))


def copper_distance(pad,obstacle):
    if 'shape' in obstacle:return p1.analytic_distance(pad,obstacle['shape'])
    region=obstacle['region'];core,radius=p1.analytic_shape(pad)
    if any(g.point_in_region(p,region) for p in core):return 0.
    if len(core)>2 and any(g.point_in_ring(g.xy(p),core) for p in region['outline']):return 0.
    edges=list(g.ring_edges(core)) if len(core)>1 else [(core[0],core[0])]
    boundaries=[e for ring in [region['outline']]+region.get('holes',[]) for e in g.ring_edges(ring)]
    return max(0.,min(g.segment_distance(a,b,c,d) for a,b in edges for c,d in boundaries)-radius)


class CopperObstacleIndex:
    def __init__(self,snapshot):
        self.rows,self.unknowns=obstacles(snapshot)
        self.index=defaultdict(g.SpatialIndex)
        for i,row in enumerate(self.rows):
            if 'shape' in row:box=p1.analytic_bounds(row['shape'])
            else:
                points=[g.xy(p) for p in row['region']['outline']]
                box=(min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points))
            error=row['error_mm'];self.index[row['layer']].insert(i,(box[0]-error,box[1]-error,box[2]+error,box[3]+error))

    def query(self,pad,clearance=0):
        a,b,c,d=p1.analytic_bounds(pad)
        return [self.rows[i] for i in self.index[pad['layer']].query((a-clearance,b-clearance,c+clearance,d+clearance))]

    def clear(self,pad,clearance):
        return all(pad.get('net_name') and pad.get('net_name')==r['net_name'] or
                   copper_distance(pad,r)-r['error_mm']+g.EPS>=clearance for r in self.query(pad,clearance))


def nearest_via_access(snapshot,pad):
    choices=[]
    for v in snapshot.get('vias',[]):
        if not pad.get('net_name') or v.get('net_name')!=pad['net_name']:continue
        try:
            if pad['layer'] not in via_layers(snapshot,v):continue
            shape=dict(x_mm=v['x_mm'],y_mm=v['y_mm'],width_mm=v['diameter_mm'],height_mm=v['diameter_mm'],shape='circle')
            choices.append((p1.analytic_distance(pad,shape),str(v['id'])))
        except (KeyError,ValueError,TypeError):continue
    return min(choices) if choices else (None,None)


def audit_placement_copper(context,pads,snapshot_id):
    from .placement_evidence import _check,transformed_pads,_pose
    physical=context.get('copper_snapshot')
    if physical is None:
        return [_check('copper:obstacles','unknown' if context.get('source')=='live' else 'not_applicable',snapshot_id,
            reason='No physical copper snapshot; native qualification requires live evidence')],[]
    index=CopperObstacleIndex(physical);rows,unknown=index.rows,list(index.unknowns);checks=[];scope=set(context['scope_component_ids'])
    limit=context.get('fabrication_profile',{}).get('minimum_pad_clearance_mm')
    if limit is None:unknown.append('No sourced electrical clearance')
    for reason in unknown:checks.append(_check('copper:coverage:'+str(len(checks)),'unknown',snapshot_id,reason=reason))
    if limit is None:return checks,[]
    moved=p1.expand_layers({k:v for k,v in pads.items() if v.get('component_id') in scope})
    affected=set()
    original=transformed_pads(context,{c['id']:_pose(c) for c in context['components']})
    originals={(r['id'],r['layer']):r for r in p1.expand_layers({k:v for k,v in original.items() if v.get('component_id') in scope})}
    for a in moved:
        old=originals.get((a['id'],a['layer']))
        nearby={ (r['id'],r['layer']):r for p in (a,old) if p for r in index.query(p,limit)}
        for b in nearby.values():
            same=a.get('net_name') and a.get('net_name')==b.get('net_name')
            d=copper_distance(a,b);error=b['error_mm']
            if same:
                if old and copper_distance(old,b)<=error+g.EPS and d>error+.0000031:
                    affected.add(a['net_name'])
                continue
            if d-error+g.EPS>=limit:continue
            checks.append(_check('copper:clearance:'+a['id']+':'+str(b['id'])+':'+a['layer'],
                'fail' if d+error+g.EPS<limit else 'unknown',snapshot_id,measured=d,limit=limit,unit='mm',
                scope={'components':[a['component_id']],'pads':[a['id']]},
                geometry={'x_mm':a['x_mm'],'y_mm':a['y_mm'],'layer':a['layer'],'net_name':a.get('net_name',''),
                    'obstacle_id':b['id'],'obstacle_net':b['net_name'],'obstacle_kind':b['kind'],'error_mm':error},
                reason='Foreign-net physical copper clearance'))
    if affected:
        policy=p1.spike_policy(context)
        allowed=policy.get('placement_only') and policy.get('allow_routing_detach')
        checks.append(_check('copper:ripup_required','fail' if not allowed else 'unknown',snapshot_id,
            required=not allowed,kind='diagnostic' if allowed else 'hard',
            scope={'components':sorted(scope)},geometry={'affected_routed_nets':sorted(affected)},
            reason='Placement detaches existing routed copper; explicit rerouting required; no tracks are removed automatically'))
    checks.append(_check('copper:obstacles','pass' if not unknown else 'unknown',snapshot_id,
        geometry={'obstacle_count':len(rows),'scope':'same-layer pad to actual tracks, bounded arcs, via annuli and poured copper'},
        reason='Unpoured polygon definitions are excluded from conductive geometry'))
    return checks,sorted(affected)
