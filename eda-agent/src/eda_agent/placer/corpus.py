# SPDX-License-Identifier: MIT
"""Board corpus and data split (plan section 3 and decision log section 6).

dev           daily work
validation    end of a phase only
validation_ext the user's example boards, same rule as validation
test          sealed: only the hash is recorded; loading needs ``allow_sealed``
stress        not part of acceptance

Boards are referenced in place (KiCad demos, the user's example folder); the
corpus never copies them into the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .toolchain import sha256_file

DEMOS = Path(os.environ.get("ELECTRONICS_AI_KICAD", r"C:\Program Files\KiCad\10.0")) / "share" / "kicad" / "demos"
EXAMPLES = Path(os.environ.get("ELECTRONICS_AI_EXAMPLES", r"D:\project\example"))


@dataclass(frozen=True)
class Entry:
    name: str
    split: str
    path: Path
    sha256: str | None = None  # recorded for sealed boards


CORPUS: tuple[Entry, ...] = (
    Entry("stickhub", "dev", DEMOS / "stickhub" / "StickHub.kicad_pcb"),
    Entry("pic_programmer", "dev", DEMOS / "pic_programmer" / "pic_programmer.kicad_pcb"),
    Entry("royalblue54L_feather", "dev", DEMOS / "royalblue54L_feather" / "RoyalBlue54L-Feather.kicad_pcb"),
    Entry("video", "dev", DEMOS / "video" / "video.kicad_pcb"),
    Entry("ecc83", "dev", DEMOS / "ecc83" / "ecc83-pp.kicad_pcb"),
    Entry("openair_max", "validation", DEMOS / "openair-max" / "One-Air-Max.kicad_pcb"),
    Entry("coldfire", "validation", DEMOS / "kit-dev-coldfire-xilinx_5213" / "kit-dev-coldfire-xilinx_5213.kicad_pcb"),
    Entry("urti_mainboard", "validation_ext", EXAMPLES / "urti-mainboard" / "mainboard.kicad_pcb"),
    Entry("esp32_poe_m1", "validation_ext", EXAMPLES / "ESP32-POE-master" / "ESP32-POE-master" / "HARDWARE"
          / "ESP32-PoE-hardware-revision-M1" / "ESP32-PoE_Rev_M1.kicad_pcb"),
    Entry("pico_evb", "validation_ext", EXAMPLES / "PICO-EVB-main" / "PICO-EVB-main" / "HARDWARE" / "PICO-EVB_Rev_A"
          / "PICO-EVB_Rev_A.kicad_pcb"),
    Entry("luckfox_carrier", "validation_ext", EXAMPLES / "LuckFox-Core3506-Carrier" / "luckfox_core3506_adapter.kicad_pcb"),
    Entry("interf_u", "test", DEMOS / "interf_u" / "interf_u.kicad_pcb"),
    Entry("cynthion", "test", EXAMPLES / "cynthion-hardware" / "cynthion.kicad_pcb",
          "51e978f0e3c02f1b22c6e8013f2d26b309a6724749f27b39abd932545bcd5313"),
    Entry("esp32_c6_devkit_lipo", "test", EXAMPLES / "ESP32-C6-DevKit-Lipo-main" / "ESP32-C6-DevKit-Lipo-main" / "HARDWARE"
          / "ESP32-C6-DevKit-Lipo.Rev.A" / "ESP32-C6-DevKit-Lipo_Rev_A.kicad_pcb",
          "836c0e9b43caabf4fe8e32c58b865ee37eeb3dc1ed65a50b79b7a2e036776b45"),
    Entry("neo6502kbd", "test", EXAMPLES / "Neo6502kbd-main" / "Neo6502kbd-main" / "HARDWARE" / "Neo6502kbd-Rev.A"
          / "Neo6502kbd_Rev_A.kicad_pcb", "e6c487d39083b7567ae3ff70b34f9aed9dc12b30900302f524f722e082690edf"),
    Entry("cm5_minima", "stress", DEMOS / "cm5_minima" / "CM5_MINIMA_3.kicad_pcb"),
    Entry("jetson_thor", "stress", DEMOS / "jetson-agx-thor-baseboard" / "jetson-agx-thor-baseboard.kicad_pcb"),
    Entry("vme_wren", "stress", DEMOS / "vme-wren" / "vme-wren.kicad_pcb"),
    Entry("hackrf", "stress", EXAMPLES / "hackrf" / "hackrf-one.kicad_pcb"),
)

ROUNDTRIP_SET = ("stickhub", "pic_programmer", "royalblue54L_feather", "video", "ecc83", "openair_max", "coldfire",
                 "interf_u")  # plan F0b: 8 boards; interf_u is I/O only (sealed definition, plan section 3)


class SealedBoardError(PermissionError):
    pass


def entries(split: str | None = None) -> list[Entry]:
    return [e for e in CORPUS if split is None or e.split == split]


def get(name: str, *, allow_sealed: bool = False, io_only: bool = False) -> Entry:
    entry = next((e for e in CORPUS if e.name == name), None)
    if entry is None:
        raise KeyError(f"unknown corpus board {name!r}")
    if entry.split == "test" and not allow_sealed and not (io_only and name == "interf_u"):
        raise SealedBoardError(f"{name} is sealed until test #1 (plan section 3)")
    return entry


def verify_sealed() -> list[dict]:
    """Hash check of the sealed boards; reads bytes only, never parses them."""
    out = []
    for e in entries("test"):
        if e.sha256 is None:
            continue
        ok = e.path.is_file() and sha256_file(e.path) == e.sha256
        out.append({"name": e.name, "present": e.path.is_file(), "hash_matches": ok})
    return out
