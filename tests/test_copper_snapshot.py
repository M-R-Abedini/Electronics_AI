"""Independent synthetic PcbDoc and bridge-adapter tests; never calls Altium."""
import asyncio
import copy
import json
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'eda-agent' / 'src'))
from eda_agent.design.copper_snapshot import (
    BoardCopperSnapshot, content_fingerprint, empty_snapshot, validate_snapshot,
)
from suite_ole import read_streams, write_streams
from suite_pcb_primitives import (
    decode_board_primitive_stream, encode_component_stream, read_board_copper,
)
from suite_reads import capture_copper_snapshot


def params(**values):
    raw = ('|' + '|'.join(f'{k}={v}' for k, v in values.items()) + '\0').encode()
    return struct.pack('<I', len(raw)) + raw


def primitive_stream(section, rows):
    # PcbDoc has no footprint-name prefix. Payloads use the existing MIT codec.
    data = encode_component_stream({'name': 'fixture', section: rows})
    prefix = 4 + struct.unpack_from('<I', data)[0]
    return data[prefix:]


def synthetic_board(tmp_path):
    outline = [{'x': 0, 'y': 0}, {'x': 10, 'y': 0}, {'x': 10, 'y': 10}, {'x': 0, 'y': 10}]
    hole = [{'x': 3, 'y': 3}, {'x': 4, 'y': 3}, {'x': 3, 'y': 4}]
    streams = {
        'Board6/Data': params(LAYER1NAME='Top', LAYER1NEXT=32, LAYER1COPTHICK='0.035mm',
                              LAYER1DIELHEIGHT='1.5mm', LAYER1DIELCONST=4.1,
                              LAYER32NAME='Bottom', LAYER32NEXT=0, LAYER32COPTHICK='1.4mil'),
        'Nets6/Data': params(NAME='GND') + params(NAME='SIG'),
        'Components6/Data': params(SOURCEDESIGNATOR='NT1', SOURCECOMMENT='NET TIE',
                                   PATTERN='NT_2', COMPONENTKIND='NETTIE', MPN='CUSTOM'),
        'Tracks6/Data': primitive_stream('tracks', [dict(x1=-1, y1=2, x2=5, y2=2,
            width=.2, net_index=1, layer='Top Layer'), dict(x1=1, y1=3, x2=2, y2=3,
                width=.4, flags='KEEPOUT', layer='Top Layer')]),
        'Arcs6/Data': primitive_stream('arcs', [dict(x=5, y=5, radius=2, start_angle=15,
                                                    end_angle=210, width=.3, net_index=0, layer='Top Layer')]),
        'Vias6/Data': primitive_stream('vias', [dict(x=5, y=2, diameter=.7, hole_size=.3,
                                                    net_index=1, from_layer='Top Layer', to_layer='Bottom Layer')]),
        'Pads6/Data': primitive_stream('pads', [dict(designator='1', component_index=0,
            x=1, y=2, width=1.2, height=.8, rotation=45, shape='rectangle', net_index=0,
            hole_size=.25), dict(designator='2', component_index=0, x=2, y=2, width=1,
                                height=1, net_index=1)]),
        'Fills6/Data': primitive_stream('fills', [dict(x1=1, y1=1, x2=2, y2=3,
                                                      rotation=90, net_index=0)]),
        'Regions6/Data': primitive_stream('regions', [dict(vertices=outline, holes=[hole], net_index=0),
            dict(vertices=hole, kind='board_cutout'), dict(vertices=outline, flags='KEEPOUT')]),
        'Polygons6/Data': params(NAME='GROUND_DEF', NET=0, LAYER='Top Layer',
                                VX0='0mm', VY0='0mm', VX1='10mm', VY1='0mm', VX2='0mm', VY2='10mm'),
        'HugeModel/Data': b'x' * 65536,
    }
    for storage, count in {'Board6': 1, 'Nets6': 2, 'Components6': 1, 'Tracks6': 2,
            'Arcs6': 1, 'Vias6': 1, 'Pads6': 2, 'Fills6': 1, 'Regions6': 3, 'Polygons6': 1}.items():
        streams[storage + '/Header'] = struct.pack('<I', count)
    path = tmp_path / 'Synthetic.PcbDoc'
    write_streams(path, streams)
    return path, streams


