# Phase 1 — Config + fail-closed profiles: plan & test design

`IMPLEMENTATION_README.md` Phase 1. **Test-first:** the verification harness below is built and the
pre-refactor baselines are captured *before* any implementation. P1.1 is `BEHAVIOR-CHANGE` in the
config **source** only (argparse → YAML+CLI); every default **value** stays identical, so the
decision stream must remain **byte-identical**. P1.2 adds a fail-closed drive gate (a deliberate,
tested new refusal).

---

## The config surface (characterized)

Two launch paths, both through `K1Finder/run_follow.sh` over SSH:

- **Tracker tab** (`Start-Tracker`, `K1Finder.ps1:1216`): `run_follow.sh <mode> <topic> --stream
  --standoff-m <v> --vx-max <v> <Get-TrackExtraArgs>` — the full override surface.
- **Control tab** (`Start-Follow`, `K1Finder.ps1:1446`): `run_follow.sh <mode> <topic>` — **no extra
  flags**, node defaults for everything.

**App-passed override flags** (must all keep working after the refactor): `--preview/--drive`,
`--bridge`, `--topic`, `--stream`, `--standoff-m`, `--vx-max`, `--appearance`, `--reid-engine`,
`--coast-frames`, `--auto-reacquire`, `--arm-reacquire`, `--max-follow-range`, `--rerun[-mode|-dir]`,
`--require-heartbeat`, `--lock-trigger`, `--gesture-model`, `--gesture-debug`, `--commands`.
Couplings to preserve: `global` appearance emits **no** `--appearance` (node default); `--arm-reacquire`
is osnet-gated and force-adds `--auto-reacquire`; `aruco` emits **no** `--lock-trigger` group; hardcoded
literals `--coast-frames 8`, `--max-follow-range 4.0`, `--rerun-mode save`. No `--max-seconds/--min-safe/
--obstacle/--depth` flags are app-passed (node-internal defaults).

**Defaults:** 29 `DEF_*` constants + `LL_DARK_THRESH`, plus the inline argparse defaults. Full inventory
in the design workflow output. `parse_args` = `follow_person_k1.py:4157-4662`.

### The #1 byte-identity trap — the None-resolved floors
Seven keys default to `None` in argparse and are **resolved post-parse from `--appearance`**
(`L4611-4640`). `defaults.yaml`/profiles **MUST store them as `null`** so the same resolution runs:

| key | global | striped | osnet |
|---|---|---|---|
| `hiconf` | 0.55 | 0.30 | 0.40 |
| `anchor_floor` | 0.50 | 0.30 | 0.35 |
| `bank_floor` | 0.55 | 0.30 | 0.40 |
| `reloc_floor` | 0.70 | 0.45 | 0.55 |
| `reloc_view_floor` | 1.01 | 0.55 | 0.55 |
| `reloc_iso_bypass_anchor` | 0.65 | 0.55 | 0.55 |
| `reloc_anchor_backstop` | =round(0.8·anchor_floor,4) → 0.40 | 0.24 | 0.28 |

Other post-parse logic that must run **exactly once** on the merged namespace: `not drive → preview`;
`p.error()` when `--lock-trigger gesture|both` without `--track` or with `reseed_anchor_floor<=0`;
`gesture_owner_margin` clamp to `iou_min`; `depth_topic` strip.

---

## Test architecture

### 1. Config-parity gate (primary, BUILT + baselined)
Snapshots the **fully-resolved** `vars(parse_args(argv))` as canonical sorted JSON for a matrix of
inputs. After the refactor, the YAML loader must reproduce **byte-identical** JSON per row.

- `_gate/dump_args.py` — imports the node (via the ROS stubs), `parse_args(argv)`, dumps
  `json.dumps(vars(args), sort_keys=True)`.
- `_gate/cfg_matrix.txt` — 20 rows (defaults; each appearance column; drive/preview; `--no-*`
  BooleanOptionalAction; gesture + clamp; choices; realistic Tracker & drive bundles).
- `_gate/config_parity.ps1 baseline|diff` — captures / diffs. **20 pre-refactor baselines captured in
  `_gate/cfg/` (all 141 keys), self-diff clean, and negative-tested** (a baked `anchor_floor` on the
  osnet row is caught).

Run after the refactor: `_gate\config_parity.ps1 diff` → must print `CONFIG-PARITY OK (20/20)`.

### 2. Decision-stream backstop (BUILT)
`_gate/gate.ps1 baseline|diff` — the replay_eval byte-identical decision-stream gate, on at least the
defaults / osnet / field configs. Catches any downstream behavioral effect the Namespace parity might
miss. (No-person clip only — person-path stays VERIFY ON ROBOT.)

### 3. Post-implementation loader assertions (to WRITE with the loader)
Behaviors the parity snapshot can't cover pre-loader — negative/semantic parity with argparse:

| Assertion | From risk |
|---|---|
| Loader **rejects** `appearance/rerun_mode/lock_trigger` not in choices (exit, like argparse) | choices bypass |
| Loader **rejects** unknown YAML keys; dest-set == current-build dest-set on defaults | unknown/stale keys |
| Loader **rejects** `drive && preview` both true; reproduces `not drive → preview` | mode mutation |
| `--lock-trigger gesture` with `track:false` or `reseed_anchor_floor<=0` **exits**; owner-margin clamp fires | gesture coupling |
| Type fidelity: float dests stay float, int stay int (no YAML `4`→int-for-`4.0`) | type coercion |
| **Precedence:** CLI wins over profile for every app flag; explicit CLI-default beats profile-non-default | precedence (SAFETY) |
| `depth_topic` stripped post-load | normalization |

---

## Risks → tests (from the adversarial design)

| # | Risk (severity) | Caught by |
|---|---|---|
| 1 | **Baked floor** where argparse left `None` (CRIT) | parity rows `appearance_{global,striped,osnet}` |
| 2 | BooleanOptionalAction true/false ↔ `--flag/--no-flag` (CRIT) | `no_*` rows + loader bool-identity test |
| 3 | int-vs-float YAML coercion → JSON `4` vs `4.0` (HIGH) | parity defaults row + type assertions |
| 4 | choices not validated from YAML (HIGH) | loader negative test |
| 5 | preview/drive mutual-excl + `not-drive→preview` lost (CRIT) | `drive_mode`/`preview_mode` rows + negative test |
| 6 | Precedence wrong — CLI not winning (`--vx-max` is safety) (CRIT) | `field_*` rows run vs a conflicting profile |
| 7 | Unknown/stale YAML keys carried into namespace (HIGH) | dest-set equality + unknown-key reject |
| 8 | Not every app flag threads as an override (CRIT) | drive each app flag vs opposite profile |
| 9 | gesture `track`/`reseed` coupling broken from YAML (HIGH) | `gesture_lock` row + negative tests |
| 10 | `depth_topic` strip skipped (MED) | defaults row + strip test |
| 11 | Non-canonical JSON → false diffs / masked ones (MED) | one shared serializer; self-diff determinism |

---

## Implementation approach (P1.1) — one commit per step, gate green each

1. **`config/` module + `defaults.yaml`** — mirror every current default value; the seven floor keys
   stored as `null`. Add `demo.yaml`/`field.yaml`/`dev.yaml` (P1.2 sets safety values; `dev`≈current).
2. **Loader wired into `parse_args`** — merge `defaults ← profile ← CLI`, then run the existing
   post-parse block **once**. Use `argparse.SUPPRESS` as the per-arg default so an explicit CLI value
   equal to the node default still beats a profile (precedence req). Keep every flag name working.
3. **Gate after each commit:** `config_parity.ps1 diff` → `CONFIG-PARITY OK (20/20)`; `gate.ps1 diff` →
   `DIFF-EMPTY`; then add the loader assertions. No default *value* changes in P1.1.

### Precedence spec (exact)
`defaults ← profile ← CLI`, merged **before** the post-parse block. Floors stay `null` at every layer.
CLI always wins; because a CLI flag collapses to a value in `vars()`, the loader must record **which
keys were explicitly passed** (SUPPRESS sentinel) so a CLI-default still overrides a profile-non-default.
The post-parse resolution runs **once** on the merged namespace — never per-layer.

---

## Go / No-Go (P1.1 byte-identical)
- Every matrix row: `CONFIG-PARITY OK (20/20)` byte-identical.
- Dest-set equality on defaults (no extra/missing keys).
- Seven floors match across global/striped/osnet (proves `null` storage + unchanged resolution).
- Precedence proven: CLI wins for `standoff_m/vx_max/max_follow_range/appearance/lock_trigger` vs a
  conflicting profile; explicit CLI-default (`--vx-max 0.18`) beats profile `vx_max:0.12`.
- Negative parity: loader exits on bad choices / unknown keys / drive+preview / gesture coupling;
  owner-margin clamp fires; types preserved; `depth_topic` stripped.
- Backstop: `gate.ps1 diff` `DIFF-EMPTY` on defaults/osnet/field.
- **NO-GO** if any row diffs one byte, any safety flag fails CLI-wins, any negative case fails to
  exit-match, or the decision stream diverges.

---

## P1.2 — fail-closed drive gate + safe profiles (BEHAVIOR-CHANGE by design)

Not byte-identical (a new startup refusal + non-default profiles). Lands **after** P1.1.

### Fail-closed drive gate
- **New key:** `allow_untethered_unsafe` (`--allow-untethered-unsafe`, `BooleanOptionalAction`, default
  `False`); add `allow_untethered_unsafe: false` to `defaults.yaml` so the exact-key-set loader stays happy.
