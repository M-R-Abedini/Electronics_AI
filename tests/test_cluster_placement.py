"""Synthetic contracts for P1; native acceptance lives in private evidence."""
import copy
import pytest
from eda_agent.design.placement_evidence import example_board_context
from eda_agent.design.cluster_placement import UnifiedBoardModel,MotifGraphExtractor,HierarchicalClusterPlacer,propose_proximity_budgets
from eda_agent.design.placement_p1 import POLICY_VERSION,quantization_budget,transformed_pad_aabb_manhattan_mm


def model():
    c=example_board_context();c['policy_version']=POLICY_VERSION;c['coverage']['pads']='complete'
    c['scope_component_ids']=['a','b']
    c['copper_snapshot']={'raw_future_geometry':{'arcs':[1,2,3]},'document_modified':True}
    return UnifiedBoardModel.from_capture(dict(context=c,capture_digest='captured-revision',trusted_live=False,
        snapshot={'original_unknown_metadata':['do','not','drop']},provenance={'source':'synthetic'}))


def test_unified_capture_roundtrip_preserves_unknown_fields_and_is_immutable():
    m=model();record=m.to_capture();record['context']['components'][0]['locked']=True
    assert not m.context['components'][0].get('locked')
    roundtrip=UnifiedBoardModel.from_capture(m.to_capture())
    assert roundtrip.to_capture()==m.to_capture()
    assert roundtrip.revision['capture_digest']=='captured-revision'
    assert roundtrip.context['copper_snapshot']['raw_future_geometry']=={'arcs':[1,2,3]}


def test_engine_requires_unified_model_instead_of_another_board_state():
    for target in [MotifGraphExtractor().extract,HierarchicalClusterPlacer().propose]:
        with pytest.raises(ValueError,match='UnifiedBoardModel'):target({})


@pytest.mark.parametrize('kind',['tvs','crystal','switching_hot_loop','kelvin_shunt'])
def test_sourced_pin_bound_motif_types_and_unverified_roles_are_distinguished(kind):
    m=model();binding=dict(id='synthetic-'+kind,type=kind,component_ids=['a','b'],pad_ids=['a.1','b.1'],
        pin_roles_verified=True,source={'kind':'user_intent','id':'synthetic-board-role-map'})
    graph=MotifGraphExtractor().extract(m,{'motif_bindings':[binding]})
    assert graph['clusters'][0]['qualified'] and graph['clusters'][0]['type']==kind
    binding['pin_roles_verified']=False
    assert not MotifGraphExtractor().extract(m,{'motif_bindings':[binding]})['clusters'][0]['qualified']


def test_decoupling_requires_both_actual_power_and_return_net_bindings():
    r=model().to_capture();c=r['context'];c['pads'][0]['net_name']='VCC';c['pads'][1]['net_name']='VCC'
    for p in list(c['pads']):
        q=copy.deepcopy(p);q.update(id=p['id']+'g',net_name='GND',pad_name='2',local_xy_mm=[.5,0]);c['pads'].append(q)
    m=UnifiedBoardModel.from_capture(r)
    b=dict(id='both-branches',ic_power_pad='a.1',cap_power_pad='b.1',ic_return_pad='a.1g',cap_return_pad='b.1g',
        power_net='VCC',return_net='GND',source={'kind':'user_intent','id':'synthetic-pin-map'})
    graph=MotifGraphExtractor().extract(m,{'decoupling_connections':[b]})
    assert graph['clusters'][0]['type']=='decoupling' and graph['clusters'][0]['qualified']
    b['return_net']='WRONG'
    assert not MotifGraphExtractor().extract(m,{'decoupling_connections':[b]})['clusters'][0]['qualified']


def test_budget_diagnostics_are_board_scoped_and_not_hard_limits():
    m=model();g=MotifGraphExtractor().extract(m)
    rows=propose_proximity_budgets(m,g)
    assert len(rows)==5 and all(r['value'] is None for r in rows)
    assert all(r['scope']==m.context['board_path'] and not r['used_as_hard_gate'] for r in rows)


def test_two_stage_proposals_are_deterministic_and_preserve_locked_components():
    r=model().to_capture();r['context']['components'][0]['locked']=True;m=UnifiedBoardModel.from_capture(r)
    opts=dict(seed=7,iterations=10,candidate_count=2,search_grid_mm=.1)
    a=HierarchicalClusterPlacer().propose(m,options=opts);b=HierarchicalClusterPlacer().propose(m,options=opts)
    assert a==b and not a['native_edits']
    assert a['stages']==['intra_cluster_existing_solver','inter_cluster_rigid_translation']
    assert all(move['component_id']!='a' and move['rotation_deg'] in [0,90,180,270]
               for candidate in a['candidates'] for move in candidate['moves'])


