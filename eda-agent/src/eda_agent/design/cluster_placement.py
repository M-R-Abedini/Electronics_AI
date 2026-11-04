# SPDX-License-Identifier: MIT
"""Lossless capture adapter and two-stage proposals using the existing placers.

Motifs inferred from topology are hypotheses, never sourced electrical limits.
The placement evidence evaluator remains the sole authority for native edits.
"""
from __future__ import annotations

import copy
import json
import math
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class UnifiedBoardModel:
    """An immutable view of one capture record, not another board database."""
    _capture_json: str

    @classmethod
    def from_capture(cls, record):
        from .placement_evidence import validate_board_context
        if not isinstance(record,dict) or not isinstance(record.get('context'),dict):
            raise ValueError('UnifiedBoardModel requires an existing captured context')
        validate_board_context(record['context'])  # Validate, retaining the original representation.
        return cls(json.dumps(record,sort_keys=True,ensure_ascii=True,allow_nan=False))

    def to_capture(self):
        return json.loads(self._capture_json)

    @property
    def context(self):
        from .placement_evidence import validate_board_context
        return validate_board_context(self.to_capture()['context'])

    @property
    def revision(self):
        from .placement_evidence import digest
        record=self.to_capture()
        return dict(board_revision_digest=record.get('capture_digest'),
                    capture_digest=record.get('capture_digest'), model_digest=digest(record),
                    source_snapshot=record['context'].get('snapshot_id'))


