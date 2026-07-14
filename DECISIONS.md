# DECISIONS — human-confirmation items

Items from `IMPLEMENTATION_README.md` that require a human decision before the
associated deletion/change is safe. Each has the evidence gathered and a
recommendation; **the call is yours.** Tick the box and I'll action it.

---

## P0.2a — Keep or remove `follow_marker.png`?

**What:** `follow_marker.png` (repo root, DICT_4X4_50 ArUco marker, printable).

**Evidence:**
- Not loaded by any code and not referenced by the app (`K1Finder.ps1` has 0
  references to `follow_marker.png`). It is a *printable* asset, not a runtime one.
- The ArUco marker is still a supported **lock trigger** for `follow_person_k1.py`.
  Per project notes, a raised-hand **gesture** became the *default* lock trigger
  (2026-07-05); ArUco is the fallback (untick "Gesture lock" in the Tracker tab).
- So the marker workflow is still reachable → the printable source still has a use.

**Recommendation was KEEP**, but the user chose REMOVE (ArUco lock trigger is being
retired in favor of the raised-hand gesture).

- [x] **Remove** it — **RESOLVED 2026-07-07:** `git rm follow_marker.png`; the file
  references in `README.md` and `K1Finder/README.md` were de-referenced. The ArUco
  code path in `follow_person_k1.py` is untouched (out of scope here); only the
  printable asset was removed.

---

## P0.2b — Is the WPF app stack abandoned? (remove it, or is it the final UI?)

**What (the WPF stack):** `K1Finder.wpf.ps1`, `K1 Finder (WPF).bat`,
`Launch K1 Finder WPF (no console).vbs`, and `K1Finder/_redesign/`
(`BASELINE.md`, `PLAN.md`, `VITALS.md`, phase screenshots).

**Evidence that it's an abandoned scaffold:**
- Its own launcher says so: `K1 Finder (WPF).bat` → *"Launch the K1 Finder (WPF)
  **scaffold** (visible console for debug output)."*
- `K1Finder.wpf.ps1` (637 lines) does **not** wire the follow pipeline — no
  `run_follow` / bridge invocation. It's a UI shell only.
- The active, documented app is `K1Finder.ps1` (PowerShell + WinForms); the
  top-level README names it the main entry point and documents its six tabs.

**Recommendation: REMOVE the WPF stack + `_redesign/`** — the WinForms
`K1Finder.ps1` is the shipping UI.

- [x] **Remove** the WPF stack + `_redesign/` (WinForms `K1Finder.ps1` is final)
  — **RESOLVED 2026-07-07:** `git rm`'d `K1Finder.wpf.ps1`, `K1 Finder (WPF).bat`,
  `Launch K1 Finder WPF (no console).vbs`, and `K1Finder/_redesign/`.

---

## P1.2 — Does a heartbeat *writer* exist? (RESOLVED — yes, ~25 Hz)

**What:** the untethered operator-deadman (`--require-heartbeat`) reads a heartbeat
file/signal; the deadman is inert unless something *writes* it at ~10 Hz.

**Status: RESOLVED (static verification 2026-07-07).** The deadman is **armed and functional
as a soft/software deadman** — NOT inert. `Start-HbRelay` (`K1Finder.ps1:1267`) runs a remote
`while read -r _; do touch /tmp/k1_hb; done`, pumped by the 40 ms `$mediaTimer` `WriteLine('h')`
at **~25 Hz** (faster than the ~10 Hz assumed). Node zeroes velocity when the mtime is stale
(`hb_stale_ms 400`); the C++ bridge independently stands the robot via `K1_REQUIRE_HB` (zero @400 ms,
kPrepare @1500 ms). All three links fail-closed; default OFF = byte-identical tethered.

- [x] Writer verified (Start-HbRelay ~25 Hz).
- ⚠️ **CAVEAT (still open):** this is a *soft* stand, **not** an independent hardware power cutoff
  (per [[k1-hardware-backstop]]), and it was **not** on-robot arm-validated. Do not treat it as
  sufficient to authorize untethered operation until robot-armed + paired with the hardware backstop
  the `UNTETHERED_FOLLOW.md` gate requires.

---

## P1.2b — demo/field profile safety values (human review)

**What:** the `demo.yaml`/`field.yaml` values are STARTING recommendations that change behavior for
those profiles (dev/no-profile stays byte-identical). Review before P1.2 lands.

- **demo:** require_heartbeat true, obstacle_brake true, max_follow_range 3.0, coast_frames 6,
  max_seconds 90.0, obstacle_target_margin 0.4.
- **field:** require_heartbeat true, obstacle_brake true, obstacle_brake_start 2.0, max_follow_range 4.0,
  hb_stale_ms 300, coast_frames 4, max_seconds 120.0, min_safe_range 0.8, vx_max 0.15.

- [ ] Approve / adjust the demo + field values (see PHASE1_PLAN.md → P1.2).

---

## P2.1 — model/wheels location + blob strategy (human review)

