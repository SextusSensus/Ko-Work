#!/usr/bin/env python3
"""train_act.py -- P7.2: train the small imitation policy on a batch_ingest dataset.

Imitates control.py's (vx, vyaw) from observation.state (7-dim) + head_rgb. The control law is a
MEMORYLESS proportional law + a fixed first-order slew (follow_person_k1.py ~L2245-2295) -- no
multimodal action distribution -- so the default is a `chunk_size=1` ACT: a small late-fusion
regressor (image CNN + state MLP -> 2-wide action head), a legal degenerate ACT. The full CVAE+
transformer sits behind `--arch act_cvao` (NOT built) and is only justified if a closed-loop rollout
later shows the slew hysteresis needs history (design pass 2026-07-10, docs/TRAIN_CONTRACT.md).

Discipline (plan invariants; enforced, never bypassed): episode-level train/val split read from
meta/splits.json VERBATIM (never re-split); TRAIN-split-only norm stats read from meta/stats.json's
`normalize` block (never hardcode indices); the checkpoint carries norm_stats + stats_hash via
eval/checkpoint_contract.py so the P7.3/P7.4 norm-mismatch bug is structurally impossible; every run
logs config + git SHA + dataset_version; val MSE reported PER FSM STATE (blended hides failure).
Never imports lerobot (torch pin conflict); never uses torch.compile (flaky on Blackwell sm_120).

Runs on any CUDA worker (desktop 3080/cu126 OR laptop 5060/cu128) -- a `train` pool job. Authored +
GPU-smoke-tested on the 5060 (sm_120). The real multi-run dataset + the honest 8 GB min_vram_gb are
VERIFY ON CLUSTER.
  python eval/train_act.py selftest                         -> TRAIN-ACT-SELFTEST-OK
  python eval/train_act.py train --dataset <ds> --out <dir> [--epochs 60 --batch 64 --img-hw 224]
"""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np
import torch
import torch.nn as nn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkpoint_contract as ckpt
import fsm_groups

STATE_NAMES = ["range_m", "bearing_deg", "rsrc_is_depth", "anchor_sim", "conf", "depth_fps", "fsm_state_id"]
ACTION_NAMES = ["vx", "vyaw"]
FSM_COL = 6
IMAGE_SCALE = 255.0
DEFAULTS = {"epochs": 60, "batch": 64, "lr": 3e-4, "weight_decay": 1e-4, "warmup": 500,
            "img_hw": 224, "img_channels": 1, "per_state_floor": 30, "seed": 0, "num_workers": 0,
            "grad_clip": 1.0, "arch": "act_k1_regressor"}


# ---- normalization (from the dataset card's stats.json normalize block) -------------------------
class Normalizer:
    """Z-scores the marked channels (epsilon-guarded), passes the categorical/boolean ones through.
    NEVER hardcodes indices -- reads them from stats['normalize']. TRAIN-split stats applied to val too
    (never recomputed over val)."""
    def __init__(self, stats):
        s = stats["observation.state"]
        self.s_mean = np.asarray(s["mean"], np.float32)
        self.s_std = np.asarray(s["std"], np.float32)
        a = stats["action"]
        self.a_mean = np.asarray(a["mean"], np.float32)
        self.a_std = np.asarray(a["std"], np.float32)
        nz = stats["normalize"]
        self.s_z = list(nz["observation.state"]["zscore_idx"])
        self.a_z = list(nz["action"]["zscore_idx"])
        self._eps = 1e-6

    def _z(self, x, mean, std, idx):
        out = np.array(x, np.float32, copy=True)
        for i in idx:
            out[..., i] = (out[..., i] - mean[i]) / (std[i] if std[i] > self._eps else 1.0)
        return out

    def norm_state(self, s):
        return self._z(s, self.s_mean, self.s_std, self.s_z)

    def norm_action(self, a):
        return self._z(a, self.a_mean, self.a_std, self.a_z)

    def denorm_action(self, a):
        out = np.array(a, np.float32, copy=True)
        for i in self.a_z:
            out[..., i] = out[..., i] * (self.a_std[i] if self.a_std[i] > self._eps else 1.0) + self.a_mean[i]
        return out


