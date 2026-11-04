import copy
import pytest
from eda_agent.design import placement_copper as pc,placement_p1 as p1
from eda_agent.design.copper_audit import audit_board_copper,decoupling_loop_measurements
from suite_reads import resolve_polygon_bindings
from suite_si_pi import SiPiService

def snapshot():
 return dict(schema=1,units='mm',source='live',board_path='synthetic.PcbDoc',complete=True,missing=[],
  coverage={k:'complete' for k in ('tracks','arcs','vias','regions','layers','pads')},
  layers=[dict(name='Top',order=0,kind='signal'),dict(name='Bottom',order=1,kind='signal')],
  tracks=[],arcs=[],vias=[],regions=[],pads=[],components=[],nets=[],net_ties=[],keepouts=[])

def pad(x=0,y=0,net='A',layer='Top'):
 return dict(id='p',component_id='c',x_mm=x,y_mm=y,width_mm=.4,height_mm=.4,shape='rectangle',layer=layer,net_name=net)

def test_foreign_track_and_via_clearance_respects_layer_and_net():
 s=snapshot();s['tracks']=[dict(id='t',start=[-2,0],end=[2,0],width_mm=.2,layer='Top',net_name='B')]
 s['vias']=[dict(id='v',x_mm=0,y_mm=1,diameter_mm=.5,start_layer='Top',end_layer='Bottom',net_name='B')]
 i=pc.CopperObstacleIndex(s)
 assert not i.clear(pad(),.1)
 assert i.clear(pad(net='B'),.1)
 assert i.clear(pad(layer='Bottom'),.1)
 assert not i.clear(pad(y=1,layer='Bottom'),.1)
 assert i.clear(pad(x=3),.1)

def test_polygon_void_and_unpoured_definition_are_not_solid_copper():
 s=snapshot();s['regions']=[dict(id='r',poured=True,layer='Top',net_name='B',outline=[[-3,-3],[3,-3],[3,3],[-3,3]],holes=[[[-1,-1],[1,-1],[1,1],[-1,1]]])]
 i=pc.CopperObstacleIndex(s)
 assert i.clear(pad(),.1) and not i.clear(pad(x=2),.1)
 s['regions'][0]['poured']=False
 assert pc.CopperObstacleIndex(s).clear(pad(x=2),.1)

def test_arc_error_is_conservative_and_bad_geometry_stays_unknown():
 s=snapshot();s['arcs']=[dict(id='a',center=[0,0],radius_mm=2,start_angle_deg=0,end_angle_deg=90,width_mm=.2,layer='Top',net_name='B')]
 i=pc.CopperObstacleIndex(s);assert not i.clear(pad(x=2),.1)
 s['tracks']=[dict(id='bad',width_mm=0)]
 assert pc.CopperObstacleIndex(s).unknowns

def test_via_access_uses_same_net_and_actual_layer_span():
 s=snapshot();s['vias']=[dict(id='v',x_mm=1,y_mm=0,diameter_mm=.4,start_layer='Top',end_layer='Top',net_name='A')]
 assert pc.nearest_via_access(s,pad())[0]==pytest.approx(.6)
 assert pc.nearest_via_access(s,pad(layer='Bottom'))==(None,None)
 assert pc.nearest_via_access(s,pad(net='OTHER'))==(None,None)

def test_polygon_parent_bindings_are_measured_not_guessed():
 s=snapshot();s['regions']=[dict(id='r',net_name='')];s['polygon_bindings']=[dict(child_id='r',polygon_id='poly',net_name='GND')]
 assert resolve_polygon_bindings(s)['regions'][0]['net_name']=='GND' and not s['regions'][0]['net_name']
 s['regions'][0]['net_name']='VCC'
 with pytest.raises(ValueError,match='disagrees'):resolve_polygon_bindings(s)

def test_targeted_audit_schema_exists():
 assert 'audit_copper_loops' in {t['name'] for t in SiPiService.list_tools()}

