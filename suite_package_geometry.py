"""Read-only, provenance-bound courtyard import from explicitly supplied PcbLibs.

SPDX-License-Identifier: MIT
No pad/overlay bounding box is promoted to a courtyard.
"""
import copy
import hashlib
import math
from pathlib import Path
import re
from collections import defaultdict

from suite_ole import read_streams
from suite_pcblib_codec import _take, _params, _component_entries, _unicode_parameter
from suite_pcb_primitives import decode_component_stream, LAYER_NAMES
from eda_agent.design.placement_evidence import inverse_transform_point
from eda_agent.design.copper_geometry import region_geometry_valid

# Bounds coordinate serialization, not a design spacing or a Tier 1 epsilon.
COORDINATE_ENCODING_BOUND_MM = .0000031


def _closed_courtyard(footprint, layer):
    for section in ('arcs','fills','regions','pads'):
        if any(p.get('layer') == layer for p in footprint.get(section, [])):
            raise ValueError('Courtyard contains unsupported curved/filled or pad geometry')
    edges = [((p['x1'],p['y1']),(p['x2'],p['y2']))
             for p in footprint.get('tracks',[]) if p.get('layer') == layer]
    if not edges or len(edges) > 10000:
        raise ValueError('A bounded closed courtyard line contour is required')
    graph = defaultdict(list)
    for a,b in edges:
        if a == b or b in graph[a]:
            raise ValueError('Degenerate or duplicate courtyard edge')
        graph[a].append(b); graph[b].append(a)
    if any(len(v) != 2 for v in graph.values()):
        raise ValueError('Courtyard contour has open or branching vertices')
    start = min(graph); result = [start]; previous = None; current = start
    while True:
        following = next(p for p in sorted(graph[current]) if p != previous)
        if following == start:
            break
        if following in result:
            raise ValueError('Courtyard is not one simple contour')
        result.append(following); previous,current = current,following
    if len(result) != len(graph) or not region_geometry_valid({'outline':result}):
        raise ValueError('Courtyard is disconnected or self-intersecting')
    return [list(p) for p in result]


def _verify_pad_identity(component, live_pads, footprint):
    def indexed(rows, key):
        out = {}
        for p in rows:
            name=str(p.get(key,''))
            if not name or name in out:
                raise ValueError('Pad identities are blank or ambiguous')
            out[name]=p
        return out
    live=indexed(live_pads,'pad_name'); saved=indexed(footprint.get('pads',[]),'designator')
    if not live or set(live) != set(saved):
        raise ValueError('Library and live footprint pad identity sets differ')
    worst = 0.0
    for name,p in live.items():
        q=saved[name]
        if p.get('geometry_complete') is False or q.get('stack_mode','simple') != 'simple':
            raise ValueError('Complex/partial pad stacks need a full layer-aware identity witness')
        x,y=inverse_transform_point((p['x_mm'],p['y_mm']),component)
        errors=[abs(x-q['x']),abs(y-q['y']),abs(p['width_mm']-q['width']),abs(p['height_mm']-q['height'])]
        worst=max(worst,*errors)
        if max(errors) > COORDINATE_ENCODING_BOUND_MM:
            raise ValueError('Live pads differ from the library geometry')
        shape=q['shape']
        if shape=='round' and q['width']!=q['height']:
            shape='oval'
        if shape not in {'round','oval','rectangle'} or p.get('shape')!=shape:
            raise ValueError('Pad shape/corner identity needs an exact supported witness')
        # Footprint rigid transforms must preserve the pad rotation too.
        expected=(component['rotation_deg'] + (q.get('rotation',0) if component['side']=='top'
                    else -q.get('rotation',0))) % 360
        if shape!='round' and abs((p.get('rotation_deg',0)-expected+90)%180-90) > .000001:
            raise ValueError('Live pad rotation differs from the library')
    return {'pad_count':len(live),'maximum_coordinate_difference_mm':worst,
            'coordinate_encoding_bound_mm':COORDINATE_ENCODING_BOUND_MM,
            'qualification':'identity/encoding check; not placement epsilon or fabrication approval'}


