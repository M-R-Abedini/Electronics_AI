"""Synthetic P1 policy regressions; never evidence of native acceptance."""
import copy
import math

import pytest

from eda_agent.design import placement_evidence as core
from eda_agent.design import placement_p1 as p1
from suite_board_geometry import subtract_rectilinear_cutouts
from suite_layout import context_from_snapshot


def shape(x=0, y=0, w=2, h=2, kind='rectangle', rotation=0, **kwargs):
    return dict(x_mm=x,y_mm=y,width_mm=w,height_mm=h,shape=kind,rotation_deg=rotation,**kwargs)


@pytest.mark.parametrize('a,b,expected',[
    (shape(kind='circle'),shape(x=3,kind='circle'),1),
    (shape(w=4,h=2,kind='oval'),shape(x=4,kind='circle'),1),
    (shape(w=4,h=2,kind='oval',rotation=90),shape(y=4,kind='circle'),1),
    (shape(),shape(x=3),1),
    (shape(kind='rounded_rectangle',corner_radius_mm=.5),shape(x=3,kind='circle'),1),
    (shape(),shape(w=.1,h=.1),0),
    (shape(w=8,h=.2,rotation=45),shape(w=8,h=.2,rotation=135),0),
])
def test_exact_analytic_distances(a,b,expected):
    assert p1.analytic_distance(a,b)==pytest.approx(expected,abs=1e-12)
    assert p1.analytic_distance(b,a)==pytest.approx(expected,abs=1e-12)


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),True,-1,0])
def test_analytic_geometry_rejects_invalid_dimensions(bad):
    with pytest.raises((ValueError,KeyError)):
        p1.analytic_shape(shape(w=bad))


def test_diagonal_copper_clearance_does_not_use_inscribed_polygon():
    a=shape(kind='circle')
    b=shape(x=3/math.sqrt(2),y=3/math.sqrt(2),kind='circle')
    assert p1.analytic_distance(a,b)==pytest.approx(1)


def test_slots_use_combined_pad_and_within_pad_rotation():
    slot={'hole_geometry':{'x_mm':0,'y_mm':0,'diameter_mm':1,'slot_length_mm':4,
                           'shape':'capsule','pad_rotation_deg':90,'rotation_within_pad_deg':90}}
    assert p1.analytic_distance(p1.hole_shape(slot),shape(x=4,kind='circle'))==pytest.approx(1)


def test_stack_expansion_keeps_different_bottom_shape_and_rejects_partial():
    pad=dict(id='p',layer='MultiLayer',layer_geometries=[shape(layer='TopLayer'),shape(w=4,kind='oval',layer='BottomLayer')])
    rows=p1.expand_layers({'p':pad})
    assert [(r['layer'],r['width_mm']) for r in rows]==[('TopLayer',2),('BottomLayer',4)]
    pad['layer_geometries'][1]['geometry_complete']=False
    with pytest.raises(ValueError,match='Partial'):p1.expand_layers({'p':pad})


def test_layer_and_slot_rigid_transform_without_losing_identity():
    raw=dict(id='p',**shape(x=2),layer_geometries=[shape(x=2,layer='TopLayer')],
             hole_geometry={'x_mm':2,'y_mm':0,'shape':'capsule','diameter_mm':1,
                            'slot_length_mm':3,'pad_rotation_deg':0,'rotation_within_pad_deg':90})
    a=dict(x_mm=0,y_mm=0,rotation_deg=0,side='top')
    b=dict(x_mm=10,y_mm=0,rotation_deg=90,side='top')
    result=p1.transform_layer_geometry(raw,raw,a,b)
    layer=result['layer_geometries'][0]
    assert [layer['x_mm'],layer['y_mm'],layer['rotation_deg']]==pytest.approx([10,2,90])
    assert result['hole_geometry']['rotation_within_pad_deg']==90
    assert raw['layer_geometries'][0]['x_mm']==2


