---

# SCAFFOLD CHARTER â€” PHASE 6â€“8 Blind-Authoring Decision

**Governing axis:** verifiability (a one-command desktop self-test on synthetic fixtures) + design-context value. **Not** typing location â€” git moves code for free. Where an adversarial verifier REFUTED a seat claim, the correction is binding below.

**Two verifier blockers are load-bearing and resolved in this charter (both confirmed against the worktree):**
- **B1 â€” phantom P6.4 occupancy.** The plan (`PHASE_6-8_PLAN.md` L137, L282) claims P6.4 folds FSM-state occupancy into `manifest.json`/`index.json` and "already expose[s] coverage gaps." **False in shipped code:** `eval/replay_eval.py::score_outcome` (L245) emits `{frames, track_frames, track_frac, standoff_in_band_frac, forbidden_forward, geofence_breach, id_switches, operator_retention, fails}` â€” **no per-state counts**. The INSUFFICIENT-floor denominator P7.5/S5 needs **does not exist**. Ownership must be assigned now (S1 + a small `label_run` extension), or v1 mints blind and forces a re-mint.
- **B2 â€” `recon.py`/rsync contract contradiction.** Plan P6G.3 (L184â€“189) names `recon.py submit|status|fetch` over rsync. The laptop has **no Python and no rsync** (`Pull-Run.ps1`/`Sync-Runs.ps1` headers state "no rsync, no Python"). The laptop-side CLI **must be PowerShell over Windows-OpenSSH scp**; rsync survives as intent only, desktop-side inside WSL.

---

## 1. Verdict Table

| Item | Verdict | Delivery form | One-line reason |
|------|---------|---------------|-----------------|
| **S1** `eval/batch_ingest.py` | **GO** (as-spec + B1 fix) | **Full scaffold** (imports proven `rrd_to_lerobot` decode; batch/hash/split logic is pure) | Design frozen in COMPUTE_PLACEMENT Â§3; risk is in mockable LOGIC; the one place blind code is genuinely verifiable â€” **must own per-FSM-state counts (B1)**. |
| **S2** recon round-trip | **GO-AS-SPEC** (reframed to PowerShell) | **Interface-stub + spec**: `desktop/Recon.ps1` skeleton (PowerShell) + `docs/RECON_CONTRACT.md` (`recon_manifest.json` schema) | Transport idiom is repo-proven (`Pull-Run.ps1`), but the substrate doesn't exist yet; **Python+rsync as specced cannot run where the gate requires (B2)**. |
| **S3** recon image | **GO-AS-SPEC** (contract + smoke only) | **Interface-stub + spec**: `desktop/recon.Dockerfile` (CANDIDATE) + stage-CLI + synthetic-TSDF smoke stage + `docs/RECON_CONTRACT.md` (shared w/ S2); **lockfile + geometry DEFERRED** | Container contract is declarative and cheap to freeze; a blind lockfile is fiction (produced by resolving on-target); real odom/loop-closure/align is unverifiable without data + GPU. |
| **S4** `docs/WSL2_SUBSTRATE.md` | **GO** | **Full doc** (spec-only == full for a runbook) | Encodes anti-default decisions already frozen (Docker-Engine-in-distro, driver-on-Windows-only, ext4-not-/mnt/c); each step self-gates; near-zero wrong-contract cost. |
| **S5** train skeleton (ACT) | **DEFER** | **Spec-only**: `docs/TRAIN_CONTRACT.md` + `eval/checkpoint_contract.py` (~60 LOC, stdlib) authored blind | The churny parts (CVAE wiring, chunk/attention off-by-ones) need an interpreter to smoke; commodity model, near-zero design-context delta; consumes an unminted format â†’ compounding drift. Contracts ship, code doesn't. |
| **S6** `eval/synth_fixtures.py` *(council-added)* | **GO** | **Full scaffold** (stdlib + numpy + rerun-sdk) | The **only** reason the self-test-or-defer constraint is satisfiable â€” S1, S2, S5 all self-test against one fixture source; three ad-hoc fabricators would drift. **Author first.** |
| **label_run occupancy patch** *(council-added, B1)* | **GO** | **Small full edit** to `eval/label_run.py` | Closes the phantom-occupancy gap the plan promised; fixture-testable offline via S6; without it, coverage-gap scheduling has no signal. |

