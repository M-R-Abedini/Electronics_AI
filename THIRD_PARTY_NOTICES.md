# License scope

This repository contains code under several licenses. The root [MIT license](LICENSE)
covers original suite coordination, setup, deployment and tests; it does not
relicense bundled sources or code adapted from them.

| Source | License | Included terms and attribution |
| --- | --- | --- |
| `eda-agent/` | Apache-2.0 | [LICENSE](eda-agent/LICENSE), [NOTICE](eda-agent/NOTICE) |
| Native ANSI validation (`suite_json_guard.py`) | MIT | [attribution and terms](notices/batch-safety-MIT.txt) |
| Native library codecs and service (`suite_*lib*.py`, `suite_ole.py`, `suite_pcb_primitives.py`) | MIT | [LICENSE](LICENSE); independently written from observed interfaces and synthetic file probes |

The ANSI validation function in `suite_json_guard.py` is adapted from
`coffeenmusic/altium-mcp` batch_safety.py with its MIT attribution retained.
The historical server, namespace adapters, native units and execution paths
have been removed. EDA sources retain their Apache-2.0 headers and notice.

[UPSTREAM.json](UPSTREAM.json) records the imported upstream commits and license
locations. Those commits identify the original snapshots, not the current suite
revision. Local modifications are recorded in [MODIFICATIONS.md](MODIFICATIONS.md).
Retain upstream copyright notices and mark modified Apache-2.0 files.
The suite deployment includes these license texts, the EDA notice and this scope
document alongside the installed sources.

The retired external library implementation is absent from the current source
and distribution. Historical tags and prior releases retain their original
licenses; this change does not relicense or rewrite them. Library schemas in `contracts/` describe the current native Python interfaces. The sanitized PCB
default-board parameter resource was generated from our synthetic empty library
saved by Altium; its provenance is recorded with the data. It was not obtained
from the retired implementation or its assets. The Windows ANSI best-fit
resource was generated independently through Microsoft's
`WideCharToMultiByte(1252, 0)` API. Its provenance is also recorded with the data.
The original Python implementation does not incorporate retired implementation
source.

`olefile` is a runtime dependency under its BSD license. Its distribution
metadata and license remain with installed wheels. Python standard-library
`zlib` retains the interpreter distribution's third-party notices.

Dependencies retain their own licenses. This document identifies the bundled
source licenses; it does not grant rights to Altium Designer or certify every
possible downstream packaging arrangement.
