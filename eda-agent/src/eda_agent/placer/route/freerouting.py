# SPDX-License-Identifier: MIT
"""Run the pinned Freerouting build with the strict configuration (architecture 7.3).

Every flag has a recorded reason in the decision log, section 1.1, plus the F0a
and F0b additions (section 6): telemetry is switched off explicitly, the
optimizer runs single-threaded so a fully routed board gives the same session
twice, and a job time budget makes Freerouting stop gracefully and save its best
result.  A run stopped by that budget depends on CPU speed and load, so its
report says it is not reproducible (``deterministic: false``).
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
    timeout_s: int = 600  # graceful job budget: Freerouting stops and saves its best result (TIMED_OUT)
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
            f"--router.job_timeout={self.timeout_s // 3600:02d}:{self.timeout_s // 60 % 60:02d}:{self.timeout_s % 60:02d}",
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
    pass_unrouted: list[int] = field(default_factory=list)
    internal_violations: int = 0

    def summary(self) -> dict:
        data = asdict(self)
        data.pop("log")
        return data


_PASS = re.compile(r"Auto-routing pass #(\d+)")
_PASS_DONE = re.compile(r"Auto-routing pass #(\d+) .*?completed in ([\d.]+) seconds with score [\d.]+ \((\d+) unrouted")
_INTERNAL = re.compile(r"Board has (\d+) pre-existing clearance violation")
SETTLE_PASSES = 3  # full passes without beating the best count before a cut-off run counts as settled
KILL_MARGIN_S = 180  # hard kill only if Freerouting ignores its own job timeout
_TIMED_OUT = re.compile(r"finished with state: TIMED_OUT|completed with timeout")
_CONVERGED = re.compile(r"has not improved by more than [\d.]+ points since pass #\d+")
_FINAL = re.compile(r"Auto-routing stage completed:.*?\((\d+) unrouted")
_PIN_VIOLATION = re.compile(r"^\s+- (\S+ \[net [^\]]*\] <-> \S+ \[net [^\]]*\])", re.MULTILINE)


def pass_history(text: str) -> list[int]:
    """Unrouted count after each full pass; the zero-second pass that restores the best board is not one."""
    rows: dict[int, int] = {}
    for number, seconds, unrouted in _PASS_DONE.findall(text):
        if float(seconds) > 0:
            rows[int(number)] = int(unrouted)
    return [rows[k] for k in sorted(rows)]


def settled(stop_reason: str, history: list[int], final_unrouted: int | None = None) -> bool:
    """Converged, fully routed, or SETTLE_PASSES full passes in a row that did not beat the best so far."""
    if stop_reason in ("converged", "completed") or final_unrouted == 0:
        return True
    if len(history) <= SETTLE_PASSES:
        return False
    return min(history[-SETTLE_PASSES:]) >= min(history[:-SETTLE_PASSES])


def parse_log(text: str) -> dict:
    passes = [int(m) for m in _PASS.findall(text)]
    internal = _INTERNAL.search(text)
    final = _FINAL.search(text)
    if _TIMED_OUT.search(text):
        reason = "timeout"
    elif _CONVERGED.search(text):
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
        "pass_unrouted": pass_history(text),
        "internal_violations": int(internal.group(1)) if internal else 0,
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
                                  stderr=subprocess.STDOUT, timeout=config.timeout_s + KILL_MARGIN_S, check=False)
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
        parsed["stop_reason"] = "killed"
    elif parsed["stop_reason"] != "timeout" and parsed["passes"] >= config.max_passes:
        parsed["stop_reason"] = "pass_limit"
    return RouterResult(returncode=code, seconds=seconds, timed_out=timed_out,
                        session=str(ses) if ses.is_file() else None, log=text, command=cmd, **parsed)