def test_real_parallel_copper_finding_has_coordinates_and_threshold():
 s=snapshot();s['tracks']=[dict(id='a',start=[0,0],end=[10,0],width_mm=.2,layer='Top',net_name='A'),dict(id='b',start=[0,.4],end=[10,.4],width_mm=.2,layer='Top',net_name='B')]
 r=audit_board_copper(s,{'net_classes':{'A':['HIGH_SPEED_SINGLE'],'B':['HIGH_SPEED_SINGLE']}},policy={'minimum_coupled_length_mm':5})
 v=next(v for v in r['violations'] if v['check']=='parallel_coupling')
 assert v['measured_value']==pytest.approx(.2) and v['limit_value']==pytest.approx(.6)
 assert {'x_mm','y_mm','layer','net_name','fix_action'}<=v.keys()

def test_routed_detach_is_blocked_by_default_and_reported_in_explicit_trial():
 from eda_agent.design import placement_evidence as e
 s=snapshot();s['tracks']=[dict(id='t',start=[-.5,0],end=[.5,0],width_mm=.2,layer='Top',net_name='A')]
 c=dict(policy_version=p1.POLICY_VERSION,board_path='synthetic.PcbDoc',scope_component_ids=['c'],
        components=[dict(id='c',x_mm=0,y_mm=0,rotation_deg=0,side='top')],pads=[pad()],copper_snapshot=s,
        fabrication_profile={'minimum_pad_clearance_mm':.1})
 checks,nets=pc.audit_placement_copper(c,{'p':pad(x=2)},'synthetic')
 assert nets==['A'] and next(x for x in checks if x['id']=='copper:ripup_required')['required']
 c['assembly_profile']={'placement_spike':dict(scope=c['board_path'],placement_only=True,allow_routing_detach=True,source={'kind':'user_intent','id':'synthetic-trial'})}
 checks,nets=pc.audit_placement_copper(c,{'p':pad(x=2)},'synthetic')
 assert nets==['A'] and not next(x for x in checks if x['id']=='copper:ripup_required')['required']

def test_joint_cluster_proposal_moves_multiple_passives_and_keeps_anchor():
 from eda_agent.design import placement_evidence as e
 from eda_agent.design.cluster_placement import UnifiedBoardModel,ScopedCopperClusterRefiner
 c=e.example_board_context();c['policy_version']=p1.POLICY_VERSION
 anchor,cap=c['components'];anchor['side']='top';cap['side']='bottom'
 other=copy.deepcopy(cap);other.update(id='c',reference='C2',y_mm=28)
 c['components'].append(other);c['scope_component_ids']=['b','c'];c['coverage']['pads']='complete'
 c['pads'][0]['net_name']='VCC';c['pads'][1]['net_name']='VCC'
 c['pads'].append(dict(c['pads'][1],id='c.1',component_id='c'))
 for p in list(c['pads']):c['pads'].append(dict(p,id=p['id']+'g',net_name='GND',local_xy_mm=[0,1]))
 s=snapshot();s['layers']=[dict(name='top',kind='signal',order=0),dict(name='bottom',kind='signal',order=1)]
 c['copper_snapshot']=s
 c['assembly_profile']={'placement_spike':dict(scope=c['board_path'],placement_only=True,critical_allowance_mm=.02,source={'kind':'user_intent','id':'synthetic-cluster'})}
 bindings=[dict(id=x,ic_power_pad='a.1',ic_return_pad='a.1g',cap_power_pad=x+'.1',cap_return_pad=x+'.1g',source={'kind':'user_intent','id':'synthetic'},power_net='VCC',return_net='GND') for x in ('b','c')]
 intent={'power_nets':['VCC'],'reference_nets':['GND'],'decoupling_connections':bindings}
 out=ScopedCopperClusterRefiner().propose(UnifiedBoardModel.from_capture({'context':c}),intent,{'search_grid_mm':1,'restart_radius_mm':2})
 assert {m['component_id'] for m in out['candidates'][0]['moves']}=={'b','c'}
 assert out['searched_poses']<=200