**What:** the reorg can move `yolo11n-pose.onnx` / OSNet / `wheels/*.whl` into `models/`, or leave them at
root. Also whether model blobs go via git-lfs or a fetch script (brief P2.1/P2.3). Affects the app's
`Ensure-*` source paths.

- [ ] Decide models/wheels location + git-lfs vs fetch-script (default rec: `models/` for onnx, keep
  `wheels/` recipe; models via fetch-script not raw git). Pending Phase 2.

---

## P3.7 — fsm/control extraction: pure move vs injected interfaces (RESOLVED by default)

**What:** the brief's P3.7 says extract `fsm.py` + `control.py` "with perception/ID/bridge passed
in as **injected interfaces**." That is a **dependency-injection restructure of `Follower`** — a
BEHAVIOR-CHANGE, not a pure move, and it can only be fully verified with person-path clips (the
local gate is no-person).

**Decision (2026-07-07, my call — you were unsure):** **leave `Follower` (+`Seed`+`CommandChannel`)
as the thin orchestrator in `follow_person_k1.py`.** This honors the §2 invariant *"if a seam can't
be cut without a logic change, stop and flag it — do not improve while moving,"* keeps every P3
commit byte-identical, and matches the plan doc's own "leaves a thin orchestrator" phrasing. The
seam extractions (P3.0–P3.6) already deliver the testable-core goal.

- [ ] **Opt in** to the injected-interfaces refactor later as a deliberate BEHAVIOR-CHANGE (needs
  person-path clips / robot validation), or leave as-is.

## P3.x — dataclass grouping (DEFERRED)

**What:** fold `Follower`'s ~77 `self._` fields into `SafetyState`/`RelockState`/`TrackState`/
`VizState`/`Other`. Field-for-field, no semantic change — but it rewrites *hundreds* of
`self.X → self.group.X` attribute accesses across the orchestrator (large, error-prone; marginal
value now that the seams are out).

- [ ] Deferred — do it if the orchestrator's state sprawl becomes a real maintenance problem.

## P4.3 — collapse detect + pose into one model (EVALUATED, merge PENDING P5.1)

**What:** the brief asks whether to drop the separate detection model and use the YOLO-pose model's
person boxes for detection too (it yields boxes **and** keypoints in one pass) — "evaluate, don't
assume; only merge if success holds."

**Data (on-Orin per-inference, dummy 480×640, median of 15, 2026-07-08):**

| model | backend | ms |
|---|---|---|
| `yolo11n.onnx` (detection, current) | ONNX/CUDA | **19.1** |
| `yolo11n-pose.onnx` | ONNX/CUDA | 19.7 |
| `yolo11n-pose.engine` (P4.2b) | TensorRT FP16 | **9.3** |

**Finding (corrects the intuition that pose is "heavier"):** pose ≈ detect on the same backend (the
keypoint head is nearly free on yolo11n), and the pose **TRT engine is ~half** the current detect
cost. So routing detection through the pose engine would cut the driving-loop neural cost
**19.1 → 9.3 ms (~51%)** — a win in **both** TRACK (1 pass, cheaper) and acquisition (2 passes → 1).
Not the regression I first assumed.

**Decision: do NOT merge yet — record the path, gate on P5.1.** The collapse swaps the (Booster-tuned)
`yolo11n.onnx` person boxes for the pose model's boxes; the brief requires **detection-quality parity
on the labeled suite before committing**, and P5.1 (the task-success scorer) is not built. Cost is
resolved (favorable); *quality* is the open risk. Also note a lower-risk alternative that needs no
model swap: TRT the **existing** detect model (same person-box quality, just FP16 → ~9–10 ms) — but
it's a Booster `.onnx` with no `.pt`, so that needs a raw ORT-TRT-EP runner (YOLO pre/post reimpl) or
a `trtexec` engine + runner (deferred with P4.2b detection-TRT).

- [ ] After P5.1 exists: A/B the pose-engine-for-detection path vs the detect model on the labeled
  suite; merge only if person-box success holds. Else keep both (current split is fine) and/or take
  the TRT-the-detect-model route instead.

---

## P6.1a — Record SDK odometry (`/odometer_state`)?

**What:** P8 (cross-run stitching) wants a trajectory prior and P7 could use odometry as an extra
observation. The C++ bridge exposes **no** pose feedback (velocity-out only), so odometry would come
from the ROS topic the K1 reportedly publishes (`/odometer_state`, planar x/y/θ).

