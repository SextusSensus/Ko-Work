#!/bin/bash
# offload_run.sh (P6.2) -- assemble the just-finished follow run into a self-contained bundle under
# runs/<run_id>/ on the Jetson, write manifest.json (files + sizes + sha256 + profile + duration),
# and prune to the last N runs. The bundle is then PULLED to the workstation by desktop/Pull-Run.ps1.
#
# Runs POST-SESSION on the Jetson -- nothing is driving, so this never touches the control loop
# (invariant: the Jetson's control loop is sacred). Pure filesystem + sha256; no sudo; no daemon; no
# network (the pull is workstation-initiated). Safe to re-run: an interrupted attempt leaves only a
# throwaway .partial and all SOURCE artifacts intact, so a retry is clean (the kill-test).
#
# Usage:  offload_run.sh [--profile NAME] [--keep N] [--reconcile] [--runs-dir DIR] [--src-dir DIR]
#   --profile   config profile in effect this run (the launcher knows it; default "unknown").
#   --keep      retain this many newest VERIFIED bundles, prune older (default 10).
#   --reconcile RECOVERY MODE (called at session START by run_follow*.sh): bundle any leftover data
#               from a crashed/killed PRIOR session that never offloaded, then exit. A no-op when the
#               prior session offloaded cleanly (no .rrd + empty JSONL). So a power-cut session's data
#               still leaves on the next boot, not only on a clean exit.
set -u

SRC="${SRC_DIR:-/home/booster}"
RUNS=""
RERUN=""
KEEP="${KEEP:-10}"
PROFILE="unknown"
RECONCILE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)   PROFILE="${2:-unknown}"; shift 2 ;;
    --keep)      KEEP="${2:-10}";         shift 2 ;;
    --reconcile) RECONCILE=1;             shift ;;
    --runs-dir)  RUNS="${2:-}";           shift 2 ;;
    --src-dir)   SRC="${2:-$SRC}";        shift 2 ;;
    *) echo "offload: unknown arg '$1'" >&2; exit 2 ;;
  esac
done
RUNS="${RUNS:-$SRC/runs}"
RERUN="${RERUN_DIR:-$SRC/rerun}"

mkdir -p "$RUNS" || { echo "offload: cannot mkdir $RUNS" >&2; exit 1; }

# The newest .rrd is this session's recording. It exists ONLY for a capture run (--rerun); a
# no-rerun run still bundles its JSONL + err (RUN_ARTIFACTS.md: pixels/depth are opt-in).
RRD="$(ls -1t "$RERUN"/k1_follow_*.rrd 2>/dev/null | head -1)"
JSONL="$SRC/k1_events.jsonl"
ERR="$SRC/k1_follow.err"

# RECONCILE (session-start recovery): only proceed if there is leftover data from a prior session that
# never offloaded -- a .rrd still in rerun/, OR a non-empty JSONL. If the prior session offloaded
# cleanly (its .rrd was moved out + JSONL truncated), there is nothing to reconcile -> quiet no-op.
# This runs BEFORE the new session writes, so the leftover is never mixed with the new run.
if [ "$RECONCILE" = "1" ]; then
  _jsz=0; [ -f "$JSONL" ] && _jsz="$(wc -c < "$JSONL" 2>/dev/null || echo 0)"
  if [ -z "$RRD" ] && [ "${_jsz:-0}" -le 1 ]; then
    echo "offload: reconcile -- no leftover session data, nothing to do"
    exit 0
  fi
  echo "offload: reconcile -- leftover session data found, bundling a prior (crashed?) session"
  [ "$PROFILE" = "unknown" ] && PROFILE="reconciled"
fi

# run_id = <UTC stamp>_<deploy sha | nogit>. Stamp from the .rrd epoch when present (ties the id to
# the recording), else now. DEPLOY_VERSION is an optional short-SHA file the deploy step may drop.
EPOCH=""
if [ -n "$RRD" ]; then
  EPOCH="$(basename "$RRD" | sed -n 's/^k1_follow_\([0-9][0-9]*\)\.rrd$/\1/p')"
fi
[ -z "$EPOCH" ] && EPOCH="$(date -u +%s)"
STAMP="$(date -u -d "@$EPOCH" +%Y%m%dT%H%M%SZ 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)"
SHA="nogit"
[ -f "$SRC/DEPLOY_VERSION" ] && SHA="$(head -c 40 "$SRC/DEPLOY_VERSION" | tr -cd 'A-Za-z0-9')"
[ -z "$SHA" ] && SHA="nogit"
RUN_ID="${STAMP}_${SHA}"

DEST="$RUNS/$RUN_ID"
TMP="$RUNS/.${RUN_ID}.partial"
if [ -d "$DEST" ]; then echo "offload: $DEST already exists -> nothing to do"; exit 0; fi
rm -rf "$TMP"; mkdir -p "$TMP" || { echo "offload: cannot mkdir $TMP" >&2; exit 1; }