def test_board_copper_shared_codec_and_bindings(tmp_path):
    path, _ = synthetic_board(tmp_path)
    result = read_board_copper(path)
    validate_snapshot(result, expected_board_path=str(path))
    assert result['source'] == 'file' and not result['provenance']['live']
    assert len(result['tracks']) == 1
    assert result['tracks'][0]['start'][0] == pytest.approx(-1, abs=2e-6)
    assert result['tracks'][0]['net_name'] == 'SIG'
    assert result['vias'][0]['start_layer'] == 'Top Layer'
    assert result['vias'][0]['drill_mm'] == pytest.approx(.3, abs=2e-6)
    assert result['pads'][0]['component'] == 'NT1'
    assert result['pads'][0]['pad_name'] == '1'
    assert result['pads'][0]['rotation_deg'] == 45
    assert result['components'][0]['parameters']['MPN'] == 'CUSTOM'
    assert result['net_ties'][0]['net_names'] == ['GND', 'SIG']
    assert len(result['keepouts']) == 2 and not result['cutouts']
    assert len(result['unsupported_regions']) == 1
    assert result['unsupported_regions'][0]['geometry_complete'] is False
    assert any('unverified board region kind' in m for m in result['missing'])
    assert len(result['holes']) == 2
    region = next(r for r in result['regions'] if r.get('holes'))
    assert len(region['holes'][0]) == 3
    definition = next(r for r in result['regions'] if r['kind'] == 'polygon')
    assert definition['poured'] is False
    dielectric = next(l for l in result['layers'] if l['kind'] == 'dielectric')
    assert dielectric['thickness_mm'] == 1.5 and dielectric['er'] == 4.1
    assert result['layers'][0]['thickness_mm'] == .035
    assert result['complete'] is False and result['missing']
    assert 'HugeModel/Data' not in result['provenance']['streams']


def test_saved_stack_next_links_exclude_named_inactive_planes_and_reject_cycles(tmp_path):
    path, streams = synthetic_board(tmp_path)
    rows = dict(LAYER1NAME='Top', LAYER1NEXT=32, LAYER1COPTHICK='0.035mm',
                LAYER1DIELHEIGHT='0.32mm', LAYER1DIELCONST=4.8,
                LAYER32NAME='Bottom', LAYER32NEXT=0, LAYER32COPTHICK='0.035mm',
                LAYER32DIELHEIGHT='0mil', LAYER39NAME='Internal Plane 1', LAYER39NEXT=0, LAYER39PREV=0)
    streams['Board6/Data'] = params(**rows)
    write_streams(path, streams)
    snapshot = read_board_copper(path)
    assert [l['kind'] for l in snapshot['layers']] == ['signal', 'dielectric', 'signal']
    assert snapshot['layers'][1]['er'] == 4.8
    assert snapshot['coverage']['layers'] == 'complete'
    rows['LAYER32NEXT'] = 1
    streams['Board6/Data'] = params(**rows)
    write_streams(path, streams)
    snapshot = read_board_copper(path)
    assert snapshot['coverage']['layers'] == 'partial'
    assert any('ordering incomplete' in m for m in snapshot['missing'])


def test_selected_ole_limits_apply_only_to_selected_payloads(tmp_path):
    path, _ = synthetic_board(tmp_path)
    selected = read_streams(path, ['Nets6'], max_stream_bytes=1024, max_total_bytes=1024)
    assert set(selected) == {'Nets6/Data', 'Nets6/Header'}
    with pytest.raises(ValueError, match='size limit'):
        read_streams(path, ['HugeModel'], max_stream_bytes=1024)