**Why not wired in P6.1:** the exact ROS **message type** can't be confirmed off-robot, and adding a
live subscription with a guessed type is not a mechanical, verifiable change (§0 of the brief: "when
unsure, stop and ask"). It is also **not** part of the P6.1 gate (RGB + depth + intrinsics + JSONL).

**To action (small follow-up commit), confirm on-robot first:**
```
ros2 topic info /odometer_state     # exact type + publisher present?
ros2 topic hz   /odometer_state     # publish rate
```
Then add a guarded BEST_EFFORT subscription (topic configurable, default `''` = disabled → inert) that
logs `x/y/θ` to the `.rrd` and an `odom_x/odom_y/odom_theta` triple into the JSONL. Fail-safe: a
missing or mismatched topic records nothing and never touches the follow.

- [x] **RESOLVED (P6.1a, commit `2541440`).** Type confirmed from the on-robot memory note:
  `/odometer_state` = `booster_interface/msg/Odometer` `{float32 x,y,theta}`. Wired a guarded,
  config-gated (`--odom-topic`, default `''` = inert) subscription in `perception.CamNode`: records
  `/odom/{x,y,theta}` to the `.rrd` and `odom_x/y/theta` to the JSONL; a missing type/workspace or bad
  message self-disables and never touches the follow. `run_follow*.sh` now source
  `BoosterRos2Interface`. **VERIFY ON ROBOT:** the live subscription (run `ros2 topic hz /odometer_state`
  to confirm rate; grep the JSONL for `odom_x`).

## P6.1b — A `capture.yaml` profile for P7/P8 runs?

**What:** pixels + depth + intrinsics record **only** under `--rerun` (save mode), which `demo`/`field`
leave **off** for loop-cost (P4.5). So P7/P8 data collection needs a distinct capture-enabled profile.

**Recommendation:** add `config/capture.yaml` (inherits `field`/`demo` safety, then sets
`rerun: true`, `rerun_mode: save`, and a chosen `rerun_image_every_n` trading fidelity vs loop-cost).
A profile changes nothing unless selected, so this is SAFE recording-config — but the fidelity/N and
whether capture piggybacks on a demo or is operator-selected is a call worth making deliberately
(and the `--rerun`+`--drive` loop-cost gate in `RERUN_PLAN.md` must pass at the chosen N).

- [x] **RESOLVED (P6.1b, commit `2db422a`).** Chose a standalone `config/capture.yaml` (demo-grade
  safety + `rerun: true`, `rerun_image_every_n: 2`, `odom_topic: '/odometer_state'`) plus
  `run_follow_capture.sh` (forces `--profile capture`, pre-compiles the bridge) so capture is usable
  headless. Deployed by the app. **Open sub-decision:** `rerun_image_every_n: 2` is a starting value —
  the `--rerun`+`--drive` loop-cost gate (RERUN_PLAN.md) must pass at it on the Orin; raise N if tight.

## P6.2a — Stamp a deploy SHA so run manifests carry a real version?

**What:** `offload_run.sh` records `git_version` in each bundle's `manifest.json` from an optional
`/home/booster/DEPLOY_VERSION` file (short SHA). The robot runs flat scp'd files with **no `.git`**, so
absent that file the manifest reads `git_version: "nogit"` and `run_id` ends in `_nogit`.

**Recommendation (small `robot-deploy` add):** have the app's deploy step write the deploying repo's
`git rev-parse --short HEAD` to `/home/booster/DEPLOY_VERSION` right after the scp push. Then every run
is traceable to the exact code that produced it (P7 dataset provenance wants this). One line in
`Deploy-RobotFiles`; no robot behavior change.

- [x] **RESOLVED (P6.2a, commit `8f51e34`).** `Deploy-FollowFiles` now stamps
  `git -C robot rev-parse --short HEAD` to `/home/booster/DEPLOY_VERSION` after the follow files land;
  best-effort (git absent / not a repo / push fail → manifest stays `nogit`, deploy still succeeds).
  **Sequencing (per the P7/P8 design pass):** land this before minting the P7.1 dataset `v1`, or record
  `git_version: nogit` honestly in the card — never fabricate a SHA.

## P6.2b — Wire the offload to run automatically on clean session end?

**What:** P6.2 ships the offload as an **on-demand** pair (`offload_run.sh` on the Jetson,
`Pull-Run.ps1` on the workstation). The plan also wants it to fire "on clean session end."

**Why not auto-wired yet:** the session lifecycle is owned by the Windows app (`K1Finder.ps1`), which
runs the SSH follow session and knows when it ends cleanly. Auto-firing means (a) the app SSHes
`offload_run.sh --profile <selected>` on the Jetson at session end, then (b) runs `Pull-Run.ps1`.
That's app-integration work (`robot-deploy`/`autonomy-ops`) worth doing deliberately rather than
folding into this task — the on-demand scripts are the reusable core and work standalone today.

- [x] **RESOLVED (P6.2b, commit `b44baaf`).** Added `desktop/Offload-Run.ps1` (ssh `offload_run.sh` →
  `Pull-Run.ps1`) and wired `Invoke-Offload` into `Stop-Tracker`: on a capture session's end (rerun
  was on), it launches Offload-Run.ps1 **detached** AFTER the robot is safed, so the WinForms teardown
  never blocks and it can never throw into the safing path. Profile label is `tracker-drive/preview`
  (the app launches with flags, not `--profile`). **VERIFY ON ROBOT:** the end-to-end ssh+pull.
  **⚠ Retention footgun (from the P7/P8 design pass) — MITIGATED by the P6.2 enhancement:**
  `offload_run.sh` moves the `.rrd` out of source + truncates the JSONL after publish. Retention now
  prunes **only `.verified` bundles** (marker written back by `Pull-Run.ps1` after a hash-checked pull),
  never an un-offloaded one, so a bundle can't be pruned before it's safely pulled to the laptop. The
  laptop→desktop hop (Sync-Runs.ps1) is still a separate copy; sync capture runs onward before deleting
  them from the laptop.

