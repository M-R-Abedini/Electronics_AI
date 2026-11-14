# SPDX-License-Identifier: MIT
"""KiCad adapter for the Board IR (any format KiCad 10 can load, KiCad 5 included).

Reading and writing go through KiCad's own ``pcbnew`` (``kicad_pcbnew_helper``)
so net classes, rules, courtyards and flips mean exactly what KiCad means.
The only thing read from the file text is the stackup, because pcbnew's SWIG
layer does not expose ``BOARD_STACKUP``.  The user's file is only ever read;
writes always go to a new path (principle P8).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..ir import (
    Board,
    Capabilities,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    Stackup,
    StackupLayer,
    derive,
)
from ..sexpr import parse
from ..toolchain import kicad, sha256_file

HELPER = Path(__file__).resolve().with_name("kicad_pcbnew_helper.py")
SIDECARS = (".kicad_pro", ".kicad_dru")
CAPABILITIES = Capabilities(can_flip=True, can_rotate_any_angle=True, can_write_copper=True, can_live_apply=False)


class KiCadAdapterError(RuntimeError):
    pass


def run_helper(*args: str | Path, timeout_s: int = 900) -> str:
    cmd = [str(kicad().python), "-I", str(HELPER), *map(str, args)]
    done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout_s,
                          check=False)
    if done.returncode != 0:
        noise = "swig/python detected a memory leak"
        err = "\n".join(line for line in done.stderr.splitlines() if noise not in line)
        raise KiCadAdapterError(f"pcbnew helper '{args[0]}' failed: {err.strip()[-1500:]}")
    return done.stdout


# --------------------------------------------------------------------------
# stackup (file text)
# --------------------------------------------------------------------------
_STACK_TYPES = {"copper": "copper", "core": "core", "prepreg": "prepreg"}


def read_stackup(text: str) -> Stackup:
    root = parse(text)
    setup = root.first("setup")
    node = setup.first("stackup") if setup is not None else None
    if node is None:
        return Stackup(source="missing")
    layers = []
    for layer in node.children("layer"):
        kind = str(layer.value("type", default="") or "").lower()
        thick = [float(t[1]) for t in layer.children("thickness") if len(t) > 1]
        eps = layer.floats("epsilon_r")
        loss = layer.floats("loss_tangent")
        mapped = _STACK_TYPES.get(kind) or ("mask" if "mask" in kind else "silk" if "silk" in kind
                                            else "paste" if "paste" in kind else "other")
        layers.append(StackupLayer(name=str(layer[1]), type=mapped,
                                   thickness_mm=round(sum(thick), 6) if thick else None,
                                   epsilon_r=eps[0] if eps else None, loss_tangent=loss[0] if loss else None,
                                   material=layer.value("material")))
    return Stackup(source="file", layers=layers)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def _pad(row: dict) -> Pad:
    return Pad(number=row["number"], net=row["net"], local=tuple(row["local"]), size=tuple(row["size"]),
               rotation=row["rotation"], shape=row["shape"], kind=row["kind"], layers=tuple(row["layers"]),
               drill=tuple(row["drill"]) if row["drill"] else None, function=row["function"],
               pin_type=row["pin_type"], uuid=row["uuid"])


def _footprint(row: dict) -> Footprint:
    cy = row["courtyard"]
    return Footprint(
        ref=row["ref"], value=row["value"], lib_id=row["lib_id"], x=row["x"], y=row["y"],
        rotation=row["rotation"], side=row["side"], locked=row["locked"],
        pads=[_pad(p) for p in row["pads"]],
        courtyard=Courtyard(source=cy["source"], polygons=[[tuple(p) for p in poly] for poly in cy["polygons"]],
                            margin_mm=cy.get("margin_mm")),
        uuid=row["uuid"], sheet=row["sheet"], description=row["description"], attributes=tuple(row["attributes"]),
    )


def _canonical_order(data: dict) -> None:
    """Fixed list order (P7): KiCad's own item order changes when a file is re-saved."""
    data["footprints"].sort(key=lambda r: (r["ref"], r["uuid"]))
    for key in ("zones", "rule_areas", "obstacles"):
        data[key].sort(key=lambda r: r["uuid"])
    data["routing"]["tracks"].sort(key=lambda t: (t["layer"], t["points"], t["width"], t["net"]))
    data["routing"]["vias"].sort(key=lambda v: (v["x"], v["y"], v["net"], v["diameter"]))


