#!/usr/bin/env bash
# Pull capture-complete follow runs FROM Todd's laptop INTO this cloud/Linux box.
#
# BLOCKER (as of 2026-09-11): this cloud VM has NO path to the home LAN.
#   Laptop LAN identity (docs): 192.168.1.95  user toddm
#   Desktop RIG2:               192.168.1.75  sshd:2222 (WSL)
#   All of those ping/ssh fail from here. Internet egress works; private RFC1918 does not.
#
# ONE THING TODD MUST DO (pick A — simplest for Plan A sample):
#   A) From the laptop, zip newest N capture-complete bundles and put an HTTPS URL in the
#      agent chat (transfer.sh / tmpfiles / Drive public link / S3 presign). Then:
#        PULL_URL='https://...' ./desktop/Pull-Runs-From-Laptop.sh --from-url
#   B) Tailscale (or similar) on the laptop + OpenSSH Server, reply with the Tailscale IP:
#        LAPTOP_HOST=<ts-ip> LAPTOP_USER=toddm ./desktop/Pull-Runs-From-Laptop.sh
#   C) Prefer GPU path: Sync-Runs.ps1 laptop -> desktop, then on the 3080 desktop:
#        .\desktop\Batch-Label-Runs.ps1 -Newest 14
#      (this cloud box is CPU-only; Autotune-Stage hard-requires CUDA)
#
# Expected laptop runs root (Auto-Tune-Service.ps1):
#   C:\Users\toddm\OneDrive\Desktop\runtime\runs
# Remote POSIX path when OpenSSH serves that tree (adjust if different):
#   /C:/Users/toddm/OneDrive/Desktop/runtime/runs   OR   ~/OneDrive/Desktop/runtime/runs
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_RUNS="${LOCAL_RUNS:-$ROOT/runs}"
NEWEST="${NEWEST:-0}"          # 0 = all; else newest N capture-complete
IDENTITY="${IDENTITY:-}"       # -i path for ssh/scp
LAPTOP_HOST="${LAPTOP_HOST:-}" # REQUIRED for ssh path (e.g. 100.x Tailscale)
LAPTOP_USER="${LAPTOP_USER:-toddm}"
LAPTOP_PORT="${LAPTOP_PORT:-22}"
REMOTE_RUNS="${REMOTE_RUNS:-}" # remote runs/ path; probed if empty
PULL_URL="${PULL_URL:-}"
MODE="${1:-}"

mkdir -p "$LOCAL_RUNS"

ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8
          -p "$LAPTOP_PORT")
[[ -n "$IDENTITY" ]] && ssh_opts+=(-i "$IDENTITY")

die() { echo "PULL-FAIL: $*" >&2; exit 1; }

is_capture_complete() {
  # local dir: manifest.json lists a .rrd whose size matches
  local d="$1" mf="$d/manifest.json"
  [[ -f "$mf" ]] || return 1
  python3 - "$d" <<'PY'
import json, os, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, "manifest.json")))
for f in m.get("files") or []:
    name = f.get("name") or ""
    if name.endswith(".rrd"):
        path = os.path.join(d, name)
        if os.path.isfile(path) and os.path.getsize(path) == int(f["bytes"]):
            sys.exit(0)
sys.exit(1)
PY
}

case "$MODE" in
  --from-url)
    [[ -n "$PULL_URL" ]] || die "set PULL_URL to an HTTPS zip/tarball of runs/"
    tmp="$(mktemp -d)"
    echo "fetching $PULL_URL ..."
    curl -fL --retry 3 -o "$tmp/bundle" "$PULL_URL"
    # zip or tar.gz
    if file "$tmp/bundle" | grep -qi zip; then
      unzip -q "$tmp/bundle" -d "$tmp/out"
    else
      mkdir -p "$tmp/out"
      tar -xf "$tmp/bundle" -C "$tmp/out"
    fi
    # Accept either runs/<id>/... or <id>/... at the archive root.
    if [[ -d "$tmp/out/runs" ]]; then src="$tmp/out/runs"; else src="$tmp/out"; fi
    shopt -s nullglob
    for d in "$src"/*/; do
      base="$(basename "$d")"
      [[ "$base" =~ ^[0-9]{8}T[0-9]{6}Z_ ]] || continue
      rm -rf "${LOCAL_RUNS:?}/$base"
      mv "$d" "$LOCAL_RUNS/$base"
    done
    rm -rf "$tmp"
    ;;
  ""|--ssh)
    [[ -n "$LAPTOP_HOST" ]] || die "LAPTOP_HOST unset (laptop not on LAN from this VM). Use --from-url or set Tailscale IP."
    target="${LAPTOP_USER}@${LAPTOP_HOST}"
    echo "probing $target ..."
    ssh "${ssh_opts[@]}" "$target" 'echo SSH-OK' || die "ssh to $target failed (key auth? OpenSSH Server up?)"

    if [[ -z "$REMOTE_RUNS" ]]; then
      for cand in \
        '/C:/Users/toddm/OneDrive/Desktop/runtime/runs' \
        'C:/Users/toddm/OneDrive/Desktop/runtime/runs' \
        '$HOME/OneDrive/Desktop/runtime/runs' \
        '~/OneDrive/Desktop/runtime/runs' \
        '~/k1/runs' \
        '~/runs'; do
        if ssh "${ssh_opts[@]}" "$target" "test -d $cand" 2>/dev/null; then
          REMOTE_RUNS="$cand"
          break
        fi
      done
    fi
    [[ -n "$REMOTE_RUNS" ]] || die "could not find remote runs/; set REMOTE_RUNS="

    echo "remote runs: $REMOTE_RUNS"
    mapfile -t ids < <(ssh "${ssh_opts[@]}" "$target" \
      "ls -1dt $REMOTE_RUNS/*/ 2>/dev/null | xargs -n1 basename" | grep -E '^[0-9]{8}T[0-9]{6}Z_' || true)
    [[ ${#ids[@]} -gt 0 ]] || die "no run dirs under $REMOTE_RUNS"
    if [[ "$NEWEST" -gt 0 && ${#ids[@]} -gt "$NEWEST" ]]; then
      ids=("${ids[@]:0:$NEWEST}")
    fi
    echo "pulling ${#ids[@]} run(s) -> $LOCAL_RUNS"
    for id in "${ids[@]}"; do
      echo "  scp -r $id ..."
      mkdir -p "$LOCAL_RUNS/$id"
      # Prefer rsync if available (resumable); else recursive scp.
      if command -v rsync >/dev/null; then
        rsync -a -e "ssh ${ssh_opts[*]}" "$target:$REMOTE_RUNS/$id/" "$LOCAL_RUNS/$id/"
      else
        scp -r "${ssh_opts[@]}" "$target:$REMOTE_RUNS/$id/." "$LOCAL_RUNS/$id/"
      fi
      if is_capture_complete "$LOCAL_RUNS/$id"; then
        echo "  OK capture-complete $id"
      else
        echo "  WARN incomplete (no size-matched .rrd): $id" >&2
      fi
    done
    ;;
  *)
    die "usage: $0 [--ssh|--from-url]"
    ;;
esac

n=0; ok=0
for d in "$LOCAL_RUNS"/*/; do
  [[ -d "$d" ]] || continue
  n=$((n+1))
  is_capture_complete "$d" && ok=$((ok+1)) || true
done
echo "PULL-OK local_runs=$LOCAL_RUNS dirs=$n capture_complete=$ok"
echo "Next:  python eval/batch_label.py --newest 3 --allow-cpu   # CPU sample"
echo "   or: python eval/batch_label.py            # needs CUDA (same gate as Autotune-Stage)"