# ---- model: chunk_size=1 ACT = late-fusion regressor --------------------------------------------
class _ResLite(nn.Module):
    def __init__(self, cin, cout, stride):
        super().__init__()
        self.c1 = nn.Conv2d(cin, cout, 3, stride, 1, bias=False)
        self.b1 = nn.BatchNorm2d(cout)
        self.c2 = nn.Conv2d(cout, cout, 3, 1, 1, bias=False)
        self.b2 = nn.BatchNorm2d(cout)
        self.act = nn.SiLU()
        self.skip = (nn.Sequential() if (stride == 1 and cin == cout)
                     else nn.Sequential(nn.Conv2d(cin, cout, 1, stride, bias=False), nn.BatchNorm2d(cout)))

    def forward(self, x):
        y = self.act(self.b1(self.c1(x)))
        y = self.b2(self.c2(y))
        return self.act(y + self.skip(x))


class ActK1Regressor(nn.Module):
    """Image CNN (mono, GAP head -> resolution-tolerant) + state MLP, late-fused to a 2-wide action
    head. Output [B, chunk_size=1, 2]. NO output squashing -- the 3-layer velocity clamp (Python + C++)
    owns saturation; a tanh here would fight de-normalization."""
    def __init__(self, img_channels=1, state_dim=7):
        super().__init__()
        self.stem = nn.Sequential(nn.Conv2d(img_channels, 32, 3, 2, 1, bias=False),
                                  nn.BatchNorm2d(32), nn.SiLU())
        self.blocks = nn.Sequential(_ResLite(32, 64, 2), _ResLite(64, 128, 2),
                                    _ResLite(128, 128, 2), _ResLite(128, 128, 2))
        self.img_head = nn.Linear(128, 128)
        self.state_enc = nn.Sequential(nn.Linear(state_dim, 64), nn.LayerNorm(64), nn.SiLU(),
                                       nn.Linear(64, 64), nn.SiLU())
        self.fuse = nn.Sequential(nn.Linear(192, 128), nn.SiLU(), nn.Linear(128, 2))

    def forward(self, image, state):
        z = self.blocks(self.stem(image))
        z = z.mean(dim=(2, 3))                       # global average pool
        img = self.img_head(z)
        st = self.state_enc(state)
        out = self.fuse(torch.cat([img, st], dim=1))
        return out.unsqueeze(1)                       # [B, 1, 2]


