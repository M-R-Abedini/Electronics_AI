# Design Intent Schema v1 & `placer__*` MCP Tool Contract

This reference specifies the exact machine-verified contracts for `design_intent.json` (`electronics-ai/design_intent`, `schema_version: 1`) and all **11 `placer__*` MCP tools** exposed by `PlacerService` (`eda-agent/src/eda_agent/placer/mcp.py`).

---

## 1. `design_intent.json` Schema v1 Contract

Every engineering assertion is a **claim**:

```json
{
  "value": "<role | target | net_class | pair | block | mechanical | policy | watts>",
  "source": {
    "kind": "motif | board | datasheet | schematic | llm | user | label",
    "ref": "REQUIRED when kind is datasheet, schematic, or llm",
    "client": "claude-code | codex | antigravity (optional for llm claims)",
    "detail": "optional short rationale"
  },
  "confidence": 0.95,
  "confirmed_by_user": false
}
```

### Hard vs Soft vs Uncertain Rules
- **Hard constraint:** `confirmed_by_user == true` OR `confidence >= 0.9`.
- **Soft penalty:** `0.6 <= confidence < 0.9` and `confirmed_by_user == false`.
- **Uncertain (becomes an open question):** `confidence < 0.6`, or a `target` with `ref: null` and non-empty `candidates`.
- **Mandatory `ref` rule:** Any claim with `source.kind` in `{"datasheet", "schematic", "llm"}` **MUST** include a non-empty `source.ref` string or `placer__intent_validate` will reject the file.
- **Canonical intent hash (`intent_sha256`):** Independent of JSON key order, list order, and `questions` (which are informational). Identical semantic intent across Claude Code, Codex, and Antigravity yields the exact same `intent_sha256` and bit-for-bit identical solver output (P7).
- **Never write `(x, y)` coordinates in `design_intent.json`:** The schema forbids arbitrary coordinates (`additionalProperties: false`).

### Valid Enumerations (Exact Schema Values)

- **Top-level keys:** `schema` (`"electronics-ai/design_intent"`), `schema_version` (`1`), `board` (`{name, sha256, path?}`), `policies` (`side_policy`: `expert` | `top_only`; `stackup`: `locked` | `propose` | `needs_decision`), `parts`, `nets`, `diff_pairs`, `blocks`, `mechanical`, `power_w`, `questions`.
- **All 37 Part Roles (`parts.<ref>.role.value`):**
  - *Active / Power ICs:* `ic`, `converter_ic`, `linear_regulator`, `power_switch`
  - *Converter / Regulator Passives:* `converter_inductor`, `converter_diode`, `converter_control`, `regulator_input_cap`, `regulator_output_cap`
  - *Capacitors:* `decap`, `bulk_cap`, `pin_cap`, `coupling_cap`
  - *Timing:* `crystal`, `oscillator`, `crystal_load_cap`, `crystal_aux`
  - *I/O & Protection:* `esd`, `connector`
  - *Resistors & Signal Passives:* `series_resistor`, `pull_up`, `pull_down`, `termination`, `bias_resistor`, `divider_resistor`, `led`, `led_resistor`, `rail_filter`, `signal_passive`
  - *Discrete & Mechanical:* `transistor`, `diode`, `switch`, `jumper`, `test_point`, `mechanical`, `battery`, `unknown`
- **Part Targets (`parts.<ref>.target.value`):**
  - Settled local target: `{"ref": "U1", "net": "+3V3", "pins": ["12", "13"]}`
  - Unsettled candidate list: `{"ref": null, "net": "+3V3", "candidates": ["U1", "U2"]}`
  - Shared rail capacitor (e.g., `bulk_cap`): `{"ref": null, "net": "VBUS", "scope": "rail"}` (`pins` and `candidates` must be omitted or empty).
- **Net Classes (`nets.<name>.class.value`):** `POWER_HIGH_CURRENT`, `SW_NODE`, `DIFF_PAIR`, `HIGH_SPEED`, `SENSITIVE_ANALOG`, `GENERAL`
- **Net Rail (`nets.<name>.rail.value`):** `power`, `ground`, `signal`
- **Differential Pair Kinds (`diff_pairs[].value.kind`):** `usb`, `lvds`, `ethernet`, `mipi`, `analog_audio`, `other` (plus optional `routing_eligible: true | false`)
- **Block Kinds (`blocks[].value.kind`):** `decap_group`, `crystal`, `converter`, `linear_regulator`, `esd_port`, `led`, `pull_up_row`, `other`
- **Mechanical Constraints (`mechanical.<ref>.value`):**
  - Lock existing pose: `{"mode": "preserve"}`
  - Constrain to a board rule area: `{"mode": "region", "region": "<rule_area_name>", "angles_deg": [0, 90, 180, 270]}`

