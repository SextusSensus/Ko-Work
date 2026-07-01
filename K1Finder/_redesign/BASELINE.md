# Phase 1 — Pinned port baseline (locked 2026-06-24)

The WPF rewrite MUST port the backend from the files below, verified by SHA-256. **Do not fork from
any `.bak` or `_opt_backup_*` copy** — they are stale and missing the cross-tab safety guards
(see Porting-fidelity rule #1 in [PLAN.md](PLAN.md)).

## Canonical view-source (the one file whose behavior is being preserved)

| File | Bytes | Modified | SHA-256 |
|---|---|---|---|
| `K1Finder.ps1` | 111045 | 2026-06-23 20:20 | `53B605DDB33CF916B9A2E48B37CE474FBF08C2D695AAB4391DC79C7AECF9A8A6` |

### Stale copies — NOT the source (confirmed byte-identical to each other)
| File | Bytes | Modified | SHA-256 (prefix) |
|---|---|---|---|
| `_opt_backup_20260623_172956\K1Finder\K1Finder.ps1` | 110080 | 2026-06-23 17:26 | `9A6443DC…` |
| `K1Finder\K1Finder.ps1.bak_20260623_201956` | 110080 | 2026-06-23 17:26 | `9A6443DC…` |

The 965-byte delta (live minus backup) is exactly the single-camera-consumer / single-loco-driver
cross-tab guards. Both stale copies already contain the Tracker tab, so "6 tabs present" does NOT
prove safety parity — only a diff against the canonical hash above does.

## Frozen deploy contract (helper files shipped to the robot from `$SCRIPT_DIR` by name)

The WPF app must sit in this same folder and keep deploying exactly these names. They are still being
iterated (note the 6/23 23:xx timestamps), so develop/cut over **in place**, not in a separate folder.

| File | Bytes | Modified | SHA-256 |
|---|---|---|---|
| `stream_cam.py` | 6600 | 2026-06-23 23:23 | `0C3C41747AFD6EE27459E55E8818934D5F1B340E21078EEADCCBEEA0FDDCDE87` |
| `run_stream.sh` | 363 | 2026-06-22 14:58 | `EF239D04AF771D5D82902FA59811EAEA1F4E3D70B6D4278BC5E8566C7E78D3A3` |
| `run_loco.sh` | 292 | 2026-06-22 14:51 | `F6A82466FADFDCCA0344F1A2A63128869976D886B39740ECFCBE98D7C3DC87B0` |
| `follow_person_k1.py` | 58338 | 2026-06-23 23:29 | `8880FE1CAA5D6D8E446D83C6A217C9E5874B53B258B0B78F5F4A2C253947650D` |
| `loco_follow_bridge.cpp` | 9538 | 2026-06-23 19:29 | `F6D4A39334AA478FC64024B05D443D7F0B23B1395FD5DB15BF28DA298B085163` |
| `run_follow.sh` | 1518 | 2026-06-23 17:14 | `CF0AD9F9A62CEEB54491C890856E0A53892A07142EAAE47315C120FEDE3FFFD4` |
| `tree_manifest.py` | 6453 | 2026-06-22 15:15 | `AF06B578852754C006DEB94337B79932D9DF2C659B1D24A3AA63E1D01AF95B76` |

## Verification gate before cutover (Phase 7)

Re-diff the finished WPF backend against the **then-current** live `K1Finder.ps1` (not this frozen
snapshot — the human may keep editing it). Reconcile any change to: the `Deploy-*` filename lists,
the remote-command builders (`run_stream.sh` / `run_follow.sh` / `run_loco.sh` arg strings), the
slider→arg getters, and the single-camera / single-loco lockout matrix.
