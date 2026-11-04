"""Extract sourced package models for layout__capture_context; never edit CAD."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from suite_package_geometry import extract_package_models


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture',required=True,type=Path,help='Private native layout capture/context JSON')
    p.add_argument('--library',action='append',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    if a.output.exists():
        p.error('Choose a new report path; existing evidence is never overwritten')
    d=json.loads(a.capture.read_text(encoding='utf-8'))
    context=d.get('context',d)
    snapshot=context.get('copper_snapshot') or d.get('snapshot')
    if not isinstance(snapshot,dict) or snapshot.get('source')!='live':
        p.error('A native live copper snapshot is required for pad identity evidence')
    result=extract_package_models(snapshot,a.library)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'models':len(result['package_geometry']),'missing':len(result['missing']),
                      'output':str(a.output),'fabrication_status':result['fabrication_status']}))


if __name__=='__main__':
    main()