def test_unknown_record_schemas_never_become_clean_evidence(tmp_path):
    path, streams = synthetic_board(tmp_path)
    streams['Tracks6/Data'] += b'\xfa' + struct.pack('<I', 4) + b'????'
    streams['Tracks7/Data'] = b'unknown version'
    streams['LayerStack/Data'] = b'opaque native stack'
    write_streams(path, streams)
    result = read_board_copper(path)
    assert len(result['tracks']) == 1
    assert not result['complete']
    assert any('unsupported primitive type 250' in x for x in result['missing'])
    assert any('Tracks7' in x for x in result['missing'])
    assert any('layer-stack' in x for x in result['missing'])
    assert result['coverage']['tracks'] == 'partial'


def test_short_and_truncated_primitive_records_are_not_zero_geometry():
    rows, missing = decode_board_primitive_stream(b'\x04\x01\0\0\0x', 4)
    assert not rows and 'unsupported short schema' in missing[0]
    with pytest.raises(ValueError, match='exceeds'):
        decode_board_primitive_stream(b'\x04\xff\0\0\0x', 4)


def test_unknown_layout_layer_and_nonfinite_coordinates_are_excluded():
    original = primitive_stream('tracks', [dict(x1=0, y1=0, x2=1, y2=1, width=.2, layer='Top Layer')])
    unknown_layer = bytearray(original)
    unknown_layer[5] = 250
    rows, missing = decode_board_primitive_stream(bytes(unknown_layer), 4)
    assert not rows and any('unsupported layer ID' in m for m in missing)
    extended = original[:1] + struct.pack('<I', 50) + original[5:] + b'x'
    rows, missing = decode_board_primitive_stream(extended, 4)
    assert not rows and any('unverified payload schema' in m for m in missing)
    arc = bytearray(primitive_stream('arcs', [dict(x=0, y=0, radius=1, start_angle=0,
        end_angle=90, width=.2, layer='Top Layer')]))
    struct.pack_into('<d', arc, 5 + 25, float('nan'))
    rows, missing = decode_board_primitive_stream(bytes(arc), 1)
    assert not rows and any('nonfinite geometry' in m for m in missing)


def test_non_copper_drawing_records_do_not_enter_copper_graph(tmp_path):
    path, streams = synthetic_board(tmp_path)
    streams['Tracks6/Data'] += primitive_stream('tracks', [dict(x1=20, y1=20, x2=30, y2=20,
        width=.5, layer='Top Overlay')])
    streams['Arcs6/Data'] += primitive_stream('arcs', [dict(x=20, y=20, radius=5, width=.5,
        start_angle=0, end_angle=180, layer='Mechanical 1')])
    streams['Tracks6/UnsupportedCopperData'] = b'unknown extension'
    write_streams(path, streams)
    result = read_board_copper(path)
    assert len(result['tracks']) == 1 and len(result['arcs']) == 1
    assert result['provenance']['excluded_non_copper_primitives'] == 2
    assert any('unparsed board stream' in m for m in result['missing'])
    assert any('Header record count mismatch' in m for m in result['missing'])


def test_missing_metadata_and_er_are_explicit(tmp_path):
    path, streams = synthetic_board(tmp_path)
    streams['Nets6/Data'] = b''
    streams['Board6/Data'] = params(LAYER1NAME='Top', LAYER1NEXT=32, LAYER1DIELHEIGHT='1mm',
                                  LAYER32NAME='Bottom', LAYER32NEXT=0, LAYER32COPTHICK='0.035mm')
    del streams['Pads6/Data']
    write_streams(path, streams)
    result = read_board_copper(path)
    assert not result['complete']
    assert any('unresolved net' in x for x in result['missing'])
    assert any('Pads6/Data absent' in x for x in result['missing'])
    assert all('er' not in l for l in result['layers'])
    assert any('Er unavailable' in x for x in result['missing'])


