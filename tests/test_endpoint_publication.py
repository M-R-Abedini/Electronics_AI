"""Transient sharing denial cannot be mistaken for no queue owner."""
import json
from pathlib import Path
import pytest
import engine_queue as queue

@pytest.mark.parametrize('denials', [2, 5])
def test_windows_endpoint_read_is_bounded_and_never_reports_absent(tmp_path, monkeypatch, denials):
    path=tmp_path/'endpoint.json'
    record={'protocol':queue.PROTOCOL,'port':1234,'pid':77,'started':1.0,'token':'a'*64}
    path.write_text(json.dumps(record))
    original=Path.read_text; calls=[]
    def read(self,*args,**kwargs):
        if self==path:
            calls.append(1)
            if len(calls)<=denials: raise PermissionError('sharing denial')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    monkeypatch.setattr(queue.sys,'platform','win32')
    monkeypatch.setattr(queue.time,'sleep',lambda _:None)
    if denials==5:
        with pytest.raises(PermissionError):queue._endpoint(tmp_path)
        assert len(calls)==5
    else:
        assert queue._endpoint(tmp_path)==record and len(calls)==3
