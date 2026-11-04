# SPDX-License-Identifier: MIT
"""Four-stage subcircuit proposals over the existing immutable placement model.

No routing, board resizing, implicit flips or fabricated pin-role limits.
Structural role recognition guides proposals; the full existing judge gates them.
"""
from __future__ import annotations
import copy,math
from collections import defaultdict
from . import placement_evidence as core,placement_p1 as p1,copper_geometry as g
from .placement_copper import CopperObstacleIndex,ViaAccessIndex

ROLES={'decoupling','series_resistor','pull_resistor','feedback','crystal','oscillator','inductor','diode','passive'}

def prepare_intent(context,intent):
    out=copy.deepcopy(intent or {});scope=set(context['scope_component_ids'])
    rails=set(out.get('power_nets',[]))|set(out.get('reference_nets',[]))|{'GND'}
    incident={p['net_name'] for p in context['pads'] if p['component_id'] in scope and p.get('net_name') and p['net_name'] not in rails}
    out['metric_net_names']=sorted(incident)
    out['metric_component_ids']=sorted(scope)
    out['systematic_placement']=True
    out.setdefault('grid_mm',.254);out.setdefault('grid_origin_mm',[0.,0.])
    out.setdefault('crossing_penalty_mm',2.)
    core._number(out['grid_mm'],'computational grid',positive=True)
    core._number(out['crossing_penalty_mm'],'crossing penalty',minimum=0)
    core._point(out['grid_origin_mm'],'computational grid origin')
    if not out.get('passive_families'):
        families=defaultdict(list);roles=recognize_roles(context,out);comps={p['id']:p for p in context['components']};byid=core.transformed_pads(context,{cid:core._pose(comp) for cid,comp in comps.items()})
        for c in context['components']:
            if c['id'] not in scope:continue
            box=core._bbox(c['courtyard_local']);dims=sorted((box[2]-box[0],box[3]-box[1]))
            role=roles[c['id']];target=byid[role['target_pad_ids'][0]];anchor=comps[target['component_id']]
            dx,dy=target['x_mm']-anchor['x_mm'],target['y_mm']-anchor['y_mm']
            face=('east' if dx>=0 else 'west') if abs(dx)>abs(dy) else ('north' if dy>=0 else 'south')
            families[(tuple(round(v,1) for v in dims),c['side'],role['role'],face)].append(c['id'])
        out['passive_families']=[dict(id='size:'+str(i),component_ids=ids) for i,ids in enumerate(families.values()) if len(ids)>1]
    return out


def recognize_roles(context,intent):
    comps={c['id']:c for c in context['components']};pads={p['id']:p for p in context['pads']}
    anchors=set(intent.get('anchor_component_ids',[]))
    if not anchors or not anchors<=comps.keys():raise ValueError('Systematic placement requires explicit captured IC anchors')
    scope=set(context['scope_component_ids'])
    if scope&anchors:raise ValueError('IC anchors must be excluded from the movable scope')
    rails=set(intent.get('power_nets',[]))|set(intent.get('reference_nets',[]))|{'GND'}
    result={}
    for cid in sorted(scope):
        own=[p for p in pads.values() if p['component_id']==cid]
        declared=intent.get('placement_roles',{}).get(cid,{})
        if 'preserve_target_distance' in declared and not isinstance(declared['preserve_target_distance'],bool):
            raise ValueError('preserve_target_distance must be boolean')
        targets=declared.get('target_pad_ids') or [p['id'] for p in pads.values() if p['component_id'] in anchors and
            p.get('net_name') not in rails and any(q.get('net_name')==p.get('net_name') and p.get('net_name') for q in own)]
        if not targets:
            targets=[p['id'] for p in pads.values() if p['component_id'] in anchors and any(q.get('net_name')==p.get('net_name') and p.get('net_name') for q in own)]
        if not set(targets)<=pads.keys() or any(pads[i]['component_id'] not in anchors for i in targets):raise ValueError('Role targets must be actual anchor pads')
        role=declared.get('role')
        if role is None:
            ref=comps[cid]['reference'].upper();nets={p.get('net_name') for p in own}
            role=('pull_resistor' if nets&rails else 'series_resistor') if ref.startswith('R') else 'crystal' if ref.startswith(('Y','X')) else 'inductor' if ref.startswith('L') else 'diode' if ref.startswith('D') else 'decoupling' if ref.startswith('C') and nets&set(intent.get('power_nets',[])) and nets&set(intent.get('reference_nets',['GND'])) else 'passive'
        if role not in ROLES or not targets:raise ValueError('Role needs a supported function and connected target pads: '+cid)
        if declared and not core._qualified(declared.get('source')):raise ValueError('Declared pin roles need provenance')
        pairs=[(q['id'],pid) for q in own for pid in targets if q.get('net_name') and q.get('net_name')==pads[pid].get('net_name')]
        if not pairs:raise ValueError('Role target pad net disagrees with captured connectivity')
        result[cid]=dict(role=role,target_pad_ids=targets,pad_pairs=pairs,
            status='declared_pin_binding' if declared else 'structural_hypothesis',source=declared.get('source'))
    return result