def live_snapshot(path):
    value = empty_snapshot(str(path), source='live')
    value.update(complete=True, coverage={k: 'complete' for k in ('tracks', 'arcs', 'vias', 'pads', 'regions', 'layers')})
    return value


def test_native_adapter_prefers_fresh_memory_even_when_disk_exists(tmp_path, monkeypatch):
    path, _ = synthetic_board(tmp_path)
    calls = []
    async def native(command, params):
        calls.append((command, params))
        return {'data': live_snapshot(path)}
    monkeypatch.setattr('suite_pcb_primitives.read_board_copper', lambda p: pytest.fail('must not read disk'))
    result = asyncio.run(capture_copper_snapshot(str(path), native, open_in_memory=True))
    assert result['complete'] and result['source'] == 'live'
    assert calls == [('pcb.get_copper_snapshot', {'board_path': str(path)})]
    assert result['provenance']['consistency'] == 'single_native_handler'


@pytest.mark.parametrize('known_open', [True, None])
def test_native_failure_does_not_silently_use_saved_file(tmp_path, known_open):
    path, _ = synthetic_board(tmp_path)
    async def native(command, params):
        raise RuntimeError('native extraction failed')
    result = asyncio.run(capture_copper_snapshot(str(path), native, open_in_memory=known_open))
    assert result['source'] == 'unavailable'
    assert not result['complete'] and not result['tracks']
    assert any('fallback refused' in x for x in result['missing'])


def test_native_wrong_board_identity_is_rejected(tmp_path):
    path, _ = synthetic_board(tmp_path)
    async def native(command, params):
        return live_snapshot('D:\\unrelated.PcbDoc')
    result = asyncio.run(capture_copper_snapshot(str(path), native))
    assert result['source'] == 'unavailable'
    assert any('identity mismatch' in x for x in result['missing'])


def test_confirmed_closed_board_allows_saved_file_fallback(tmp_path):
    path, _ = synthetic_board(tmp_path)
    async def native(command, params):
        raise RuntimeError('not loaded')
    result = asyncio.run(capture_copper_snapshot(str(path), native, open_in_memory=False))
    assert result['source'] == 'file' and result['tracks']
    result = asyncio.run(capture_copper_snapshot(str(path), prefer_live=False, open_in_memory=True))
    assert result['source'] == 'unavailable'


def test_validation_and_content_fingerprint_ignore_only_ephemeral_changes(tmp_path):
    value = live_snapshot(tmp_path / 'fixture.PcbDoc')
    value['pads'] = [dict(id='address1', component='U1', pad_name='1', net_name='N', layer='Top Layer',
        x_mm=1, y_mm=2, width_mm=1, height_mm=1, shape='round', rotation_deg=0)]
    value['components'] = [dict(id='component1', designator='U1', pad_ids=['address1'])]
    value['holes'] = [dict(id='hole1', owner_id='address1', x_mm=1, y_mm=2, diameter_mm=.3)]
    value['nets'] = ['N', 'GND']
    other = copy.deepcopy(value)
    other['source'] = 'file'; other['provenance'] = {'captured_at': 100}
    other['board_identity'] = 'new-address'
    other['pads'][0]['id'] = 'new-pad'
    other['components'][0].update(id='new-component', pad_ids=['new-pad'])
    other['holes'][0].update(id='new-hole', owner_id='new-pad')
    other['nets'].reverse()
    assert content_fingerprint(value) == content_fingerprint(other)
    assert BoardCopperSnapshot.from_dict(value).fingerprint() == content_fingerprint(value)
    other['pads'][0]['x_mm'] += .001
    assert content_fingerprint(value) != content_fingerprint(other)
    other['pads'][0]['x_mm'] = float('nan')
    with pytest.raises(ValueError, match='numeric geometry'):
        validate_snapshot(other)
    value['missing'].append('unknown void')
    with pytest.raises(ValueError, match='Complete snapshot'):
        validate_snapshot(value)