---

## P6G.3a -- recon.py/rsync -> Recon.ps1/scp (RESOLVED-BY-DESIGN)

**What:** the plan's P6G.3 names `recon.py submit|status|fetch <run_id>` over **rsync** as the
laptop-side recon job CLI. The charter's blocker **B2** (`docs/SCAFFOLD_CHARTER.md`) confirmed the
contradiction against the worktree: this laptop has **no Python and no rsync** (the
`Pull-Run.ps1`/`Sync-Runs.ps1` headers state it), so the CLI as specced cannot run on the machine the
gate requires it to run from.

**Decision (charter S2):** the laptop CLI is **`desktop/Recon.ps1`**
(`-Submit | -Status | -Fetch | -SelfTest`), PowerShell over **Windows-OpenSSH scp**, mirroring
`Pull-Run.ps1`'s manifest-verify-or-fetch idiom (per-file sha256, skip-if-match, resumable to
convergence). **rsync survives as desktop-side intent only** -- inside the WSL distro, never a laptop
binary. The desktop-side job wrapper is `desktop/recon_job.sh` (bash, deployed into the distro).

- [x] **RESOLVED-BY-DESIGN (2026-07-10).** No robot/plan behavior change -- transport substitution
  only; `docs/HANDOFF_DESKTOP.md` P6G.3 updated to match. The round-trip itself stays
  `VERIFY ON DESKTOP` (P6G.3 stub-then-real gate).
- **SUPERSEDED by P6G.0 (LAN-pool pivot):** the B2 finding still holds (laptop submit CLI is
  PowerShell/scp, not Python/rsync), but the target is now the **scheduler** (`desktop/Submit-Job.ps1`
  -> `cluster/submit.py`), not a direct `Recon.ps1` round-trip to one desktop. Recon is one job type in
  the pool; the manifest-verify-or-fetch scp idiom is reused by the worker. The untracked `Recon.ps1`
  the workflow drafted is NOT committed (its transport logic moves into `cluster/worker.py`).

---

## P8.2a -- person-masking v0 masks ONLY the logged TARGET box (bystander gap)

**What:** the recon container's person-masking v0 (charter S3) dilates the logged TARGET box
(`/camera/rgb/target` Boxes2D) and zeroes depth inside it before BOTH odom and TSDF. **Bystanders are
NOT masked** -- the recorder logs only the locked target's box today; all-person YOLO boxes are never
written to the `.rrd`. A bystander walking through frame leaves ghost geometry in the mesh and can
bias odometry.

**Options (open decision):**
- (a) **Recorder extension:** log all person detection boxes to the `.rrd` (small `rerun_sink`
  addition; slight per-frame recording cost; needs a robot deploy and only helps FUTURE captures).
- (b) **Offline re-detect:** run a person detector inside the recon container over the decoded RGB
  (no robot change, fixes already-captured runs; adds a model dep + nondeterminism to the image).

- [ ] Decide (a) record-all-boxes vs (b) re-detect-in-container. Until then v0 ships target-only
  masking with dilation + masked fraction recorded in `recon_manifest.json`; treat meshes from
  bystander-heavy runs as suspect.

---

## P6.4a -- B1 dual-ownership rule: card counts authoritative, index occupancy advisory