class MotifGraphExtractor:
    """Reuse existing structural crystal/switch recognizers; bind real pads.

    Explicit sourced motif bindings can identify TVS/entry, Kelvin shunts and
    commutation paths that a netlist alone cannot distinguish reliably.
    Unverified structures are labelled hypotheses and cannot pass a gate.
    """
    TYPES={'decoupling','tvs','crystal','switching_hot_loop','kelvin_shunt'}

    def extract(self,model,intent=None):
        from .motifs import _kind_from_refdes
        from .pcb_placement import _infer_switch_node_groups,_infer_crystal_groups,_pair_decaps_to_power_pins
        from .placement_evidence import _qualified
        from eda_agent.placement.autoplace import PlaceComp,PlacePin,PlaceNet
        if not isinstance(model,UnifiedBoardModel):
            raise ValueError('MotifGraphExtractor input must be UnifiedBoardModel')
        context=model.context;intent=copy.deepcopy(intent or {})
        comps={c['id']:c for c in context['components']}
        refs={c['reference']:cid for cid,c in comps.items()}
        pads=defaultdict(list);nets=defaultdict(set)
        for p in context['pads']:
            pads[p['component_id']].append(p)
            if p.get('net_name'):nets[p['net_name']].add(p['component_id'])
        proxies=[PlaceComp(c['reference'],0,0,c['x_mm'],c['y_mm'],
                   pins=tuple(PlacePin(0,0,p.get('net_name','')) for p in pads[cid])) for cid,c in comps.items()]
        proxy_nets=[PlaceNet(tuple(comps[cid]['reference'] for cid in sorted(ids)),name=net) for net,ids in sorted(nets.items())]
        clusters=[];diagnostics=[]
        for kind,detector in [('crystal',_infer_crystal_groups),('switching_hot_loop',_infer_switch_node_groups)]:
            groups=defaultdict(list)
            for ref,group in detector(proxies,proxy_nets).items():groups[group].append(refs[ref])
            for group,ids in sorted(groups.items()):
                clusters.append(dict(id=kind+':'+str(group),type=kind,component_ids=sorted(ids),
                    status='structural_hypothesis',qualified=False,
                    source={'kind':'measured_geometry','id':context.get('snapshot_id','offline')},
                    method='existing_pcb_placement.'+detector.__name__))
        by_ic=defaultdict(set)
        for cap,binding in _pair_decaps_to_power_pins(proxies,proxy_nets).items():
            # Existing recognizer may offer multiple IC power pins. Keep each
            # structural association explicit instead of inventing pin roles.
            bindings=binding if isinstance(binding,list) else [binding]
            for row in bindings:
                if row and row[0] in refs:
                    by_ic[refs[row[0]]].update((refs[cap],refs[row[0]]))
        for ic,ids in sorted(by_ic.items()):
            clusters.append(dict(id='decoupling-hypothesis:'+ic,type='decoupling',component_ids=sorted(ids),
                qualified=False,status='structural_hypothesis',
                source={'kind':'measured_geometry','id':context.get('snapshot_id','offline')},
                method='existing_pcb_placement._pair_decaps_to_power_pins',
                limitation='inferred rail roles; explicit VCC/GND pin mapping still required'))
        # Both VCC and GND pad mappings are required. Declared rail roles avoid
        # turning every grounded signal capacitor into an accepted decoupler.
        for binding in intent.get('decoupling_connections',[]):
            keys=('ic_power_pad','cap_power_pad','ic_return_pad','cap_return_pad')
            indexed={p['id']:p for p in context['pads']}
            if not all(binding.get(k) in indexed for k in keys):
                diagnostics.append(dict(type='decoupling',status='unknown',reason='Incomplete VCC/GND pad mapping'));continue
            rows=[indexed[binding[k]] for k in keys]
            valid=(binding.get('power_net') and binding.get('return_net')
                and binding['power_net']!=binding['return_net']
                and all(p.get('net_name')==n for p,n in zip(rows,[binding['power_net']]*2+[binding['return_net']]*2)))
            qualified=bool(valid and _qualified(binding.get('source')))
            clusters.append(dict(id='decoupling:'+binding['id'],type='decoupling',
                component_ids=sorted({p['component_id'] for p in rows}),pad_ids=[p['id'] for p in rows],
                status='bound_topology' if qualified else 'unknown',qualified=qualified,
                source=copy.deepcopy(binding.get('source')),method='captured_two_branch_pad_net_bindings'))
        for binding in intent.get('motif_bindings',[]):
            kind=binding.get('type');ids=binding.get('component_ids',[])
            if kind not in self.TYPES or not ids or not set(ids)<=comps.keys() or not binding.get('id'):
                raise ValueError('Invalid physical motif binding')
            pids=binding.get('pad_ids',[]);indexed={p['id']:p for p in context['pads']}
            if not pids or not set(pids)<=indexed.keys() or any(indexed[p]['component_id'] not in ids for p in pids):
                raise ValueError('Motif binding needs captured pad identities of its components')
            qualified=_qualified(binding.get('source')) and bool(binding.get('pin_roles_verified'))
            clusters.append(dict(id=kind+':'+binding['id'],type=kind,component_ids=sorted(set(ids)),pad_ids=pids,
                qualified=qualified,status='bound_topology' if qualified else 'structural_hypothesis',
                source=copy.deepcopy(binding.get('source')),method='declared_pin_role_binding'))
        for kind in self.TYPES:
            if not any(c['type']==kind for c in clusters):
                diagnostics.append(dict(type=kind,status='unknown',reason='No supported topology/qualified pin-role binding'))
        # Deterministic overlap arbitration: qualified explicit bindings first.
        claimed=set();selected=[]
        priority={'switching_hot_loop':0,'kelvin_shunt':0,'tvs':1,'crystal':1,'decoupling':2}
        for c in sorted(clusters,key=lambda c:(not c['qualified'],priority[c['type']],-len(c['component_ids']),c['id'])):
            if claimed&set(c['component_ids']):
                diagnostics.append(dict(id=c['id'],status='overlap_not_selected'));continue
            selected.append(c);claimed.update(c['component_ids'])
        return dict(clusters=selected,diagnostics=diagnostics,revision=model.revision,
                    component_kinds={cid:_kind_from_refdes(c['reference']) for cid,c in comps.items()},
                    qualification='topology/proposal analysis; not routed-loop or datasheet certification')


