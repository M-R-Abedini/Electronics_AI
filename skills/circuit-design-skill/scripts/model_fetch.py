#!/usr/bin/env python3
"""Find a SPICE model on the open web and turn it into a spec entry.

The catalog in ``catalog/models.json`` lists parts whose models are published
in a stable, fetchable place.  A URL can also be supplied directly.  The tool
downloads the file, extracts the requested model, hashes it, and writes a
``models`` entry that ``design_spec.py`` / ``generate.py`` accept.

It deliberately does not claim the model is correct for a physical part.  The
entry records vendor, part, source and sha256 so a reviewer can check it.

Usage::

    python3 scripts/model_fetch.py --list
    python3 scripts/model_fetch.py --part 2N3904 --out models/
    python3 scripts/model_fetch.py --part 2N7002 --write-spec specs/buck.json --ref M1
    python3 scripts/model_fetch.py --url https://example.com/part.lib \\
        --part XYZ --vendor Acme --source https://example.com/part.lib
    python3 scripts/model_fetch.py --file downloaded.lib --part XYZ \\
        --vendor Acme --source https://example.com/part.lib
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from design_spec import _extract_model_block, requires_model, validate_spec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "models.json"
MAX_BYTES = 5_000_000
TIMEOUT = 45
UA = "circuit-design-skill/model-fetch (+https://github.com/davlug3/circuit-design-skill)"


class FetchError(RuntimeError):
    """A model could not be retrieved or validated."""


def load_catalog(path: str | Path = CATALOG) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise FetchError(f"{path} is not a valid model catalog")
    return data


def find_entry(catalog: dict[str, Any], part: str) -> dict[str, Any]:
    needle = part.strip().lower()
    for entry in catalog["entries"]:
        names = [entry.get("part", ""), entry.get("model", "")]
        names.extend(entry.get("aliases") or [])
        if needle in {str(name).lower() for name in names if name}:
            return entry
    known = ", ".join(entry.get("part", "?") for entry in catalog["entries"])
    raise FetchError(f"no catalog entry for {part!r}; known parts: {known}")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def download(url: str) -> bytes:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
    except ValueError as exc:
        raise FetchError(f"{url}: invalid URL ({exc})") from exc
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status = getattr(response, "status", 200)
            if status and status >= 400:
                raise FetchError(f"{url}: HTTP {status}")
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        text = str(reason)
        if "CERTIFICATE_VERIFY_FAILED" in text or isinstance(reason, ssl.SSLError):
            raise FetchError(
                f"{url}: TLS certificate verification failed ({reason}). "
                "Do not disable verification; choose another source or download "
                "the file manually and pass it with --file."
            ) from exc
        raise FetchError(f"{url}: {reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"{url}: timed out after {TIMEOUT}s") from exc
    if len(data) > MAX_BYTES:
        raise FetchError(f"{url}: file is larger than {MAX_BYTES} bytes")
    if not data.strip():
        raise FetchError(f"{url}: downloaded an empty file")
    return data


def available_models(text: str, limit: int = 12) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"^\s*\.(?:model|subckt)\s+(\S+)", text,
                             re.IGNORECASE | re.MULTILINE):
        name = match.group(1)
        if name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def model_type(card: str) -> str | None:
    match = re.search(r"^\s*\.model\s+\S+\s+([A-Za-z][A-Za-z0-9_]*)",
                      card, re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


def attribution(text: str, limit: int = 8) -> list[str]:
    """Header lines that look like a licence or copyright notice."""
    keys = ("copyright", "licen", "restricted", "permission", "free of charge",
            "all rights", "proprietary", "terms")
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("*") and any(key in line.lower() for key in keys):
            if line not in lines:
                lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def parse_pins(text: str | None) -> list[str]:
    if not text:
        return []
    return [pin.strip() for pin in text.split(",") if pin.strip()]


def resolve_pins(value: Any, source_text: str) -> list[str]:
    """Normalise a catalog's subckt_pins, or [] when not a subcircuit."""
    if not value:
        return []
    if isinstance(value, str):
        return parse_pins(value)
    if isinstance(value, list):
        return [str(pin) for pin in value]
    raise FetchError(f"{source_text}: subckt_pins must be a list or comma string")


