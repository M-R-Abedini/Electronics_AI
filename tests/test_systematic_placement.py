import copy,math
import pytest
from eda_agent.design import placement_evidence as e,placement_p1 as p1
from eda_agent.design.systematic_placement import prepare_intent,recognize_roles,escape_checks,scoped_metrics,SystematicClusterPlacer
from eda_agent.design.cluster_placement import UnifiedBoardModel
from eda_agent.design.placement_copper import ViaAccessIndex,nearest_via_access

SRC={'kind':'user_intent','id':'synthetic-systematic-placement'}

def context():
 c=e.example_board_context();c['policy_version']=p1.POLICY_VERSION;c['coverage']['pads']='complete'
 c['components'][0].update(id='a',reference='U1',x_mm=15,y_mm=30)
 c['components'][1].update(id='b',reference='R1',x_mm=25,y_mm=32,side='bottom')
 c['pads']=[dict(id='a1',component_id='a',net_name='A',local_xy_mm=[1,0],width_mm=.4,height_mm=.4,shape='rectangle',layer='Top',rotation_deg=0),dict(id='b1',component_id='b',net_name='A',local_xy_mm=[.5,0],width_mm=.4,height_mm=.4,shape='rectangle',layer='Bottom',rotation_deg=0),dict(id='b2',component_id='b',net_name='GND',local_xy_mm=[-.5,0],width_mm=.4,height_mm=.4,shape='rectangle',layer='Bottom',rotation_deg=0)]
 c['scope_component_ids']=['b'];c['copper_snapshot']=dict(complete=True,coverage={},missing=[],tracks=[],arcs=[],vias=[],regions=[],layers=[dict(name='Top',kind='signal',order=0),dict(name='Bottom',kind='signal',order=1)])
 c['assembly_profile']={'placement_spike':dict(scope=c['board_path'],placement_only=True,source=SRC)}
 return c

def intent():return dict(anchor_component_ids=['a'],power_nets=[],reference_nets=['GND'],grid_mm=1.,grid_origin_mm=[0,0],escape_via_diameter_mm=.3,placement_roles={'b':dict(role='pull_resistor',target_pad_ids=['a1'],source=SRC)})

def test_full_role_bindings_require_captured_matching_pin_nets():
 c=context();i=intent()
 for role in ('decoupling','series_resistor','pull_resistor','feedback','crystal','oscillator','inductor','diode'):
  i['placement_roles']['b']['role']=role
  assert recognize_roles(c,i)['b']['role']==role
 i['placement_roles']['b']['target_pad_ids']=['missing']
 with pytest.raises(ValueError):recognize_roles(c,i)

def test_scoped_ratsnest_keeps_remote_endpoints_and_untangles_pin_edges():
 pads={n:dict(id=n,net_name=net,x_mm=x,y_mm=y) for n,net,x,y in [('a','A',0,0),('b','A',10,10),('c','B',0,10),('d','B',10,0),('rail','GND',999,999)]}
 i=dict(metric_net_names=['A','B'],grid_mm=1,grid_origin_mm=[0,0],passive_families=[])
 before=scoped_metrics({},i,pads,{})
 pads['b']['y_mm']=0;pads['d']['y_mm']=10
 after=scoped_metrics({},i,pads,{})
 assert before[1]==1 and after[1]==0 and after[0]<before[0]

def test_banking_requires_uniform_pitch_common_rotation_and_declared_grid():
 i=dict(metric_net_names=[],grid_mm=1,grid_origin_mm=[0,0],passive_families=[dict(component_ids=['b','c','d'])])
 poses={cid:dict(x_mm=x,y_mm=20,rotation_deg=0) for cid,x in [('b',10),('c',12),('d',14)]}
 assert scoped_metrics({},i,{},poses)[2]==1
 poses['d']['x_mm']=15
 assert scoped_metrics({},i,{},poses)[2]==0
 poses['d']['x_mm']=14;poses['d']['rotation_deg']=90
 assert scoped_metrics({},i,{},poses)[2]<1