def escape_channels(context,intent):
    anchors=set(intent.get('escape_anchor_component_ids',intent.get('anchor_component_ids',[])));rails=set(intent.get('power_nets',[]))|set(intent.get('reference_nets',[]))|{'GND'}
    length=intent.get('escape_length_mm',.8);trace=intent.get('escape_track_width_mm',.15)
    sizes=[v['diameter_mm'] for v in context.get('copper_snapshot',{}).get('vias',[]) if v.get('diameter_mm',0)>0]
    via=intent.get('escape_via_diameter_mm',min(sizes) if sizes else None)
    gap=context.get('fabrication_profile',{}).get('minimum_pad_clearance_mm')
    if gap is None or any(type(v) not in (float,int) or not math.isfinite(v) or v<=0 for v in (length,trace,via)):raise ValueError('Escape reservations need positive track/via dimensions and captured clearance')
    width=trace+2*gap;via_width=via+2*gap;length=max(length,via_width)
    comps={c['id']:c for c in context['components']};rows=[]
    world=core.transformed_pads(context,{cid:core._pose(c) for cid,c in comps.items()})
    for p in world.values():
        if p.get('component_id') not in anchors or not p.get('net_name') or p['net_name'] in rails:continue
        c=comps[p['component_id']];box=core._bbox(core._component_ring(c,core._pose(c)));dx=p['x_mm']-c['x_mm'];dy=p['y_mm']-c['y_mm']
        horizontal=abs(dx)/max(box[2]-box[0],.001)>abs(dy)/max(box[3]-box[1],.001)
        direction=(1 if dx>=0 else -1,0) if horizontal else (0,1 if dy>=0 else -1)
        bound=(box[2] if dx>=0 else box[0]) if horizontal else (box[3] if dy>=0 else box[1])
        end=(bound+direction[0]*length,p['y_mm']) if horizontal else (p['x_mm'],bound+direction[1]*length)
        start=(p['x_mm'],p['y_mm'])
        from .placement_copper import segment_shape
        rows.append(dict(id='escape:'+p['id'],layer=p['layer'],net_name=p['net_name'],pad_id=p['id'],
            shape=segment_shape(dict(start=start,end=end,width_mm=width)),
            source=dict(kind='engineering_policy',id='straight-escape:'+str(context.get('snapshot_id')),revision='1'),
            via_landing_diameter_mm=via_width,
            limitation='straight trace escape; via landing must be staggered beyond dense pin rows; not completed fanout or routing'))
    return rows


