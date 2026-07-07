# OSNet Follow-Lock — Robustness + 10 Hz Enhancement Plan

Grounded against the live `follow_person_k1.py`. From a 3-lens (rootcause/robustness/speed) workflow with adversarial anti-drift + 10 Hz critique. Driven by a real on-robot striped run that **degraded then lost the lock**.

## Root cause of the degradation (ranked)
- **R1 (dominant, structural):** the veto has **no pass path beyond the single frozen anchor view**. `_associate` vetoes on `gal.anchor_pass(ph)` which gates *only* on `sim_fn(anchor_feat, feat) >= anchor_floor`; the gallery enters only as a cost *discount* (`g = max(anchor, gallery)` seeded to anchor, so it can reorder but never *rescue*). When the single seed-view goes stale (anchor cosine 0.41→0.10 in the log), every candidate fails → `scored` empties → `LOST best_cost=n/a`.
- **R2 (dominant, throughput):** `embed()` = one `session.run` **per person per frame**, unbatched/uncached → N–2N TRT launches/frame → overruns the 10 Hz period → `SLOW-LOOP` → bridge watchdog → standby.
- **R3 (amplifier):** `_preprocess` **stretches** the crop to 256×128 (aspect ignored) → distorted body → low same-person cosine (0.25–0.41) to begin with (worse at the logged 0.34–0.69 m range).
- **R4:** the `osnet` floor defaults are guesses → flicker (too low) or veto-target (too high).
- **R5:** no temporal hysteresis on the veto — one sub-floor frame empties `scored`.
- **R6 (minor):** EMA self-sim drifts while the frozen veto key stays fixed.

## Ship order (the plan's staging — P0 has zero anti-drift surface)

### P0-A — aspect-preserving letterbox in `ReidEngine._preprocess`  ✅
Letterbox/pad to 256×128 (gray 114) instead of stretch. `--reid-letterbox` (default ON). Strengthens anti-drift (more self-consistent embeddings). **Invalidates the guessed floors → pair with P0-C.**

### P0-B — batched + cached embed  ✅  (the 10 Hz fix)
`ReidEngine.embed_batch(frame, boxes)` → ONE `session.run((M,3,H,W))` when the model batch axis is **dynamic**, else a per-row loop (the standard OSNet export is fixed batch=1 — **never let `session.run((M,…))` raise → it would all-`None` → veto everyone → instant LOST on a crowded frame**). `Follower._embed_persons` stamps `p['_ph']` once/frame; `_associate` reads `_ph`. `--reid-every-n` (default 1 = byte-identical) caches per `track_id`, but **always** re-embeds the bound target + isolated-near-target, and **fresh** embeds in `_try_reacquire` (no stale vector into the armed relock). Scatter back by explicit valid-index. Anti-drift-neutral (identical math).

### P0-C — read-only cosine audit log  ✅
One line/frame: bound-target `anchor_sim`, gallery `g_sim`, best other-person sim. Zero behavior change. **Prerequisite for tuning any floor** — set osnet floors from the same-person vs cross-person percentiles on *this* camera/range.

### P1-A — gallery-aware veto, HARDENED  (the true R1 fix — ships AFTER P0-C audit)
`TargetGallery.survive(feat)`: pass iff `anchor_sim >= anchor_floor` **OR** (`anchor_sim >= veto_relax_floor(≥0.30)` AND **k≥2-view** gallery agreement `>= veto_gallery_floor(≥bank_floor)` AND `(best_gallery - max_distractor) >= veto_sep` AND the candidate is the **bound + isolated** target). Three-layer anti-drift: only `admit`-passed (isolation+conf+spacing+bank_floor) views can grant survival; the immutable frozen anchor stays a hard backstop (`veto_relax_floor`); k≥2-view + distractor separation closes the "stranger rides one loose back-pose view" door. Crash-safe → False. Default OFF (`veto_gallery_floor` None = anchor-only, byte-identical). **Validate on a same-clothing-bystander clip before enabling.**

### P1-B/C — veto-grace + N-of-M loss hysteresis (cheap, safe follow-ons). P2 — medoid anchor-refresh, INT8 (deferred).

## Sacred (unchanged)
Identity born only in `_try_seed`; frozen `anchor_hist` immutable + always a hard veto backstop; slot-0 AND-veto stays a *necessary* condition (gallery adds a PASS path only for the bound+isolated target via strictly-admitted views, never looser than anchor); `--reid-every-n 1` / global / striped byte-identical; COASTING depth-only vx; HARD clamps; every new path fails closed (None/False), never fail-open; `_try_reacquire` stays stricter than TRACK.
