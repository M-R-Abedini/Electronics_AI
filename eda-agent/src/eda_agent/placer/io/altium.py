# SPDX-License-Identifier: MIT
"""Saved Altium PCB and project sheets as input to the intent reader."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from ...fileio.altium_project import read_project_sheets
from ...fileio.altium_sch import (
    RECORD_PIN,
    read_schdoc_records,
    read_schematic_components,
)
from ..geometry import rotate
from ..ir import (
    Board,
    Capabilities,
    CopperLayer,
    Footprint,
    Net,
    Pad,
    Stackup,
    StackupLayer,
    derive,
)
from ..toolchain import sha256_file

PIN_TYPES = ("input", "bidirectional", "output", "open_collector", "passive",
             "tri_state", "open_emitter", "power")


def _schematic_metadata(project: Path) -> tuple[dict, list[dict], list[str]]:
    symbols = defaultdict(list)
    evidence, missing = [], []
    sheets = read_project_sheets(project)
    if not sheets:
        missing.append("no project schematic sheets found")
    for sheet in sheets:
        records = read_schdoc_records(sheet)
        by_owner = defaultdict(list)
        for row in records:
            owner = row.get("OwnerIndex", "")
            if row.get("RECORD") == RECORD_PIN and owner.isdecimal() and row.get("Designator"):
                by_owner[int(owner)].append(row)
        for component in read_schematic_components(sheet, records=records):
            if component["designator"]:
                symbols[component["designator"]].append((sheet, component, by_owner[component["owner_index"]]))
        evidence.append({"path": str(sheet), "sha256": sha256_file(sheet)})
    return symbols, evidence, missing


def _dimension(value: str) -> float:
    if value.endswith("mil"):
        return float(value[:-3]) * 0.0254
    if value.endswith("mm"):
        return float(value[:-2])
    raise ValueError(f"Altium position has no explicit unit: {value!r}")


def _read_intent_board(path: Path, project: Path, snapshot: dict) -> Board:
    symbols, sheets, missing = _schematic_metadata(project)
    by_ref = defaultdict(list)
    for row in snapshot["pads"]:
        if row["component"]:
            by_ref[row["component"]].append(row)
    copper = [row for row in snapshot["layers"] if "layer_id" in row]
    names = {
        row["name"]: "F.Cu" if i == 0 else "B.Cu" if i == len(copper) - 1 else f"In{i}.Cu"
        for i, row in enumerate(copper)
    }
    layers = [CopperLayer(row["name"], names[row["name"]], i) for i, row in enumerate(copper)]
    footprints, counts, matched = [], Counter(), Counter()
    for row in snapshot["components"]:
        ref = row["designator"]
        params = row["parameters"]
        x, y = _dimension(params["X"]), -_dimension(params["Y"])
        rotation = float(params["ROTATION"])
        side = {"TOP": "F", "BOTTOM": "B"}[params["LAYER"]]
        candidates = symbols.get(ref, [])
        source_id = params.get("SOURCEUNIQUEID", "").split("\\")[-1]
        exact = [entry for entry in candidates if source_id and entry[1]["unique_id"] == source_id]
        # A repeated schematic reference is ambiguous without the PCB's source ID.
        chosen = exact if source_id else candidates
        if len(chosen) == 1:
            sheet, component, records = chosen[0]
            matched["parts"] += 1
        else:
            sheet, component, records = None, {}, []
            if candidates:
                missing.append(f"{ref}: schematic reference ambiguous or source ID differs")
        pin_data = defaultdict(set)
        for record in records:
            code = record.get("Electrical", "")
            pin_type = PIN_TYPES[int(code)] if code.isdecimal() and int(code) < len(PIN_TYPES) else None
            number, name = record["Designator"], record.get("Name")
            pin_data[number].add((name if name and name != number else None, pin_type))
        pads = []
        for native in by_ref[ref]:
            number, net = native["pad_name"], native["net_name"] or None
            metadata = pin_data.get(number, set())
            function, pin_type = next(iter(metadata)) if len(metadata) == 1 else (None, None)
            matched["pads_named"] += bool(function)
            matched["pads_typed"] += bool(pin_type)
            if net:
                counts[net] += 1
            drill = native.get("drill_mm", 0)
            pads.append(Pad(
                number, net, rotate(native["x_mm"] - x, -native["y_mm"] - y, -rotation),
                (native["width_mm"], native["height_mm"]), rotation=native["rotation_deg"] - rotation,
                shape={"round": "circle", "rounded_rectangle": "roundrect"}.get(native["shape"], native["shape"]),
                kind=("thru_hole" if native["is_plated"] else "np_thru_hole") if drill else "smd",
                layers=("*.Cu",) if native["layer"] == "Multi-Layer" else (
                    (names[native["layer"]],) if native["layer"] in names else ()
                ),
                drill=(drill, drill) if drill else None, function=function, pin_type=pin_type,
            ))
        footprints.append(Footprint(
            ref, component.get("value") or component.get("lib_reference") or row["comment"],
            row["footprint"], x, y, rotation, side, params.get("LOCKED") == "TRUE", pads,
            uuid=row["id"], sheet=sheet.name if sheet else None,
            description=component.get("description", ""),
        ))
    layer_stack = [
        StackupLayer(names.get(row["name"], row["name"]), "copper" if "layer_id" in row else "dielectric",
                     row.get("thickness_mm"), row.get("er"))
        for row in snapshot["layers"]
    ]
    board = derive(Board(
        path.stem, footprints, outline_valid=False, copper_layers=layers,
        nets=[Net(net, "", counts[net]) for net in sorted(set(snapshot["nets"]) - {""})],
        stackup=Stackup("file" if layer_stack else "missing", layer_stack),
        capabilities=Capabilities(False, False, False, False),
        source={"path": str(path), "sha256": sha256_file(path), "project": str(project),
                "schematic": {"sheets": sheets, **matched}, "format": "altium_saved_intent",
                "native_coverage": snapshot["coverage"]},
    ))
    board.unknowns.extend(missing)
    board.unknowns.extend(["native PCB placement outline and fabrication rules are unavailable in this adapter",
                           "pad geometry is limited to the saved-file reader's coverage"])
    board.unknowns.extend(snapshot["missing"])
    return board


def read_intent_board(path: str | Path, project: str | Path) -> Board:
    """Join saved PCB net membership to SchDoc pins by component and pin designator."""
    from suite_pcb_primitives import read_board_copper

    path, project = Path(path).resolve(strict=True), Path(project).resolve(strict=True)
    if path.suffix.lower() != ".pcbdoc" or project.suffix.lower() != ".prjpcb":
        raise ValueError("expected .PcbDoc and .PrjPcb files")
    return _read_intent_board(path, project, read_board_copper(path))