def escape_checks(context,intent,pads,snapshot,moved_component_ids=None):
    if not intent.get('systematic_placement'):return []
    scope=set(context['scope_component_ids']);channels=escape_channels(context,intent);checks=[];roles=recognize_roles(context,intent)
    rows=p1.expand_layers({pid:p for pid,p in pads.items() if p.get('component_id') in scope})
    for p in rows:
        for ch in channels:
            role=roles[p['component_id']]
            if role['status']=='declared_pin_binding' and role['role'] in {'series_resistor','inductor','diode'} and ch['pad_id'] in role['target_pad_ids']:continue
            if p['layer']!=ch['layer'] or p.get('net_name')==ch['net_name']:continue
            if not g.bbox_overlap(p1.analytic_bounds(p),p1.analytic_bounds(ch['shape'])):continue
            if p1.analytic_distance(p,ch['shape'])<g.EPS:
                active=moved_component_ids is None or p['component_id'] in moved_component_ids
                checks.append(core._check(ch['id']+':'+p['id'],'fail',snapshot,required=active,
                    scope={'components':[p['component_id']],'pads':[p['id'],ch['pad_id']]},
                    source=ch['source'],reason='Pad obstructs a reserved foreign-net signal escape' if active else 'Unchanged existing escape obstruction outside the applied move set',geometry=ch))
    checks.append(core._check('escape:coverage','pass',snapshot,geometry=dict(channel_count=len(channels),routing_verified=False)))
    baseline=core.transformed_pads(context,{c['id']:core._pose(c) for c in context['components']})
    allowance=p1.spike_policy(context).get('critical_allowance_mm',0)
    for cid,role in roles.items():
        if not intent.get('placement_roles',{}).get(cid,{}).get('preserve_target_distance'):continue
        for a,b in role['pad_pairs']:
            before=p1.transformed_pad_aabb_manhattan_mm(baseline[a],baseline[b])
            after=p1.transformed_pad_aabb_manhattan_mm(pads[a],pads[b])
            checks.append(core._check('role_proximity:'+a+':'+b,'pass' if before is not None and after is not None and after<=before+allowance else 'fail',snapshot,
                scope={'components':[cid],'pads':[a,b]},measured=after,limit=before+allowance if before is not None else None,
                source=role['source'],reason='Explicit role-distance nonregression policy; not an inferred datasheet maximum'))
    return checks


def scoped_metrics(context,intent,pads,poses):
    names=set(intent.get('metric_net_names',[]));points=defaultdict(list)
    for p in pads.values():
        if p.get('net_name') in names:points[p['net_name']].append(p)
    hpwl=sum(intent.get('signal_net_weights',{}).get(n,1)*(max(p['x_mm'] for p in ps)-min(p['x_mm'] for p in ps)+max(p['y_mm'] for p in ps)-min(p['y_mm'] for p in ps)) for n,ps in points.items())
    from .placement_p0 import crossing_proxy
    crosses=crossing_proxy({p['id']:p for ps in points.values() for p in ps},set())
    members=set();aligned=set();origin=intent['grid_origin_mm'];pitch=intent['grid_mm']
    for family in intent.get('passive_families',[]):
        ids=[cid for cid in family['component_ids'] if cid in poses];members.update(ids)
        for axis in ('x_mm','y_mm'):
            other='y_mm' if axis=='x_mm' else 'x_mm';groups=defaultdict(list)
            for cid in ids:
                p=poses[cid]
                if all(abs((p[a]-origin[i])/pitch-round((p[a]-origin[i])/pitch))<1e-7 for i,a in enumerate(('x_mm','y_mm'))):groups[(round(p[axis],7),round(p['rotation_deg'],7))].append(cid)
            for ids_in_line in groups.values():
                if len(ids_in_line)<2:continue
                values=sorted(poses[cid][other] for cid in ids_in_line);gaps=[b-a for a,b in zip(values,values[1:])]
                if min(gaps)>g.EPS and max(gaps)-min(gaps)<1e-7:aligned.update(ids_in_line)
    alignment=len(aligned)/len(members) if members else 0.
    return hpwl,crosses,alignment


def _shift(p,dx,dy):
    out=dict(p,x_mm=p['x_mm']+dx,y_mm=p['y_mm']+dy)
    if p.get('layer_geometries'):out['layer_geometries']=[dict(r,x_mm=r['x_mm']+dx,y_mm=r['y_mm']+dy) for r in p['layer_geometries']]
    return out