def propose_proximity_budgets(model,graph):
    """Board-scoped diagnostics from actual package envelopes; never a hard gate."""
    from .placement_evidence import _bbox
    c=model.context;by_id={r['id']:r for r in c['components']};out=[]
    for kind in sorted(MotifGraphExtractor.TYPES):
        clusters=[r for r in graph['clusters'] if r['type']==kind]
        extents=[]
        for group in clusters:
            for cid in group['component_ids']:
                ring=by_id[cid].get('courtyard_local')
                if ring:
                    x0,y0,x1,y1=_bbox(ring);extents.append(math.hypot(x1-x0,y1-y0))
        out.append(dict(type=kind,value=max(extents) if extents else None,status='proposed' if extents else 'unknown',
            unit='mm',scope=c.get('board_path'),source={'snapshot':c.get('snapshot_id'),
                'method':'maximum captured package-envelope diagonal; diagnostic search radius, not electrical limit',
                'component_ids':sorted({i for g in clusters for i in g['component_ids']}),
                'layers':copy.deepcopy(c.get('layers',[]))},approved=False,used_as_hard_gate=False))
    return out


class HierarchicalClusterPlacer:
    """Bounded local refinement, then rigid meta-cluster placement.

    Reuses the existing fine and coarse placers. No fixed project templates,
    board resizing, side flips or new native write path are introduced.
    """
    def propose(self,model,intent=None,options=None):
        from .placement_evidence import (generate_placement_candidates,transformed_pads,
            _pose,_component_ring,_bbox,transform_point,inverse_transform_point)
        from eda_agent.placement.autoplace import PlaceComp,PlaceNet,PlacePin,PlaceOptions,BoardRegion,plan_placement
        from eda_agent.units import MM_PER_MIL
        if not isinstance(model,UnifiedBoardModel):raise ValueError('Placer requires UnifiedBoardModel')
        options=dict(options or {});intent=copy.deepcopy(intent or {});context=model.context
        count=options.get('candidate_count',3);iterations=options.get('iterations',60);seed=options.get('seed',0)
        if any(isinstance(v,bool) or not isinstance(v,int) for v in (count,iterations,seed)) or not 0<=count<=16 or not 0<=iterations<=2000 or not 0<=seed<2**32-16:
            raise ValueError('Invalid hierarchical computational budget')
        pitch=options.get('search_grid_mm',.127)
        if isinstance(pitch,bool) or not isinstance(pitch,(int,float)) or not math.isfinite(pitch) or pitch<=0:
            raise ValueError('Invalid computational search pitch')
        graph=MotifGraphExtractor().extract(model,intent)
        components={c['id']:c for c in context['components']};scope=set(context['scope_component_ids'])
        poses={cid:_pose(c) for cid,c in components.items()};candidates=[]
        # Stage 1: existing pin-aware solver within each bounded cluster;
        # unrelated parts remain fixed obstacles in that solver's context.
        relevant=[g for g in graph['clusters'] if scope&set(g['component_ids'])]
        local_moves={}
        for group in relevant[:64] if count else []:
            local=copy.deepcopy(context);local['scope_component_ids']=sorted(scope&set(group['component_ids']))
            # Local proposal calculation needs nearby obstacles, not hundreds
            # of remote fixed packages fighting each other in every shove.
            # The immutable full model remains the final gate's input.
            rings={c['id']:_component_ring(c,_pose(c)) for c in local['components']}
            group_points=[p for cid in group['component_ids'] for p in (rings[cid] or [])]
            if group_points and all(r is not None for r in rings.values()):
                x0,y0,x1,y1=_bbox(group_points);radius=max(x1-x0,y1-y0,pitch)
                roi=(x0-radius,y0-radius,x1+radius,y1+radius)
                keep=set(group['component_ids'])
                for cid,ring in rings.items():
                    a,b,c,d=_bbox(ring)
                    if not (c<roi[0] or d<roi[1] or a>roi[2] or b>roi[3]):keep.add(cid)
                local['components']=[c for c in local['components'] if c['id'] in keep]
                local['pads']=[p for p in local['pads'] if p['component_id'] in keep]
            for c in local['components']:
                c.setdefault('allowed_rotations_deg',[0,90,180,270])
            proposal_intent=copy.deepcopy(intent)
            local_ids={c['id'] for c in local['components']};local_pads={p['id'] for p in local['pads']}
            proposal_intent['blocks']=[dict(b,component_ids=sorted(set(b.get('component_ids',[]))&local_ids))
                for b in intent.get('blocks',[]) if set(b.get('component_ids',[]))&local_ids]
            proposal_intent['critical_paths']=[p for p in intent.get('critical_paths',[]) if set(p.get('pad_ids',[]))<=local_pads]
            proposal_intent['decoupling_connections']=[b for b in intent.get('decoupling_connections',[])
                if all(b.get(k) in local_pads for k in ('ic_power_pad','cap_power_pad','ic_return_pad','cap_return_pad'))]
            output=generate_placement_candidates(local,proposal_intent,dict(mode='refine',candidate_count=1,
                iterations=iterations,seed=seed,search_grid_mm=pitch))
            if output:
                for move in output[0]['moves']:
                    local_moves[move['component_id']]=move
                    poses[move['component_id']].update({k:move[k] for k in ('x_mm','y_mm','rotation_deg','side')})
        if local_moves and count>1:
            candidates.append(dict(candidate_id='cluster-local-all:'+str(seed),moves=list(local_moves.values()),
                stage='intra_cluster',seed=seed,proxy_only=True))
        # Stage 2: collapse the proposed relative poses into rigid meta nodes.
        groups=[g['component_ids'] for g in graph['clusters']]
        claimed={cid for ids in groups for cid in ids}
        groups += [[cid] for cid in sorted(components) if cid not in claimed]
        pads=transformed_pads(context,poses);scale=1/MM_PER_MIL
        metas=[];centers={};members={};pad_refs=defaultdict(set)
        for n,ids in enumerate(groups):
            mid='meta:'+str(n);members[mid]=ids
            rings=[_component_ring(components[cid],poses[cid]) for cid in ids]
            if any(r is None for r in rings):continue
            x0,y0,x1,y1=_bbox([p for ring in rings for p in ring]);center=((x0+x1)/2,(y0+y1)/2);centers[mid]=center
            sides={poses[cid]['side'] for cid in ids}
            fixed=len(sides)!=1 or any(components[cid].get('locked') or components[cid].get('fixed') or cid not in scope for cid in ids)
            pin_rows=[p for p in pads.values() if p.get('component_id') in ids and p.get('x_mm') is not None]
            pins=tuple(PlacePin((p['x_mm']-center[0])*scale,(p['y_mm']-center[1])*scale,p.get('net_name','')) for p in pin_rows)
            metas.append(PlaceComp(mid,(x1-x0)*scale,(y1-y0)*scale,center[0]*scale,center[1]*scale,
                layer='Bottom' if sides=={'bottom'} else 'Top',fixed=fixed,pins=pins,rotatable=False))
            for p in pin_rows:
                if p.get('net_name'):pad_refs[p['net_name']].add(mid)
        if count and len(metas)==len(groups):
            box=_bbox(context['outline']['outline']);region=BoardRegion(*(v*scale for v in box))
            excluded=set(intent.get('reference_nets',[]))|set(intent.get('power_nets',[]))|{'GND'}
            nets=[PlaceNet(tuple(sorted(ids)),name=name,weight=0 if name in excluded else 1) for name,ids in sorted(pad_refs.items())]
            coarse=plan_placement(metas,nets,region,PlaceOptions(iterations=iterations,grid_mils=pitch*scale,
                clearance_mils=0,optimize_rotation=False))
            moves=[]
            for meta in metas:
                cx,cy=centers[meta.ref];nx,ny=coarse.positions[meta.ref]
                dx,dy=(0,0) if meta.fixed else (nx/scale-cx,ny/scale-cy)
                for cid in members[meta.ref]:
                    if components[cid].get('locked') or components[cid].get('fixed') or cid not in scope:continue
                    p=poses[cid];moves.append(dict(component_id=cid,designator=components[cid]['reference'],
                        **dict(p,x_mm=p['x_mm']+dx,y_mm=p['y_mm']+dy)))
            candidates.append(dict(candidate_id='cluster-coarse:'+str(seed),moves=moves,seed=seed,
                stage='inter_cluster_rigid_translation',proxy_only=True,solver='existing_autoplace_meta_nodes'))
        if count>len(candidates):
            # Existing constructive solver is an alternative for a globally
            # infeasible baseline; grouping remains a proposal bias, not a gate.
            constructive=copy.deepcopy(intent)
            constructive['physical_clusters']=copy.deepcopy(graph['clusters'])
            fallback=generate_placement_candidates(context,constructive,dict(mode='construct',candidate_count=1,
                seed=seed,iterations=iterations,search_grid_mm=pitch))
            for candidate in fallback:
                candidate['candidate_id']='cluster-construct:'+str(seed)
                candidate['stage']='existing_constructive_grouped_alternative';candidates.append(candidate)
        return dict(candidates=candidates[:count],graph=graph,budgets=propose_proximity_budgets(model,graph),
            revision=model.revision,stages=['intra_cluster_existing_solver','inter_cluster_rigid_translation'],
            local_cluster_budget=64,local_clusters_processed=min(len(relevant),64) if count else 0,
            local_clusters_unprocessed=max(0,len(relevant)-64) if count else len(relevant),
            local_obstacles='intersecting envelope bounds in computational neighborhood; full-model final audit',
            native_edits=False,grid_qualification='computational search pitch; native on-grid status unproven',
            via_transition_cost={'kind':'soft_proxy','return_path_verified':False})

