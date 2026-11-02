"""Physical geometry precision regression for the RV1106 R91 clearance."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'eda-agent' / 'src'))
from eda_agent.route.model import RoutingProblem, dist_seg_rect
from eda_agent.route.router import validate_solution


def r91_geometry():
    return {
        'units': 'mils', 'coordinate_precision_mils': .0001,
        'bbox': {'x1': 2750, 'y1': 2030, 'x2': 2850, 'y2': 2120},
        'pads': [{'x': 2814.685, 'y': 2075, 'x_size': .6 / .0254,
                  'y_size': .6 / .0254, 'rotation': 180, 'layer': 'TopLayer',
                  'net': 'OSC_XTAL_OUT', 'comp': 'R91', 'name': '1'}],
    }


def test_r91_fractional_pad_geometry_rejects_false_integer_mil_clearance_pass():
    geometry = r91_geometry()
    track = {'x1': 2796, 'y1': 2056, 'x2': 2796, 'y2': 2096,
             'width': 6, 'layer': 'TopLayer', 'net_name': 'OSC_XIN'}
    rules = {'clearance_mils': 4, 'track_width_mils': 6, 'layers': ['TopLayer']}
    solution = {'tracks': [track], 'vias': []}
    precise = RoutingProblem.from_geometry(geometry, rules, grid_pitch_mils=2)
    obstacle = precise.geoms[0]
    assert obstacle['cx'] == 2814.685
    edge_clearance = dist_seg_rect(2796, 2056, 2796, 2096, obstacle['cx'], obstacle['cy'],
                                   obstacle['hw'], obstacle['hh']) - track['width'] / 2
    assert edge_clearance == pytest.approx(3.87397637795)
    check = validate_solution(precise, solution)
    assert check['ok'] is False
    assert check['violations'][0]['kind'] == 'track_rect'
    assert check['violations'][0]['distance_mils'] == 6.874
    assert check['violations'][0]['required_mils'] == 7

    # Old native export rounded both the centre and pad width, hiding this.
    rounded = copy.deepcopy(geometry)
    rounded['pads'][0].update(x=2815, x_size=24, y_size=24)
    old_problem = RoutingProblem.from_geometry(rounded, rules, grid_pitch_mils=2)
    assert validate_solution(old_problem, solution)['ok'] is True


def test_native_geometry_retains_resolution_for_every_coordinate_and_dimension():
    source = (Path(__file__).resolve().parents[1] / 'eda-agent/scripts/altium/Generic.pas').read_text(encoding='utf-8')
    helper = source.split('Function Gen_PcbCoordMils(', 1)[1].split('Function Gen_GetPcbGeometry(', 1)[0]
    handler = source.split('Function Gen_GetPcbGeometry(', 1)[1].split('Function Gen_IncrementDesignators(', 1)[0]
    assert 'FloatToJsonStr(Coord / 10000.0)' in helper
    assert 'CoordToMils(' not in handler
    assert '"units":"mils"' in handler and '"coordinate_precision_mils":0.0001' in handler
    for member in ('Seg.vx', 'Seg.vy', 'Seg.cx', 'Seg.cy', 'Seg.Radius',
                   'Track.X1', 'Track.Y1', 'Track.X2', 'Track.Y2', 'Track.Width',
                   'Arc.XCenter', 'Arc.YCenter', 'Arc.Radius', 'Arc.LineWidth',
                   'Pad.X', 'Pad.Y', 'Pad.TopXSize', 'Pad.TopYSize', 'Pad.HoleSize', 'Pad.HoleWidth',
                   'Via.X', 'Via.Y', 'Via.Size', 'Via.HoleSize',
                   'Text.XLocation', 'Text.YLocation', 'Text.Size', 'Text.Width',
                   'Contour.X[K]', 'Contour.Y[K]', 'CompObj.X', 'CompObj.Y',
                   'BR.X1', 'BR.Y1', 'BR.X2', 'BR.Y2'):
        assert 'Gen_PcbCoordMils(' + member + ')' in handler