# ---- dataset: RAW LeRobot-v3 (batch_ingest output) ----------------------------------------------
class RawLeRobotV3FollowDataset(torch.utils.data.Dataset):
    """Card-first loader. Reads meta/{info.json,splits.json,stats.json,video_index.json}; refuses
    loudly before opening any parquet on a missing split / stats-hash mismatch / dataset_version skew.
    mp4 decoded lazily; parquet frame t -> the nearest-EARLIER image via video_index image_frame_indices
    (the load-bearing alignment: the mp4 holds only the decimated frames, not T)."""
    def __init__(self, root, split, img_hw=224, img_channels=1):
        self.root = root
        self.img_hw = int(img_hw)
        self.img_channels = int(img_channels)
        with open(os.path.join(root, "meta", "info.json")) as f:
            self.card = json.load(f)
        assert self.card["features"]["observation.state"]["names"] == STATE_NAMES, "state contract drift"
        with open(os.path.join(root, "meta", "splits.json")) as f:
            splits = json.load(f)
        if splits.get("dataset_version") != self.card["dataset_version"]:
            raise SystemExit("REFUSE: splits.json dataset_version != card")
        if not splits.get("val") or not splits.get("train"):
            raise SystemExit("REFUSE: empty train or val split")
        run_ids = splits[split]
        idx_of = self.card["run_id_to_episode_index"]
        stats_path = os.path.join(root, "meta", "stats.json")
        self.stats_hash = ckpt.sha256_file(stats_path)
        if self.stats_hash != self.card["stats_hash"]:
            raise SystemExit("REFUSE: stats.json bytes != card stats_hash")
        with open(stats_path) as f:
            self.stats = json.load(f)
        if "normalize" not in self.stats:
            raise SystemExit("REFUSE: stats.json has no normalize block")
        # Council #9: splits train set must match the TRAIN-only norms baked into stats.
        _bound = self.stats.get("train_run_ids_sha256") or self.card.get("train_run_ids_sha256")
        if _bound:
            import hashlib
            _got = hashlib.sha256(("\n".join(run_ids) + "\n").encode("utf-8")).hexdigest()
            if split == "train" and _got != _bound:
                raise SystemExit(
                    "REFUSE: splits.json train run-ids sha256 %s != stats/card train_run_ids_sha256 %s "
                    "(hand-edited splits under stale TRAIN-only norms)" % (_got[:12], _bound[:12]))
            _listed = self.stats.get("train_run_ids")
            if split == "train" and isinstance(_listed, list) and list(run_ids) != list(_listed):
                raise SystemExit("REFUSE: splits.json train run-ids != stats.train_run_ids")
        self.norm = Normalizer(self.stats)
        with open(os.path.join(root, "meta", "video_index.json")) as f:
            self.vindex = json.load(f)
        self.splits_source = "%s/meta/splits.json@sha256:%s" % (
            self.card["dataset_version"], ckpt.sha256_file(os.path.join(root, "meta", "splits.json")))

        import pyarrow.parquet as pq
        self.frames = []                              # flat list of (ep_key, row) for __getitem__
        self.ep_state = {}
        self.ep_action = {}
        self._vid_cache = {}
        for rid in run_ids:
            if rid not in idx_of:
                raise SystemExit("REFUSE: split run_id %s not in card map" % rid)
            ekey = "episode_%06d" % idx_of[rid]
            # Fail-closed on a non-mp4 episode: a png-fallback / failed encode (video_backend != "mp4")
            # feeds all-black frames to the CNN while every other gate (state contract, stats_hash, FSM)
            # still passes -- a silently degraded checkpoint. Refuse before any training starts.
            _vinfo = self.vindex.get(ekey, {})
            if _vinfo.get("image_frame_indices") and _vinfo.get("video_backend") != "mp4":
                raise SystemExit("REFUSE: episode %s (run %s) declares %d image frames but "
                                 "video_backend=%r != 'mp4' -- re-mint with a working mp4 encoder "
                                 "(imageio-ffmpeg)." % (ekey, rid, len(_vinfo["image_frame_indices"]),
                                                        _vinfo.get("video_backend")))
            t = pq.read_table(os.path.join(root, "data", "chunk-000", ekey + ".parquet"))
            st = np.asarray(t.column("observation.state").to_pylist(), np.float32)
            ac = np.asarray(t.column("action").to_pylist(), np.float32)
            # FSM sanity: every id must map (batch_ingest guarantees it; assert never trusts blindly).
            for v in st[:, FSM_COL].tolist():
                if fsm_groups.name_for_id(v) == fsm_groups.UNMAPPED:
                    raise SystemExit("REFUSE: unmapped fsm_state_id %r in %s" % (v, rid))
            self.ep_state[ekey] = st
            self.ep_action[ekey] = ac
            for row in range(st.shape[0]):
                self.frames.append((ekey, row))

    def __len__(self):
        return len(self.frames)

    def _episode_frames(self, ekey):
        """Decode the episode mp4 once (cached). Returns (frames[N,H,W,3] uint8, image_frame_indices)."""
        if ekey in self._vid_cache:
            return self._vid_cache[ekey]
        vi = self.vindex[ekey]
        ifi = vi.get("image_frame_indices", [])
        mp4 = os.path.join(self.root, "videos", "chunk-000", "observation.images.head_rgb", ekey + ".mp4")
        frames = []
        if ifi and os.path.isfile(mp4):
            import imageio.v2 as imageio
            rd = imageio.get_reader(mp4, format="FFMPEG")
            for fr in rd:
                frames.append(np.asarray(fr))
            rd.close()
        # Tolerate ONE torn tail frame; refuse a real truncation. Silently trimming to min() would
        # freeze the whole back of the episode on one stale image (every downstream gate still green).
        if ifi and len(frames) < len(ifi) - 1:
            raise SystemExit("REFUSE: episode %s decoded %d mp4 frames but the index declares %d "
                             "(truncated encode) -- would train on stale/frozen frames." %
                             (ekey, len(frames), len(ifi)))
        n = min(len(frames), len(ifi))
        out = (frames[:n], ifi[:n])
        self._vid_cache[ekey] = out
        return out

    def _image_for(self, ekey, row):
        frames, ifi = self._episode_frames(ekey)
        img = None
        if frames:
            j = -1
            for k, fidx in enumerate(ifi):           # nearest-earlier: largest ifi <= row
                if fidx <= row:
                    j = k
                else:
                    break
            if j >= 0:
                img = frames[j]
        if img is None:                              # pre-first-image frame -> black (never a future image)
            img = np.zeros((self.img_hw, self.img_hw, 3), np.uint8)
        import cv2
        if img.shape[0] != self.img_hw or img.shape[1] != self.img_hw:
            img = cv2.resize(img, (self.img_hw, self.img_hw))
        if self.img_channels == 1:
            g = (0.299 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.114 * img[:, :, 2]).astype(np.float32)
            return (g / IMAGE_SCALE)[None, :, :]     # [1,H,W]
        return np.transpose(img.astype(np.float32) / IMAGE_SCALE, (2, 0, 1))  # [3,H,W]

    def __getitem__(self, i):
        ekey, row = self.frames[i]
        st = self.ep_state[ekey][row]
        ac = self.ep_action[ekey][row]
        image = self._image_for(ekey, row)
        return (torch.from_numpy(self.norm.norm_state(st)),
                torch.from_numpy(image),
                torch.from_numpy(self.norm.norm_action(ac)),
                float(st[FSM_COL]))                   # raw fsm id (for per-state bucketing)


