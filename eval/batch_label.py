#!/usr/bin/env python3
"""batch_label.py -- Linux/cloud twin of desktop/Batch-Label-Runs.ps1 (Plan A offline labelling).

Scans runs/<run_id>/ for capture-complete bundles (manifest + size-matched .rrd), runs
eval/rrd_label.py with the same Autotune-Stage defaults, writes autotune/<run_id>/, and
tallies PASS/FAIL/INCONCLUSIVE + depth_pairing + suspect_iddrift (via rrd_obstacles).

CUDA is required by default (same gate as Autotune-Stage). Pass --allow-cpu ONLY for a
small sample with honest limits: full SegFormer+YOLO11x on CPU can take many hours/run.

Examples:
  python eval/batch_label.py --dry-run
  python eval/batch_label.py --newest 3 --allow-cpu --model yolo11n.pt --no-seg
  python eval/batch_label.py --newest 14          # needs CUDA
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GATE_VERSION = 3
# Match Autotune-Stage.ps1 $LabelArgs defaults (heavy). --allow-cpu callers usually override.
DEFAULT_LABEL_ARGS = [
    "--track", "--merge-radius", "0.5", "--seg", "--seg-every", "2",
    "--stride", "2", "--model", "yolo11x.pt",
    "--seg-model", "nvidia/segformer-b2-finetuned-ade-512-512",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_complete(run_dir: Path) -> Path | None:
    mf = run_dir / "manifest.json"
    if not mf.is_file():
        return None
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return None
    for f in man.get("files") or []:
        name = f.get("name") or ""
        if name.endswith(".rrd"):
            rrd = run_dir / name
            if rrd.is_file() and rrd.stat().st_size == int(f["bytes"]):
                return rrd
    return None


def read_validation(adir: Path) -> dict | None:
    vf = adir / "label_validation.json"
    if not vf.is_file():
        return None
    try:
        j = json.loads(vf.read_text(encoding="utf-8"))
        if int(j.get("gate_version", -1)) < GATE_VERSION:
            return None
        return j
    except Exception:
        return None


def depth_pairing_stats(val: dict | None) -> dict | None:
    if not val:
        return None
    dp = (val.get("checks") or {}).get("depth_pairing")
    if not dp:
        return None
    return {
        "status": dp.get("status"),
        "frac_paired": dp.get("frac_paired"),
        "paired": dp.get("paired"),
        "labelled_frames": dp.get("labelled_frames"),
        "why": dp.get("why"),
    }


def iddrift_stats(py: str, obstacles_py: Path, adir: Path) -> dict | None:
    jsonl = adir / "obstacles.jsonl"
    out_js = adir / "obstacle_set.json"
    if not jsonl.is_file() or not obstacles_py.is_file():
        return None
    rc = subprocess.call(
        [py, str(obstacles_py), str(jsonl), "--json", str(out_js)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0 or not out_js.is_file():
        return None
    try:
        data = json.loads(out_js.read_text(encoding="utf-8"))
        obs = data.get("obstacles") or []
        suspects = [o for o in obs if o.get("suspect_iddrift")]
        return {
            "n_obstacles": len(obs),
            "n_suspect_iddrift": len(suspects),
            "suspects": [
                {"id": o.get("id"), "cls": o.get("cls"),
                 "spread_m": o.get("spread_m"), "range_m": o.get("range_m")}
                for o in suspects
            ],
        }
    except Exception:
        return None


def write_summary(adir: Path, run_id: str, val: dict) -> None:
    lines = [
        f"K1 autotune -- {run_id}   (generated {datetime.now().strftime('%Y-%m-%d %H:%M')})",
        f"geometry validation: {val.get('status')}",
    ]
    for name, c in (val.get("checks") or {}).items():
        lines.append(f"  {name:<6} {json.dumps(c, separators=(',', ':'))}")
    if val.get("floor_calibration"):
        lines.append(f"floor calibration: {json.dumps(val['floor_calibration'], separators=(',', ':'))}")
    os_path = adir / "obstacle_summary.json"
    if os_path.is_file():
        try:
            o = json.loads(os_path.read_text(encoding="utf-8"))
            if o.get("unique") is not None:
                lines.append(f"obstacles: {o['unique']} unique from {o.get('detections')} detections")
                for cls, n in sorted((o.get("by_class") or {}).items(), key=lambda x: -x[1]):
                    lines.append(f"  {cls:<16} {n}")
            else:
                lines.append(f"detections: {o.get('detections')}")
        except Exception:
            pass
    (adir / "SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=None, help="runs/ root (default: <repo>/runs)")
    ap.add_argument("--autotune", default=None, help="autotune/ root (default: <repo>/autotune)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--newest", type=int, default=0)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-cpu", action="store_true",
                    help="permit labelling without CUDA (honest: slow; use tiny models)")
    ap.add_argument("--no-seg", action="store_true", help="drop SegFormer (faster CPU sample)")
    ap.add_argument("--model", default=None, help="override YOLO weights (e.g. yolo11n.pt for CPU)")
    ap.add_argument("--timeout-min", type=int, default=90)
    ap.add_argument("--out-tally", default=None)
    a = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    runs = Path(a.runs) if a.runs else root / "runs"
    autotune = Path(a.autotune) if a.autotune else root / "autotune"
    label_py = root / "eval" / "rrd_label.py"
    obstacles_py = root / "eval" / "rrd_obstacles.py"
    out_tally = Path(a.out_tally) if a.out_tally else autotune / "batch_label_tally.json"
    weights = Path(os.path.expanduser("~/.cache/k1_weights"))
    weights.mkdir(parents=True, exist_ok=True)
    autotune.mkdir(parents=True, exist_ok=True)

    if not runs.is_dir():
        print(f"batch: no runs folder: {runs}", file=sys.stderr)
        return 2
    if not label_py.is_file():
        print(f"batch: missing {label_py}", file=sys.stderr)
        return 2

    cands = []
    for d in sorted(runs.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or not d.name[:16].count("T") == 1:
            continue
        if not (len(d.name) > 16 and d.name[8] == "T" and "Z_" in d.name):
            continue
        rrd = capture_complete(d)
        if rrd:
            cands.append({"run_id": d.name, "dir": d, "rrd": rrd, "bytes": rrd.stat().st_size})

    if a.newest > 0:
        cands = cands[: a.newest]
    if a.sample > 0 and len(cands) > a.sample:
        rng = random.Random()
        cands = sorted(rng.sample(cands, a.sample), key=lambda x: x["run_id"], reverse=True)

    print(f"batch_label  runs={runs}  autotune={autotune}  gate=v{GATE_VERSION}  NoSend=ALWAYS")
    print(f"eligible capture-complete: {len(cands)}  dry_run={a.dry_run} force={a.force} "
          f"newest={a.newest} sample={a.sample}")
    if not cands:
        print("batch: nothing to do (need manifest.json + size-matched .rrd under runs/)")
        return 0

    if not a.dry_run:
        probe = subprocess.run(
            [a.python, "-c", "import sys,torch; sys.exit(0 if torch.cuda.is_available() else 3)"],
            capture_output=True)
        if probe.returncode != 0 and not a.allow_cpu:
            print("batch: CUDA unavailable -- pass --allow-cpu for a small CPU sample "
                  "(honest: hours/run with default models)", file=sys.stderr)
            return 2
        if probe.returncode != 0:
            print("preflight: CPU-only (--allow-cpu); prefer yolo11n + --no-seg for samples")
        else:
            print("preflight: CUDA OK")

    label_args = list(DEFAULT_LABEL_ARGS)
    if a.no_seg:
        label_args = [x for x in label_args if x not in ("--seg", "--seg-every", "2")
                      and not x.startswith("nvidia/segformer")]
        # remove orphaned --seg-every value already handled; also drop --seg-model pair
        cleaned = []
        skip_next = False
        for i, x in enumerate(label_args):
            if skip_next:
                skip_next = False
                continue
            if x == "--seg-model":
                skip_next = True
                continue
            cleaned.append(x)
        label_args = cleaned
    if a.model:
        for i, x in enumerate(label_args):
            if x == "--model" and i + 1 < len(label_args):
                label_args[i + 1] = a.model
                break

    tally = {
        "pass": 0, "fail": 0, "inconclusive": 0, "skipped": 0, "errors": 0,
        "skipped_pass": 0, "skipped_fail": 0, "skipped_inconclusive": 0, "skipped_other": 0,
        "started_utc": utc_now(),
        "gate_version": GATE_VERSION,
        "depth_pairing": {"pass": 0, "inconclusive": 0, "skip": 0, "other": 0, "frac_paired": []},
        "suspect_iddrift": {"runs_with_suspects": 0, "total_suspects": 0},
        "device": "cpu" if (not a.dry_run and probe.returncode != 0) else ("cuda" if not a.dry_run else "n/a"),
        "runs": [],
    }

    def bump_dp(dp):
        if not dp:
            return
        st = dp.get("status")
        if st == "PASS":
            tally["depth_pairing"]["pass"] += 1
        elif st == "INCONCLUSIVE":
            tally["depth_pairing"]["inconclusive"] += 1
        elif st == "SKIP":
            tally["depth_pairing"]["skip"] += 1
        else:
            tally["depth_pairing"]["other"] += 1
        if dp.get("frac_paired") is not None:
            tally["depth_pairing"]["frac_paired"].append(float(dp["frac_paired"]))

    def bump_id(idr):
        if idr and int(idr.get("n_suspect_iddrift") or 0) > 0:
            tally["suspect_iddrift"]["runs_with_suspects"] += 1
            tally["suspect_iddrift"]["total_suspects"] += int(idr["n_suspect_iddrift"])

    for c in cands:
        adir = autotune / c["run_id"]
        prior = read_validation(adir)
        prior_st = prior.get("status") if prior else None
        row = {
            "run_id": c["run_id"],
            "rrd_mb": round(c["bytes"] / (1024 * 1024), 1),
            "action": None, "status": None, "stage_exit": None, "note": None,
        }

        if prior_st and not a.force:
            row["action"] = "skipped"
            row["status"] = prior_st
            row["note"] = "already labelled at current gate"
            tally["skipped"] += 1
            key = {"PASS": "skipped_pass", "FAIL": "skipped_fail",
                   "INCONCLUSIVE": "skipped_inconclusive"}.get(prior_st, "skipped_other")
            tally[key] += 1
            row["depth_pairing"] = depth_pairing_stats(prior)
            bump_dp(row["depth_pairing"])
            if not a.dry_run:
                row["iddrift"] = iddrift_stats(a.python, obstacles_py, adir)
                bump_id(row.get("iddrift"))
            print(f"  SKIP  {c['run_id']}  ({row['rrd_mb']} MB) -- already {prior_st}")
            tally["runs"].append(row)
            continue

        if a.dry_run:
            row["action"] = "dry_run"
            row["note"] = f"would re-label (was {prior_st})" if prior_st else "would label"
            print(f"  PLAN  {c['run_id']}  ({row['rrd_mb']} MB)  {row['note']}")
            tally["runs"].append(row)
            continue

        if a.force and prior_st:
            adir.mkdir(parents=True, exist_ok=True)
            vf = adir / "label_validation.json"
            if vf.is_file():
                vf.rename(adir / f"label_validation.force{time.strftime('%Y%m%d%H%M%S')}.json")
            attempts = adir / ".label_attempts"
            if attempts.is_file():
                attempts.unlink()

        adir.mkdir(parents=True, exist_ok=True)
        (adir / ".no_send").write_text(utc_now() + "\n", encoding="utf-8")
        for name in ("tune_report.json", "tune_patch.yaml", "depth_replay.json", "manifest.json"):
            src = c["dir"] / name
            if src.is_file():
                (adir / name).write_bytes(src.read_bytes())

        print(f"\n>>> LABEL {c['run_id']}  ({row['rrd_mb']} MB) ...")
        row["action"] = "labelled"
        cmd = [
            a.python, str(label_py), str(c["rrd"]),
            "--out", str(adir / "labeled.rrd"),
            "--jsonl", str(adir / "obstacles.jsonl"),
            "--summary-json", str(adir / "obstacle_summary.json"),
            "--validation-json", str(adir / "label_validation.json"),
        ] + label_args
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, cwd=str(weights), timeout=a.timeout_min * 60)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 2
            row["note"] = f"labelling exceeded {a.timeout_min} min"
        row["stage_exit"] = rc
        val = read_validation(adir)
        st = val.get("status") if val else None
        row["status"] = st
        if val:
            write_summary(adir, c["run_id"], val)
        row["depth_pairing"] = depth_pairing_stats(val)
        bump_dp(row["depth_pairing"])
        row["iddrift"] = iddrift_stats(a.python, obstacles_py, adir)
        bump_id(row.get("iddrift"))
        elapsed = int(time.time() - t0)

        if st == "PASS":
            tally["pass"] += 1
            print(f"  OK    {c['run_id']} -> PASS ({elapsed}s)")
        elif st == "FAIL":
            tally["fail"] += 1
            print(f"  FAIL  {c['run_id']} ({elapsed}s)")
        elif st == "INCONCLUSIVE":
            tally["inconclusive"] += 1
            print(f"  INC   {c['run_id']} -> INCONCLUSIVE ({elapsed}s)")
        else:
            tally["errors"] += 1
            if not row["note"]:
                row["note"] = f"no validation (exit {rc})"
            print(f"  ERR   {c['run_id']}  exit={rc}  {row['note']}")

        if row.get("depth_pairing"):
            print(f"         depth_pairing={row['depth_pairing'].get('status')} "
                  f"frac={row['depth_pairing'].get('frac_paired')}")
        if row.get("iddrift"):
            print(f"         suspect_iddrift={row['iddrift']['n_suspect_iddrift']}/"
                  f"{row['iddrift']['n_obstacles']} obstacles")
        tally["runs"].append(row)

    fracs = tally["depth_pairing"]["frac_paired"]
    if fracs:
        tally["depth_pairing"]["frac_summary"] = {
            "n": len(fracs),
            "min": round(min(fracs), 4),
            "median": round(statistics.median(fracs), 4),
            "max": round(max(fracs), 4),
        }
    else:
        tally["depth_pairing"]["frac_summary"] = None
    tally["finished_utc"] = utc_now()
    out_tally.write_text(json.dumps(tally, indent=2), encoding="utf-8")

    print("\n======== batch tally (mutually exclusive) ========")
    print(f"  PASS          {tally['pass']}  (labelled this run)")
    print(f"  FAIL          {tally['fail']}")
    print(f"  INCONCLUSIVE  {tally['inconclusive']}")
    print(f"  skipped       {tally['skipped']}  (prior PASS={tally['skipped_pass']} "
          f"FAIL={tally['skipped_fail']} INC={tally['skipped_inconclusive']})")
    print(f"  errors        {tally['errors']}")
    print(f"  eligible      {len(cands)}")
    print(f"  corpus        PASS={tally['pass'] + tally['skipped_pass']} "
          f"FAIL={tally['fail'] + tally['skipped_fail']} "
          f"INC={tally['inconclusive'] + tally['skipped_inconclusive']}")
    print(f"  depth_pairing PASS={tally['depth_pairing']['pass']} "
          f"INC={tally['depth_pairing']['inconclusive']} SKIP={tally['depth_pairing']['skip']}")
    fs = tally["depth_pairing"]["frac_summary"]
    if fs:
        print(f"  frac_paired   min={fs['min']} median={fs['median']} max={fs['max']} (n={fs['n']})")
    print(f"  suspect_iddrift  runs_with={tally['suspect_iddrift']['runs_with_suspects']} "
          f"total_suspects={tally['suspect_iddrift']['total_suspects']}")
    print(f"tally -> {out_tally}")
    if a.dry_run:
        return 0
    return 2 if tally["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