def test_native_source_has_loaded_only_identity_checks_and_private_dispatch():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    handler = source.split('Function Copper_LoadedBoard(', 1)[1].split('Procedure Copper_Push', 1)[0]
    assert 'ResolveTargetBoard(Path)' in handler
    assert 'Client.OpenDocument' not in handler and 'ResolvePCBBoard(' not in handler
    main = (root / 'eda-agent/scripts/altium/Main.pas').read_text(encoding='utf-8')
    resolver = main.split('Function ResolveTargetBoard(',1)[1].split('Function RequireTargetPCBBoard(',1)[0]
    assert 'Client.GetDocumentByPath(TargetBoardPath)' in resolver
    assert 'Client.OpenDocument' not in resolver and 'RequireTargetPCBBoard(' not in resolver
    assert 'ExpandFileName(Result.FileName)' in resolver and "If TargetBoardPath = '' Then" in resolver
    assert "'get_copper_snapshot':" in source and "'focus_copper_violation':" in source
    assert '"poured":false' in source
    assert 'Region.Kind = eRegionKind_BoardCutout' in source
    assert 'Region.Kind = 2' not in source


def test_live_diagnostic_counts_survive_adapter_without_claiming_coverage(tmp_path):
    value = live_snapshot(tmp_path / 'fixture.PcbDoc')
    value.update(complete=False, missing=['unreadable copper primitive type 1 accessor geometry'],
                 missing_evidence_events=3672,
                 missing_counts=[{'reason': 'unreadable copper primitive type 1 accessor geometry',
                                  'occurrences': 3672}])
    value['coverage']['arcs'] = 'partial'
    value['capture_metrics'] = {'elapsed_ms': 140,
        'profile': [{'type_accessor': '1:geometry:failed', 'calls': 3672, 'elapsed_ms': 120}]}
    async def native(command, params):
        return {'data': value}
    result = asyncio.run(capture_copper_snapshot(value['board_path'], native, open_in_memory=True))
    assert result['complete'] is False
    assert result['missing_counts'][0]['occurrences'] == 3672
    assert result['capture_metrics']['profile'][0]['calls'] == 3672
    other = copy.deepcopy(result)
    other['capture_metrics']['profile'][0]['elapsed_ms'] = 9999
    other['native_snapshot_token'] = 'another-request'
    assert content_fingerprint(other) == content_fingerprint(result)