def test_targeted_live_independent_audit_uses_strict_length_and_same_layer(tmp_path):
 import asyncio
 async def run():
  s=snapshot();s['board_path']=str(tmp_path/'scratch.PcbDoc');s['nets']=['A','B']
  s['tracks']=[dict(id='a',start=[0,0],end=[5,0],width_mm=.2,layer='Top',net_name='A'),dict(id='b',start=[0,.4],end=[5,.4],width_mm=.2,layer='Top',net_name='B')]
  service=SiPiService(tmp_path/'data');topology={'net_roles':{'A':'high_speed_single','B':'high_speed_single'}}
  r=await service.audit_copper_loops(snapshot=s,topology=topology)
  assert not [v for v in r['violations'] if v['check']=='parallel_coupling']
  for t in s['tracks']:t['end'][0]=10
  r=await service.audit_copper_loops(snapshot=s,topology=topology)
  assert any(v['check']=='parallel_coupling' for v in r['violations'])
  s['tracks'][1]['layer']='Bottom'
  r=await service.audit_copper_loops(snapshot=s,topology=topology)
  assert not [v for v in r['violations'] if v['check']=='parallel_coupling']
 asyncio.run(run())

def test_graph_via_contact_uses_actual_copper_shape_on_each_layer():
 from eda_agent.design.copper_audit import CopperGraph
 s=snapshot();p=pad(net='A',layer='MultiLayer');p.update(component='C1',pad_name='1',is_plated=True,
   layer_geometries=[pad(net='A',layer='Top'),dict(pad(net='A',layer='Bottom'),width_mm=3.)])
 s['pads']=[p];s['vias']=[dict(id='v',x_mm=1,y_mm=0,diameter_mm=.3,start_layer='Top',end_layer='Top',net_name='A')]
 g=CopperGraph(s,'A');assert g.shortest(g.pad_nodes['p'],g.via_nodes['v']) is None
 s['vias'][0].update(start_layer='Bottom',end_layer='Bottom')
 g=CopperGraph(s,'A');assert g.shortest(g.pad_nodes['p'],g.via_nodes['v']) is not None

def test_unrouted_decoupling_does_not_construct_expensive_unrelated_graph(monkeypatch):
 import eda_agent.design.copper_audit as ca
 s=snapshot();s['pads']=[dict(pad(),id='u',component='U1',pad_name='5'),dict(pad(x=2),id='c',component='C1',pad_name='2'),dict(pad(x=3,net='GND'),id='g',component='C1',pad_name='1')]
 monkeypatch.setattr(ca,'CopperGraph',lambda *a,**k:pytest.fail('Isolated terminal cannot have a routed path'))
 r=ca.decoupling_loop_measurements(s,{'motifs':[dict(kind='decoupling',intent_verified=True,component='C1',source_component='U1',source_pin='5',cap_power_pin='2',cap_ground_pin='1')]})
 assert r[0]['measured_value'] is None and r[0]['ground_to_via_mm'] is None

def test_indexed_hole_geometry_matches_full_scan_without_losing_small_gaps():
 from eda_agent.design.copper_geometry import indexed_region,segment_region_intervals,point_in_region
 r=dict(outline=[[-5,-5],[20,-5],[20,20],[-5,20]],holes=[[[x,y],[x+.2,y],[x+.2,y+.2],[x,y+.2]] for x in range(10) for y in range(10)])
 indexed=indexed_region(r)
 for a,b in [([-2,.1],[12,.1]),([.1,-2],[.1,12]),([-.1,-.1],[12,12]),([4.01,4.01],[4.1,4.1])]:
  assert segment_region_intervals(a,b,r)==segment_region_intervals(a,b,indexed)
 for p in [(0,0),(.1,.1),(1.3,1.3),(10,10)]:assert point_in_region(p,r)==point_in_region(p,indexed)
 assert '_hole_index' not in r