# --- collect (COPY everything into .partial; SOURCES stay put until AFTER a successful publish, so
#     an interrupt here is recoverable). Cleanup/rotation happens only post-publish, below. ---
if [ -n "$RRD" ] && [ -f "$RRD" ]; then
  cp "$RRD" "$TMP/" || { echo "offload: copy rrd failed" >&2; exit 1; }
  INTR="$(dirname "$RRD")/intrinsics.json"
  [ -f "$INTR" ] && cp "$INTR" "$TMP/"
fi
[ -f "$JSONL" ] && cp "$JSONL" "$TMP/events.jsonl"
[ -f "$ERR" ]   && cp "$ERR"   "$TMP/k1_follow.err"
[ -d "$SRC/config" ]         && cp -r "$SRC/config"         "$TMP/config"
[ -f "$SRC/DEPLOY_VERSION" ] && cp    "$SRC/DEPLOY_VERSION" "$TMP/DEPLOY_VERSION"

# --- duration (s) from the events JSONL first/last t; best-effort. ---
DUR="null"
if [ -f "$TMP/events.jsonl" ]; then
  DUR="$(python3 - "$TMP/events.jsonl" <<'PY' 2>/dev/null || echo null
import json, sys
ts = []
for ln in open(sys.argv[1]):
    try:
        v = json.loads(ln).get("t")
    except Exception:
        continue
    if isinstance(v, (int, float)):
        ts.append(v)
print(round(ts[-1] - ts[0], 1) if len(ts) >= 2 else "null")
PY
)"
  [ -z "$DUR" ] && DUR="null"
fi

# --- manifest.json via python3 (guaranteed on the Jetson; produces valid JSON). ---
if ! python3 - "$TMP" "$RUN_ID" "$PROFILE" "$DUR" "$STAMP" "$SHA" <<'PY'
import json, os, sys, hashlib
tmp, run_id, profile, dur, stamp, sha = sys.argv[1:7]
files = []
for root, _, names in os.walk(tmp):
    for n in sorted(names):
        if n == "manifest.json":
            continue
        p = os.path.join(root, n)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        files.append({"name": os.path.relpath(p, tmp).replace(os.sep, "/"),
                      "bytes": os.path.getsize(p), "sha256": h.hexdigest()})
man = {"run_id": run_id, "created_utc": stamp, "git_version": sha, "profile": profile,
       "duration_s": (None if dur == "null" else float(dur)),
       "file_count": len(files), "total_bytes": sum(f["bytes"] for f in files),
       "files": files}
json.dump(man, open(os.path.join(tmp, "manifest.json"), "w"), indent=2, sort_keys=True)
print("offload: manifest %d files %d bytes" % (len(files), man["total_bytes"]))
PY
then
  echo "offload: manifest build FAILED -> leaving .partial, sources intact" >&2
  exit 1
fi

# --- atomic publish, THEN rotate sources (only now that the bundle is safely in place). ---
mv "$TMP" "$DEST" || { echo "offload: publish mv failed" >&2; exit 1; }
echo "offload: staged $DEST"
# rotate: the .rrd + sidecar now live in the bundle -> remove the originals (avoid disk doubling);
# truncate the append-mode JSONL so the NEXT session's ticks start a fresh per-run log.
[ -n "$RRD" ] && [ -f "$RRD" ] && rm -f "$RRD" "$(dirname "$RRD")/intrinsics.json"
[ -f "$JSONL" ] && : > "$JSONL"

# --- retention: prune ONLY workstation-VERIFIED bundles, oldest-first, beyond $KEEP. ---
# A bundle is verified when Pull-Run.ps1 writes a `.verified` marker into it after a hash-checked pull.
# An UN-verified (un-offloaded) bundle is NEVER pruned, even past $KEEP -- losing an un-offloaded run
# must be impossible. Un-verified pile-up is a loud disk-pressure alert, not a silent delete. (.partial
# staging dirs start with a dot -> excluded from the "*/" glob, never matched.)
_verified="$(ls -1dt "$RUNS"/*/ 2>/dev/null | while read -r d; do [ -f "${d}.verified" ] && echo "$d"; done)"
if [ -n "$_verified" ]; then
  echo "$_verified" | tail -n +$((KEEP + 1)) | while read -r d; do
    [ -n "$d" ] && { echo "offload: retention prune (verified) $d"; rm -rf "$d"; }
  done
fi
_unver="$(ls -1d "$RUNS"/*/ 2>/dev/null | while read -r d; do [ -f "${d}.verified" ] || echo "$d"; done | grep -c . || true)"
if [ "${_unver:-0}" -gt "$KEEP" ]; then
  echo "OFFLOAD-DISK-ALERT $_unver un-offloaded bundle(s) under $RUNS (workstation/network down?) -- RETAINED, not pruned. Pull them (Pull-Run.ps1) to free space." >&2
fi
echo "OFFLOAD-OK $RUN_ID"