def extract_package_models(snapshot, library_paths):
    """Return qualified models plus explicit per-component omissions; never edit."""
    from eda_agent.design.copper_snapshot import validate_snapshot
    validate_snapshot(snapshot)
    if len(snapshot['components'])>2000 or len(snapshot['pads'])>100000 or not 1<=len(library_paths)<=64:
        raise ValueError('Package extraction exceeds bounded input size')
    components=snapshot['components']
    by_ref=defaultdict(list)
    for p in snapshot['pads']:
        by_ref[p.get('component','')].append(p)
    candidates=defaultdict(list); diagnostics=[]; dependencies=[]
    for value in library_paths:
        path=Path(value).resolve()
        if path.suffix.lower() != '.pcblib' or not path.is_file():
            raise ValueError('Explicit existing .PcbLib files required')
        payload=path.read_bytes()
        if len(payload) > 128*1024**2:
            raise ValueError('Library input exceeds size bound')
        file_digest=hashlib.sha256(payload).hexdigest()
        dependencies.append({'path':str(path),'sha256':file_digest})
        # Ignore embedded model payloads; this helper only needs typed contours.
        import olefile
        with olefile.OleFileIO(str(path)) as ole:
            names=['/'.join(p) for p in ole.listdir(streams=True,storages=False)]
        sections={n.rsplit('/',1)[0] for n in names if n.count('/')==1 and n.endswith('/Parameters')}
        selected=[n for n in names if n in {'Library/Data','SectionKeys'} or
                  n.count('/')==1 and n.rsplit('/',1)[1] in
                  {'Data','Parameters','WideStrings','UniqueIDPrimitiveInformation','PrimitiveGuids'}
                  and n.rsplit('/',1)[0] in sections]
        streams=read_streams(path,selected,max_stream_bytes=32*1024**2,max_total_bytes=64*1024**2)
        header=_params(_take(streams['Library/Data'])[0])
        roles=[LAYER_NAMES[int(m.group(1))] for key,v in header.items()
               if (m:=re.fullmatch(r'LAYER(\d+)MECHKIND',key)) and v=='CourtyardTop'
               and int(m.group(1)) in LAYER_NAMES]
        if len(roles) != 1:
            diagnostics.append({'library':str(path),'status':'unknown','reason':'No single explicit canonical CourtyardTop layer role'})
            continue
        for section,params,data_name in _component_entries(streams):
            name=_unicode_parameter(params,'PATTERN',data_name)
            if not any(c.get('footprint')==name for c in components):
                continue
            try:
                prefix=section+'/'
                extra={k[len(prefix):]:v for k,v in streams.items() if k.startswith(prefix)}
                fp=decode_component_stream(extra['Data'],extra.get('WideStrings',b''),extra)
                if fp.get('opaque_records'):
                    raise ValueError('Unsupported footprint primitives prevent contour qualification')
                ring=_closed_courtyard(fp,roles[0])
                candidates[name].append({'ring':ring,'footprint':fp,'path':str(path),
                    'sha256':file_digest,'layer':roles[0]})
            except (ValueError,KeyError) as error:
                diagnostics.append({'library':str(path),'footprint':name,'status':'unknown','reason':str(error)})
    models={}; missing=[]
    for c in components:
        options=candidates.get(c.get('footprint'),[])
        if len(options)!=1:
            missing.append({'reference':c['designator'],'footprint':c.get('footprint'),
                            'reason':'No unique qualified library contour'})
            continue
        source=options[0]
        try:
            witness=_verify_pad_identity(c,by_ref[c['designator']],source['footprint'])
        except (ValueError,KeyError) as error:
            missing.append({'reference':c['designator'],'footprint':c.get('footprint'),'reason':str(error)})
            continue
        models[c['designator']]={'courtyard_local':copy.deepcopy(source['ring']),
            'courtyard_source':{'kind':'measured_geometry','id':source['path']+'#'+c['footprint'],
                'sha256':source['sha256'],'layer':source['layer'],
                'boundary_interpretation':'declared_courtyard_centerline','pad_identity_witness':witness}}
    # Detect concurrent source edits instead of binding a mixed read to an old hash.
    for dep in dependencies:
        if hashlib.sha256(Path(dep['path']).read_bytes()).hexdigest()!=dep['sha256']:
            raise ValueError('Library changed during extraction')
    return {'package_geometry':models,'missing':missing,'diagnostics':diagnostics,
            'library_dependencies':dependencies,'fabrication_profile':{},
            'fabrication_status':'unknown; not supplied','native_edits':False}