class SystematicClusterPlacer:
    def propose(self,model,intent,options):
        c=model.context;intent=prepare_intent(c,intent);roles=recognize_roles(c,intent)
        comps={x['id']:x for x in c['components']};scope=set(c['scope_component_ids'])
        if not 1<=len(scope)<=32:raise ValueError('Systematic interactive scope is 1..32 movable components')
        pitch=options.get('search_grid_mm',intent['grid_mm']);radius=options.get('restart_radius_mm',4.064)
        if type(pitch) not in (int,float) or pitch<=0 or type(radius) not in (int,float) or not 0<radius/pitch<=96:raise ValueError('Invalid bounded systematic search grid')
        origin=intent['grid_origin_mm'];clearance=c['fabrication_profile']['minimum_pad_clearance_mm']
        copper=CopperObstacleIndex(c.get('copper_snapshot',{}))
        if copper.unknowns:raise ValueError('Incomplete physical obstacle extraction')
        physical=c.get('copper_snapshot',{});channels=escape_channels(c,intent);vias=ViaAccessIndex(physical)
        poses={cid:core._pose(x) for cid,x in comps.items()};base=core.transformed_pads(c,poses);current=dict(base)
        fixed=g.SpatialIndex();fixed_pads=defaultdict(g.SpatialIndex);fixed_rows=[]
        for cid,comp in comps.items():
            if cid not in scope:fixed.insert(cid,core._bbox(core._component_ring(comp,poses[cid])))
        for row in p1.expand_layers({pid:p for pid,p in base.items() if p.get('component_id') not in scope}):
            fixed_pads[row['layer']].insert(len(fixed_rows),p1.analytic_bounds(row));fixed_rows.append(row)
        baseline_critical={}
        bindings=intent.get('decoupling_connections',[])
        def critical_values(pads,changed_cid=None):
            out={}
            for b in bindings:
                ids=[b[k] for k in ('ic_power_pad','cap_power_pad','ic_return_pad','cap_return_pad')]
                if changed_cid is not None and not any(pads[pid]['component_id']==changed_cid for pid in ids):continue
                legs=[p1.transformed_pad_aabb_manhattan_mm(pads[a],pads[z]) for a,z in (ids[:2],ids[2:])]
                out['decap:'+b['id']]=sum(legs)
                for i,v in enumerate(legs):out['decap:'+b['id']+':'+str(i)]=v
                via=sum(v for pid in (ids[1],ids[3]) if (v:=vias.nearest(pads[pid])[0]) is not None)
                out['loop:'+b['id']]=sum(legs)+via
            for p in intent.get('critical_paths',[]):
                if changed_cid is not None and not any(pads[pid]['component_id']==changed_cid for pid in p['pad_ids']):continue
                for i,(a,b) in enumerate(zip(p['pad_ids'],p['pad_ids'][1:])):out[p['id']+':'+str(i)]=p1.transformed_pad_aabb_manhattan_mm(pads[a],pads[b])
            return out
        baseline_critical=critical_values(base);allow=p1.spike_policy(c).get('critical_allowance_mm',0)
        def critical_ok(pads,changed_cid=None):
            values=critical_values(pads,changed_cid)
            if not all(v is not None and v<=baseline_critical[k]+allow for k,v in values.items()):return False
            for cid,role in roles.items():
                if changed_cid is not None and changed_cid!=cid:continue
                if intent.get('placement_roles',{}).get(cid,{}).get('preserve_target_distance'):
                    if any(p1.transformed_pad_aabb_manhattan_mm(pads[a],pads[b])>p1.transformed_pad_aabb_manhattan_mm(base[a],base[b])+allow for a,b in role['pad_pairs']):return False
            return True
        def objective(pads,pp):
            values=critical_values(pads);hpwl,cross,align=scoped_metrics(c,intent,pads,pp)
            critical_sum=sum(v for k,v in values.items() if not k.startswith('decap:'))
            return (critical_sum,hpwl+intent['crossing_penalty_mm']*(cross or 0),cross or 0,hpwl,1-(align or 0))
        placed={};placed_rows={};searched=0;alternatives=[];rejections=defaultdict(lambda:defaultdict(int))
        priority={'decoupling':0,'crystal':0,'oscillator':0,'series_resistor':1,'feedback':1,'inductor':1,'diode':1,'pull_resistor':2,'passive':3}
        def electrical_slack(cid):
            values=critical_values(base,cid)
            return sum(v for key,v in values.items() if not key.startswith('decap:')) if values else float('inf')
        order=sorted(scope,key=lambda cid:(priority[roles[cid]['role']],electrical_slack(cid),cid))
        def legal(cid,pose,pm):
            def reject(why):rejections[cid][why]+=1;return False
            comp=comps[cid];ring=core._component_ring(comp,pose)
            if not core._contained(ring,c['outline']):return reject('outline')
            box=core._bbox(ring)
            for other in fixed.query(box):
                if other==cid or other in placed:continue
                if comps[other]['side']==pose['side'] and core._interior_overlap(ring,core._component_ring(comps[other],poses[other])):return reject('fixed_body')
            for other,otherpose in placed.items():
                if other!=cid and otherpose['side']==pose['side'] and core._interior_overlap(ring,core._component_ring(comps[other],otherpose)):return reject('placed_body')
            for row in p1.expand_layers(pm):
                if not copper.clear(row,clearance):return reject('copper')
                for i in fixed_pads[row['layer']].query(tuple(v+d for v,d in zip(p1.analytic_bounds(row),(-clearance,-clearance,clearance,clearance)))):
                    other=fixed_rows[i]
                    if other.get('component_id')==cid or other.get('component_id') in placed:continue
                    if row.get('net_name') and row['net_name']==other.get('net_name'):continue
                    if p1.analytic_distance(row,other)+g.EPS<clearance:return reject('fixed_pad')
                for other,rows in placed_rows.items():
                    if other==cid:continue
                    for p in rows:
                        if p['layer']==row['layer'] and (not row.get('net_name') or row['net_name']!=p.get('net_name')) and p1.analytic_distance(row,p)+g.EPS<clearance:return reject('placed_pad')
                for ch in channels:
                    role=roles[cid]
                    if role['status']=='declared_pin_binding' and role['role'] in {'series_resistor','inductor','diode'} and ch['pad_id'] in role['target_pad_ids']:continue
                    if row['layer']==ch['layer'] and row.get('net_name')!=ch['net_name'] and g.bbox_overlap(p1.analytic_bounds(row),p1.analytic_bounds(ch['shape'])) and p1.analytic_distance(row,ch['shape'])<g.EPS:return reject('escape')
            return True
        for stage in range(2):
            for cid in order:
                comp=comps[cid];pairs=roles[cid]['pad_pairs'];target_points=[(base[p]['x_mm'],base[p]['y_mm']) for p in roles[cid]['target_pad_ids']]
                cx=sum(p[0] for p in target_points)/len(target_points);cy=sum(p[1] for p in target_points)/len(target_points)
                step_pitch=2*pitch if stage==0 or cid not in placed else pitch
                search_radius=radius if stage==0 or cid not in placed else min(radius,2.032)
                if stage and cid in placed:cx,cy=poses[cid]['x_mm'],poses[cid]['y_mm']
                cx=origin[0]+round((cx-origin[0])/step_pitch)*step_pitch;cy=origin[1]+round((cy-origin[1])/step_pitch)*step_pitch
                small=dict(c,components=[comp],pads=[p for p in c['pads'] if p['component_id']==cid]);shortlist=[]
                for rot in (comp.get('allowed_rotations_deg',[0,90,180,270]) if options.get('optimize_rotation',True) else [comp['rotation_deg']]):
                    rotated=core.transformed_pads(small,{cid:dict(core._pose(comp),rotation_deg=rot)})
                    steps=int(search_radius/step_pitch)
                    xs={cx+i*step_pitch for i in range(-steps,steps+1)};ys={cy+i*step_pitch for i in range(-steps,steps+1)}
                    local=core._bbox(core._component_ring(comp,dict(core._pose(comp),rotation_deg=rot)))
                    local=(local[0]-comp['x_mm'],local[1]-comp['y_mm'],local[2]-comp['x_mm'],local[3]-comp['y_mm'])
                    roi=(cx-search_radius,cy-search_radius,cx+search_radius,cy+search_radius)
                    boxes=[fixed.boxes[other] for other in fixed.query(roi) if other!=cid and other not in placed and comps[other]['side']==comp['side']]
                    boxes += [core._bbox(core._component_ring(comps[other],p)) for other,p in placed.items() if other!=cid and p['side']==comp['side']]
                    for box in boxes:
                        for value in (origin[0]+math.ceil((box[2]-local[0]-origin[0])/pitch)*pitch,origin[0]+math.floor((box[0]-local[2]-origin[0])/pitch)*pitch):
                            if roi[0]<=value<=roi[2]:xs.add(value)
                        for value in (origin[1]+math.ceil((box[3]-local[1]-origin[1])/pitch)*pitch,origin[1]+math.floor((box[1]-local[3]-origin[1])/pitch)*pitch):
                            if roi[1]<=value<=roi[3]:ys.add(value)
                    points={(x,y) for x in xs for y in ys}
                    # Retain a bounded legalization neighborhood of the current
                    # pose. A distant anchor may be surrounded by fixed copper
                    # and packages; pulling every passive into that region is
                    # not a valid substitute for finding an actual free slot.
                    if not (roi[0]<=comp['x_mm']<=roi[2] and roi[1]<=comp['y_mm']<=roi[3]):
                        bx=origin[0]+round((comp['x_mm']-origin[0])/pitch)*pitch
                        by=origin[1]+round((comp['y_mm']-origin[1])/pitch)*pitch
                        extent=min(radius,2.032);n=int(extent/(2*pitch))
                        lx={bx+i*2*pitch for i in range(-n,n+1)};ly={by+i*2*pitch for i in range(-n,n+1)}
                        local_roi=(bx-extent,by-extent,bx+extent,by+extent)
                        for other in fixed.query(local_roi):
                            if other==cid or other in placed or comps[other]['side']!=comp['side']:continue
                            box=fixed.boxes[other]
                            for value in (origin[0]+math.ceil((box[2]-local[0]-origin[0])/pitch)*pitch,origin[0]+math.floor((box[0]-local[2]-origin[0])/pitch)*pitch):
                                if local_roi[0]<=value<=local_roi[2]:lx.add(value)
                            for value in (origin[1]+math.ceil((box[3]-local[1]-origin[1])/pitch)*pitch,origin[1]+math.floor((box[1]-local[3]-origin[1])/pitch)*pitch):
                                if local_roi[1]<=value<=local_roi[3]:ly.add(value)
                        points.update((x,y) for x in lx for y in ly)
                    for x,y in sorted(points,key=lambda q:abs(q[0]-cx)+abs(q[1]-cy)):
                            searched+=1;pose=dict(x_mm=x,y_mm=y,rotation_deg=rot,side=comp['side']);dx,dy=x-comp['x_mm'],y-comp['y_mm']
                            pm={pid:_shift(p,dx,dy) for pid,p in rotated.items()}
                            guide=sum(abs(pm[a]['x_mm']-base[b]['x_mm'])+abs(pm[a]['y_mm']-base[b]['y_mm']) for a,b in pairs)
                            if len(shortlist)>=16 and guide>shortlist[-1][0]+g.EPS:continue
                            projected=dict(current);projected.update(pm)
                            if not critical_ok(projected,cid):rejections[cid]['critical']+=1;continue
                            if not legal(cid,pose,pm):continue
                            shortlist.append((guide,pose,pm));shortlist.sort(key=lambda r:r[0]);shortlist=shortlist[:16]
                if not shortlist:continue
                best=min(shortlist,key=lambda row:objective(dict(current,**row[2]),dict(poses,**{cid:row[1]})))
                placed[cid]=best[1];poses[cid]=best[1];current.update(best[2]);placed_rows[cid]=p1.expand_layers(best[2])
            if placed:
                alternatives.append(dict(id='systematic-'+('construct' if stage==0 else 'untangle'),moves=[dict(component_id=cid,**p) for cid,p in sorted(placed.items())]))
            if stage==0:
                for cid in scope-set(placed):
                    fixed.insert(cid,core._bbox(core._component_ring(comps[cid],poses[cid])))
                    for row in p1.expand_layers({pid:p for pid,p in base.items() if p['component_id']==cid}):
                        fixed_pads[row['layer']].insert(len(fixed_rows),p1.analytic_bounds(row));fixed_rows.append(row)
        # Stage four: snap same-size banks to candidate rows/columns only when
        # earlier electrical objectives and hard reservations do not regress.
        for _ in range(len(scope)):
            invalid=[cid for cid in placed if not legal(cid,poses[cid],{pid:p for pid,p in current.items() if p.get('component_id')==cid})]
            if not invalid:break
            for cid in invalid:
                placed.pop(cid,None);placed_rows.pop(cid,None);poses[cid]=core._pose(comps[cid])
                current.update({pid:p for pid,p in base.items() if p.get('component_id')==cid})
                fixed.insert(cid,core._bbox(core._component_ring(comps[cid],poses[cid])))
                for row in p1.expand_layers({pid:p for pid,p in base.items() if p.get('component_id')==cid}):
                    fixed_pads[row['layer']].insert(len(fixed_rows),p1.analytic_bounds(row));fixed_rows.append(row)
        if placed:alternatives.append(dict(id='systematic-legalized',moves=[dict(component_id=cid,**p) for cid,p in sorted(placed.items())]))
        for family in intent['passive_families']:
            ids=[cid for cid in family['component_ids'] if cid in placed]
            if len(ids)<2:continue
            for axis in ('x_mm','y_mm'):
                other='y_mm' if axis=='x_mm' else 'x_mm';line=round(sum(poses[cid][axis] for cid in ids)/len(ids)/pitch)*pitch
                ordered=sorted(ids,key=lambda cid:poses[cid][other])
                for rot in (0,90,180,270):
                    if not options.get('optimize_rotation',True) and any(comps[cid]['rotation_deg']!=rot for cid in ids):continue
                    extent=max(core._bbox(core._component_ring(comps[cid],dict(poses[cid],rotation_deg=rot)))[2 if other=='x_mm' else 3]-core._bbox(core._component_ring(comps[cid],dict(poses[cid],rotation_deg=rot)))[0 if other=='x_mm' else 1] for cid in ids)
                    step=math.ceil((extent+clearance)/pitch)*pitch
                    start=round((sum(poses[cid][other] for cid in ids)/len(ids)-(len(ids)-1)*step/2)/pitch)*pitch
                    trial={cid:dict(poses[cid],rotation_deg=rot,**{axis:line,other:start+i*step}) for i,cid in enumerate(ordered)}
                    pm=core.transformed_pads(dict(c,components=[comps[cid] for cid in ids],pads=[p for p in c['pads'] if p['component_id'] in ids]),trial)
                    saved_placed=dict(placed);saved_rows=dict(placed_rows);placed.update(trial)
                    placed_rows.update({cid:p1.expand_layers({pid:p for pid,p in pm.items() if p['component_id']==cid}) for cid in ids})
                    projected=dict(current,**pm);pp=dict(poses,**trial)
                    if critical_ok(projected) and all(legal(cid,trial[cid],{pid:p for pid,p in pm.items() if p['component_id']==cid}) for cid in ids) and objective(projected,pp)<=objective(current,poses):
                        poses.update(trial);current.update(pm)
                    else:placed=saved_placed;placed_rows=saved_rows
        if placed:alternatives.append(dict(id='systematic-bank',moves=[dict(component_id=cid,**p) for cid,p in sorted(placed.items())]))
        return dict(candidates=alternatives,intent=intent,roles=roles,searched_poses=searched,
                    stages=['full_subcircuit_roles','pin_facing_construct','ratsnest_and_escape_refinement','conditional_passive_banking'],
                    moved_component_count=len(placed),escape_channel_count=len(channels),rejections={cid:dict(v) for cid,v in rejections.items()},native_edits=False)