**Rejected minority position (Devil's-advocate DEFER on S1/S2/S3-code):** S1 is kept GO because its risk was correctly shown by three seats + Verifier-1 to sit in mockable logic above an *already-proven, stdlib-importable* decoder â€” the DA's "experimental-API breaks reuse" claim was **REFUTED** (the `>=0.33` dep is a documented call-time desktop dep, cleanly bypassed by the S6 episode-provider seam). The DA's S2/S3 concerns are **honored** by demoting both to interface-stub+spec, not full code.

---

## 2. Binding Contracts, Self-Tests, and Mitigations (per GO item)

### S6 â€” `eval/synth_fixtures.py` *(author first; everything else self-tests against it)*

**Binding contracts:**
- Emits `offload_run.sh`-shaped bundles under a target `runs/<run_id>/` dir: `manifest.json` (per-file `sha256`/`bytes`, `git_version`, `profile`, `duration_s`, `file_count`, `total_bytes`, `files[]` â€” matching `offload_run.sh` L143â€“162 schema, `sort_keys`), `events.jsonl` (per-tick `t/fsm/walk/vx/vy/vyaw/loop_ms/seed/hb_req`), `k1_follow.err` with `TRACK` lines matching `replay_eval._TRACK_RE` + a `REPLAY-END frames=N` line, `config/defaults.yaml`, and a real `.rrd` via rerun-sdk mirroring `k1_rerun.py` entity paths/timelines (`/follow/*`, `/cmd/*`, `/reid/sim`, `/track/conf`, `/health/depth_fps`, `/fsm/state_id`, decimated `/camera/rgb`, `DepthImage(meter=1.0)`, static `Pinhole`).
- **Deterministic under `--seed`** (non-negotiable â€” S1's re-run==same-`content_hash` gate depends on it).
- **Planted edge cases** (required, not optional): one labeled `pass`; one **unlabeled** (absent `manifest.outcome`); one **no-`.rrd`** non-capture bundle (JSONL+err only, per `offload_run.sh` L59); one with an **unmapped FSM-state string**; one with an **under-floor** FSM state for S5's INSUFFICIENT rendering.
- **Outcome vocabulary is derived, not enumerated** (verifier MINOR): code vocab is `{pass, review, incomplete}` + absent-label. "unlabeled" = absent `manifest.outcome`; "no-rrd" = bundle-contents inference. Do not invent enum strings that don't exist in `label_run.py`.

**Mitigations:** stdlib + numpy + rerun-sdk only; one file; no cleverness. rerun-sdk is the ingest dep anyway.
**Self-test:** folded into consumers â€” S6 correctness is asserted by S1's selftest reproducing `content_hash` twice over S6 output.

---

### S1 â€” `eval/batch_ingest.py`

**Binding contracts (freeze verbatim in the file header):**
- **Reuse, don't fork:** `from rrd_to_lerobot import read_rrd, assemble_episode, _write_video` â€” confirmed stdlib-only at import (`argparse, json, os, sys`; rerun/numpy/pyarrow lazy). The two review-fixes (trim-to-first-`/follow/range`; nearest-EARLIER image never future) are load-bearing â€” inherit them, do not re-type.
- **Layer seam (mandatory for a data-free selftest):** (a) rrdâ†’episode adapter that imports the decoder; (b) batch core (indexing, splits, stats, hashing, card) consuming an **injected iterator of in-memory episodes** â€” so `selftest` needs no rerun/`.rrd`/GPU.
- **RAW LeRobot-v3 layout only.** Never `import lerobot` (pins `torch<2.12`; not hash-stable). Mirror `rrd_to_lerobot.write_raw`.
- **Episode = one `runs/<run_id>/` bundle.** `episode_index` = 0-based contiguous over INCLUDED runs, ordered `sorted(run_id)` ascending. `run_id` is stable identity; card carries the full `run_idâ†’episode_index` map.
- **`meta/splits.json`** keyed by **`run_id`** (not `episode_index`): `{"version":1,"dataset_version":"k1_follow_v1","seed":<int>,"method":"seeded_shuffle_sorted_run_ids","val_fraction":<f>,"train":[run_id...],"val":[run_id...]}`, both lists sorted, split is a **pure deterministic function of sorted included `run_id`s + seed**. **Refuse to mint** below 2 included episodes or with 0 val episodes (no `--allow-single` in v1). P7.2 reads this and **never re-splits**.
- **`meta/stats.json`** over **TRAIN split only**; state+action only (no image stats in v1). Per-channel `min/max/mean/std(ddof=0)/count`, **float64** accumulation, fixed sorted-episode order; must equal a plain-numpy reference (asserted in selftest). Carry a `normalize` block naming z-scored vs passthrough channel indices (`rsrc_is_depth` idx 2, `fsm_state_id` idx 6 passthrough) so P7.2 never hardcodes indices.
- **`stats_hash`** = `sha256` of the **exact file bytes** of `meta/stats.json` (write pinned: `json.dump(sort_keys=True, indent=2)`). Embedded verbatim in every P7.2 checkpoint; P7.3/P7.4 assert card-hash==checkpoint-hash.
- **`content_hash`** = `sha256` over the sorted sequence of `relpath + "\0" + sha256(file_bytes) + "\n"` for every file under `data/` and `meta/`, **EXCLUDING** `meta/info.json` (self-reference) and **all `videos/**` bytes**. mp4s are represented by `meta/video_index.json` (per-episode frame_indexâ†’source `.rrd frame_idx` map, frame count, dims, pinned encode params `libx264/crf/yuv420p/-threads 1/+faststart`) â€” that file **is** hashed. mp4-byte determinism is **not** claimed â†’ mark `VERIFY ON DESKTOP`.
- **FSM assertion (BLOCKER semantics):** load `eval/fsm_states.json` (record its version in card). Every `/fsm/state_id` value in every ingested `.rrd` must match a frozen id within `1e-6` (member of `{3.0,2.0,1.5,1.0,0.0}`). Any other value â€” **including `unmapped_id -1.0`** â€” **aborts the whole mint** naming `run_id`+value+frame. No skip-softening.
- **Label filter:** source of truth = each bundle's `manifest.json` label block (`label_run.py` writes it; `index.json` is derived). Default include = `{"pass"}`; `--include-outcomes` overrides; recorded verbatim in card. An **unlabeled** run (absent `manifest.outcome`) is excluded **loudly** â€” one line per `run_id` + reason, an `excluded_runs` list with per-run reasons (`unlabeled | outcome-filtered | no-rrd-not-a-capture-run | no-TRACK-frames | manifest-hash-mismatch`), non-zero exclusion count printed. Never zero-fill.
- **Pre-ingest integrity:** verify each bundle's `manifest.json` `sha256`s vs on-disk files before reading; mismatch = exclude loudly.
- **P7.1 gate is filter-aware:** `N episodes == N runs PASSING the filter`; `total offloaded == included + excluded (enumerated)`.
- **B1 â€” S1 OWNS per-FSM-state counts** (the plan's promised P6.4 occupancy does not exist in code): compute per-episode AND per-split (train/val) per-state **frame counts** from the `.rrd` `/fsm/state_id` stream, keyed by canonical name via a **shared fsm-grouping helper** (see below), written into the card. Do not read them from `label_run` â€” verified absent.
- **Shared fsm-grouping helper** (micro-module, imported by S1's card writer, S5's val report, and the future P7.5 grader): loads `fsm_states.json`, exposes one idâ†’canonical-name reverse map with the **single SEARCH/SEARCH_MARKER rule** (id 1.0 reports as canonical `SEARCH_MARKER`, `SEARCH` documented as its alias; `SEARCHING=1.5` distinct; `-1.0` â†’ `UNMAPPED` = data bug). One source of grouping truth â€” two hand-rolled maps guaranteed to drift.
- **Card provenance block:** `dataset_version`, feature contract verbatim (incl. `vy â‰¡ 0, omitted`), `content_hash`, `stats_hash`, `fps`, `fsm_states` version, rerun-sdk version, ingest-tool git SHA (or honest `nogit`), encode params, split seed/method, **per-run rows** (`run_id, git_version, profile, outcome, scorer_version, duration_s`, per-state counts), include-filter + `excluded_runs` with reasons, trust block (depth loosely aligned + not exported, intrinsics approximate `fy:=fx`, mono RGB, `vy` omitted, timestamps synthetic at nominal fps), episode trim rule + frames-dropped-per-episode (honest note that pre-lock SEARCH is dropped â†’ SEARCH counts near-zero), obsâ†”action tick-pairing convention (`action_t` = command emitted same control tick as `obs_t`).
- **Versioning:** `--version k1_follow_v{N}` explicit; **refuse if output dir exists** (never overwrite/auto-increment). Depth stays OUT (keep `--with-depth` failing loudly); odometry channel waits for P6.1a runs â†’ `v{N+1}`.
- **`dataset_card/` mirror** (info.json + card + splits.json + stats.json, no parquet/mp4) synced back to the laptop runs store â€” laptop stays source-of-truth for dataset identity; bytes live on disposable ext4 (regenerable by determinism).
- **Small-N behavior defined:** refuse below N=3 included, OR 1-episode val with a loud small-N warning recorded in the card. Undefined here = P7.2 rework.

**Mitigations:** single file; no new deps beyond `rrd_to_lerobot`'s (numpy, pyarrow); no metaprogramming.
**Mandated self-test:** `python eval/batch_ingest.py selftest` â€” via S6, fabricates 3 valid + 1 unlabeled + 1 no-rrd + 1 unmapped-FSM bundle; mints twice; asserts: **identical `content_hash` across runs**, N episodes == N included, unlabeled excluded loudly, unmapped-id **aborts the mint**, `meta/stats.json` == plain-numpy TRAIN-only reference on known synthetic values, `splits.json` shape + determinism (pure fn of run_id), **per-state counts present and matching fixture ground truth**. Prints `INGEST-SELFTEST-OK`. No robot data, no GPU.
**`VERIFY ON DESKTOP`:** first real multi-run `.rrd` corpus; mp4 encode-param determinism; `pyarrow` version stability of the double-run hash.

---

### S2 â€” recon round-trip (reframed: **PowerShell**, per B2)

**Binding contracts:**
- **Laptop CLI = `desktop/Recon.ps1`** (PowerShell), NOT `recon.py`. Verbs `-Submit <run_id> | -Status [run_id] | -Fetch <run_id> | -SelfTest`; params `-DesktopHost/-Port/-IdentityFile`. Mirror `Pull-Run.ps1`'s `Invoke-SshCapture`/`Invoke-Scp` manifest-verify-or-fetch idiom verbatim. Desktop-side job wrapper = `desktop/recon_job.sh` (bash, deployed into the WSL distro). **Record the `recon.pyâ†’Recon.ps1` deviation in `DECISIONS.md`.**
- **Transport:** laptop-initiated only, Windows OpenSSH client â†’ WSL-distro `sshd` (default port 2222, **key auth**, no password â€” new box, no legacy constraint). Submit = per-file scp driven by the bundle's `manifest.json`, skipping files whose remote `sha256` already matches; fetch = same idiom driven by `recon_manifest.json`. Both resumable/converging; loud `RECON-PUSH-INCOMPLETE`/`RECON-FETCH-INCOMPLETE` on mismatch. rsync survives as **intent only**, never a laptop binary.
- **Inbox/outbox layout** (all ext4, never `/mnt/c`): `~/k1/inbox/<run_id>/` = bundle, immutable, RO-mounted into container; `~/k1/outbox/<run_id>/` = artifacts + `recon.log` + per-stage `status.json` + `recon_manifest.json`, RW. Wipe-and-resubmit of both = the P6G.3 disposability gate.
- **Lock semantics = container name, no hand-rolled lockfile.** `recon_job.sh` validates inbox hashes vs `manifest.json` (refuses on torn bundle), then `docker run -d --name recon_<run_id>`. The container name IS the lock: submit refuses `RECON-BUSY <run_id>` if any `recon_*` container runs, and refuses if an un-fetched outbox exists for that `run_id`. Crash-release is free (docker owns the pid); a died job is a permanently-visible exited container, never silent.
- **`recon_manifest.json` schema (frozen now in `docs/RECON_CONTRACT.md`, shared with S3):** `{run_id, image_digest (=recon_version), stages:[{name, status: ok|failed|skipped|not_implemented, exit_code, wall_s, params, metrics, inputs_hash}], seed, intrinsics_source, started_utc, finished_utc}`. Failures recorded loudly with the failing stage named â€” never a silent hang.
- **Fetch merge stays Python-free:** artifacts â†’ `runs/<run_id>/recon/` on the laptop; extend **`desktop/Runs.ps1`** to surface `recon_manifest.json` into `runs/index.json` (P6.4-style, PowerShell). A stage marked `failed` makes fetch exit nonzero.
- **Runbook line (script header):** disable **desktop** Windows sleep before submitting (`powercfg /change standby-timeout-ac 0`) â€” laptop sleep is harmless by design (detached container); desktop sleep suspends WSL2 and kills the GPU job.

**Mitigations:** no daemon, no queue (YAGNI); one lock via container name; transport helper is a thin function (abstracts scp vs future robocopy); no route/credentials to the robot from this path.
**Mandated self-test (Tier 1, no SSH/Docker/data):** `powershell -File desktop/Recon.ps1 -SelfTest` â€” fabricates a synthetic bundle (via S6 output shape) in `$env:TEMP`, exercises manifest verify-or-fetch, corrupts one byte and asserts the mismatch is caught, prints `RECON-SELFTEST-OK`.
**`VERIFY ON DESKTOP` (Tier 2, documented in-script, after P6G.1):** full loop from the laptop against a stub image (`recon_job.sh --stub` = busybox copying inboxâ†’outbox + fake per-stage status + `recon_manifest.json`), plus wipe-and-resubmit. The Tier-1 selftest is **not** the P6G.3 gate â€” tag it so nobody mistakes it.

---

### S3 â€” recon image (contract + smoke only; geometry + lockfile DEFERRED)

**Binding contracts (author blind):**
- **`docs/RECON_CONTRACT.md`** (shared with S2): stages `= ingest|odom|tsdf|simexport|align`; output tree `runs/<id>/recon/{trajectory.jsonl, pose_graph.json, mesh_visual.ply, mesh_collision/part_*.obj, apriltag_observations.json, recon_manifest.json}`; every artifact **names its frame per `FRAMES.md`** (frame-tagging enforced in the manifest writer â€” an untagged artifact is a bug). `recon_manifest.json` schema = the S2-shared schema above.
- **Stage CLI:** `recon --run <id> --stages odom,tsdf,align[,splat]` â€” each stage a uniform `(bundle_dir, out_dir, prior_stage_outputs)` function; unknown stage = loud error. `odom/tsdf/align/splat` **registered but exit `not_implemented` LOUDLY** (named per-stage `not_implemented` status in the manifest, never a fake `ok`).
- **`recon_version` = image digest**, read at runtime from a build-injected label/env â€” **never hardcoded** (the digest exists only after a build). Recorded in every artifact.
- **NO COLMAP anywhere** (doctrine comment at the top of the entrypoint): poses from RGBD odometry + loop closure only; splat initializes from the TSDF cloud.
- **Fail-closed intrinsics gate (executable, per P8.1 hard-prereq):** metric stages (`odom`, `tsdf`) **refuse** on `approximate:true` intrinsics (the `fy:=fx` seed) unless `--allow-approximate-intrinsics` is passed and recorded in the manifest.
- **Depth-unit sanity dies in ingest:** assert finite in-range (~0.15â€“15 m) `DepthImage(meter=1.0)` values; record `depth_scale` â€” the 1000Ã— mm-vs-m garage dies here.
- **RGBD pairing joins by WALL time**, never `frame_idx` (depth `frame_idx` is best-effort per RUN_ARTIFACTS Â§3.1), with a max-skew reject (~60 ms) + pairing stats in the manifest.
- **Person masking v0** = the logged TARGET box only (`/camera/rgb/target` Boxes2D â€” all-person YOLO boxes are NOT recorded today): dilate, zero depth inside before BOTH odom and TSDF; record dilation + masked fraction. Flag the bystander gap in `DECISIONS.md`.
- **Sim-export contract documented** (stage stubbed): watertight collision mesh via **convex decomposition** (per-part watertight by construction), quadric-decimated visual mesh, frame-tagged. Keeps the stage enum stable.
- **Candidate Dockerfile** (`desktop/recon.Dockerfile`), header `# CANDIDATE â€” pins verified at first desktop build; record digest then`. **Image v1 is CPU-lean:** Open3D (CPU wheel) + OpenCV + numpy + rerun-sdk (pinned `0.33.x` to match the robot writer) + CoACD + an AprilTag lib; **NO torch/gsplat/CUDA-python** (splat/CUDA arrive as a v2 image bump at P6G.4). This collapses most blind-pinning risk. Ship `requirements-recon.in` (unpinned intent) + a lock-**generation** recipe.

**Explicitly DEFERRED to desktop:** the committed **lockfile** (generated from the first successful desktop build via `pip freeze` in-container, committed then â€” a blind lockfile is fiction); real **odom/loop-closure/align** implementations (unverifiable without data + GPU; align's gate literally needs two real runs from different days); all **splat** deps.

**Mitigations:** one `ARG` block for base tag/digest; stage contents are xr-engineer's seat, build hygiene is autonomy-ops'; no COLMAP slot in the registry.
**Mandated self-test (after on-box build):** `docker run --rm k1recon:dev recon --selftest` â€” generates synthetic RGBD of a known plane + identity poses in-container (pure numpy, no robot data), runs the TSDF-integrate smoke stage, asserts mesh vertex count > 0 and plane-fit RMS under threshold, emits a well-formed `recon_manifest.json` including a deliberately-failed stage recorded loudly. Prints `RECON-IMAGE-SELFTEST-OK`.
**`VERIFY ON DESKTOP`:** P6G.2 garage-bundle end-to-end + digest-stable rebuild (data-blocked); CUDA/Open3D wheel resolution.

---

### S4 â€” `docs/WSL2_SUBSTRATE.md`

**Binding contracts:**
- **Structure = decisions-first, self-gating:** each numbered stage leads with the decision + why, then exact commands, then its own PROVE-IT verification command + expected output. The reader never proceeds on faith.
- **PRESCRIPTIVE (stable â€” write exact commands):** `/etc/wsl.conf [boot] systemd=true`; `.wslconfig` memory cap (~48 GB of 64, leaving Windows headroom) + `networkingMode`; **NVIDIA driver on Windows ONLY** â€” never a Linux GPU driver in the distro (CUDA user-mode via `/usr/lib/wsl/lib`); `openssh-server` on **port 2222, `PasswordAuthentication no`** + laptop-side `ssh-keygen`/key-install (S2 targets this); `New-NetFirewallRule` inbound allow for 2222 (the classic silent gate-killer); data layout `~/k1/{inbox,outbox,runs,bin}` on **ext4** with the 9P `/mnt/c` warning; `powercfg /change standby-timeout-ac 0` (S2's dead-job cause).
- **VERIFY-ON-BOX (phrase as checks, use the `WORKSTATION_SETUP.md` âœ…/âš  legend):** distro name (`wsl --list --online`, prefer 24.04 LTS if listed); **explicit BRANCH** on mirrored networking support for this Win11 build â†’ mirrored if supported, else `netsh interface portproxy` fallback (first-class, with the re-run-on-WSL-IP-change caveat, NOT a footnote); Docker Engine apt keyring/repo lines (âš  confirm at docs.docker.com); `nvidia-container-toolkit` repo lines (âš  confirm at NVIDIA's page â€” this path has churned); Windows driver adequacy for the WSL CUDA path; chosen `nvidia/cuda` base tag existence (record digest after first pull); vhdx placement if C: is tight.
- **Doctrine callouts as warnings:** never a Linux GPU driver in WSL; never run data under `/mnt/c`.
- **Cross-link, don't duplicate:** supersedes `WORKSTATION_SETUP.md` for substrate but preserves its verified findings (`rerun-sdk>=0.33` hard ingest dep; do NOT install lerobot; torch = cu126 index for the 3080).
- **Ends with the P6G.1 gate verbatim:** laptop-initiated `ssh -p 2222 <user>@<desktop> "docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi"` shows the 3080, plus an ext4 read-speed check on a copied test bundle. Followed by the ordered what-runs-next pointer (P6G.2 build â†’ P6G.3 round-trip â†’ P7.1 selftest) handing off to `HANDOFF_DESKTOP.md`.
- **As-built instruction:** direct the desktop session to edit the doc in place as commands actually run.

**Mitigations:** no code; every externally-sourced install sequence marked "verify current syntax/vendor docs before running."
**Self-test:** N/A (document) â€” the documented exception. Each stage embeds its own PROVE-IT check; the terminal acceptance is the P6G.1 gate command run from the laptop.

---

## 3. Authoring Order + Rationale

1. **S6 `eval/synth_fixtures.py`** â€” **first, unconditionally.** It is the shared fixture source that makes S1's and S2's self-tests (and later S5's) satisfiable at all. Nothing that claims a self-test can be trusted before S6 exists. Deterministic-under-`--seed` is the gating property.
2. **`label_run.py` occupancy patch + shared fsm-grouping helper** â€” immediately after S6, before S1's card writer. Closes B1 (per-state occupancy from `events.jsonl` `fsm` field into label metrics/`index.json`) and provides the single idâ†’canonical-name map S1's card + S5's report + P7.5 all import. Fixture-testable via S6. **Doing this before S1 prevents S1 minting a v1 card against a phantom field.**
3. **S1 `eval/batch_ingest.py`** â€” the highest-value blind artifact; co-designs the cardâ†”checkpoint stats-hash handshake with S5's contract while design context is hot. Depends on S6 (selftest) + the helper (per-state counts).
4. **`docs/RECON_CONTRACT.md`** (the `recon_manifest.json` + stage schema) â€” freeze before both S2 and S3 code, since both bind to it. Authored by the same head that writes S3 to keep them coherent.
5. **S3 `recon.Dockerfile` + stage-CLI + smoke stage** â€” against the frozen contract; CPU-lean image, geometry/lockfile deferred.
6. **S2 `desktop/Recon.ps1` + `recon_job.sh` + `Runs.ps1` extension** â€” against S3's frozen manifest schema.
7. **S4 `docs/WSL2_SUBSTRATE.md`** â€” **order-independent**, and is the **first thing the desktop session executes** (P6G.1 must pass before any other self-test can run). Can be written in parallel any time; place it here only because 1â€“6 are the interdependent chain.
8. **S5 contracts** (`docs/TRAIN_CONTRACT.md` + `eval/checkpoint_contract.py`) â€” last of the blind work; must be S1-consistent (same `stats_hash` definition, same fsm-grouping helper, same `splits.json` consumption rule).

**Sequencing vs the still-running code-review findings:** the two confirmed blockers (B1 phantom occupancy, B2 recon.py/rsync) are already folded into steps 2 and 4/6 respectively â€” they are **prerequisites**, resolved before the dependent code is authored, not post-hoc patches. No GO item's contract is left waiting on an unresolved review finding. If the still-running review surfaces a *third* material contradiction in `rrd_to_lerobot.py`'s decode (the one imported, unmodified surface), S1 pauses at step 3 until it's adjudicated â€” but the verifier already confirmed that reuse path is stdlib-clean and the two existing review-fixes are load-bearing and correct, so this is a low-probability hold.

---

## 4. Explicit DEFER List (what the desktop gets instead)

| Deferred | Why deferred | Desktop gets (spec pointer) |
|----------|--------------|------------------------------|
| **S5 ACT train skeleton** (`train_act.py`) | Churny parts (CVAE wiring, chunk/attention/dataloader off-by-ones, resume semantics) need an interpreter to smoke â€” blind authoring converts 5-min REPL fixes into desktop debug-of-untested-code; commodity model, near-zero design-context delta; consumes an unminted format â†’ compounding drift; sits last in the desktop's own ordering, so authoring now buys zero schedule. | **`docs/TRAIN_CONTRACT.md`** (checkpoint payload `{state_dict, norm_stats verbatim, stats_hash, dataset_version, splits_source, config, git_sha, torch/env}`; `splits.json` read-never-resplit; refuse on `dataset_version` mismatch; **per-FSM-state val MSE** with per-state sample counts + INSUFFICIENT floors via the shared helper; fixed image-normalization constant recorded in the checkpoint; chunk-consumption + obsâ†”action tick-pairing named as MUST-DECIDE-BEFORE-P7.2 P7.3 fields; no-lerobot + torch-cu126 restated). Plus **`eval/checkpoint_contract.py`** (~60 LOC stdlib) â€” save/load helpers enforcing the payload + `stats_hash==card` assert, imported by future `train.py`, P7.3 export, P7.4 shadow node, so the #1 norm-mismatch bug is structurally impossible. **Its own self-test:** `python eval/checkpoint_contract.py selftest` (round-trip synthetic payload, assert mismatch raises loudly, happy path byte-stable). The desktop authors `train_act.py` from scratch against this frozen contract; its `selftest` (CPU overfit on S6 fixtures, loss-decrease + checkpoint/stats-hash round-trip + per-state report) ships from the desktop. |
| **S3 recon lockfile** | A lockfile is produced by resolving on the target platform; a blind-typed one is fiction and inverts the point of pinning (3080 = sm_86 constrains Open3D/CUDA combos). `recon_version` = image digest, which cannot exist without a build. | `requirements-recon.in` + the lock-**generation** recipe in the Dockerfile; desktop mints + commits the real lockfile from the first successful build. |
| **S3 geometry stages** (real odom / loop-closure / TSDF-on-real-data / cross-run align / AprilTag) | Unverifiable on every axis blind â€” no data, no GPU; align's gate needs two real runs from different days; synthetic-clean odom flatters the metric and over-fits the design to fake data. | The frozen stage CLI + `recon_manifest.json` schema + loud `not_implemented` stubs + the synthetic-TSDF smoke stage + all doctrine (no-COLMAP, splat-from-TSDF, masking, wall-time pairing, intrinsics gate). Desktop implements against the first real (or Open3D-rendered) bundle. |
| **S3 splat + CUDA image (v2)** | Not needed until P6G.4 (itself gated on P8.3); GPU/Open3D-CUDA is the messiest drift surface. | v1 is CPU-lean; splat arrives as a documented image version bump. |

**Nothing else is added.** YAGNI holds: no new scaffolds. The P7.3/P7.4 Jetson items and the P7.5 scorer are correctly absent (Jetson-verifiable only / data-blocked). Depth export stays out (`--with-depth` fails loudly); odometry channel waits for P6.1a runs â†’ `v{N+1}`.

**One-line addition to `docs/HANDOFF_DESKTOP.md` "what this box does first":** step 0 = run the shipped self-tests (`batch_ingest selftest`, `checkpoint_contract selftest`, `Recon.ps1 -SelfTest`) **immediately after the Python env exists and BEFORE any real data work** â€” blind-authored code is presumed broken until its self-test passes on the desktop. Plus a pointer block naming the authored scaffolds + their one-command self-tests + the S5 deferral contracts.

---

## 5. Unresolved Disagreements (called out honestly)

1. **S1 as full code vs interface-stub+spec.** The Devil's-advocate holds S1 should be interface-stub+spec (decode imported, batch logic as code) to avoid *any* re-typed decode risk; the other four seats + Verifier-1 treat it as full scaffold. **Resolution taken:** full scaffold, but the DA's core mitigation is **binding** â€” the decoder is *imported never re-typed*, and the batch core sits behind an injected episode-provider seam so the selftest touches zero rerun/`.rrd`. This is functionally the DA's interface-stub with a different label; the disagreement is nominal, but flagged because if the desktop finds `assemble_episode` needs batch-edge changes, the **fork-vs-patch temptation is a real latent second-decode-path risk** the charter forbids (patch the shared function, never fork).

2. **Whether the `label_run` occupancy patch belongs in S1 or as its own edit.** sim-eval/Verifier-1 want it in `label_run.py` (occupancy from `events.jsonl`, flowing to `index.json` for run-scheduling signal); policy-engineer's framing implies S1 can self-source per-state counts from the `.rrd` directly. **Resolution taken:** **both, deliberately** â€” S1 owns the *card's* per-state counts from the `.rrd` (authoritative for the dataset), and `label_run.py` gets the occupancy patch for the *run-index* coverage-gap signal. They must agree via the shared helper. Residual risk: if the two ever diverge (e.g., `.rrd` vs `events.jsonl` FSM sampling differ), the card is authoritative for training and the index is advisory for scheduling â€” stated so no one treats them as a single source.

3. **Whether S2/S3 should be code at all vs spec-only.** The Devil's-advocate argues both are pure environment-glue whose mock self-tests "certify a scaffold that fails on first real contact" â€” i.e., spec-only. The four builder seats keep them as thin contract-bearing skeletons. **Resolution taken:** interface-stub+spec (skeleton + frozen contract), **not** full implementations and **not** spec-only â€” the transport idiom (S2) and container plumbing (S3) are repo-proven enough that a Tier-1 mock genuinely catches manifest/hash/lock-logic regressions, while the environment-dependent Tier-2 loop is explicitly tagged `VERIFY ON DESKTOP` so no one mistakes the mock for the P6G.3/P6G.2 gate. The DA's warning is honored as the **tagging discipline**, not as a reason to withhold the skeletons.