def rule_snapshot():
    return dict(board_path='synthetic.PcbDoc',native_rules={'rules':[dict(name='Clearance',rule_kind=0,
        enabled=True,drc_enabled=True,scope_1='All',scope_2='All',priority=1)]},
        native_rule_values={'coord_mm':2.54e-6,'items':[dict(name='Clearance',gap_coord=40000)]})


def test_board_rule_source_is_captured_constraint_not_fabrication_certification():
    result=p1.board_clearance(rule_snapshot())
    assert result['minimum_pad_clearance_mm']==pytest.approx(.1016)
    assert result['clearance_source']=='board_drc_rule' and not result['fabricator_verified']


def test_clearance_detects_bottom_layer_violation_despite_clear_top_pads():
    s=rule_snapshot();s.update(layers=[dict(name=n,kind='signal') for n in ('TopLayer','BottomLayer')],missing=[],holes=[])
    profile=p1.board_clearance(s)
    a=dict(id='a',component_id='a',net_name='A',layer='MultiLayer',geometry_complete=False,
           layer_geometries=[shape(w=.5,h=.5,layer='TopLayer'),shape(w=1.6,h=.5,layer='BottomLayer')])
    b=dict(id='b',component_id='b',net_name='B',layer='MultiLayer',geometry_complete=False,
           layer_geometries=[shape(x=1,w=.5,h=.5,layer='TopLayer'),shape(x=1,w=.5,h=.5,layer='BottomLayer')])
    c=dict(fabrication_profile=profile,scope_component_ids=['a','b'],copper_snapshot=s,coverage={'pads':'complete'})
    checks=p1.manufacturing_checks(c,{'a':a,'b':b},'synthetic')
    failures=[r for r in checks if r['status']=='fail']
    assert len(failures)==1 and failures[0]['geometry']['layer']=='BottomLayer'


def test_slot_hole_distance_uses_dedicated_captured_hole_rule():
    s=rule_snapshot();s['native_rules']['rules'].append(dict(name='HoleGap',rule_kind=52,
        enabled=True,drc_enabled=True,scope_1='All',scope_2='All',priority=1))
    s['native_rule_values']['items'].append(dict(name='HoleGap',gap_coord=100000))
    slot=dict(x_mm=0,y_mm=0,shape='capsule',diameter_mm=1,slot_length_mm=4,pad_rotation_deg=0,rotation_within_pad_deg=0)
    circle=dict(x_mm=2,y_mm=0,shape='circle',diameter_mm=1,pad_rotation_deg=0,rotation_within_pad_deg=0)
    s.update(layers=[],missing=[],holes=[dict(owner_id='a',hole_geometry=slot),dict(owner_id='b',hole_geometry=circle)])
    pads={i:dict(id=i,component_id=i,net_name='SHARED',layer='TopLayer',hole_geometry=h,**shape(x=x))
          for i,h,x in [('a',slot,0),('b',circle,2)]}
    c=dict(fabrication_profile=p1.board_clearance(s),scope_component_ids=['a','b'],copper_snapshot=s,coverage={'pads':'complete'})
    failures=[r for r in p1.manufacturing_checks(c,pads,'synthetic') if r['status']=='fail']
    assert len(failures)==1 and failures[0]['id'].startswith('manufacturing:hole_clearance:')
    assert failures[0]['limit_value']==pytest.approx(.254)


@pytest.mark.parametrize('change',['disabled','unscoped','missing_value','pad_specific'])
def test_no_unsafe_fallback_when_applicable_rule_is_unknown(change):
    s=rule_snapshot();r=s['native_rules']['rules'][0]
    if change=='disabled':r['enabled']=False
    if change=='unscoped':r['scope_1']='IsPad'
    if change=='missing_value':s['native_rule_values']['items']=[]
    if change=='pad_specific':s['native_rules']['rules'].append(dict(r,name='Analog',scope_1="InNet('A')"))
    assert p1.board_clearance(s)=={}