def board_from_dump(data: dict, stackup: Stackup, name: str) -> Board:
    _canonical_order(data)
    board = Board(
        name=name,
        footprints=[_footprint(r) for r in data["footprints"]],
        outline=[Outline(outer=[tuple(p) for p in o["outer"]], holes=[[tuple(p) for p in h] for h in o["holes"]])
                 for o in data["outline"]["polygons"]],
        outline_valid=data["outline"]["valid"],
        board_area_mm2=data["outline"]["area_mm2"],
        copper_layers=[CopperLayer(name=c["name"], canonical=c["canonical"], index=c["index"],
                                   declared_type=c["type"]) for c in data["copper_layers"]],
        rules=data["rules"],
        netclasses=[NetClass(**c) for c in data["netclasses"]],
        nets=[Net(name=n["name"], netclass=n["class"], pads=n["pads"]) for n in data["nets"]],
        zones=data["zones"],
        rule_areas=data["rule_areas"],
        obstacles=data["obstacles"],
        zone_fill_by_layer_mm2=data["zone_fill_by_layer_mm2"],
        stackup=stackup,
        capabilities=CAPABILITIES,
        source=data["source"],
        routing_items=data["routing_items"],
        routing=data["routing"],
        teardrops=data["teardrops"],
    )
    board.stackup.board_thickness_mm = data["rules"].get("board_thickness")
    return derive(board)


# DRC checks the placer interprets, with KiCad's default severity.  Only a project
# override below the default is kept: KiCad writes every default explicitly when
# it re-saves a project, so the raw table is not a stable fact about the design.
PLACER_DRC_CHECKS = {"courtyards_overlap": "error"}


def read_severities(project: Path) -> dict[str, str]:
    """Project overrides of the DRC checks the placer uses (``.kicad_pro`` rule_severities)."""
    if not project.is_file():
        return {}
    try:
        data = json.loads(project.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sev = ((data.get("board") or {}).get("design_settings") or {}).get("rule_severities") or {}
    return {k: v for k, v in sorted(sev.items())
            if k in PLACER_DRC_CHECKS and v != PLACER_DRC_CHECKS[k] and v in ("ignore", "warning")}


def read_board(path: str | Path) -> Board:
    path = Path(path).resolve()
    if not path.is_file():
        raise KiCadAdapterError(f"board not found: {path}")
    with tempfile.TemporaryDirectory(prefix="eai_dump_") as tmp:
        out = Path(tmp) / "dump.json"
        run_helper("dump", path, out)
        data = json.loads(out.read_text(encoding="utf-8"))
    data["source"].update({"path": str(path), "sha256": sha256_file(path)})
    stackup = read_stackup(path.read_text(encoding="utf-8", errors="replace"))
    board = board_from_dump(data, stackup, path.stem)
    board.drc_severities = read_severities(path.with_suffix(".kicad_pro"))
    # pin names the board file lacks (a board converted from another EDA) come from its schematics
    from . import kicad_sch

    found = kicad_sch.enrich(board, path)
    if found["pads_named"] or found["pads_typed"]:
        board.source["schematic_pins"] = found
    return board


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def write_board(src: str | Path, board: Board, dst: str | Path, *, strip_routing: bool = False,
                strip_zones: bool = False, unfill_zones: bool = False) -> Path:
    """Write ``board``'s placement into a new file ``dst`` (never ``src``) with its project sidecars."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if src == dst:
        raise KiCadAdapterError("refusing to overwrite the source board (principle P8)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in SIDECARS:
        if src.with_suffix(suffix).is_file():
            shutil.copyfile(src.with_suffix(suffix), dst.with_suffix(suffix))
    with tempfile.TemporaryDirectory(prefix="eai_place_") as tmp:
        placement = Path(tmp) / "placement.json"
        placement.write_text(json.dumps(board.placement()), encoding="utf-8")
        result_path = Path(tmp) / "result.json"
        mode = "2" if strip_zones else "3" if unfill_zones else "1" if strip_routing else "0"
        run_helper("place", src, placement, dst, mode, result_path)
        result = json.loads(result_path.read_text(encoding="utf-8"))
    if result["missing"]:
        raise KiCadAdapterError(f"{len(result['missing'])} footprints of the placement are not on the board")
    return dst