**What:** per-FSM-state occupancy now exists in TWO places, deliberately (charter B1 resolution,
section 5.2): the **dataset card** (S1 `eval/batch_ingest.py` computes per-episode and per-split
frame counts from the `.rrd` `/fsm/state_id` stream) and **`runs/index.json`** (the
`eval/label_run.py` occupancy patch derives per-state counts from `events.jsonl`'s `fsm` field).

**Decision (the rule):** the **card is authoritative for training** (P7.2 consumes it; P7.5
INSUFFICIENT floors judge against it); the **index occupancy is advisory for run scheduling** only
(coverage-gap "which state needs more capture runs" queries). Both sides MUST key canonical state
names through the shared **`eval/fsm_groups.py`** helper -- two hand-rolled maps are guaranteed to
drift. If card and index ever disagree on a run's per-state counts beyond expected `.rrd`-vs-JSONL
sampling skew, that is a **data bug to investigate**, never a value to reconcile silently.

- [x] **RESOLVED-BY-DESIGN (2026-07-10).** Rule recorded so nobody treats the two counts as a single
  source of truth or "fixes" a divergence by copying one over the other.

---

## P6G.0 -- ARCHITECTURE PIVOT: single central desktop -> LAN heterogeneous job-pool

**What (decided with the user 2026-07-10, `docs/CLUSTER_PLAN.md`):** replace the "one disposable
desktop GPU box" substrate with a **LAN job-pool** -- multiple computers each run a Docker **worker**
that pulls **independent jobs** from one **scheduler**, matched to hardware by capability tag
(cuda/cpu/npu/mps). Control plane = gRPC (register/lease/report, pull-based); data plane = the proven
sha256-verified scp bundle transport. Filesystem+JSON queue, single scheduler; **no K8s/Slurm/Ray/
broker.** NOT distributed data-parallel SGD (the tiny ACT trains slower split across a LAN than on the
3080 alone -- comms dominate); a DDP job type is designed-for, not built.

**Why:** the parallelism in this workload is across *jobs*, most embarrassingly parallel -- P8.2 recon
is one job per run, P7.2 is seed/HP sweeps, plus splat/ingest. A job-pool speeds that up and fits the
NPU/MPS/CPU/GPU mix; DDP of one small model does not.

**What this SUPERSEDES (the audit):**
- `PHASE_6-8_PLAN.md` §2 "the desktop is stateless compute" -> **"the worker POOL is stateless"**
  (disposability now per-worker; laptop still single source of truth).
- `PHASE_6-8_PLAN.md` §6 out-of-scope "no standing job queues, watchers" -> **deliberately overridden**
  (a standing scheduler + queue is the point; still no watchers/daemons on the robot, no cloud).
- P6G.1 single-WSL2-desktop -> **per-CUDA-worker-node provisioning** (`docs/WSL2_SUBSTRATE.md`
  re-scoped, banner added; `docs/CLUSTER_SETUP.md` generalizes it).
- P6G.3 `Recon.ps1` direct laptop->one-desktop round-trip (P6G.3a below) -> **the scheduler client**
  (`cluster/submit.py` + `desktop/Submit-Job.ps1`); recon becomes one job *type*. The
  `recon_manifest.json`/stage contract (`docs/RECON_CONTRACT.md`) survives unchanged.
- `docs/HANDOFF_DESKTOP.md` + `docs/COMPUTE_PLACEMENT.md` single-desktop framing -> pool framing
  (banners added; task->hardware-CLASS mapping survives, task->specific-BOX does not).

**Unchanged invariants:** laptop = source of truth; workers stateless/disposable; robot never a worker
and no worker has robot credentials; byte-identical doctrine stays robot-side; YAGNI (gRPC is the one
new dep, justified by the RPC contract).

- [x] **RESOLVED-BY-DESIGN (2026-07-10).** Plan updated in-repo (`CLUSTER_PLAN.md` is the authoritative
  substrate design; the Desktop-copy `PHASE_6-8_PLAN.md` P6G is superseded -- update it when convenient).
  Scaffolds (`cluster/`) authored next; all `VERIFY ON CLUSTER`. The pivot-agnostic scaffolds
  (dataset/contracts/fixtures) are untouched -- they are jobs the pool runs.

---

## P7.6 -- GRADUATION CRITERIA: what must ALL be true before the shadow policy may actuate

> **⚠ SUPERSEDED (2026-07-14) BY "P7.6 — GRADUATION CRITERIA + RUNTIME SHIELD SPEC (2026-07-13 —
> BINDING)" below.** This earlier draft's agreement bands (|Δvx|≤0.03 / |Δvyaw|≤0.05) and sample
> floors (TRACK 2000, …) are NOT authoritative — the 2026-07-13 entry is the single binding version
> the P7.5 grader implements verbatim. Kept for history only. (Council review wf_f2789658 flagged the
> two entries as contradictory-and-both-binding; resolved in favor of the newer, more complete one
> that carries the shield ladder + the PARKED phantom-free criterion.)

**This ships the CRITERIA, not the shield.** Per the plan invariant, the shadow policy NEVER actuates
until every item below exists AND passes AND a human flips the switch. Building the shield (a
`runtime-safety` initiative) and flipping the switch are separate, future, human-approved work. Numbers
here are **starting thresholds** -- tune with data, but a future session can implement against them
without interpretation. The C++ floor + mode-keyed deadman gate stay sovereign underneath ALL of this,
always.

### A. Offline agreement gate (from P7.5, per FSM state)

Metric: per control tick, the shadow action `(shadow_vx, shadow_vyaw)` vs the executed P-controller
action `(vx, vyaw)`, bucketed by the executed-tick FSM state (canonical name via `eval/fsm_groups.py`).
"In-band" = `|shadow_vx - vx| <= 0.03 m/s` AND `|shadow_vyaw - vyaw| <= 0.05 rad/s` (starting bands ~
1/6 of the hard clamps). Score the **Wilson 95% LOWER bound** of the in-band fraction (conservative,
not the point estimate).

| FSM state | in-band Wilson-low >= | min sample floor (ticks) |
|---|---|---|
| TRACK | 0.95 | 2000 |
| REACQUIRE | 0.90 | 500 |
| SEARCHING | 0.85 | 500 |
| SEARCH_MARKER | 0.85 | 300 |
| PARKED | 0.99 (near-zero action) | 100 |