def test_live_snapshot_filters_graphics_before_electrical_accessors():
    source = (Path(__file__).resolve().parents[1] / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    handler = source.split('Function PCB_GetCopperSnapshot(', 1)[1].split('Function PCB_FocusCopperViolation(', 1)[0]
    classify = handler.split('While Obj <> Nil Do', 1)[1].split("Stage := 'geometry'", 1)[0]
    excluded = classify.index('Inc(ExcludedNonCopper)')
    assert excluded < classify.index('GetLayerString(LayerId)')
    assert excluded < classify.index('Copper_TypedNet(Obj, ObjectType)')
    assert excluded < classify.index('Copper_TypedKeepout(Obj, ObjectType)')
    assert ' Obj.Net' not in handler and ' Obj.IsKeepout' not in handler
    loop_body = handler.split('While Obj <> Nil Do', 1)[1].split('Finally Board.BoardIterator_Destroy', 1)[0]
    protected = loop_body.split('Try', 1)[1].split('Finally', 1)[0]
    assert 'Continue;' not in protected and 'Obj := Iter.NextPCBObject' not in protected
    assert 'SkipPrimitive := True' in protected and protected.count('If Not SkipPrimitive Then Begin') == 2
    assert 'AddFilter_LayerSet(AllLayers)' not in handler
    assert 'Copper_Push(Missing,' not in handler
    assert 'If Missing.Count = 0 Then CoverageState' in handler
    assert 'type_accessor' in handler and ':failed' in handler
    diagnostic = source.split('Procedure Copper_Missing(', 1)[1].split('Procedure Copper_Profile(', 1)[0]
    assert 'Inc(Total)' in diagnostic and 'Items.Count >= 128' in diagnostic
    progress = source.split('Procedure Copper_Progress(', 1)[1].split('Function Copper_TypedNet(', 1)[0]
    assert progress.index('If Not FileExists(MarkerPath) Then Exit') < progress.index('WriteFileContent(')
    assert "ExtractJsonValue(ReadFileContent(MarkerPath), 'started_ms')" in progress
    assert "JsonInt('started_ms', Started)" not in progress


def test_mechanical_counts_cover_v7_and_component_graphics_without_classic_sets():
    source = (Path(__file__).resolve().parents[1] / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    scanner = source.split('Function PCB_CountPrimitiveLayers(', 1)[1].split('Function PCB_CountPrimitivesOnLayer(', 1)[0]
    assert 'AddFilter_LayerSet' not in scanner and 'AddFilter_ObjectSet' not in scanner
    assert 'Comp.GroupIterator_Create' in scanner and 'Comp.GroupIterator_Destroy' in scanner
    assert 'PCB_CountLayerPrimitive(Child, Seen, LayerIds, Counts)' in scanner
    count_one = source.split('Procedure PCB_CountLayerPrimitive(', 1)[1].split('Function PCB_CountPrimitiveLayers(', 1)[0]
    assert 'Seen.IndexOf(Key)' in count_one and 'IntToStr(Prim.Layer)' in count_one
    listing = source.split('Function PCB_GetMechLayerNames(', 1)[1].split('Function RectsOverlap(', 1)[0]
    assert 'Lyr := MechLayerFromNumber(Num)' in listing
    assert 'Lyr := LayerObj.LayerID' not in listing
    assert 'primitive_counts_complete' in listing and 'Prims := -1' in listing
    assert 'If Not Enabled Then Continue' not in listing
    assert listing.count('PCB_CountPrimitiveLayers(Board, LayerIds, Counts)') == 1


def test_native_proof_is_private_deterministic_and_checked_before_first_edit():
    source = (Path(__file__).resolve().parents[1] / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    capture = source.split('Function PCB_GetCopperSnapshot(', 1)[1].split('Function PCB_FocusCopperViolation(', 1)[0]
    assert capture.index('If Not IsValidRequestId(RequestId)') < capture.index("'copper_snapshot_' + RequestId")
    assert capture.index('If FileExists(ProofPath)') < capture.index('WriteFileContent(ProofPath, Item)')
    proof = capture.split("Item := '{\"schema\":1", 1)[1].split('If Length(Item)', 1)[0]
    assert 'elapsed_ms' not in proof and 'ProfileJson' not in proof and 'RequestId' not in proof
    assert "JsonStr('native_snapshot_json', Item)" not in capture
    assert "JsonStr('native_snapshot_token', RequestId)" in capture
    assert '"capture_metrics":' in capture
    apply = source.split('Function PCB_ApplyCopperRules(', 1)[1]
    assert apply.index('If Not IsValidRequestId(ProofToken)') < apply.index('Expected := Trim(ReadFileContent(ProofPath))')
    assert apply.index('Fresh := Trim(ReadFileContent(ProofPath))') < apply.index('Fresh <> Expected')
    assert "ExtractJsonValue(Reply, 'data')" not in apply
    assert 'If Copy(Reply, 1, Length(SnapshotPrefix)) = SnapshotPrefix Then FreshToken := RequestId' in apply
    assert apply.index('Fresh <> Expected') < apply.index('PCBServer.PreProcess')
    wrapper = (Path(__file__).resolve().parents[1] / 'eda_stdio.py').read_text(encoding='utf-8')
    boundary = wrapper.split('async def _suite_copper_apply_rules(', 1)[1].split('# Keep private', 1)[0]
    assert "re.fullmatch(r'[A-Za-z0-9_-]{1,64}', expected_native_snapshot_token)" in boundary
    assert boundary.index('Invalid native copper snapshot token') < boundary.index('send_command_async(')
