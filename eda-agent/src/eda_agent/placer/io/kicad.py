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
import re
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
def _pad(row: dict, name_to_canon: dict[str, str] | None = None) -> Pad:
    layers = tuple(name_to_canon.get(lyr, lyr) for lyr in row["layers"]) if name_to_canon else tuple(row["layers"])
    return Pad(number=row["number"], net=row["net"], local=tuple(row["local"]), size=tuple(row["size"]),
               rotation=row["rotation"], shape=row["shape"], kind=row["kind"], layers=layers,
               drill=tuple(row["drill"]) if row["drill"] else None, function=row["function"],
               pin_type=row["pin_type"], uuid=row["uuid"])


def _footprint(row: dict, name_to_canon: dict[str, str] | None = None) -> Footprint:
    cy = row["courtyard"]
    return Footprint(
        ref=row["ref"], value=row["value"], lib_id=row["lib_id"], x=row["x"], y=row["y"],
        rotation=row["rotation"], side=row["side"], locked=row["locked"],
        pads=[_pad(p, name_to_canon) for p in row["pads"]],
        courtyard=Courtyard(
            source=cy["source"],
            polygons=[[tuple(p) for p in poly] for poly in cy["polygons"]],
            margin_mm=cy.get("margin_mm"),
            holes=[[[tuple(p) for p in hole] for hole in poly_holes] for poly_holes in cy.get("holes", [])],
        ),
        uuid=row["uuid"], sheet=row["sheet"], description=row["description"], attributes=tuple(row["attributes"]),
        sheet_path=row.get("sheet_path") or None,
        body_bounds_mm=tuple(row["body_bounds_mm"]) if row.get("body_bounds_mm") else None,
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
    name_to_canon = {c["name"]: c["canonical"] for c in data["copper_layers"]}
    board = Board(
        name=name,
        footprints=[_footprint(r, name_to_canon) for r in data["footprints"]],
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
        silkscreen_texts=data.get("silkscreen_texts", []),
        artwork=data.get("artwork", []),
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


def _upgrade_legacy_copy(src: Path, tmp_dir: Path) -> Path:
    """Upgrade a legacy KiCad board (e.g. KiCad 5 version 20171130) inside a temp copy (P8-safe)."""
    dst = tmp_dir / src.name
    shutil.copyfile(src, dst)
    for suffix in SIDECARS:
        sidecar = src.with_suffix(suffix)
        if sidecar.is_file():
            shutil.copyfile(sidecar, dst.with_suffix(suffix))
    done = subprocess.run(
        [str(kicad().cli), "pcb", "upgrade", "--force", str(dst)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False,
    )
    if done.returncode != 0:
        raise KiCadAdapterError(f"kicad-cli pcb upgrade failed: {(done.stderr or done.stdout).strip()[-1000:]}")
    return dst


def _legacy_file(src: Path) -> bool:
    with src.open("rb") as board_file:
        header = board_file.read(4096)
    version = re.search(rb"\(version\s+(\d+)\)", header)
    return version is not None and int(version[1]) < 20200101


def read_board(path: str | Path, *, include_fill: bool = False) -> Board:
    path = Path(path).resolve()
    if not path.is_file():
        raise KiCadAdapterError(f"board not found: {path}")
    upgraded = False
    with tempfile.TemporaryDirectory(prefix="eai_dump_") as tmp:
        tmp_dir = Path(tmp)
        out = tmp_dir / "dump.json"
        load_path = path
        try:
            run_helper("dump", load_path, out, "1" if include_fill else "0")
        except KiCadAdapterError:
            # Legacy KiCad 4/5 boards fail direct KiCad 10 LoadBoard; upgrade a temp copy (never src, P8).
            load_path = _upgrade_legacy_copy(path, tmp_dir)
            run_helper("dump", load_path, out, "1" if include_fill else "0")
            upgraded = True
        data = json.loads(out.read_text(encoding="utf-8"))
        stackup = read_stackup(load_path.read_text(encoding="utf-8", errors="replace"))
    data["source"].update({"path": str(path), "sha256": sha256_file(path)})
    if upgraded:
        data["source"]["upgraded_from_legacy"] = True
    board = board_from_dump(data, stackup, path.stem)
    board.drc_severities = read_severities(path.with_suffix(".kicad_pro"))
    # pin names the board file lacks (a board converted from another EDA) come from its schematics
    from . import kicad_sch

    found = kicad_sch.enrich(board, path)
    if found["pads_named"] or found["pads_typed"] or found["sheets_assigned"] or found["units_mapped"]:
        board.source["schematic_pins"] = found
    return board


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def write_board(src: str | Path, board: Board, dst: str | Path, *, strip_routing: bool = False,
                strip_zones: bool = False, unfill_zones: bool = False,
                preserve_outside_routing: bool = False,
                clean_silkscreen: bool = False, silkscreen_config: dict | None = None,
                result_out: dict | None = None) -> Path:
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
        mode = (
            "4" if preserve_outside_routing
            else "2" if strip_zones
            else "3" if unfill_zones
            else "1" if strip_routing
            else "0"
        )
        silk_arg = "0"
        if silkscreen_config is not None:
            if silkscreen_config.get("policy") != "off":
                silk_cfg_path = Path(tmp) / "silkscreen_config.json"
                silk_cfg_path.write_text(json.dumps(silkscreen_config), encoding="utf-8")
                silk_arg = str(silk_cfg_path)
        elif clean_silkscreen:
            silk_arg = "1"
        load_path = _upgrade_legacy_copy(src, Path(tmp)) if _legacy_file(src) else src
        run_helper("place", load_path, placement, dst, mode, result_path, silk_arg)
        result = json.loads(result_path.read_text(encoding="utf-8"))
    if result_out is not None:
        result_out.update(result)
    if result["missing"]:
        raise KiCadAdapterError(f"{len(result['missing'])} footprints of the placement are not on the board")
    return dst


def refill_copy(src: Path, dst: Path) -> Path:
    """Refill a working copy against its project rules and persist the exact checked geometry."""
    src, dst = src.resolve(), dst.resolve()
    if src == dst:
        raise KiCadAdapterError("refill requires a separate output path")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in SIDECARS:
        sidecar = src.with_suffix(suffix)
        if sidecar.is_file():
            shutil.copyfile(sidecar, dst.with_suffix(suffix))
    run_helper("refill", src, dst)
    return dst


def lock_routing_copy(src: Path, dst: Path) -> Path:
    """Make retained ECO copper fixed for the router, with the matching rule files."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_helper("lock_routing", src, dst, dst.with_suffix(".lock.json"))
    for suffix in SIDECARS:
        sidecar = src.with_suffix(suffix)
        if sidecar.is_file():
            shutil.copyfile(sidecar, dst.with_suffix(suffix))
    return dst