- **A state below its sample floor is `INSUFFICIENT`** -- its threshold does NOT count as met (an
  agreement number on a handful of SEARCH ticks is theater). You close the gap by SCHEDULING capture
  runs that deliberately induce that state (step behind an obstacle -> REACQUIRE/SEARCH), never by
  lowering the floor. FSM-occupancy (P6.4 index + P7.1 card) tells you which states are short.
- **Corpus:** >= 10 offloaded runs spanning >= 3 distinct sessions/days; ALL five states at or above
  their floors; every state's Wilson-low at or above its threshold. One checkpoint hash, graded across
  the whole corpus by `eval/` (P7.5).

### B. The runtime-safety shield (must be BUILT + unit-tested before actuation)

A graded intervention ladder (never a bare kill); the learned policy runs only while ALL rungs are live:

- **Rung 0 nominal:** shadow action passes the OOD check AND the action-envelope clamp -> actuate.
- **Rung 1 caution:** mild OOD / thin uncertainty margin -> tighten the action envelope (reduce the vx/
  vyaw limits toward the P-controller's), log.
- **Rung 2 hold (turn-only):** OOD, or a perception-health flag (track unstable, `depth_starved`,
  `range_source != depth`) -> suppress forward vx, yaw-only, per the existing `forbid_forward` keystone.
- **Rung 3 fallback-to-P-controller:** sustained disagreement / OOD / uncertainty over N ticks -> hand
  control back to `control.py`'s P-controller (the known-good baseline). **The learned policy is NEVER
  in the loop without the P-controller as a live, instant fallback.**
- **Rung 4 safe-stop:** the existing C++ staleness/deadman floor -- untouched, always underneath.

Shield components that must exist:
1. **OOD / uncertainty check** -- Mahalanobis distance of the live observation vs the training
   distribution (the dataset card's per-channel `stats.json` mean/std -- already emitted by
   `batch_ingest.py`) over a threshold, plus an action-uncertainty proxy (ensemble/temporal variance).
2. **Action-envelope clamp** -- the shadow action is hard-clamped to (a) the same velocity limits as the
   P-controller (the Python<->C++ clamp, unchanged) AND (b) a **per-FSM-state envelope** derived from
   the training data: never command outside the range `control.py` ever produced in that state.
3. **Fallback-to-P-controller** -- a live, tested handover that is the DEFAULT the moment any rung above
   0 trips.

### C. Trigger <-> failure-taxonomy 1:1 mapping (binding)

Every offline failure category P7.5 names (e.g. "forward-lunge disagreement in REACQUIRE", "yaw
oscillation in SEARCH", "standoff creep in TRACK") MUST map to a specific online shield trigger above.
**A failure mode with no online gate is a blocker to actuation** -- every dangerous offline failure has
an online guard, and every guard is offline-measurable. The mapping table is authored alongside the
P7.5 scorecard (taxonomy doctrine: `runtime-safety references/integration.md`).

- [x] **CRITERIA RECORDED (2026-07-10).** P7.6 ships the criteria; building the shield + the eventual
  human-approved switch-flip are a future initiative gated on A (agreement corpus) + B (shield built +
  tested) + C (mapping complete). Until then the shadow node stays log-only, no bridge handle (P7.4).

---

## P6/P7 — On-robot verification of the off-robot pipeline (2026-07-10)

Ran the full offload -> pull -> read -> ingest chain against a **real robot session**
(K1 at 192.168.1.81, GPU clocks pinned via `sudo jetson_clocks`). Results:

**VERIFIED on real robot data:**
- `offload_run.sh` bundled a live session -> `OFFLOAD-OK 20260710T225555Z_nogit`
  (7 files, valid `manifest.json` sha256s, 120 MB `.rrd`).
- `Pull-Run.ps1` pulled + hash-verified (7/7) + marked `.verified` on the Jetson ->
  `PULL-OK`. **Found+fixed a real bug** in the process: the `$remoteRuns` discovery
  variable clobbered the `$RemoteRuns` param (PowerShell case-insensitivity), doubling
  the run-id in every scp path. (commit `fix(pull): ...`).
- **RERUN_COMPAT holds on REAL data** (previously only synthetic): rerun 0.33.1 read the
  robot's 0.23.1-written `.rrd` via the legacy `stream()` fallback -> 160 RGB frames
  (544x448 uint8), 45 depth frames, 5 scalar streams. No decode errors.
- Ingest adapter (`rrd_to_lerobot.assemble_episode`) on real data: default correctly
  **REFUSES** the no-TRACK run (fail-closed, never fabricates `range=0.0` rows); the
  FSM-enum assert **passes** on the robot's real `fsm_state_id` values (id 1.0 ->
  SEARCH_MARKER, zero UNMAPPED -> no enum divergence between the deployed robot and the
  frozen `fsm_groups` vocab).

**GAP diagnosed (blocks a real trainable dataset):** the robot's CURRENT (old) deployment
does **NOT log the follow observation/action channels into the `.rrd`**. Proof: this
session's `events.jsonl` shows a rich real follow — **351 TRACK ticks, 272 REACQUIRE,
771 lock (`seed:true`) ticks, 237 nonzero-velocity ticks (drove, vx up to 0.13)** — yet the
`.rrd` has **zero** `/follow/range`, `/follow/bearing`, `/reid/sim`, `/track/conf` and
**zero** `/cmd/vx`, `/cmd/vyaw`. Only `/health/depth_fps` + a sparse `/fsm/state_id` made
it into the `.rrd`. So the blocker is the **deployed code, not the session** — a
better/locked follow won't help; the old `k1_rerun.py`+`follow_person_k1.py` simply don't
emit those channels to rerun. `events.jsonl` carries actions+fsm but not the observation
state (no range/bearing/sim/conf), so an events-join can't substitute either.

**[ ] DECISION — deploy the P6.1 capture-recording code to the robot?** To mint a real
training episode we must deploy the updated capture stack (`k1_rerun.py` pinhole+odom+
`/follow/*`+`/cmd/*` logging, `config/capture.yaml`, `run_follow_capture.sh`, and the
`follow_person_k1.py` sink wiring) and then run one **heartbeat-enabled locked-follow
session**. Pushing a new build of the ~4.5k-line control loop to a physical robot is a
`VERIFY ON ROBOT` / behavior-risk step — recommend deploying via the app's tested
`Deploy-FollowFiles` path (not an ad-hoc scp of the control loop). Until deployed, the
off-robot dataset/train pipeline is verified on the decode/adapter paths but **not** on a
real trainable episode.

---

## P7.6 — GRADUATION CRITERIA + RUNTIME SHIELD SPEC (2026-07-13, spec-only — BINDING)

**What this is:** the complete, concrete list of conditions that must ALL hold before the shadow
policy is ever allowed to actuate, plus the spec of the runtime shield that must exist first.
Written per PHASE_6-8_PLAN.md P7.6 (skills: runtime-safety + sim-eval). **This entry ships no
code and flips no switch** — building the shield and enabling actuation is a separate, future,
human-approved initiative. Until then the plan §2 invariant stands: the shadow node has NO code
path to the bridge (absent, not flagged off).

Numbers below are **provisional v1**: binding as written, revisable ONLY by a new DECISIONS
entry citing P7.5 scorecard data (a threshold changed without data is a violation). Calibration
doctrine: thresholds sit where the P7.5 offline numbers show agreement degrading — the taxonomy
below is shared with the offline grader precisely so every gate is offline-tunable.

### A. Definitions (shared with P7.5 — the grader MUST implement these verbatim)

- **Tick pair:** `(shadow_vx, shadow_vyaw)` vs `(vx, vyaw)` actually sent, same tick, raw units
  (m/s, rad/s), paired under the checkpoint's recorded `obs_action_pairing` convention (P7.3
  contract). Off-by-one pairing invalidates every number below.
