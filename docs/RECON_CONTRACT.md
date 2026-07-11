# RECON CONTRACT -- the frozen recon job interface (charter S2 + S3, shared)

**Status: FROZEN.** This is the contract both sides of the recon round-trip code against:
the laptop CLI (`desktop/Recon.ps1`, charter S2), the desktop job wrapper
(`desktop/recon_job.sh`, S2), and the recon container (`desktop/recon/cli.py` +
`desktop/recon/recon.Dockerfile`, S3). Authored blind per `docs/SCAFFOLD_CHARTER.md`; the
schema and stage enum below do NOT change without a coordinated amendment across all three
consumers -- an ad-hoc field rename here breaks code on two other machines. Everything
environment-dependent is tagged `VERIFY ON DESKTOP`.

Related: `docs/FRAMES.md` (frame conventions -- READ it; every artifact names its frame),
`docs/RUN_ARTIFACTS.md` (what a capture bundle carries), `docs/HANDOFF_DESKTOP.md` (P6G
ordering), `DECISIONS.md` P6G.3a (recon.py/rsync -> Recon.ps1/scp) and P8.2a (person-mask
bystander gap).

---

## 1. Topology and layout

Laptop-initiated only (Windows OpenSSH -> WSL-distro sshd, default port 2222, key auth).
No daemon, no queue, no route to the robot from this path. All desktop data on ext4,
never `/mnt/c`.

```
~/k1/inbox/<run_id>/      the offload bundle (manifest.json + .rrd + events.jsonl + ...)
                          IMMUTABLE once submitted: RO-mounted into the container; nothing
                          on the desktop ever writes into it. Re-submit = wipe + re-push.
~/k1/outbox/<run_id>/     RW job output: recon artifacts + recon_manifest.json (container-
                          owned) + recon.log / status.json (wrapper-owned; shape is the
                          wrapper's concern, the manifest is the authoritative record).
```

**Lock semantics: the container name IS the lock.** `recon_job.sh` runs
`docker run -d --name recon_<run_id>`; submit refuses `RECON-BUSY` while any `recon_*`
container exists, and refuses if an un-fetched outbox exists for that run_id. A died job is
a permanently visible exited container, never a silent hang.

**Disposability gate (P6G.3):** wipe `~/k1/inbox/<run_id>` + `~/k1/outbox/<run_id>`,
resubmit from the laptop, and the fetched artifacts must be tolerance-identical. The
desktop holds no state the laptop cannot regenerate.

**Fetch:** artifacts land in laptop `runs/<run_id>/recon/`; `desktop/Runs.ps1` surfaces
`recon_manifest.json` into `runs/index.json`. Any stage with `status: "failed"` makes
fetch exit nonzero.

---

## 2. Container invocation contract

- **Image:** `k1recon:v1` (v1 is CPU-lean -- no torch/gsplat/CUDA; those arrive as the v2
  image bump at P6G.4). Built from `desktop/recon/recon.Dockerfile` with the REPO ROOT as
  build context.
- **Entrypoint:** `recon` (`desktop/recon/cli.py`). The CLI tolerates a doubled leading
  `recon` token, so both `docker run k1recon:v1 --selftest` and
  `docker run k1recon:v1 recon --selftest` work.
- **Mounts:** bundle RO at `/in`, output RW at `/out`:
  `docker run -d --name recon_<run_id> -v ~/k1/inbox/<run_id>:/in:ro -v ~/k1/outbox/<run_id>:/out ...`
- **recon_version = the image digest**, read at runtime from env `RECON_IMAGE_DIGEST` --
  never hardcoded (a digest exists only after a build). The job wrapper MUST inject it:
  `-e RECON_IMAGE_DIGEST="$(docker inspect --format '{{.Id}}' k1recon:v1)"`.
  If unset the container records `"unset"` and prints a loud `RECON-VERSION-UNPINNED`
  warning; a P6G.2-gated run with `image_digest: "unset"` does not count as pinned.
- **CLI:** `recon --run <run_id> --stages ingest,odom,... [--allow-approximate-intrinsics]
  [--seed N] [--bundle-dir /in] [--out-dir /out]` and `recon --selftest`.
  Unknown stage names are a loud usage error; `splat` is reserved for image v2 and says so.
  `ingest` ALWAYS runs first even if not requested (its validation is the fail-closed gate
  for everything after it).
- **Process exit codes:** `0` all requested stages ok; `1` any stage failed; `3` no
  failures but at least one stage exited `not_implemented` (nonzero on purpose -- nobody
  gets to script a stub as success); `2` usage error (argparse).

---

## 3. Stage enum and per-stage I/O

Stage enum (FROZEN): `ingest | odom | tsdf | simexport | align`. (`splat` reserved for
image v2; it initializes from the TSDF cloud, per doctrine.) Canonical execution order is
the enum order. In image v1 only `ingest` is implemented; the rest are registered and exit
`not_implemented` LOUDLY in the manifest -- never a fake `ok`.