def build_entry(*, part: str, key: str, model_name: str, vendor: str,
                source: str, text: str, data: bytes, subckt_pins: Any = None,
                notes: str | None = None,
                expected_type: str | None = None) -> dict[str, Any]:
    card, is_subckt = _extract_model_block(text, model_name)
    if card is None:
        found = available_models(text)
        hint = f" Available models: {', '.join(found)}" if found else ""
        raise FetchError(
            f"{source}: does not define .model or .subckt {model_name!r}.{hint}"
        )
    if is_subckt:
        pins = resolve_pins(subckt_pins, source)
        if not pins:
            raise FetchError(
                f"{source}: {model_name!r} is a .subckt. Its node order is part "
                "of the interface; supply --subckt-pins (for example D,G,S) or "
                "add it to the catalog entry."
            )
    else:
        pins = []
        actual_type = model_type(card)
        if expected_type and actual_type and expected_type.upper() != actual_type.upper():
            raise FetchError(
                f"{source}: expected a {expected_type} model, found {actual_type}"
            )

    entry: dict[str, Any] = {
        "file": "",  # filled once the caller knows the output path
        "name": model_name,
        "vendor": vendor,
        "part": part,
        "source": source,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if pins:
        entry["subckt_pins"] = pins
    if notes:
        entry["notes"] = notes
    entry["_key"] = key
    entry["_card"] = card
    entry["_subckt"] = is_subckt
    return entry


def safe_filename(part: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._") or "model"
    return stem + ".lib"


def write_model(outdir: Path, part: str, data: bytes, *, force: bool) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / safe_filename(part)
    if path.exists():
        if path.read_bytes() == data:
            return path
        if not force:
            raise FetchError(
                f"{path} already exists with different content. "
                "Re-run with --force to replace it."
            )
    path.write_bytes(data)
    return path


def file_reference(path: Path, spec_dir: Path | None) -> str:
    if spec_dir is None:
        return str(path)
    return os.path.relpath(path, spec_dir)


ENTRY_KEYS = ("name", "vendor", "part", "source", "sha256", "subckt_pins", "notes")


def clean_entry(entry: dict[str, Any], file_ref: str) -> dict[str, Any]:
    out: dict[str, Any] = {"file": file_ref}
    for key in ENTRY_KEYS:
        value = entry.get(key)
        if value not in (None, "", []):
            out[key] = value
    return out


def select_components(spec: dict[str, Any], key: str, entry: dict[str, Any],
                      ref: str | None) -> list[dict[str, Any]]:
    comps = spec.get("components") or []
    if ref:
        matches = [comp for comp in comps if comp.get("ref") == ref]
        if not matches:
            raise FetchError(f"component {ref!r} is not in the spec")
        comp = matches[0]
        if not requires_model(comp.get("type", "")):
            raise FetchError(
                f"component {ref!r} is type {comp.get('type')!r}; "
                "only a semiconductor takes a device model"
            )
        return matches

    aliases = {key}
    aliases.update(str(value) for value in (entry.get("part"), entry.get("name")) if value)
    named = [comp for comp in comps if comp.get("model") in aliases]
    if named:
        return named

    candidates = [
        comp for comp in comps
        if requires_model(comp.get("type", "")) and not comp.get("model")
    ]
    if len(candidates) == 1:
        return candidates
    if not candidates:
        raise FetchError(
            "no component references this part and no unassigned semiconductor "
            "matches it; pass --ref to choose one"
        )
    refs = ", ".join(str(comp.get("ref")) for comp in candidates)
    raise FetchError(f"several semiconductors could take this model ({refs}); pass --ref")


def write_spec(spec_path: Path, entry: dict[str, Any], key: str,
               file_ref: str, ref: str | None, *, force: bool) -> None:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    models = raw.get("models")
    if models is None:
        models = raw["models"] = {}
    if not isinstance(models, dict):
        raise FetchError(f"{spec_path}: 'models' must be an object")
    if key in models and models[key] != clean_entry(entry, file_ref) and not force:
        raise FetchError(
            f"{spec_path}: models[{key!r}] already exists with different content; "
            "pass --force to replace it"
        )
    selected = select_components(raw, key, entry, ref)
    for comp in selected:
        comp["model"] = key
    models[key] = clean_entry(entry, file_ref)

    base_dir = spec_path.resolve().parent
    report = validate_spec(raw, base_dir=base_dir)
    if not report["ok"]:
        raise FetchError(
            "the updated spec does not validate; nothing was written:\n  "
            + "\n  ".join(report["errors"])
        )
    spec_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def load_source(args: argparse.Namespace) -> tuple[dict[str, Any], str, bytes]:
    catalog = load_catalog(args.catalog)
    entry: dict[str, Any] = {}
    if args.part:
        try:
            entry = dict(find_entry(catalog, args.part))
        except FetchError:
            if not (args.url or args.file):
                raise
            # A direct URL may name a part the catalog does not know yet.
            entry = {}

    if args.file and args.url:
        raise FetchError("choose --url or --file, not both")

    if args.file:
        path = Path(args.file)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FetchError(f"cannot read {path}: {exc}") from exc
        source = args.source
        if not source:
            raise FetchError("--file requires --source so the provenance is recorded")
    elif args.url:
        data = download(args.url)
        source = args.source or args.url
    else:
        source = str(entry.get("source") or "")
        if not source:
            raise FetchError("the catalog entry has no source URL")
        data = download(source)

    if len(data) > MAX_BYTES:
        raise FetchError(f"{source}: file is larger than {MAX_BYTES} bytes")
    text = _decode(data)

    part = args.part or str(entry.get("part") or "")
    if not part:
        raise FetchError("--part is required for a direct URL or file")
    key = args.key or part
    model_name = args.name or str(entry.get("model") or part)
    vendor = args.vendor or str(entry.get("vendor") or "")
    if not vendor:
        raise FetchError("a vendor is required; pass --vendor or use a catalog entry")
    notes = args.notes or entry.get("notes")
    pins = args.subckt_pins if args.subckt_pins is not None else entry.get("subckt_pins")
    expected = args.type or entry.get("type")

    built = build_entry(
        part=part, key=key, model_name=model_name, vendor=vendor, source=source,
        text=text, data=data, subckt_pins=pins, notes=notes,
        expected_type=expected,
    )
    entry.update(built)
    return entry, text, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a SPICE model from an open source and emit a models entry.")
    parser.add_argument("--part", help="catalog part (or metadata for --url/--file)")
    parser.add_argument("--url", help="download this model file directly")
    parser.add_argument("--file", help="use an already downloaded model file")
    parser.add_argument("--list", action="store_true", help="list the catalog")

    parser.add_argument("--catalog", default=str(CATALOG), help="catalog JSON path")
    parser.add_argument("--source", help="provenance URL (required with --file)")
    parser.add_argument("--name", help="model name inside the file (default: catalog name)")
    parser.add_argument("--vendor", help="model vendor")
    parser.add_argument("--type", help="expected device type, e.g. NPN, D, NMOS")
    parser.add_argument("--subckt-pins", help="node order for a .subckt, e.g. D,G,S")
    parser.add_argument("--key", help="spec key (default: part name)")
    parser.add_argument("--notes", help="extra note for the spec entry")
    parser.add_argument("--out", help="directory for the model file (default: models/)")
    parser.add_argument("--write-spec", help="update this spec's models and component ref")
    parser.add_argument("--ref", help="component reference to assign (default: infer)")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing model file or spec entry")
    parser.add_argument("--json", action="store_true", help="print only the JSON entry")
    args = parser.parse_args(argv)

    try:
        if args.list:
            catalog = load_catalog(args.catalog)
            for entry in catalog["entries"]:
                pins = entry.get("subckt_pins")
                extra = f" pins={','.join(pins)}" if pins else ""
                print(f"{entry.get('part'):<10} {entry.get('model', ''):<12} "
                      f"{entry.get('type', ''):<6} {entry.get('vendor', '')}{extra}")
                print(f"           {entry.get('source', '')}")
            return 0

        if not (args.part or args.url or args.file or args.list):
            parser.error("choose --part, --url, --file, or --list")
        if args.url and args.file:
            parser.error("--url and --file are mutually exclusive")
        if (args.url or args.file) and not args.part:
            parser.error("--part is required with --url/--file so the entry has a name")

        entry, text, data = load_source(args)

        spec_path = Path(args.write_spec).resolve() if args.write_spec else None
        if args.out:
            outdir = Path(args.out)
        elif spec_path is not None:
            outdir = spec_path.parent / "models"
        else:
            outdir = Path("models")
        out_path = write_model(outdir, str(entry["part"]), data, force=args.force)
        spec_dir = spec_path.parent if spec_path is not None else None
        file_ref = file_reference(out_path, spec_dir)
        final = clean_entry(entry, file_ref)

        if spec_path is not None:
            write_spec(spec_path, entry, str(entry["_key"]), file_ref, args.ref,
                       force=args.force)

        if args.json:
            print(json.dumps({"models": {entry["_key"]: final}}, indent=2))
            return 0

        kind = "subcircuit" if entry["_subckt"] else f".model {model_type(entry['_card']) or ''}".strip()
        print(f"fetched {entry['part']} as {entry['name']} ({kind})")
        print(f"  vendor: {entry['vendor']}")
        print(f"  source: {entry['source']}")
        print(f"  saved:  {out_path}  sha256={entry['sha256']}")
        if entry.get("license"):
            print(f"  license: {entry['license']}")
        if entry.get("notes"):
            print(f"  note:   {entry['notes']}")
        for line in attribution(text):
            print(f"  header: {line}")
        if entry.get("subckt_pins"):
            print(f"  pins:   {','.join(entry['subckt_pins'])}")
        if spec_path is not None:
            print(f"  wrote:  {spec_path} (component model set to {entry['_key']})")
        print("\nAdd to a spec:")
        print(json.dumps({"models": {entry["_key"]: final}}, indent=2))
        return 0
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