- **Agreement band (v1):** `|Δvx| ≤ 0.05 m/s AND |Δvyaw| ≤ 0.10 rad/s`. A tick inside the band
  agrees; outside disagrees. (Scale: the DEPLOYED clamp is `vx ∈ [−0.06, +0.18] m/s` (hard ceiling
  ±0.30, `common.py HARD_VX_LIMIT`; field.yaml lowers the default cap to 0.15) and
  `vyaw ∈ [−0.30, +0.30]`; the 0.05 m/s band is ≈28% of the +0.18 forward cap. vx is **asymmetric**
  — reverse is capped at −0.06 — so reason in absolute Δ, never a symmetric "|vx|".)
- **Per-state:** every metric computed separately per frozen FSM id (`eval/fsm_states.json` v1:
  TRACK, REACQUIRE, SEARCHING, SEARCH_MARKER, PARKED) via `eval/fsm_groups.py`. Blended numbers
  do not count for anything.
- **Wilson LB:** lower bound of the 95% Wilson interval on the in-band proportion.

### B. Offline agreement criteria (per state; ALL must hold on the graduating checkpoint)

| state | min ticks (floor) | in-band % | Wilson LB | p99 excursion cap |
|---|---|---|---|---|
| TRACK | ≥ 3000 | ≥ 95% | ≥ 0.93 | p99 |Δvx| ≤ 0.10 m/s, p99 |Δvyaw| ≤ 0.20 rad/s |
| REACQUIRE | ≥ 600 | ≥ 90% | ≥ 0.85 | same caps |
| SEARCHING | ≥ 300 | ≥ 90% | ≥ 0.85 | same caps |
| SEARCH_MARKER | ≥ 300 | ≥ 95% | ≥ 0.90 | same caps |
| PARKED | ≥ 600 | ≥ 99% "phantom-free" | ≥ 0.97 | max |shadow_vx| ≤ 0.02, |shadow_vyaw| ≤ 0.05 |

- **A floor unmet = the state is INSUFFICIENT = the checkpoint does NOT graduate**, no matter how
  good its other states look. Close coverage gaps by scheduling runs that induce the state
  (step behind an obstacle for REACQUIRE/SEARCH), never by lowering the floor (plan P7.5).
