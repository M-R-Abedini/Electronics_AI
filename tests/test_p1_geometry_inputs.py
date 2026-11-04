"""Synthetic geometry/transport regression fixtures; no live acceptance claims."""
import copy
import pytest
from suite_board_geometry import subtract_rectilinear_cutouts as subtract
from eda_agent.design.copper_geometry import point_in_region, polygon_area

OUTER = [[0,0],[10,0],[10,10],[0,10]]


def area(region):
    return abs(polygon_area(region['outline']))-sum(abs(polygon_area(h)) for h in region['holes'])


@pytest.mark.parametrize('cutouts,expected', [
    ([],100),
    ([[[8,4],[12,4],[12,6],[8,6]]],96),
    ([[[11,4],[12,4],[12,6],[11,6]]],100),
    ([[[2,2],[5,2],[5,5],[2,5]],[[4,4],[7,4],[7,7],[4,7]]],83),
    ([[[2,2],[4,2],[4,4],[2,4]],[[6,6],[8,6],[8,8],[6,8]]],92),
    ([[[2,2],[4,2],[4,4],[2,4]]]*2,96),
    ([[[0,4],[2,4],[2,6],[0,6]]],96),
])
def test_exact_set_difference_area_and_membership(cutouts, expected):
    before=copy.deepcopy(cutouts)
    region=subtract(OUTER,cutouts)
    assert area(region)==expected
    assert cutouts==before
    for x in (.5,2.5,4.5,6.5,8.5,9.5):
        for y in (.5,2.5,4.5,6.5,8.5,9.5):
            wanted=point_in_region((x,y),{'outline':OUTER}) and not any(
                point_in_region((x,y),{'outline':c}) for c in cutouts)
            assert point_in_region((x,y),region)==wanted


def test_cutout_order_winding_and_duplicate_are_set_invariant():
    a=[[1,1],[3,1],[3,3],[1,3]]
    b=[[8,4],[12,4],[12,6],[8,6]]
    assert subtract(OUTER,[a,b])==subtract(list(reversed(OUTER)),[list(reversed(b)),a,a])


@pytest.mark.parametrize('outer,cuts', [
    (OUTER,[[[4,-1],[6,-1],[6,11],[4,11]]]),  # disconnected FR4
    (OUTER,[[[-1,-1],[11,-1],[11,11],[-1,11]]]),  # no FR4
    (OUTER,[[[1,1],[2,2],[1,3]]]),  # unsupported diagonal
    ([[0,0],[10,10],[0,10],[10,0]],[]),  # invalid source
    (OUTER,[[[0,0],[2,0],[2,2],[0,2]],[[2,2],[4,2],[4,4],[2,4]]]),  # non-manifold
])
def test_unsupported_geometry_stays_explicit(outer,cuts):
    with pytest.raises(ValueError):
        subtract(outer,cuts)


def test_no_snap_or_buffer_erases_a_thin_slot():
    c=[[5,2],[5.000001,2],[5.000001,8],[5,8]]
    region=subtract(OUTER,[c])
    assert not point_in_region((5.0000005,5),region)
    assert point_in_region((4.999999,5),region)


def test_explicit_resource_bound_and_nonfinite_input():
    with pytest.raises(ValueError,match='budget'):
        subtract(OUTER,[[[1,1],[2,1],[2,2],[1,2]]],max_cells=1)
    with pytest.raises(ValueError):
        subtract([[0,0],[float('nan'),0],[10,10]],[])


def test_large_gate_response_is_bounded_without_changing_full_evidence():
    from suite_layout import bounded_checks
    raw={'passed':False,'unresolved_blockers':['unknown:'+str(i) for i in range(1000)],
         'report':{'required_checks':['check:'+str(i) for i in range(1000)],
                   'blockers':['unknown:'+str(i) for i in range(1000)]}}
    before=copy.deepcopy(raw)
    public=bounded_checks(raw)
    assert not public['passed']
    assert len(public['unresolved_blockers'])==64
    assert public['unresolved_blockers_total']==1000 and public['unresolved_blockers_truncated']
    assert public['report']['required_checks_total']==1000
    assert public['report']['blockers_total']==1000
    assert raw==before, 'Server gate/artifact must retain every blocker and required check'
