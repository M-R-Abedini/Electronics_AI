# SPDX-License-Identifier: MIT
"""Locate and fingerprint the external tools the placer depends on.

Every run report records the exact tool versions and hashes (architecture
4.6), so this module is the single place that knows where KiCad, Java and the
pinned Freerouting jar live.  Nothing here is cached across processes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Freerouting 2.5.0 release jar.  A different build is accepted only after the
# F0a experiment table has been repeated with it (architecture 7.3).
FREEROUTING_VERSION = "2.5.0"
FREEROUTING_SHA256 = "f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7"
KICAD_VERSION = "10.0.8"

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNTIME_DIR = REPO_ROOT / ".runtime"


class ToolchainError(RuntimeError):
    """A required external tool is missing or is not the pinned build."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class KiCad:
    root: Path
    python: Path
    cli: Path
    version: str


@dataclass(frozen=True)
class Freerouting:
    jar: Path
    java: Path
    java_version: str
    sha256: str
    version: str = FREEROUTING_VERSION


def _kicad_root() -> Path:
    env = os.environ.get("ELECTRONICS_AI_KICAD")
    candidates = [Path(env)] if env else []
    candidates.append(Path(r"C:\Program Files\KiCad\10.0"))
    for root in candidates:
        if (root / "bin" / "kicad-cli.exe").is_file():
            return root
    raise ToolchainError("KiCad 10 not found; set ELECTRONICS_AI_KICAD to its install folder")


@cache
def kicad() -> KiCad:
    root = _kicad_root()
    cli = root / "bin" / "kicad-cli.exe"
    python = root / "bin" / "python.exe"
    if not python.is_file():
        raise ToolchainError(f"KiCad bundled Python missing: {python}")
    out = subprocess.run([str(cli), "version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         timeout=60, check=False)
    version = out.stdout.strip()
    if version != KICAD_VERSION:
        raise ToolchainError(f"KiCad {KICAD_VERSION} required, found {version or 'unknown'}")
    return KiCad(root=root, python=python, cli=cli, version=version)


def _java() -> Path:
    env = os.environ.get("ELECTRONICS_AI_JAVA")
    if env:
        return Path(env)
    prefix = os.environ.get("CONDA_PREFIX")  # set inside `pixi run`
    if prefix:
        for rel in ("Library/bin/java.exe", "Library/lib/jvm/bin/java.exe", "bin/java.exe"):
            if (Path(prefix) / rel).is_file():
                return Path(prefix) / rel
    found = shutil.which("java")
    if not found:
        raise ToolchainError("Java not found; run through `pixi run` (openjdk 25)")
    return Path(found)


@cache
def freerouting() -> Freerouting:
    jar = Path(os.environ.get("ELECTRONICS_AI_FREEROUTING_JAR")
               or RUNTIME_DIR / "tools" / "freerouting" / f"freerouting-{FREEROUTING_VERSION}.jar")
    if not jar.is_file():
        raise ToolchainError(f"Freerouting jar missing: {jar}")
    digest = sha256_file(jar)
    if digest != FREEROUTING_SHA256:
        raise ToolchainError(f"Freerouting jar SHA-256 mismatch: {digest}")
    java = _java()
    out = subprocess.run([str(java), "-version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         timeout=60, check=False)
    first = (out.stderr or out.stdout).splitlines()[:1]
    return Freerouting(jar=jar, java=java, java_version=first[0] if first else "unknown", sha256=digest)
