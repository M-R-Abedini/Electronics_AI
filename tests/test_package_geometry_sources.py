"""Synthetic binary-library inputs; never counted as real PCB acceptance."""
import copy
import pytest
from suite_pcblib_codec import write_library, _take, _params, _param_bytes, block
from suite_ole import read_streams, write_streams
from suite_package_geometry import extract_package_models
from eda_agent.design.copper_snapshot import empty_snapshot
from eda_agent.design.placement_evidence import transform_point


def fixture(tmp_path, *, role=True, closed=True, side='top'):
    path=tmp_path/'synthetic.PcbLib'
    points=[[-1,-1],[2,-1],[2,1],[-1,1]]
    edges=list(zip(points,points[1:]+points[:1]))
    fp={'name':'SYNTHETIC','pads':[{'designator':str(i+1),'x':i,'y':0,'width':.6,'height':.6,
        'shape':'rectangle','rotation':0,'layer':'Top Layer'} for i in range(2)],
        'tracks':[{'x1':a[0],'y1':a[1],'x2':b[0],'y2':b[1],'width':.05,
                   'layer':'Mechanical 15'} for a,b in (edges if closed else edges[:-1])]}
    write_library(path,[fp])
    streams=read_streams(path)
    if role:
        header,offset=_take(streams['Library/Data'])
        params=_params(header); params['LAYER71MECHKIND']='CourtyardTop'
        streams['Library/Data']=block(_param_bytes(list(params.items())))+streams['Library/Data'][offset:]
        write_streams(path,streams)
    snapshot=empty_snapshot(str(tmp_path/'synthetic.PcbDoc'),source='live')
    c={'id':'1','designator':'J1','footprint':'SYNTHETIC','x_mm':10,'y_mm':20,'rotation_deg':90,'side':side}
    snapshot['components']=[c]
    snapshot['pads']=[]
    for i in range(2):
        x,y=transform_point([i,0],c)
        snapshot['pads'].append({'id':str(i+10),'component':'J1','pad_name':str(i+1),'x_mm':x,'y_mm':y,
            'width_mm':.6,'height_mm':.6,'shape':'rectangle','rotation_deg':90,'layer':'TopLayer'})
    return path,snapshot,points


@pytest.mark.parametrize('side',['top','bottom'])
def test_sourced_closed_contour_with_live_pad_identity(tmp_path,side):
    path,snapshot,points=fixture(tmp_path,side=side)
    saved=path.read_bytes(); before=copy.deepcopy(snapshot)
    result=extract_package_models(snapshot,[path])
    model=result['package_geometry']['J1']
    assert len(model['courtyard_local'])==len(points)
    for observed,expected in zip(sorted(model['courtyard_local']),sorted(points)):
        assert observed==pytest.approx(expected,abs=.00000254), 'Native coordinate encoding must be preserved'
    assert model['courtyard_source']['kind']=='measured_geometry'
    assert model['courtyard_source']['pad_identity_witness']['pad_count']==2
    assert result['fabrication_profile']=={} and not result['native_edits']
    assert path.read_bytes()==saved and snapshot==before


@pytest.mark.parametrize('case',['no_role','open_contour','pad_shift','shape_change','duplicate_pad','complex_stack'])
def test_no_qualified_model_from_incomplete_or_mismatched_evidence(tmp_path,case):
    path,snapshot,_=fixture(tmp_path,role=case!='no_role',closed=case!='open_contour')
    if case=='pad_shift': snapshot['pads'][0]['x_mm']+=.01
    if case=='shape_change': snapshot['pads'][0]['shape']='round'
    if case=='duplicate_pad': snapshot['pads'][1]['pad_name']='1'
    if case=='complex_stack': snapshot['pads'][0]['geometry_complete']=False
    result=extract_package_models(snapshot,[path])
    assert result['package_geometry']=={}
    assert len(result['missing'])==1


def test_duplicate_matching_sources_are_ambiguous(tmp_path):
    a=tmp_path/'a'; b=tmp_path/'b'; a.mkdir(); b.mkdir()
    path,snapshot,_=fixture(a); other,_,_=fixture(b)
    result=extract_package_models(snapshot,[path,other])
    assert not result['package_geometry']
    assert 'unique' in result['missing'][0]['reason']


def test_concurrent_library_change_is_refused(tmp_path,monkeypatch):
    import suite_package_geometry as module
    path,snapshot,_=fixture(tmp_path)
    original=module.read_streams
    def read_then_change(*args,**kw):
        result=original(*args,**kw)
        path.write_bytes(path.read_bytes()+b'concurrent-change')
        return result
    monkeypatch.setattr(module,'read_streams',read_then_change)
    with pytest.raises(ValueError,match='changed during'):
        extract_package_models(snapshot,[path])