Output tree (desktop `~/k1/outbox/<run_id>/`, fetched to laptop `runs/<run_id>/recon/`):

```
recon_manifest.json           always -- the authoritative record, written even on failure
trajectory.jsonl              odom      frame: run_local  (one JSON line per paired RGBD
                                        frame: wall_t + T_run_local_camera)
pose_graph.json               odom      frame: run_local  (nodes + loop-closure edges)
mesh_visual.ply               tsdf      frame: run_local
mesh_collision/part_*.obj     simexport frame: run_local  (convex decomposition -- each
                                        part watertight by construction; visual mesh is
                                        quadric-decimated)
apriltag_observations.json    align     frames: anchor_<id> per tag (+ its map pose once
                                        cross-run alignment exists)
```

| stage     | image v1  | what it does (or will do)                                          |
|-----------|-----------|--------------------------------------------------------------------|
| ingest    | IMPLEMENTED | Validates the bundle fail-closed: (a) every `manifest.json` sha256 vs disk; (b) intrinsics gate input (`intrinsics.json` -> `intrinsics_source`); (c) depth-unit sanity via the `.rrd` read -- median of finite positive `/camera/depth` values must lie in **[0.15, 15] m** (`DepthImage(meter=1.0)`); a median in the hundreds is the 1000x mm-vs-m garage and it dies HERE, not as a broken mesh. A bundle with no `.rrd` or no depth frames is refused (not a capture run / not P8-usable). |
| odom      | not_implemented | RGBD odometry + loop closure (NO COLMAP -- doctrine). Wall-time RGBD pairing + person masking per section 5. Emits `trajectory.jsonl` + `pose_graph.json`. Metric stage: intrinsics gate applies. |
| tsdf      | not_implemented | TSDF integration over masked, paired RGBD along the odom trajectory -> `mesh_visual.ply`. Metric stage: intrinsics gate applies. |
| simexport | not_implemented | Watertight collision mesh via convex decomposition (CoACD; per-part watertight by construction) + quadric-decimated visual mesh, frame-tagged. |
| align     | not_implemented | AprilTag anchor observations + cross-run alignment into `map`. Its real gate needs two real runs from different days -- desktop work by definition. |

**Frame-tagging rule (binding, per `docs/FRAMES.md` section 3):** every artifact names its
frame. The manifest writer ENFORCES it -- each artifact entry a stage reports must carry a
`frame` field from the vocabulary below, or the writer raises (an untagged artifact is a
bug, not a warning). Vocabulary:

- `run_local` -- this run's local frame, seeded at its odom origin (FRAMES.md: per-run
  meshes/trajectories are local until P8.3 aligns them).
- `map`       -- the persistent multi-run garage frame (post-align only).
- `anchor_<i>` -- AprilTag `i`'s frame (integer tag id, e.g. `anchor_23`).
- `camera`    -- the color optical frame; used by the selftest's synthetic scene
  (identity poses: camera == world there).

---

## 4. `recon_manifest.json` -- FROZEN schema

Written atomically (`.tmp` + rename) by the container into `/out`, ALWAYS -- including on
failure (a failed run with no manifest is the one forbidden outcome). JSON object key
order is non-semantic (`sort_keys` on write); the `stages` array preserves execution order.

```json
{
  "run_id": "20260710T153000Z_ab12cd3",
  "image_digest": "sha256:...",
  "stages": [
    {
      "name": "ingest",
      "status": "ok",
      "exit_code": 0,
      "wall_s": 12.34,
      "params": {"depth_min_m": 0.15, "depth_max_m": 15.0, "max_pair_skew_s": 0.06,
                 "person_mask": "target-box-v0", "mask_dilate_frac": 0.15},
      "metrics": {"files_verified": 7, "depth_frames": 412, "depth_median_m": 2.31,
                  "depth_in_range_frac": 0.998, "depth_scale_m_per_unit": 1.0,
                  "intrinsics_source": "approximate_hfov",
                  "artifacts": []},
      "inputs_hash": "sha256hex..."
    }
  ],
  "seed": 0,
  "intrinsics_source": "approximate_hfov",
  "started_utc": "20260710T153000Z",
  "finished_utc": "20260710T153412Z"
}
```

Field semantics:

- `run_id` -- the bundle's run_id (`--run`); ingest fails loudly if the bundle's own
  `manifest.json` disagrees (wrong-mount detector). The selftest manifest uses
  `"selftest"`.
- `image_digest` -- `RECON_IMAGE_DIGEST` env verbatim, or the honest sentinel `"unset"`.
- `stages[].status` -- `ok | failed | skipped | not_implemented`.
  - `failed`: the stage ran and refused or errored; the human-readable reason is in
    `metrics.error`; every subsequent stage is recorded `skipped`. Fetch exits nonzero.
  - `skipped`: not run because an earlier stage failed (`metrics.note` names it).
  - `not_implemented`: a v1 stub; loud, distinct from both `ok` and `failed`; does NOT
    halt later stages (they each record their own honest status).