class ScopedCopperClusterRefiner:
    """Bounded joint passive proposals; all final moves use the existing judge.

    Coordinate descent uses real pad roles and existing geometry, not a fixed
    project template. Other parts stay fixed. Every proposal is still audited
    against the complete immutable capture, including routed attachments.
    """
    def propose(self,model,intent,options):
        from .placement_evidence import _pose,_component_ring,_bbox,transformed_pads
        from . import placement_p1 as p1,copper_geometry as g
        from .placement_copper import CopperObstacleIndex,nearest_via_access
        c=model.context;components={r['id']:r for r in c['components']};scope=set(c['scope_component_ids'])
        pitch=options.get('search_grid_mm',.127);radius=options.get('restart_radius_mm',3.048)
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (pitch,radius)) or radius/pitch>80:
            raise ValueError('Scoped search requires positive bounded radius/pitch <= 80')
        if len(scope)>16:raise ValueError('Scoped copper refinement budget is 16 movable components')
        bindings=intent.get('decoupling_connections',[])
        if not bindings:raise ValueError('Scoped copper refinement needs explicit sourced decoupling pad bindings')
        pads={p['id']:p for p in c['pads']};physical=c.get('copper_snapshot',{});copper=CopperObstacleIndex(physical)
        if copper.unknowns:raise ValueError('Copper obstacle coverage is incomplete: '+'; '.join(copper.unknowns))
        clearance=c['fabrication_profile']['minimum_pad_clearance_mm'];allow=p1.spike_policy(c).get('critical_allowance_mm',0)
        target=p1.spike_policy(c).get('target_proximity_mm');poses={cid:_pose(x) for cid,x in components.items()}
        rings={cid:_component_ring(x,poses[cid]) for cid,x in components.items()}
        if any(r is None for r in rings.values()):raise ValueError('Scoped refinement needs captured component envelopes')
        moves={};searched=0
        for binding in bindings:
            cap_id=pads[binding['cap_power_pad']]['component_id']
            if cap_id not in scope:continue
            cap=components[cap_id]
            if cap.get('fixed') or cap.get('locked'):continue
            ic_id=pads[binding['ic_power_pad']]['component_id'];ic=components[ic_id]
            selected=[p for p in c['pads'] if p['component_id'] in {cap_id,ic_id}]
            small=dict(c,components=[cap,ic],pads=selected);initial={i:_pose(components[i]) for i in (cap_id,ic_id)}
            before=transformed_pads(small,initial)
            pairs=[(binding['ic_power_pad'],binding['cap_power_pad']),(binding['ic_return_pad'],binding['cap_return_pad'])]
            limits=[p1.transformed_pad_aabb_manhattan_mm(before[a],before[b]) for a,b in pairs]
            via_limits={pid:nearest_via_access(physical,before[pid])[0] for _,pid in pairs}
            board_index=g.SpatialIndex()
            for cid,ring in rings.items():
                if cid!=cap_id and components[cid]['side']==cap['side']:board_index.insert(cid,_bbox(ring))
            best=None;steps=int(radius/pitch)
            for rot in cap.get('allowed_rotations_deg',[0,90,180,270]):
                p=dict(_pose(cap),rotation_deg=rot);initial[cap_id]=p
                transformed=transformed_pads(small,initial);envelope=_bbox(_component_ring(cap,p))
                cap_rows=p1.expand_layers({k:v for k,v in transformed.items() if v['component_id']==cap_id})
                cap_bounds={pid:p1.analytic_bounds(transformed[pid]) for _,pid in pairs}
                ic_bounds={pid:p1.analytic_bounds(transformed[pid]) for pid,_ in pairs}
                for ix in range(-steps,steps+1):
                    x=ic['x_mm']+ix*pitch;dx=x-cap['x_mm']
                    for iy in range(-steps,steps+1):
                        searched+=1;y=ic['y_mm']+iy*pitch;dy=y-cap['y_mm']
                        box=(envelope[0]+dx,envelope[1]+dy,envelope[2]+dx,envelope[3]+dy)
                        if any(min(box[2],board_index.boxes[k][2])-max(box[0],board_index.boxes[k][0])>g.EPS and
                               min(box[3],board_index.boxes[k][3])-max(box[1],board_index.boxes[k][1])>g.EPS for k in board_index.query(box)):continue
                        legs=[]
                        for a,b in pairs:
                            aa,bb=ic_bounds[a],cap_bounds[b];bb=(bb[0]+dx,bb[1]+dy,bb[2]+dx,bb[3]+dy)
                            legs.append(max(0,aa[0]-bb[2],bb[0]-aa[2])+max(0,aa[1]-bb[3],bb[1]-aa[3]))
                        if any(v is None or v>limit+allow or target is not None and v>target for v,limit in zip(legs,limits)):continue
                        positioned=[dict(row,x_mm=row['x_mm']+dx,y_mm=row['y_mm']+dy) for row in cap_rows]
                        if not all(copper.clear(row,clearance) for row in positioned):continue
                        via_sum=0.
                        for _,pid in pairs:
                            pad=dict(transformed[pid],x_mm=transformed[pid]['x_mm']+dx,y_mm=transformed[pid]['y_mm']+dy)
                            d,_=nearest_via_access(physical,pad);old=via_limits[pid]
                            if d is not None:via_sum+=d
                        if sum(legs)+via_sum>sum(limits)+sum(v for v in via_limits.values() if v is not None)+allow:continue
                        score=(sum(legs)+via_sum,sum(legs),math.hypot(dx,dy))
                        if best is None or score<best[0]:best=(score,dict(component_id=cap_id,x_mm=x,y_mm=y,rotation_deg=rot,side=cap['side']))
            if best:
                moves[cap_id]=best[1];poses[cap_id]=best[1];rings[cap_id]=_component_ring(cap,best[1])
        return dict(candidates=[dict(id='scoped-copper-joint',moves=list(moves.values()))] if moves else [],
                    searched_poses=searched,movable_component_count=len(scope),proposed_component_count=len(moves),
                    native_edits=False,method='bounded pad/via-aware coordinate descent; full-board final gate')