# ---- per-FSM-state val MSE report ---------------------------------------------------------------
def val_report(model, ds, device, norm, per_state_floor, use_bf16):
    model.eval()
    buckets = {}                                      # name -> list of (pred_raw[2], target_raw[2])
    loader = torch.utils.data.DataLoader(ds, batch_size=256, shuffle=False)
    with torch.no_grad():
        for state, image, action, fsm_id in loader:
            state, image = state.to(device), image.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=use_bf16):
                pred = model(image, state)[:, 0, :].float().cpu().numpy()   # normalized
            pred_raw = norm.denorm_action(pred)
            tgt_raw = norm.denorm_action(action.numpy())
            for k in range(len(fsm_id)):
                name = fsm_groups.name_for_id(float(fsm_id[k]))
                if name == fsm_groups.UNMAPPED:
                    raise SystemExit("REFUSE: UNMAPPED fsm id in val set (data bug)")
                buckets.setdefault(name, []).append((pred_raw[k], tgt_raw[k]))
    report = {"per_state_floor": per_state_floor, "states": {}}
    for name in fsm_groups.CANONICAL_NAMES:
        rows = buckets.get(name, [])
        n = len(rows)
        if n < per_state_floor:
            report["states"][name] = {"count": n, "status": "INSUFFICIENT",
                                      "note": "n=%d < floor=%d" % (n, per_state_floor)}
            continue
        p = np.array([r[0] for r in rows]); t = np.array([r[1] for r in rows])
        mse = ((p - t) ** 2).mean(axis=0)
        report["states"][name] = {"count": n, "status": "ok",
                                  "mse_vx": float(mse[0]), "mse_vyaw": float(mse[1])}
    return report