- `stages[].exit_code` -- `0` ok, `1` failed, `3` not_implemented, `null` skipped.
- `stages[].wall_s` -- monotonic wall seconds spent in the stage.
- `stages[].params` -- the knobs actually in effect. Metric stages record
  `allow_approximate_intrinsics` here when the override was passed (section 5).
- `stages[].metrics` -- free-form stage output. `metrics.artifacts` is the frame-tagged
  artifact list: `[{"path": <relative to /out>, "frame": <vocab, section 3>, "bytes": N}]`.
- `stages[].inputs_hash` -- **image v1 definition:** the bundle hash = sha256 over the
  sorted sequence of `name + "\0" + sha256 + "\n"` lines from the bundle
  `manifest.json` `files[]`. Identical bundle -> identical hash, which is what the
  wipe-and-resubmit gate compares. When stages start consuming prior-stage outputs, the
  definition extends to cover those consumed artifacts -- amend this doc first.
- `seed` -- run-wide seed (`--seed`, default 0); everything stochastic downstream must
  draw from it.
- `intrinsics_source` -- `calibrated | approximate_hfov | missing | synthetic | unknown`
  (from ingest; `synthetic` = selftest; `unknown` = ingest never got that far).
- `started_utc` / `finished_utc` -- `%Y%m%dT%H%M%SZ`, whole job.

---

## 5. Doctrine gates (binding on every implementation, v1 and later)

1. **NO COLMAP.** Anywhere, ever, in this image or its successors. Poses come from RGBD
   odometry + loop closure; splats (v2) initialize from the TSDF cloud. A COLMAP dep in
   `requirements-recon.*` is a contract violation.
2. **Fail-closed approximate-intrinsics gate** (P8.1 hard prereq). The recorder's
   `intrinsics.json` is `approximate: true` (`fy:=fx` from `--hfov-deg`; see FRAMES.md
   2.1) -- good enough to document a run, NOT good enough for metric TSDF to trust. The
   metric stages (`odom`, `tsdf`) REFUSE on anything but `intrinsics_source: calibrated`
   unless `--allow-approximate-intrinsics` is passed, and the override is recorded in that
   stage's `params`. Missing intrinsics refuse the same way. Never silently degrade.
3. **Depth-unit sanity dies in ingest.** Finite in-range [0.15, 15] m assert on the
   `.rrd`'s `/camera/depth` (`DepthImage(meter=1.0)`); `depth_scale_m_per_unit` recorded
   in metrics. The 1000x mm-vs-m garage is caught here, not in P8.3.
4. **RGBD pairing joins by WALL time, never `frame_idx`.** Depth `frame_idx` is
   best-effort, not stamp-matched (RUN_ARTIFACTS.md section 3.1). Max-skew reject:
   **0.060 s** (`max_pair_skew_s`, recorded in params); pairing stats (pairs kept /
   rejected) go in the odom stage's metrics.
5. **Person masking v0 = the logged TARGET box only.** Dilate the `/camera/rgb/target`
   Boxes2D box (dilation fraction recorded in params), zero depth inside it before BOTH
   odom and TSDF, record the masked fraction in metrics. Bystanders are NOT masked --
   all-person YOLO boxes are not recorded today; that gap is flagged in `DECISIONS.md`
   P8.2a (treat meshes from bystander-heavy runs as suspect until resolved).
6. **Loud failure, never a silent hang or a fake ok.** Every abnormal path names its
   stage in the manifest; `not_implemented` is a first-class loud status.

---

## 6. Container selftest (the P6G.2 smoke)

```
docker run --rm k1recon:v1 recon --selftest        # or: python desktop/recon/cli.py --selftest
```

No robot data, no GPU, no network, no mounts needed. It: generates synthetic RGBD of a
known plane (pure numpy) with identity poses; runs an Open3D TSDF integrate; asserts the
extracted mesh has > 0 vertices and a plane-fit RMS under 0.010 m; writes the mesh
(frame-tagged `camera`, exercising the enforcement path); and emits a well-formed
`recon_manifest.json` that INCLUDES a deliberately-failed `align` stage recorded loudly --
proving the failure plumbing renders. Prints `RECON-IMAGE-SELFTEST-OK` and exits 0 (the
deliberate failure is expected and does not fail the selftest).

This selftest is NOT the P6G.2 gate (that needs the garage bundle end-to-end) and NOT the
P6G.3 gate (that is the wipe-and-resubmit round trip). It proves the image plumbing only.

---

## 7. Deferred (what image v1 does NOT contain -- charter DEFER list)

- The committed **lockfile**: minted on the desktop from the first successful build
  (`pip freeze` in-container -> commit as `requirements-recon.lock`); a blind lockfile is
  fiction. `requirements-recon.in` carries the unpinned intent.
- Real **odom / loop-closure / TSDF-on-real-data / align / AprilTag** implementations
  (`VERIFY ON DESKTOP`, data-blocked; align needs two real runs from different days).
- **splat + CUDA** (image v2, P6G.4, gated on P8.3).
