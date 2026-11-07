# SPDX-License-Identifier: MIT
"""Run the pinned Freerouting build with the strict configuration (architecture 7.3).

Every flag has a recorded reason in the decision log, section 1.1, plus the F0a
additions (section 6): telemetry is switched off explicitly, and the optimizer
runs single-threaded so a fully routed board gives the same session twice.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..toolchain import Freerouting, freerouting


@dataclass(frozen=True)
class RouterConfig:
    neck_width_um: float  # board minimum track width from the .kicad_pro
    max_passes: int = 100  # upper bound; Freerouting stops earlier on its own convergence rule
    timeout_s: int = 1200
    automatic_neckdown: bool = False
    fanout: bool = False
    strict_drc: bool = True
    optimizer_threads: int = 1

    def flags(self) -> list[str]:
        def b(v: bool) -> str:
            return "true" if v else "false"

        return [
            f"--router.automatic_neckdown={b(self.automatic_neckdown)}",
            f"--router.fanout.enabled={b(self.fanout)}",
            f"--router.neck_width_um={self.neck_width_um:g}",
            f"--router.strict_drc={b(self.strict_drc)}",
            f"--router.autorouter.max_passes={self.max_passes}",
            f"--router.optimizer.max_threads={self.optimizer_threads}",
        ]


_PRIVACY = [
    "--gui.enabled=false",
    "--api_server.enabled=false",
    "--mcp_server.enabled=false",
    "-da",
    "--usage_and_diagnostic_data.disable_analytics=true",
    "--profile.allow_telemetry=false",
    "--profile.allow_contact=false",
]


@dataclass
class RouterResult:
    returncode: int
    seconds: float
    timed_out: bool
    session: str | None
    log: str
    command: list[str]
    passes: int = 0
    stop_reason: str = "unknown"
    pre_existing_violations: list[str] = field(default_factory=list)
    log_unrouted: int | None = None

    def summary(self) -> dict:
        data = asdict(self)
        data.pop("log")
        return data


_PASS = re.compile(r"Auto-routing pass #(\d+)")
_CONVERGED = re.compile(r"has not improved by more than [\d.]+ points since pass #\d+")
_FINAL = re.compile(r"Auto-routing stage completed:.*?\((\d+) unrouted")
_PIN_VIOLATION = re.compile(r"^\s+- (\S+ \[net [^\]]*\] <-> \S+ \[net [^\]]*\])", re.MULTILINE)


def parse_log(text: str) -> dict:
    passes = [int(m) for m in _PASS.findall(text)]
    final = _FINAL.search(text)
    if _CONVERGED.search(text):
        reason = "converged"
    elif "Skipping optimization" in text or "optimization stage" in text.lower():
        reason = "completed"
    else:
        reason = "unknown"
    return {
        "passes": max(passes) if passes else 0,
        "stop_reason": reason,
        "log_unrouted": int(final.group(1)) if final else None,
        "pre_existing_violations": sorted(set(_PIN_VIOLATION.findall(text))),
    }


def command(tool: Freerouting, dsn: Path, ses: Path, work: Path, config: RouterConfig) -> list[str]:
    return [str(tool.java), "-Djava.awt.headless=true", "-jar", str(tool.jar),
            "-de", str(dsn), "-do", str(ses), f"--user_data_path={work}",
            *_PRIVACY, *config.flags()]


def route(dsn: Path, ses: Path, work: Path, config: RouterConfig) -> RouterResult:
    tool = freerouting()
    work.mkdir(parents=True, exist_ok=True)
    cmd = command(tool, dsn, ses, work, config)
    log_path = work / "freerouting.stdout.log"
    start = time.monotonic()
    timed_out = False
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        try:
            done = subprocess.run(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=config.timeout_s, check=False)
            code = done.returncode
        except subprocess.TimeoutExpired:
            timed_out, code = True, -1
    seconds = round(time.monotonic() - start, 2)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    file_log = work / "freerouting.log"
    if file_log.is_file():
        text += "\n" + file_log.read_text(encoding="utf-8", errors="replace")
    parsed = parse_log(text)
    if timed_out:
        parsed["stop_reason"] = "timeout"
    elif parsed["passes"] >= config.max_passes:
        parsed["stop_reason"] = "pass_limit"
    return RouterResult(returncode=code, seconds=seconds, timed_out=timed_out,
                        session=str(ses) if ses.is_file() else None, log=text, command=cmd, **parsed)