def _git_sha():
    try:
        return subprocess.check_output(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                                        "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL).decode().strip() or "nogit"
    except (subprocess.SubprocessError, OSError):
        return "nogit"


def _config(cfg):
    return {"chunk_size": 1, "chunk_consumption": "replan_every_tick", "obs_action_pairing": "same_tick",
            "image_norm": {"scale": IMAGE_SCALE}, "arch": cfg["arch"], "lr": cfg["lr"],
            "epochs": cfg["epochs"], "batch": cfg["batch"], "seed": cfg["seed"],
            "per_state_floor": cfg["per_state_floor"], "img_hw": [cfg["img_hw"], cfg["img_hw"]],
            "img_channels": cfg["img_channels"], "weight_decay": cfg["weight_decay"]}


def train(dataset, out_dir, cfg):
    os.makedirs(out_dir, exist_ok=True)
    torch.manual_seed(cfg["seed"]); np.random.seed(cfg["seed"])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    use_bf16 = device.type == "cuda"
    tr = RawLeRobotV3FollowDataset(dataset, "train", cfg["img_hw"], cfg["img_channels"])
    va = RawLeRobotV3FollowDataset(dataset, "val", cfg["img_hw"], cfg["img_channels"])
    model = ActK1Regressor(cfg["img_channels"]).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"], weight_decay=cfg["weight_decay"])
    loader = torch.utils.data.DataLoader(tr, batch_size=cfg["batch"], shuffle=True,
                                         num_workers=cfg["num_workers"], drop_last=False,
                                         pin_memory=(device.type == "cuda"))
    total_steps = max(1, cfg["epochs"] * len(loader))
    warmup = min(cfg["warmup"], total_steps)

    def lr_at(step):
        if step < warmup:
            return step / max(1, warmup)
        p = (step - warmup) / max(1, total_steps - warmup)
        return (1e-5 / cfg["lr"]) + 0.5 * (1 - 1e-5 / cfg["lr"]) * (1 + math.cos(math.pi * p))

    loss_fn = nn.L1Loss()
    step = 0
    for epoch in range(cfg["epochs"]):
        model.train()
        for state, image, action, _fsm in loader:
            for g in opt.param_groups:
                g["lr"] = cfg["lr"] * lr_at(step)
            state, image, action = state.to(device), image.to(device), action.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=use_bf16):
                pred = model(image, state)
                loss = loss_fn(pred, action.unsqueeze(1))
            opt.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), cfg["grad_clip"])
            opt.step(); step += 1
    report = val_report(model, va, device, tr.norm, cfg["per_state_floor"], use_bf16)

    payload = ckpt.build_payload(
        state_dict={k: v.cpu() for k, v in model.state_dict().items()},
        norm_stats=tr.stats, stats_hash=tr.stats_hash, dataset_version=tr.card["dataset_version"],
        splits_source=tr.splits_source, config=_config(cfg), git_sha=_git_sha(),
        torch_version=torch.__version__,
        env={"python": sys.version.split()[0], "platform": sys.platform,
             "cuda": torch.version.cuda, "device": (torch.cuda.get_device_name(0) if device.type == "cuda" else "cpu"),
             "node_id": os.environ.get("K1_NODE_ID", "unknown")})
    cpath = os.path.join(out_dir, "best.pt")
    torch.save(payload, cpath)
    with open(cpath + ".val_report.json", "w") as f:
        json.dump(report, f, sort_keys=True, indent=2)
    print("VAL REPORT (raw units, per FSM state):")
    for name, r in report["states"].items():
        if r["status"] == "ok":
            print("  %-14s n=%-5d mse_vx=%.5f mse_vyaw=%.5f" % (name, r["count"], r["mse_vx"], r["mse_vyaw"]))
        else:
            print("  %-14s %s" % (name, r["note"]))
    print("WROTE %s (dataset_version=%s stats_hash=%s...)" % (cpath, tr.card["dataset_version"], tr.stats_hash[:12]))
    return report