def test_derived_envelope_needs_actual_body_in_addition_to_pads():
    c=core.example_board_context();c['policy_version']=p1.POLICY_VERSION
    for comp in c['components']:
        comp.pop('courtyard_local');comp.pop('courtyard_source')
    out=p1.prepare_context(c)
    assert all(not row.get('courtyard_local') for row in out['components'])
    assert all(row.get('envelope_diagnostic') for row in out['components'])


@pytest.mark.parametrize('ratio,ignored',[(.9,0),(.9001,1),(1,1)])
def test_cutout_override_is_explicit_strict_threshold_and_raw_evidence_kept(ratio,ignored):
    outline=[[0,0],[10,0],[10,10],[0,10]]
    cut=[[0,0],[10*ratio,0],[10*ratio,10],[0,10]]
    result=subtract_rectilinear_cutouts(outline,[cut],ignore_full_board_artifacts=True)
    assert result['ignored_full_board_cutout_contours']==ignored
    if ignored:
        assert result['outline']==outline
        assert result['warnings'][0]['raw_contour']==cut
        assert 'unverified' in result['warnings'][0]['interpretation']
    if ratio==1:
        with pytest.raises(ValueError,match='entire board'):
            subtract_rectilinear_cutouts(outline,[cut])  # P0 behavior unchanged.


def test_p0_remains_selectable_and_unknown_policy_is_rejected():
    c=core.example_board_context()
    assert core.evaluate_placement(c)['policy_version']==p1.P0_VERSION
    c['policy_version']=p1.POLICY_VERSION
    assert core.evaluate_placement(c)['policy_version']==p1.POLICY_VERSION
    c['policy_version']='unrecognized'
    with pytest.raises(ValueError,match='Unsupported'):core.evaluate_placement(c)


def test_snapshot_iterator_reordering_is_not_a_physical_edit():
    from suite_layout import capture_digest
    s={'pads':[dict(id='a',x_mm=1),dict(id='b',x_mm=2)],'board_outline':[[0,0],[10,0],[10,10]]}
    reordered=copy.deepcopy(s);reordered['pads'].reverse()
    assert capture_digest(s)==capture_digest(reordered)
    reordered['pads'][0]['x_mm']+=.000001
    assert capture_digest(s)!=capture_digest(reordered)
    reordered=copy.deepcopy(s);reordered['board_outline'].reverse()
    assert capture_digest(s)!=capture_digest(reordered)


def test_unchanged_remote_collision_is_reported_but_moved_component_must_be_clear():
    c=core.example_board_context();c['policy_version']=p1.POLICY_VERSION
    c['coverage']['pads']='complete'
    remote=copy.deepcopy(c['components'][0]);remote.update(id='remote',x_mm=14.2,reference='REMOTE')
    c['components'].append(remote)
    # Move component b closer to a but keep it clear of the remote defect.
    move=[dict(component_id='b',x_mm=20,y_mm=25,rotation_deg=0,side='top')]
    report=core.evaluate_placement(c,moves=move)
    assert report['feasible']
    assert report['unchanged_baseline_defects']
    assert any(row.get('baseline_disposition') for row in report['checks'])
    blocked=core.evaluate_placement(c,moves=[dict(move[0],x_mm=14)])
    assert not blocked['feasible']


def test_repairing_moved_collision_precedes_ordinary_hpwl_but_remote_defects_are_not_a_reward():
    c=core.example_board_context();c['policy_version']=p1.POLICY_VERSION;c['coverage']['pads']='complete'
    remote=copy.deepcopy(c['components'][0]);remote.update(id='remote',x_mm=14.2,reference='REMOTE')
    c['components'].append(remote)
    repaired=core.compare_placement_candidates(c,candidates=[dict(id='repair',moves=[dict(component_id='a',x_mm=11.5)])])
    assert repaired['selection_status']=='selected'
    assert repaired['evaluations'][1]['metrics']['signal_hpwl']['value']>repaired['evaluations'][0]['metrics']['signal_hpwl']['value']
    pointless=core.compare_placement_candidates(c,candidates=[dict(id='away',moves=[dict(component_id='b',x_mm=26)])])
    assert pointless['selection_status']=='blocked' and not pointless['plan']['apply_eligible']