- **PARKED is special (safety-critical):** "phantom-free" means the shadow output is inside the
  null band while the executed command is identically zero. A policy that wants to move a parked
  robot fails graduation outright.
- **Data basis:** ≥ 10 shadow-enabled live sessions across ≥ 3 distinct days, ALL runs P6.4-labeled,
  scorer + dataset + checkpoint versions pinned in the scorecard; plus the full labeled replay suite.

### C. Non-interference criteria (the shadow must be a perfect ghost first)

1. Decision-stream diff vs shadow-off: **empty** on every session (existing P7.4 gate).
2. Loop-timing: shadow-on p99 `loop_ms` within **10%** of the shadow-off distribution over the
   same session count; staleness-event rate not statistically higher (same tiers).
3. The shadow node **self-suspends on any staleness event** and logs it; ≥ 1 session must
   demonstrate the suspend path actually firing (induce it deliberately).

### D. Offline failure taxonomy → runtime shield triggers (1:1, both directions)

P7.5 MUST name disagreement categories with exactly these keys; the shield MUST gate on each.
An offline mode with no runtime gate, or a gate with no offline measure, is a spec violation
(runtime-safety `references/integration.md` doctrine).

| taxonomy key (P7.5 measures) | definition (offline) | runtime detector | shield rung |
|---|---|---|---|
| `phantom_motion` | shadow motion while executed = 0 / PARKED / forbid_forward | null-band check on proposed cmd vs gate state | **Hold** |
| `lunge` | Δvx > band with shadow overspeeding toward target | envelope: proposed vx vs range-scaled cap (existing anti-lunge gates) | **Caution** → Fallback on repeat |
| `wrong_direction` | sign-opposed vyaw with |Δvyaw| > band | agreement-vs-P divergence check | **Caution** → Fallback |
| `oscillation` | tick-to-tick shadow jerk: |d(cmd)/dt| > 2× the slew limit | temporal-incoherence check | **Caution** |
| `ood_scene` | obs outside the trained distribution (feature-space distance on the checkpoint's normalized obs; threshold at the P7.5 knee) | OOD detector (cheap, classical first) | **Caution** → Fallback after dwell |
| `lost_track_action` | nonzero shadow cmd while track conf < floor / no lock | perception-health gate (reuses ReID health + track conf) | **Hold** |
| `stale_input` | obs staleness beyond tier | liveness watchdog (existing staleness tiers) | **Safe-stop** (existing path) |

### E. The shield (spec of what must be BUILT before any switch flips)

Architecture: the policy **proposes**, the shield **disposes** — a node in the action path
(policy → shield → bridge), never an observer racing to e-stop. Graded rungs, never a bare kill:

- **Nominal** — policy cmd passes through the EXISTING 3-layer velocity clamps + slew limits
  (unchanged; the shield adds no new nominal-path math).
- **Caution** — velocity-scale the proposed cmd by 0.5 and tighten the range gates; triggered by
  any Caution-rung detector in §D.
- **Hold** — zero-velocity stable posture; policy output not consumed; recover only after the
  trigger clears for the dwell.
- **Fallback-to-P-controller** — the incumbent `control.py` proportional law takes the tick.
  Structural requirement: **the P-controller keeps computing every tick forever** (it is never
  removed from the loop); fallback is an atomic source-swap, not a mode change.
- **Safe-stop** — the EXISTING safety spine exit (`MoveCommand(0,0,0)` + `kPrepare`, C++ floor,
  mode-keyed fail-closed gate). The shield adds NO new stop mechanism — it reuses the proven one.
- **Human handoff** — the existing gamepad-held deadman remains sovereign and above all rungs.

Rules: **escalate immediately, de-escalate slowly** (dwell ≥ 3 s clear for Caution→Nominal,
≥ 5 s for Hold; hysteresis prevents flapping). **Fail-safe:** if the shield itself crashes or
its inputs go stale, the tick falls back to the P-controller (the incumbent, fully-safeguarded
behavior) and an alert is logged — the system never defaults to trusting the policy. The C++
floor and heartbeat gate stay sovereign beneath everything; the shield consumes single-digit ms
of the loop budget, measured under the same combined YOLO+ReID load as P7.3.

### F. Structural preconditions (all must exist; each independently verifiable)

1. P7.5 scorecard implementing §A–§B verbatim, one command, per-state, versions pinned.
2. The §E shield built + its own replay/selftest gates green, including a demonstrated trip of
   EVERY rung on injected inputs (per-rung test vectors, not just Nominal).
3. TRT parity + latency gates (P7.3) passed on the graduating checkpoint, power mode recorded.
4. §C non-interference proven on the same build that would actuate.
5. Every §D taxonomy key measured in ≥ 1 offline scorecard (so thresholds are calibrated, not guessed).
6. A new signed DECISIONS entry, written by a human, naming the checkpoint hash + scorecard +
   shield build being enabled. **Enabling actuation is a code change through review — never a
   runtime flag, config default, or env var.**