---

## 2. All 11 `placer__*` MCP Tools (Exact Signatures & Roles)

| Tool | Read-Only | Purpose & Key Arguments |
|---|---|---|
| `placer__inspect` | Yes | Read KiCad board into Board IR; return layers, inferred roles, `stackup_policy`, sides, netclasses, diff pairs, fixed parts, unknowns, capacity warnings, and mechanical evidence (`board_path`, `intent_path?`, `mechanical_refs?`). |
| `placer__intent_draft` | No | Draft `<board>.design_intent.json` from netlist + schematic without overwriting existing files; returns open `questions` (`board_path`, `out_path?`). |
| `placer__intent_validate` | Yes | Validate `design_intent.json` against JSON Schema v1, board refs/nets/pins, target roles, and source rules; returns `errors`, `warnings`, `intent_sha256`, and remaining questions (`board_path`, `intent_path`). |
| `placer__score` | Yes | Evaluate any board placement (`legality`, `hpwl`, `crossings`, `rudy`, `pathfinder`, `critical_loops`, `regularity`, `lint`, `pro_score`) + optional strict Freerouting route check (`board_path`, `route=true`, `intent_path?`). Never writes the board. |
| `placer__place_board` | No | Full-board V2.0 placement (`S0–S5` Rust core + `floorplan=true` blocks/escapes/PathFinder Cell Inflation/passive alignment/mask-aperture avoidance/F6 silkscreen DFA) + optional DRC-guarded `S6a` critical copper (`s6a=true`) + Freerouting + DRC + Lint + HTML gallery (`board_path`, `side_policy?`, `seed=0`, `route=true`, `s6a=true`, `intent_path?`, `floorplan=false`, `schematic_path?`). Writes a NEW file. |
| `placer__place_cluster` | No | Local ECO placement for `refs` only while anchoring all outside footprints and preserving zone definitions; set `preserve_routing="outside_cluster"` for Safe ECO mode (keeps healthy outside tracks/vias and surgically strips only moved-pad/colliding/dangling copper) (`board_path`, `refs`, `intent_path?`, `seed=0`, `preserve_routing="none"`). |
| `placer__gallery` | Yes | Build side-by-side HTML gallery for run reports; set `include_images=true` (max 2 runs) to inspect rendered top/bottom PNGs directly in the MCP response (`runs`, `title?`, `include_images=false`). |
| `placer__diagnose` | Yes | Inspect localized unrouted nets, blocked escapes, congestion hotspots, and DRC findings with numeric evidence and PNG crops (`run`, `limit=8`, `include_images=true`). |
| `placer__refine` | No | Test one local single-component hypothesis (`rotation_deg`, `toward_ref`, `away_from_ref`, `step_mm`) on an independent copy; run `preview=true` before equal-budget routing (`run`, `change`, `preview=false`, `route_timeout_s=180`). Automatically rejects regressions and inconclusive runs. |
| `placer__apply` | No | Transactional apply (`adapter`: `kicad_file` default | `altium_plan` | `kicad_ipc`). Default `in_place=false` writes an atomic sibling copy (`P8`); explicit `in_place=true` checks passing gates + live SHA-256 freshness + verified backup + atomic swap (`run`, `adapter="kicad_file"`, `target_path?`, `in_place=false`, `use_routed=true`, `allow_failed_gates=false`, `force_stale=false`, `allow_flip=false`, `dry_run=false`). |
| `placer__revert` | No | Roll back a recorded `placer__apply` transaction in `< 2 s` with SHA-256 verification; refuses to clobber post-apply manual edits unless `force=true` (`apply_id="latest"`, `force=false`). |

---

## 3. V1 vs V2 Namespace Separation

- **Always use `placer__*`** for Electronics AI 2.0 workflows (KiCad native + Altium intent/Level-1 plans).
- Do **not** mix V1 `layout__*` tools into a `placer__*` pipeline unless the user explicitly asks to execute a V1 live Altium script session.