- **Insertion point:** in `parse_args`, just before `return args` (L4661, after depth-topic normalization)
  — the earliest point all three flags resolve and strictly **before** `Follower(args)`/`rclpy.init`/CamNode/
  YOLO/any bridge spawn. Use `p.error(...)` (exit 2, clean CLI message), mirroring the sibling lock-trigger
  refusals at L4645-4649. (Not `__init__`/`run()` — those spin up CamNode+YOLO first.)
- **Condition:** `if args.drive and not args.require_heartbeat and not args.allow_untethered_unsafe: p.error(...)`.
- **Truth table:** drive+require_heartbeat → RUN (deadman armed); drive+override → RUN (+loud WARN in
  `__init__` ~L1965 so the .rrd records the bypass); drive+neither → **REFUSE** (exit 2); drive+both → RUN
  (require_heartbeat wins). Preview unaffected (drive-only gate).
- **Defense-in-depth:** optional redundant assert in `start_drive_chain` (L2291) before `Bridge()` (L2303).
- **Test (BEHAVIOR-CHANGE):** headless argparse-level cases in the committed `config_selftest.py` — default
  `--drive` → `SystemExit(2)`; `--drive --require-heartbeat` → OK; `--drive --allow-untethered-unsafe` → OK.
  `VERIFY ON ROBOT`: killing the HB relay stands the robot in the HB tier.

### Heartbeat deadman — RESOLVED (was the DECISIONS.md P1.2 open item)
Static verification of the writer→node→bridge chain: **ARMED AND FUNCTIONAL as a soft/software deadman**
when the "Deadman HB" box is ticked (all three links fail-closed):
- **WRITER:** `Start-HbRelay` (`K1Finder.ps1:1267`) = ssh child running `while read -r _; do touch /tmp/k1_hb; done`,
  pumped by the 40 ms `$mediaTimer` `WriteLine('h')` → **~25 Hz** (not the ~10 Hz assumed — faster, with
  margin); started only when `--require-heartbeat`. Any WiFi drop / UI-thread freeze / laptop sleep / kill
  stops the touches within ~one tick.
- **NODE:** `_drive_vel` zeroes all velocity before `send_velocity` when `/tmp/k1_hb` is stale/missing
  (`getmtime` fail-closed; `hb_stale_ms 400`).
- **BRIDGE:** env-armed `K1_REQUIRE_HB` ~50 Hz watchdog: zero @400 ms, stand+kPrepare @1500 ms.
- Default OFF → byte-identical tethered; trips within ~400 ms of any loss.
- **CAVEAT (carry into P1.2 + UNTETHERED_FOLLOW.md):** this is a *soft* stand, **not** an independent
  hardware power cutoff, and was **not** on-robot arm-validated here (static only). Correctly fail-closed
  today, but do not treat as sufficient to authorize untethered operation until robot-armed and paired with
  the hardware backstop.

### Safe demo/field profiles — STARTING values (human review; DECISIONS.md)
BEHAVIOR-CHANGE for those profiles; `dev`/no-profile stays byte-identical. Both set `require_heartbeat: true`
so selecting them satisfies the fail-closed gate; neither unlocks untethered (still FORBIDDEN).
- **demo.yaml** (supervised indoor): `require_heartbeat: true`, `obstacle_brake: true`, `max_follow_range: 3.0`,
  `coast_frames: 6`, `max_seconds: 90.0`, `obstacle_target_margin: 0.4`.
- **field.yaml** (open ground, stricter): `require_heartbeat: true`, `obstacle_brake: true`,
  `obstacle_brake_start: 2.0`, `max_follow_range: 4.0`, `hb_stale_ms: 300`, `coast_frames: 4`,
  `max_seconds: 120.0`, `min_safe_range: 0.8`, `vx_max: 0.15`.

### run_follow_demo.sh + the config deploy gap
- **`run_follow_demo.sh`:** mirror `run_follow.sh` but hoist the compile block OUT of the drive branch
  (pre-compile unconditionally — same verified g++ recipe + exit-3/`k1_compile.err`/`BRIDGE`-marker contract)
  and add `--profile demo` **before** `"$@"` on both exec lines (CLI still overrides). `run_follow_field.sh`
  (`--profile field`) is the natural sibling. Requires P1.1b's `--profile` loader.
- **Deploy gap (shared with P1.1b, see below):** the app deploys a *hardcoded* file list; `config/` and
  `run_follow_demo.sh` must be added to `Deploy-FollowFiles`.

---

## Status
- ✅ Config surface characterized; defaults + None-floor table locked.
- ✅ Config-parity gate built, 20 pre-refactor baselines captured, self-diff clean, negative-tested.
- ✅ Decision-stream backstop available.
- ⏳ Loader + `config/` + profiles — **not started** (implementation).
- ⏳ Loader negative/precedence assertions — to write with the loader.
- ⏳ P1.2 heartbeat-writer verification.