def test_escape_reservation_rejects_foreign_moving_pad_on_same_layer_only():
 c=context();i=prepare_intent(c,intent());poses={x['id']:e._pose(x) for x in c['components']};pads=e.transformed_pads(c,poses)
 pads['b2'].update(x_mm=16.8,y_mm=30,layer='Top',layer_geometries=[])
 assert any(x['status']=='fail' for x in escape_checks(c,i,pads,'synthetic'))
 pads['b2']['layer']='Bottom'
 assert not any(x['status']=='fail' for x in escape_checks(c,i,pads,'synthetic'))

def test_systematic_search_is_bounded_rotates_real_pins_and_keeps_anchor():
 c=context();i=intent();out=SystematicClusterPlacer().propose(UnifiedBoardModel.from_capture({'context':c}),i,dict(search_grid_mm=1,restart_radius_mm=3))
 assert out['candidates'] and out['stages']==['full_subcircuit_roles','pin_facing_construct','ratsnest_and_escape_refinement','conditional_passive_banking']
 assert all(m['component_id']=='b' for p in out['candidates'] for m in p['moves'])
 assert out['searched_poses']<1000

@pytest.mark.parametrize('angle',[0,37,90,180,270])
def test_fast_via_access_matches_analytic_rotated_copper(angle):
 p=dict(id='p',x_mm=.1,y_mm=.2,width_mm=2,height_mm=1,shape='rounded_rectangle',corner_radius_mm=.2,rotation_deg=angle,layer='Top',net_name='GND')
 s=dict(layers=[dict(name='Top',kind='signal',order=0)],vias=[dict(id='v',x_mm=2.1,y_mm=1.9,diameter_mm=.4,start_layer='Top',end_layer='Top',net_name='GND')])
 assert ViaAccessIndex(s).nearest(p)[0]==pytest.approx(nearest_via_access(s,p)[0],abs=1e-10)

def test_obstacle_edge_candidates_find_a_narrow_slot_missed_by_coarse_grid():
 c=context();c['components'][1]['courtyard_local']=[[-1,-.7],[1,-.7],[1,.7],[-1,.7]]
 for cid,y,h in [('low',24.56,4.56),('high',35.33,4.67)]:
  co=copy.deepcopy(c['components'][0]);co.update(id=cid,reference=cid,x_mm=20,y_mm=y,side='bottom',fixed=True,courtyard_local=[[-10,-h],[10,-h],[10,h],[-10,h]])
  c['components'].append(co)
 i=intent();i['grid_mm']=.127
 out=SystematicClusterPlacer().propose(UnifiedBoardModel.from_capture({'context':c}),i,dict(search_grid_mm=.127,restart_radius_mm=3,optimize_rotation=False))
 assert out['candidates']
 assert all(29.82<=m['y_mm']<=29.96 for m in out['candidates'][-1]['moves'])

def test_explicit_driver_distance_policy_rejects_farther_series_proposal():
 c=context();i=intent();i['placement_roles']['b'].update(role='series_resistor',preserve_target_distance=True)
 i=prepare_intent(c,i);poses={co['id']:e._pose(co) for co in c['components']};pads=e.transformed_pads(c,poses)
 pads['b1']['x_mm']+=2
 assert any(x['id'].startswith('role_proximity:') and x['status']=='fail' for x in escape_checks(c,i,pads,'synthetic'))

