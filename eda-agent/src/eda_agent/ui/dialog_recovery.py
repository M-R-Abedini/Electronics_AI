# Modified 2026-11-02 by M-R-Abedini; see root MODIFICATIONS.md.
"""Short-lived, observed capabilities for benign modal recovery only.

No bridge requests, ownership changes, keyboard fallbacks or CAD writes occur
here. A capability identifies one native session and one real button handle.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import threading
import time

import psutil

from . import windows

MAX_AGE_SECONDS = 30.0
_observations = {}
_lock = threading.Lock()
_observer = secrets.token_hex(16)


def normalise(caption):
    return re.sub(r"[&\s.]+", "", str(caption or "")).casefold()


def approved(title, class_name, caption):
    """Exact known dialog families; an OK on an arbitrary wizard is unsafe."""
    action = normalise(caption)
    if (class_name == "TDesignRuleCheckForm"
            and re.fullmatch(r"Design Rule Checker(?: \[(?:mil|mm)\])?", title or "")):
        return action in {"rundesignrulecheck", "run", "report", "cancel"}
    return (title == "Error" and class_name in {"TMessageForm", "#32770"}
            and action in {"ok", "cancel"})


def session_identity(pid):
    """Verify existing durable native ownership without claiming or resetting it."""
    import psutil
    from ..config import get_config

    process = psutil.Process(pid)
    if process.name().casefold() not in {"x2.exe", "dxp.exe", "altiumdesigner.exe"}:
        raise RuntimeError("Recovery target is not an Altium process")
    started = process.create_time()
    workspace = Path(get_config().workspace_dir).resolve()

    def marker(name):
        path = workspace / name
        if path.stat().st_size > 16384:
            raise ValueError("Oversized native identity marker")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Invalid native identity marker")
        return value

    ready, owner = marker("bridge-ready.json"), marker("bridge-owner.json")
    session = ready.get("session_id")
    if (not isinstance(session, str) or not session
            or owner.get("session_id") != session or owner.get("pid") != pid
            or owner.get("session_started_at") != started):
        raise RuntimeError("Recovery cannot verify the existing native session")
    frames = [w for w in windows.enumerate_windows(pid=pid)
              if w.class_name == "TDocumentForm"]
    if len(frames) != 1:
        raise RuntimeError("Recovery requires exactly one Altium main window")
    return {"native_session_id": session, "pid": pid,
            "process_started_at": started, "main_hwnd": frames[0].hwnd,
            "workspace": str(workspace), "observer_id": _observer}


def observe(pid, descriptions):
    """Attach capabilities only to exact, visible, enabled observed buttons."""
    try:
        session = session_identity(pid)
    except (OSError, ValueError, RuntimeError, ImportError, psutil.Error):
        return None
    now = time.monotonic()
    with _lock:
        for token, (created, _) in list(_observations.items()):
            if now - created > MAX_AGE_SECONDS:
                del _observations[token]
        for dialog in descriptions:
            if not dialog.get("enabled") or not dialog.get("visible"):
                continue
            for button in dialog["buttons"]:
                if (not button.get("enabled") or not button.get("visible")
                        or not approved(dialog["title"], dialog["class"], button["caption"])):
                    continue
                binding = {**session, "dialog_hwnd": dialog["hwnd"],
                           "dialog_title": dialog["title"], "dialog_class": dialog["class"],
                           "button_hwnd": button["hwnd"], "button_caption": button["caption"],
                           "button_class": button["class"]}
                # A fresh listing can confirm the same capability, but cannot
                # extend its lifetime. Replaced windows/buttons get new tokens.
                token = next((key for key, (_, value) in _observations.items()
                              if {k: v for k, v in value.items()
                                  if k not in {"token", "observed_at"}} == binding), None)
                if token is None:
                    token = secrets.token_hex(24)
                    binding["token"] = token
                    binding["observed_at"] = datetime.now(timezone.utc).isoformat()
                    if len(_observations) >= 256:
                        del _observations[next(iter(_observations))]
                    _observations[token] = (now, binding)
                button["recovery_binding"] = dict(_observations[token][1])
    return session


def resolve(pid, binding, caption, dialog_title=""):
    """Consume a capability and re-observe exact process/window/control identity."""
    if not isinstance(binding, dict):
        raise ValueError("Recovery binding must be an observed object")
    with _lock:
        observation = _observations.pop(binding.get("token"), None)
    if observation is None or observation[1] != binding:
        raise ValueError("Recovery observation is unknown or already consumed; list dialogs again")
    if time.monotonic() - observation[0] > MAX_AGE_SECONDS:
        raise ValueError("Recovery observation expired; list dialogs again")
    try:
        session = session_identity(pid)
    except (OSError, ValueError, RuntimeError, ImportError, psutil.Error) as exc:
        raise ValueError('Recovery native session/process cannot be verified; list dialogs again') from exc
    if any(binding.get(key) != value for key, value in session.items()):
        raise ValueError("Recovery native session/process changed; list dialogs again")
    if (normalise(caption) != normalise(binding["button_caption"])
            or (dialog_title and dialog_title.casefold() not in binding["dialog_title"].casefold())):
        raise ValueError("Recovery action does not match the observed caption")
    matches = [d for d in windows.dialogs(pid) if d.hwnd == binding["dialog_hwnd"]]
    if len(matches) != 1:
        raise ValueError("Observed recovery dialog is gone or ambiguous")
    target = matches[0]
    if (target.pid != pid or target.title != binding["dialog_title"]
            or target.class_name != binding["dialog_class"] or not target.enabled or not target.visible
            or not approved(target.title, target.class_name, caption)):
        raise ValueError("Recovery dialog identity/state changed")
    matches = [b for b in target.buttons() if normalise(b.text) == normalise(caption)]
    if (len(matches) != 1 or matches[0].hwnd != binding["button_hwnd"]
            or matches[0].text != binding["button_caption"]
            or matches[0].class_name != binding["button_class"]
            or not matches[0].enabled or not matches[0].visible
            or windows.window_pid(matches[0].hwnd) != pid):
        raise ValueError("Recovery button identity/state changed")
    return target, matches[0]
