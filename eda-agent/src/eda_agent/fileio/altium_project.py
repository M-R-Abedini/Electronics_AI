# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Headless Altium project reader (roadmap V1: hardware CI).

Real designs are multi-sheet: a ``.PrjPcb`` project owns several ``.SchDoc``
sheets. The authoritative sheet list lives in the sibling
``.PrjPcbStructure`` file as pipe-delimited records:

    Record=TopLevelDocument|FileName=main.SchDoc|SheetNumber=
    Record=SubDocument|FileName=power.SchDoc|...

An uncompiled project may lack the structure file. Its explicit ``[DocumentN]``
sections provide a fallback; print-view paths are ignored.
This lets ``eda-agent review`` cover a whole project, not one sheet, and
enables cross-sheet checks (e.g. a net label that appears on only one sheet
but nowhere else is a likely typo). Fully offline.
"""

from __future__ import annotations

from pathlib import Path


def read_project_sheets(prjpcb_path: str | Path) -> list[Path]:
    """Return the .SchDoc sheets of a project, resolved to absolute paths.

    Reads the sibling ``<project>.PrjPcbStructure`` for ``FileName=*.SchDoc``
    records and resolves each relative to the project directory. Missing
    files are skipped. Falls back to explicit project documents when the
    structure lists no readable schematic.
    """
    prjpcb_path = Path(prjpcb_path)
    project_dir = prjpcb_path.parent
    structure = prjpcb_path.with_suffix(".PrjPcbStructure")
    sheets: list[Path] = []
    seen: set[Path] = set()

    def add(name: str) -> None:
        if not name.lower().endswith(".schdoc"):
            return
        path = (project_dir / name.replace("\\", "/")).resolve()
        if path.is_file() and path not in seen:
            seen.add(path)
            sheets.append(path)

    if structure.is_file():
        text = structure.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            for field in line.split("|"):
                if field.startswith("FileName="):
                    add(field.partition("=")[2].strip())
    if not sheets and prjpcb_path.is_file():
        document_section = False
        for line in prjpcb_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                document_section = line.lower().startswith("[document") and line[9:-1].isdigit()
            elif document_section and line.lower().startswith("documentpath="):
                add(line.partition("=")[2].strip())
    return sheets