def test_restart_uses_worker_pinned_project_without_rebuilding_mutable_sources(tmp_path,monkeypatch):
 import bridge_coordination as bc
 workspace=tmp_path/'workspace';workspace.mkdir();runtime=tmp_path/'runtime';runtime.mkdir()
 script=tmp_path/'pinned.PrjScr';script.write_text('synthetic script')
 exe=tmp_path/'X2.exe';exe.write_bytes(b'synthetic executable path only')
 monkeypatch.setenv('ELECTRONICS_AI_EDA_SCRIPT',str(script))
 for key,value in [('WORKSPACE',workspace),('RUNTIME',runtime),('LAUNCH_STATE',runtime/'launch.json'),('SCRIPT',None),('EXE',exe)]:monkeypatch.setattr(bc,key,value)
 state={'pid':123,'hwnd':456,'blocked':False}
 monkeypatch.setattr(bc,'_guard_editor',lambda:state)
 monkeypatch.setattr(bc,'_retire_restarted_session',lambda:None)
 monkeypatch.setattr(bc,'_handler_busy',lambda:False)
 monkeypatch.setattr(bc,'_cleanly_released',lambda s:True)
 monkeypatch.setattr(bc,'_confirm_editor_return',lambda s:None)
 monkeypatch.setattr(bc,'_session_started_at',lambda pid:1.)
 monkeypatch.setattr(bc,'prepare_runtime',lambda:pytest.fail('Do not rebuild different code during a request'))
 monkeypatch.setattr(bc.time,'sleep',lambda t:None)
 calls=[]
 def launch(command,**kwargs):
  calls.append(command);(workspace/'bridge-ready.json').write_text('{}')
  return type('Process',(),{'pid':999})()
 monkeypatch.setattr(bc.subprocess,'Popen',launch)
 if not hasattr(bc.subprocess,'CREATE_NO_WINDOW'):monkeypatch.setattr(bc.subprocess,'CREATE_NO_WINDOW',0,raising=False)
 assert bc._ensure(lambda timeout:{'pong':True})['pong']
 assert str(script) in calls[0]

def test_shipping_eda_systematic_preview_uses_same_engine_without_native_reads():
 import ast,asyncio,__future__
 from pathlib import Path
 file=Path(__file__).resolve().parents[1]/'eda-agent/src/eda_agent/tools/pcb.py'
 node=next(n for n in ast.walk(ast.parse(file.read_text(encoding='utf-8'))) if isinstance(n,ast.AsyncFunctionDef) and n.name=='pcb_plan_placement')
 class Mcp:
  def tool(self):return lambda fn:fn
 scope={'mcp':Mcp(),'get_bridge':lambda:pytest.fail('Systematic context preview must not contact Altium')}
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(file),'exec',flags=__future__.annotations.compiler_flag),scope)
 result=asyncio.run(scope['pcb_plan_placement'](engine='systematic',board_context=context(),design_intent=intent(),placement_options={'search_grid_mm':1,'restart_radius_mm':3}))
 assert result['dry_run'] and not result['native_edits'] and result['engine']=='systematic'
 assert result['systematic_analysis']['stages'][-1]=='conditional_passive_banking'

def test_free_subcircuit_forms_a_common_angle_uniform_bank_when_feasible():
 c=context();c['components'][0]['courtyard_local']=[[-4,-1],[4,-1],[4,1],[-4,1]]
 other=copy.deepcopy(c['components'][1]);other.update(id='c',reference='R2',x_mm=26,y_mm=35);c['components'].append(other)
 c['pads'][0]['local_xy_mm']=[-1,0]
 c['pads'].append(dict(c['pads'][0],id='a2',net_name='B',local_xy_mm=[3,0]))
 for p in list(c['pads']):
  if p['component_id']=='b':c['pads'].append(dict(p,id=p['id'].replace('b','c'),component_id='c',net_name='B' if p['net_name']=='A' else 'GND'))
 c['scope_component_ids']=['b','c'];i=intent();i['grid_mm']=.5;i['placement_roles']['c']=dict(role='pull_resistor',target_pad_ids=['a2'],source=SRC);i['passive_families']=[dict(component_ids=['b','c'])]
 out=SystematicClusterPlacer().propose(UnifiedBoardModel.from_capture({'context':c}),i,dict(search_grid_mm=.5,restart_radius_mm=2))
 moves=out['candidates'][-1]['moves'];poses={x['id']:e._pose(x) for x in c['components']};poses.update({m['component_id']:m for m in moves})
 assert len(moves)==2 and scoped_metrics(c,out['intent'],{},poses)[2]==1

def test_pure_view_handlers_do_not_require_pre_edit_checkpoint():
 from suite_checkpoints import is_mutation
 for name,args in [('obj_select',{'object_type':'ComponentObject','filter':'Name=R1'}),('obj_deselect_all',{}),('obj_zoom',{'action':'selection'})]:assert not is_mutation(name,args)
 assert is_mutation('obj_modify_objects',{'properties':'X=99|Selected=true'})