# ---- selftest (tiny GPU overfit + checkpoint round-trip) ----------------------------------------
def _selftest():
    import shutil
    import tempfile
    import synth_fixtures as sf
    import batch_ingest as bi
    base = tempfile.mkdtemp(prefix="k1train-")
    try:
        eps = [e for e in sf.make_episodes(seed=0) if e["meta"].get("outcome") == "pass"
               and fsm_groups.UNMAPPED not in fsm_groups.count_ids(np.asarray(e["state"])[:, FSM_COL].tolist())]
        ds = os.path.join(base, "ds")
        bi.mint_dataset(eps, ds, "k1_follow_v1", seed=0, val_fraction=0.34)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print("SELFTEST device:", device, "-", (torch.cuda.get_device_name(0) if device.type == "cuda" else "cpu"))

        # (2) OVERFIT a tiny batch on GPU -- loss must fall.
        tr = RawLeRobotV3FollowDataset(ds, "train", img_hw=64, img_channels=1)
        model = ActK1Regressor(1).to(device)
        opt = torch.optim.AdamW(model.parameters(), lr=1e-3)
        loader = torch.utils.data.DataLoader(tr, batch_size=min(16, len(tr)), shuffle=True)
        state, image, action, _ = next(iter(loader))
        state, image, action = state.to(device), image.to(device), action.to(device)
        l0 = None
        for s in range(60):
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=(device.type == "cuda")):
                loss = nn.L1Loss()(model(image, state), action.unsqueeze(1))
            if l0 is None:
                l0 = float(loss.detach())
            opt.zero_grad(); loss.backward(); opt.step()
        lf = float(loss.detach())
        assert lf < 0.5 * l0, "overfit loss did not fall: %.4f -> %.4f" % (l0, lf)
        print("  overfit loss %.4f -> %.4f (GPU trains)" % (l0, lf))

        # (3) checkpoint round-trip via checkpoint_contract; a corrupted stats_hash must be caught.
        payload = ckpt.build_payload(
            state_dict={k: v.cpu() for k, v in model.state_dict().items()}, norm_stats=tr.stats,
            stats_hash=tr.stats_hash, dataset_version=tr.card["dataset_version"],
            splits_source=tr.splits_source, config=_config(dict(DEFAULTS)), git_sha=_git_sha(),
            torch_version=torch.__version__, env={"python": "x", "platform": "x", "cuda": "x"})
        p = os.path.join(base, "best.pt"); torch.save(payload, p)
        loaded = torch.load(p, weights_only=False)
        ckpt.validate_payload(loaded, expect_stats_hash=tr.stats_hash)
        try:
            ckpt.validate_payload({**loaded, "stats_hash": "0" * 64}, expect_stats_hash=tr.stats_hash)
        except ValueError:
            pass
        else:
            raise AssertionError("corrupted stats_hash must raise CKPT-CONTRACT VIOLATION")
        print("  checkpoint round-trip + stats-hash gate OK")

        # (4) per-FSM-state report, with at least one INSUFFICIENT row (tiny val set guarantees it).
        va = RawLeRobotV3FollowDataset(ds, "val", img_hw=64, img_channels=1)
        rep = val_report(model, va, device, tr.norm, per_state_floor=1000, use_bf16=(device.type == "cuda"))
        assert any(r["status"] == "INSUFFICIENT" for r in rep["states"].values()), "expected an INSUFFICIENT row"
        assert set(rep["states"]) == set(fsm_groups.CANONICAL_NAMES)
        print("  per-state report rendered (INSUFFICIENT rows present)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("train")
    t.add_argument("--dataset", required=True); t.add_argument("--out", required=True)
    for k, v in DEFAULTS.items():
        t.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "selftest" or a.cmd is None:
        _selftest(); print("TRAIN-ACT-SELFTEST-OK"); return 0
    cfg = {k: getattr(a, k) for k in DEFAULTS}
    train(a.dataset, a.out, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