def test_coarse_stage_preserves_relative_local_result_of_a_bound_cluster():
    m=model();binding=dict(id='entry',type='tvs',component_ids=['a','b'],pad_ids=['a.1','b.1'],
        pin_roles_verified=True,source={'kind':'user_intent','id':'synthetic-entry-pins'})
    result=HierarchicalClusterPlacer().propose(m,{'motif_bindings':[binding]},dict(seed=2,iterations=5,candidate_count=2))
    assert len(result['candidates'])==2
    local={m['component_id']:m for m in result['candidates'][0]['moves']}
    coarse={m['component_id']:m for m in result['candidates'][1]['moves']}
    assert result['candidates'][0]['stage']=='intra_cluster'
    for axis in ('x_mm','y_mm'):
        assert coarse['b'][axis]-coarse['a'][axis]==pytest.approx(local['b'][axis]-local['a'][axis])


def test_gateway_hierarchical_mode_uses_capture_and_remains_offline(tmp_path):
    import asyncio
    from suite_layout import LayoutService
    async def run():
        service=LayoutService(tmp_path)
        captured=await service.capture_context(board_context=model().context,policy_version=POLICY_VERSION)
        planned=await service.plan_placement(captured['context_id'],options=dict(mode='hierarchical',iterations=5,candidate_count=1))
        assert planned['policy_version']==POLICY_VERSION and not planned['native_edits']
        assert planned['hierarchical_analysis']['revision']['source_snapshot']=='synthetic-baseline'
        gate=await service.validate_plan(planned['plan_id'])
        assert not gate['passed'] and 'trusted_live_capture' in gate['unresolved_blockers']
    asyncio.run(run())


def test_candidate_count_does_not_truncate_relevant_local_clusters():
    r=model().to_capture();c=r['context'];c['outline']=[[10,20],[60,20],[60,40],[10,40]]
    for comp in list(c['components']):
        extra=copy.deepcopy(comp);extra.update(id=comp['id']+'2',reference=comp['reference']+'2',x_mm=comp['x_mm']+25)
        c['components'].append(extra)
    for p in list(c['pads']):
        extra=copy.deepcopy(p);extra.update(id=p['id']+'2',component_id=p['component_id']+'2');c['pads'].append(extra)
    c['scope_component_ids']=['a','b','a2','b2']
    bindings=[dict(id=str(i),type='tvs',component_ids=ids,pad_ids=pids,pin_roles_verified=True,
                   source={'kind':'user_intent','id':'synthetic'}) for i,ids,pids in
                   [(1,['a','b'],['a.1','b.1']),(2,['a2','b2'],['a.12','b.12'])]]
    intent={'motif_bindings':bindings,'critical_paths':[{'id':'remote-leg','pad_ids':['a.12','b.12']}],
            'blocks':[{'id':'remote','component_ids':['a2','b2'],'roles':[]}]}
    result=HierarchicalClusterPlacer().propose(UnifiedBoardModel.from_capture(r),intent,
               dict(candidate_count=1,iterations=2,seed=1))
    assert result['local_clusters_processed']==2 and result['local_clusters_unprocessed']==0
    assert len(result['candidates'])==1


@pytest.mark.parametrize('options',[{'candidate_count':True},{'candidate_count':17},{'iterations':-1},{'search_grid_mm':float('nan')}])
def test_computational_budgets_are_validated(options):
    with pytest.raises(ValueError):HierarchicalClusterPlacer().propose(model(),options=options)


def test_unproved_grid_origin_keeps_quantization_epsilon_zero():
    c=model().context;acceptance=dict(status='PASS',target=c['board_path'],native_apply=True,restore='PASS',maximum_error_mm=.000002)
    assert quantization_budget(c,acceptance)['epsilon_quant_mm']==0
    c['native_grid']=dict(source='native_capture',on_grid_verified=True,pitch_mm=[.1,.1],origin_mm=[10,20])
    assert quantization_budget(c,acceptance)['epsilon_quant_mm']==0, 'A boolean claim is not a native measurement'
    acceptance.update(exact_moves=[dict(component_id='a',x_mm=14,y_mm=25)],
                      readback_coords=[dict(component_id='a',x_mm=14.000002,y_mm=25)])
    assert quantization_budget(c,acceptance)['epsilon_quant_mm']==pytest.approx(.000002)
    c['components'][0]['x_mm']=14.03
    assert quantization_budget(c,acceptance)['epsilon_quant_mm']==0
    c['components'][0]['x_mm']=14
    acceptance['target']='other-board.PcbDoc'
    assert quantization_budget(c,acceptance)['epsilon_quant_mm']==0


def test_p1_critical_gap_consumes_largest_layer_extent_and_ignores_soft_via_proxy():
    a=dict(x_mm=0,y_mm=0,width_mm=1,height_mm=1,shape='rectangle',rotation_deg=0,
        layer_geometries=[dict(x_mm=0,y_mm=0,width_mm=4,height_mm=1,shape='rectangle',rotation_deg=0)])
    b=dict(x_mm=4,y_mm=0,width_mm=2,height_mm=2,shape='circle',rotation_deg=0)
    assert transformed_pad_aabb_manhattan_mm(a,b,1e6)==1
