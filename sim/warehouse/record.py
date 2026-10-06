"""Episode recording + dataset exporters for the K1 warehouse sim.

A Recorder rides along an episode WITHOUT changing it: it never draws from env.rng and never
writes env.model / env.data (head-POV frames render from a private MjData copy). Each row is one
control step as the policy lived it: the snapshot the action was chosen on (pose, flags, actors,
TRAIN_CONTRACT state, the scan, the head frame), the action, and what the step did.

Exporters. Every one marks its output SIMULATED, and dataset() names files k1sim_*:
  jsonl    header {"type": "scenario"} (exact) + one {"type": "step"} line per row; action is
           exact, so header + actions replay the episode bit for bit
  rrd      Rerun .rrd on the robot's own entity paths + timelines (robot/k1_rerun.py), so
           eval/rrd_to_lerobot.py and the other .rrd tools read it unchanged -- but only when
           asked: their shared reader (rrd_to_lerobot.read_rrd / read_rrd_frames) refuses any
           .rrd carrying the static /sim/provenance marker unless allow_sim=True, so sim data
           cannot reach calibration, Local Map ingest or a dataset by accident. rrd_to_lerobot.py
           --allow-sim converts one and labels it booster_k1_sim.
  lerobot  eval/rrd_to_lerobot.write_raw layout: TRAIN_CONTRACT state/action + head_rgb mp4,
           meta/info.json + meta/sim_provenance.json say SIMULATED

observation.state is SYNTHETIC: the sim has no YOLO/ReID, so contract_state() maps the sim's
detector model onto the TRAIN_CONTRACT columns. Right shapes and semantics for pipeline plumbing
and pretraining; not a measured robot signal.

Usage:  python record.py selftest      -> RECORD-SELFTEST-OK
        python record.py dataset --task follow --episodes 10 --format lerobot --out data/
"""
import argparse, copy, functools, importlib, io, json, math, pathlib, shutil, subprocess, sys, tempfile, time

import mujoco
import numpy as np
from PIL import Image

from k1_warehouse import (CTRL_DT, I_SCAN, N_RAYS, RAY_FOV, RAY_MAX, REPO, STALE_S, K1WarehouseEnv,
                          action_to_cmd, heuristic, render_option, wrap)

EVAL_DIR = REPO / "eval"
FSM = json.loads((EVAL_DIR / "fsm_states.json").read_text())["states"]   # frozen ids, never hardcoded
REACQ_S = 5.0                     # unseen this long -> SEARCHING (same cap as the obs 'since' input)
HEAD_PITCH_DEG = -15.0            # K1_22dof.urdf head_realsense_rgb_joint rpy -1.835 = 15 deg below level
HFOV_DEG = math.degrees(RAY_FOV)  # POV covers what the sim's person detector covers
DEPTH_MAX = 10.0                  # m; beyond (open sky over the walls) reads 0 = no return, like the RealSense
RRD_IMAGE_EVERY = 3               # mirrors RerunSink image_every_n: raw Image + DepthImage are ~0.5 MB/frame
FPS = round(1 / CTRL_DT)
TASK_TEXT = {"follow": "follow the locked person", "pick": "visit the pick stations, then return to the dock"}


def _need(mod, hint):
    try:
        return importlib.import_module(mod)
    except ImportError as e:
        raise RuntimeError(f"{mod} is needed for this: {hint}") from e


def _rrd_to_lerobot():
    if str(EVAL_DIR) not in sys.path:
        sys.path.append(str(EVAL_DIR))        # appended, so sim/warehouse modules win any name clash
    import rrd_to_lerobot
    return rrd_to_lerobot


@functools.lru_cache(maxsize=1)
def _git_sha():
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else "nogit"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def contract_state(env):
    """TRAIN_CONTRACT observation.state from sim internals. Deterministic and read-only:
    [range_m, bearing_deg, rsrc_is_depth, anchor_sim, conf, depth_fps, fsm_state_id].
    Synthetic mapping (the sim has no YOLO/ReID):
      follow  seen: range/bearing to the BELIEVED target (env.belief = last detection, so a swapped
              lock points at the wrong person, like the robot); anchor_sim 0.85 on the true target,
              0.55 when the lock jumped to a worker; conf 0.9; FSM TRACK.
              unseen: NaN range/bearing/anchor_sim/conf and range source 0, exactly what the robot
              emits on unlocked ticks (follow_person_k1.py _emit_follow_obs), so the exporters drop
              these rows like rrd_to_lerobot.assemble_episode does; FSM REACQUIRE < REACQ_S, then SEARCHING.
      pick    range/bearing to the current goal, conf 1, anchor_sim 0, FSM TRACK.
    A finite range is always depth-sourced at a steady 10 fps. bearing_deg uses the robot's
    convention: positive = target to the RIGHT (follow_person_k1.py pinhole helpers)."""
    x, y, yaw = env._pose()
    if env.task == "follow":
        if not env.seen_now:
            fsm = "REACQUIRE" if env.t - env.seen_t < REACQ_S else "SEARCHING"
            return [math.nan, math.nan, 0.0, math.nan, math.nan, 10.0, float(FSM[fsm])]
        p, anchor, conf, fsm = env.belief, 0.85 if env.belief_k == 0 else 0.55, 0.9, "TRACK"
    else:
        p, anchor, conf, fsm = env.goals[0], 0.0, 1.0, "TRACK"
    bearing = -math.degrees(wrap(math.atan2(p[1] - y, p[0] - x) - yaw))
    return [float(math.dist((x, y), p)), float(bearing), 1.0, anchor, conf, 10.0, float(FSM[fsm])]


def _jpeg(rgb):
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _png16(depth_mm):
    buf = io.BytesIO()
    Image.fromarray(depth_mm).save(buf, "PNG")
    return buf.getvalue()


def decode_rgb(b):
    """Recorder JPEG frame -> HxWx3 uint8."""
    return np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))


def decode_depth(b):
    """Recorder depth PNG (uint16 mm) -> HxW float32 m, 0 = no return."""
    return np.asarray(Image.open(io.BytesIO(b)), np.float32) / 1000.0


class Recorder:
    """Records one episode at a time: begin(obs) after env.reset(), step(...) after env.step().
    .rows    per-step dicts, JSON-able (see _snap + step for the fields)
    .frames  head-POV JPEG bytes per row (empty when frames=False); .depths the matching depth PNG
             on every RRD_IMAGE_EVERY-th row (the only ones export_rrd logs), None on the others
    .scenario  the exact scenario (deep copy), so any recording replays.
    Rendering happens in the calling thread; use one Recorder from one thread at a time."""

    def __init__(self, env, frame_size=(320, 240), frames=True):
        self.env, self.size, self.frames_on = env, (int(frame_size[0]), int(frame_size[1])), frames
        self.rows, self.frames, self.depths, self.scenario, self.wall0 = [], [], [], None, 0.0
        self._pre = self._r = self._model = None
        # Head POV without the robot's own body. An env built without the visual layer
        # (visuals=False) records its proxies instead of an empty scene. Depth is always of the
        # proxies: the depth sensor stream the ray scan and planner work on, whatever is drawn.
        self._opt = render_option(head=True, sensor=not env.visuals)
        self._dopt = render_option(head=True, sensor=True)

    def begin(self, obs=None):
        """Start a recording on the episode env.reset() just built. Pass the reset obs so row 0
        carries its scan too (the scan is noisy, so it can only come from the obs)."""
        self.rows, self.frames, self.depths = [], [], []
        self.scenario = copy.deepcopy(self.env.scenario)
        self.wall0 = time.time()
        self._pre = self._snap(obs)

    def step(self, action, obs, reward, terminated, truncated, info):
        if self._pre is None:
            raise RuntimeError("Recorder.begin() must follow env.reset()")
        row, jpg, dep = self._pre
        env = self.env
        row.update(action=np.asarray(action, np.float64).ravel().tolist(),   # raw policy output
                   cmd=list(action_to_cmd(action)),                          # clamped: what the driver sends
                   cmd_applied=env.cmd.tolist(),                             # what the loco runs (latency/drops/watchdog)
                   reward=float(reward), terminated=bool(terminated), truncated=bool(truncated),
                   success=bool(info.get("success", False)), stats=dict(env.stats))
        self.rows.append(row)
        if jpg is not None:
            self.frames.append(jpg)
            self.depths.append(dep)
        self._pre = self._snap(obs)

    def _snap(self, obs):
        """What the policy is about to act on. Reads env only."""
        env = self.env
        x, y, yaw = env._pose()
        row = {"i": len(self.rows), "t": float(env.t), "pose": [float(x), float(y), float(yaw)],
               "stale": bool(env.t - env.last_rx > STALE_S), "standing": bool(env.t < env.walk_ready_t),
               "seen": bool(env.task == "follow" and env.seen_now),
               "workers": [w["pos"].tolist() for w in env.workers],
               "belief": None if env.belief is None else env.belief.tolist(), "belief_k": int(env.belief_k),
               "forklift": None if env.forklift is None else env.forklift_pos().tolist(),
               "state": contract_state(env),
               # 16 low (0.10 m) then 16 high (0.8 m) depth rays in m, right to left (obs order).
               "scan": None if obs is None else (np.asarray(obs[I_SCAN:I_SCAN + 2 * N_RAYS], float) * RAY_MAX).tolist()}
        return (row, *self._render(depth=row["i"] % RRD_IMAGE_EVERY == 0)) if self.frames_on else (row, None, None)

    def _render(self, depth=True):
        """Head-POV RGB (JPEG) + depth (uint16 mm PNG, or None when depth=False) of the current env state.
        RGB is of the drawn scene (the visual layer when the env has one); depth is of the proxies,
        so it matches the row's ray scan and is the same with visuals on or off."""
        env, (w, h) = self.env, self.size
        m = env.model
        if self._model is not m:                    # every reset recompiles the world
            self.close()
            # Scene buffer sized to the model: the 10000 default drops every geom past it, and the
            # workers/forklift/boxes are built after the statics, so a dense map would hide them.
            self._r, self._vd, self._model = mujoco.Renderer(m, h, w, max_geom=m.ngeom + 1000), mujoco.MjData(m), m
            self._head = m.body("k1/head_realsense_rgb_link").id
        d, vd, r = env.data, self._vd, self._r
        # Private MjData: copy positions in, redo kinematics here. env.data is never written, and
        # mocap actors moved after the last mj_step show where they are now.
        vd.qpos[:], vd.mocap_pos[:], vd.mocap_quat[:] = d.qpos, d.mocap_pos, d.mocap_quat
        mujoco.mj_kinematics(m, vd)
        mujoco.mj_camlight(m, vd)
        yaw, el = float(vd.qpos[2]), math.radians(HEAD_PITCH_DEG)
        cam = mujoco.MjvCamera()                    # free camera: eye at the head, along yaw, pitched down
        cam.distance, cam.azimuth, cam.elevation = 1.0, math.degrees(yaw), HEAD_PITCH_DEG
        cam.lookat[:] = vd.xpos[self._head] + [math.cos(el) * math.cos(yaw), math.cos(el) * math.sin(yaw), math.sin(el)]
        k = math.tan(math.radians(HFOV_DEG) / 2) * h / w / math.tan(math.radians(m.vis.global_.fovy) / 2)

        def scene(opt):
            r.update_scene(vd, cam, opt)
            # Free cameras use the model's fovy (45 deg vertical); widen the frustum to HFOV_DEG.
            for c in r.scene.camera:
                c.frustum_bottom, c.frustum_top = c.frustum_bottom * k, c.frustum_top * k
        scene(self._opt)
        rgb = r.render()
        if not depth:
            return _jpeg(rgb), None
        if self.env.visuals:                        # RGB drew the visual layer: depth sees the proxies
            scene(self._dopt)
        r.enable_depth_rendering()
        dep = r.render()
        r.disable_depth_rendering()
        return _jpeg(rgb), _png16(np.where(dep > DEPTH_MAX, 0, np.round(dep * 1000)).astype(np.uint16))

    def close(self):
        if self._r is not None:
            self._r.close()
        self._r = self._model = None


# ---------------------------------------------------------------------- exporters
def provenance(rec):
    scn = rec.scenario
    return {"simulated": True, "sim": "sim/warehouse/k1_warehouse.py",
            "sim_version": f"{scn['format']} v{scn['version']}", "task": scn["task"], "seed": scn["seed"],
            "world": scn["world"].get("name"), "world_source": scn["world"].get("source"),
            "git_sha": _git_sha(), "steps": len(rec.rows), "frame_size": list(rec.size)}


def _round(v):
    if isinstance(v, float):
        return round(v, 4) if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round(x) for x in v]
    return v


def export_jsonl(rec, path):
    """Header line carries the EXACT scenario (unrounded: it must replay bit for bit); step lines
    are rounded to 1e-4 except action and cmd, which stay exact so the header scenario + the
    actions replay the episode bit for bit. Frames are not included (binary; use rrd or lerobot)."""
    with open(path, "w") as f:
        f.write(json.dumps({"type": "scenario", "simulated": True, "steps": len(rec.rows),
                            "scenario": rec.scenario}) + "\n")
        for row in rec.rows:
            # cmd is clipped finite by action_to_cmd; a NaN raw action (broken policy) becomes null.
            act = [a if math.isfinite(a) else None for a in row["action"]]
            f.write(json.dumps({"type": "step", **_round(row), "action": act, "cmd": row["cmd"]},
                               allow_nan=False) + "\n")
    return str(path)


def export_rrd(rec, path):
    """Rerun .rrd with the robot's entity paths and frame_idx/wall timelines. Its own recording
    stream (not the process-global one), app id k1_sim, recording name + /sim/provenance say SIMULATED."""
    rr = _need("rerun", "pip install rerun-sdk==0.33.1")
    if not rec.rows:
        raise ValueError("nothing recorded")
    r2l = _rrd_to_lerobot()
    scn, (w, h) = rec.scenario, rec.size
    s = rr.RecordingStream("k1_sim")
    s.save(str(path))
    s.send_recording_name(f"SIMULATED k1sim {scn['task']} seed {scn['seed']}")
    s.log("/sim/provenance", rr.TextLog(json.dumps(provenance(rec))), static=True)
    if rec.frames:
        f = w / 2 / math.tan(math.radians(HFOV_DEG) / 2)
        s.log("/camera/rgb", rr.Pinhole(resolution=[w, h], focal_length=[f, f], principal_point=[w / 2, h / 2]),
              static=True)
    for i, row in enumerate(rec.rows):
        s.set_time("frame_idx", sequence=i)
        s.set_time("wall", timestamp=rec.wall0 + row["t"])
        for (ent, _), v in zip(r2l.STATE_ENTITIES + r2l.ACTION_ENTITIES, row["state"] + row["cmd"]):
            if ent == "/follow/range_source":
                v = 2.0 if v >= 0.5 else 0.0       # robot code: 2 depth, 0 no range (unlocked; NaN range)
            s.log(ent, rr.Scalars(v))
        if rec.frames and i % RRD_IMAGE_EVERY == 0:
            s.log("/camera/rgb", rr.Image(decode_rgb(rec.frames[i])))
            s.log("/camera/depth", rr.DepthImage(decode_depth(rec.depths[i]), meter=1.0))
    s.flush()
    s.disconnect()
    return str(path)


def export_lerobot(rec, out_dir):
    """eval/rrd_to_lerobot.write_raw layout, relabelled SIMULATED, + meta/sim_provenance.json
    (with the exact scenario, so every episode replays). Like rrd_to_lerobot.assemble_episode,
    only locked rows (finite range) are kept: unlocked REACQUIRE/SEARCHING rows are dropped."""
    _need("pyarrow", "pip install pyarrow")
    if rec.frames:
        _need("imageio", "pip install imageio imageio-ffmpeg")
    if not rec.rows:
        raise ValueError("nothing recorded")
    keep = [i for i, r in enumerate(rec.rows) if math.isfinite(r["state"][0])]
    if not keep:
        raise ValueError("no locked frames: the target was never seen in this recording")
    r2l = _rrd_to_lerobot()
    out, task = pathlib.Path(out_dir), rec.scenario["task"]
    state = np.array([rec.rows[i]["state"] for i in keep], np.float32)
    action = np.array([rec.rows[i]["cmd"] for i in keep], np.float32)
    # ponytail: every frame decoded at once (~230 KB each, write_raw wants a list); stream it if
    # episodes ever get long enough for that to hurt.
    rgb = [decode_rgb(rec.frames[i]) for i in keep] if rec.frames else [None] * len(keep)
    info = r2l.write_raw(str(out), keep, state, action, rgb, FPS, TASK_TEXT[task],
                         f"local/k1_sim_{task}", False)
    info.update(robot_type="booster_k1_sim",
                note="SIMULATED: K1 warehouse sim (sim/warehouse/), NOT robot data. observation.state is a "
                     "synthetic mapping of the sim's detector model (record.py contract_state). "
                     "See meta/sim_provenance.json.")
    (out / "meta" / "info.json").write_text(json.dumps(info, indent=2))
    (out / "meta" / "sim_provenance.json").write_text(json.dumps({**provenance(rec), "scenario": rec.scenario},
                                                                 indent=2))
    return info


def export_lerobot_zip(rec):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        ds = pathlib.Path(td) / "ds"
        export_lerobot(rec, ds)
        return pathlib.Path(shutil.make_archive(str(ds), "zip", ds)).read_bytes()


# ---------------------------------------------------------------------- CLI
def dataset(a):
    world = scn = None
    if a.scenario:
        import sim_io
        scn = sim_io.load_scenario(a.scenario)
        if a.episodes != 1:
            raise SystemExit("--scenario is one exact episode: use --episodes 1 (or --localmap for many on a map)")
    elif a.localmap:
        import sim_io
        world = sim_io.localmap_to_world(a.localmap)
    policy = heuristic
    if a.policy != "heuristic":
        from stable_baselines3 import PPO
        ppo = PPO.load(a.policy, device="cpu")
        policy = lambda o: ppo.predict(o, deterministic=True)[0]
    frames = not a.no_frames and a.format != "jsonl"
    env = K1WarehouseEnv(task=scn["task"] if scn else a.task, world=world, visuals=frames)   # dress only what is drawn
    rec = Recorder(env, frames=frames)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for e in range(a.episodes):
        obs, _ = env.reset(seed=None if scn else a.seed + e, options={"scenario": scn} if scn else None)
        rec.begin(obs)
        done = False
        while not done:
            act = policy(obs)
            obs, rew, term, trunc, info = env.step(act)
            rec.step(act, obs, rew, term, trunc, info)
            done = term or trunc
        p = out / f"k1sim_ep_{env.scenario['seed']}"     # 'sim' in the name: never mistaken for a robot recording
        if a.format == "lerobot":
            export_lerobot(rec, p)
        else:
            p = p.with_suffix("." + a.format)
            (export_jsonl if a.format == "jsonl" else export_rrd)(rec, p)
        print(f"{p.name}: steps={len(rec.rows)} success={info['success']} falls={info['falls']} "
              f"collisions={info['collisions']} human_contacts={info['human_contacts']}")
    rec.close()


def selftest():
    # contract_state: read-only, deterministic, the documented mapping.
    env = K1WarehouseEnv(task="follow")
    env.reset(seed=5)
    rng0 = env.rng.bit_generator.state
    assert contract_state(env) == contract_state(env) and env.rng.bit_generator.state == rng0
    x, y, yaw = env._pose()
    env.belief = np.array([x + 2 * math.cos(yaw + 0.3), y + 2 * math.sin(yaw + 0.3)])   # 2 m, 0.3 rad LEFT
    env.seen_now, env.belief_k = True, 0
    s = contract_state(env)
    assert abs(s[0] - 2) < 1e-9 and abs(s[1] + math.degrees(0.3)) < 1e-9, s     # left = negative bearing
    assert s[2:] == [1.0, 0.85, 0.9, 10.0, FSM["TRACK"]], s
    env.belief_k = 1
    assert contract_state(env)[3] == 0.55
    env.seen_now, env.seen_t = False, env.t - 1.0
    s = contract_state(env)                     # unlocked: NaN + source 0, like the robot's emitter
    assert all(math.isnan(s[k]) for k in (0, 1, 3, 4)) and s[2] == 0.0 and s[6] == FSM["REACQUIRE"], s
    env.seen_t = env.t - 2 * REACQ_S
    assert contract_state(env)[6] == FSM["SEARCHING"]
    env = K1WarehouseEnv(task="pick")
    env.reset(seed=5)
    assert contract_state(env)[3:] == [0.0, 1.0, 10.0, FSM["TRACK"]]

    # Recording must not change the episode: same seed + policy, the lean training env without a
    # Recorder vs a dressed (visuals=True) env with one.
    T = 60
    runs = []
    for on in (False, True):
        env = K1WarehouseEnv(task="follow", visuals=on)
        obs, _ = env.reset(seed=11)
        rec = Recorder(env) if on else None
        if rec:
            rec.begin(obs)
        seq = [obs]
        for _ in range(T):
            act = heuristic(obs)
            obs, rew, term, trunc, info = env.step(act)
            if rec:
                rec.step(act, obs, rew, term, trunc, info)
            seq.append(obs)
            assert not (term or trunc), "pick a seed that runs 60 steps"
        runs.append((np.array(seq), dict(env.stats)))
    assert np.array_equal(runs[0][0], runs[1][0]) and runs[0][1] == runs[1][1], "Recorder changed the episode"
    assert len(rec.rows) == len(rec.frames) == len(rec.depths) == T
    assert [r["i"] for r in rec.rows] == list(range(T)) and rec.rows[0]["t"] == 0.0 and len(rec.rows[0]["scan"]) == 32
    assert [d is not None for d in rec.depths] == [i % RRD_IMAGE_EVERY == 0 for i in range(T)], "depth only where rrd logs it"
    im, dp = decode_rgb(rec.frames[0]), decode_depth(rec.depths[0])
    assert im.shape == (240, 320, 3) and im.std() > 5, "blank frame"
    assert dp.shape == (240, 320) and dp.max() <= DEPTH_MAX and (dp > 0.3).mean() > 0.5, (dp.min(), dp.max())
    print(f"frame {len(rec.frames[0])} B jpeg, depth {len(rec.depths[0])} B png")
    assert rec._r.scene.maxgeom == env.model.ngeom + 1000, "renderer scene buffer not sized to the model"
    lean = K1WarehouseEnv(task="follow")        # no visual layer: the head POV draws the proxies, not nothing
    lr = Recorder(lean)
    lr.begin(lean.reset(seed=11)[0])
    assert decode_rgb(lr._pre[1]).std() > 5, "a visuals=False env recorded a blank frame"
    # ...and depth is of the proxies either way: the dressed env's reset frame, to the mm rounding.
    assert np.abs(decode_depth(lr._pre[2]) - decode_depth(rec.depths[0])).max() < 1.5e-3, "depth depends on visuals"
    lr.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = pathlib.Path(td)
        state = np.array([r["state"] for r in rec.rows], np.float32)
        action = np.array([r["cmd"] for r in rec.rows], np.float32)
        seen = [i for i, r in enumerate(rec.rows) if r["seen"]]     # locked rows: the only ones exported
        assert 0 < len(seen) < T and seen == [i for i in range(T) if np.isfinite(state[i, 0])], seen

        # jsonl: exact scenario header + T step lines, rounded except the exact action.
        lines = [json.loads(ln) for ln in open(export_jsonl(rec, td / "ep.jsonl"))]
        assert lines[0]["type"] == "scenario" and lines[0]["scenario"] == json.loads(json.dumps(rec.scenario))
        assert len(lines) == T + 1 and all(ln["type"] == "step" for ln in lines[1:])
        assert np.allclose(np.array([ln["state"] for ln in lines[1:]], float), state, atol=1e-4, equal_nan=True)
        # ...and it replays: header scenario + the file's actions -> the recorded episode, bit for bit.
        env2 = K1WarehouseEnv(task="follow")
        o2, _ = env2.reset(options={"scenario": lines[0]["scenario"]})
        seq2 = [o2] + [env2.step(np.array(ln["action"]))[0] for ln in lines[1:]]
        assert np.array_equal(np.array(seq2), runs[1][0]), "jsonl does not replay bit for bit"

        # rrd: the repo's own reader keeps exactly the locked frames, with the same state/action columns.
        r2l = _rrd_to_lerobot()
        ep_rrd = export_rrd(rec, td / "ep.rrd")
        try:
            r2l.read_rrd(ep_rrd)                    # robot tools refuse sim recordings by default
            raise AssertionError("read_rrd accepted a SIMULATED .rrd without allow_sim")
        except r2l.SimulatedRecordingError:
            pass
        scalars, images, depth = r2l.read_rrd(ep_rrd, allow_sim=True)
        prov = [str(ch.to_record_batch().to_pylist()) for ch in r2l._load_store(str(td / "ep.rrd")).stream()
                if ch.is_static and str(ch.entity_path) == "/sim/provenance"]
        assert prov and '"simulated": true' in prov[0], "rrd not marked SIMULATED"
        frames, st, ac, rgb = r2l.assemble_episode(scalars, images)
        assert frames == seen, frames
        assert np.allclose(st, state[seen], rtol=1e-6, atol=1e-6) and np.allclose(ac, action[seen], rtol=1e-6, atol=1e-6)
        n_img = math.ceil(T / RRD_IMAGE_EVERY)
        assert len(images) == n_img and len(depth) == n_img and rgb[-1].shape == (240, 320, 3), (len(images), len(depth))

        # lerobot: parquet reads back the locked rows as float32[7] / float32[2], labelled SIMULATED.
        import pyarrow.parquet as pq
        info = export_lerobot(rec, td / "lr")
        tab = pq.read_table(td / "lr/data/chunk-000/episode_000000.parquet")
        assert tab.num_rows == len(seen) and all(str(tab.schema.field(c).type.value_type) == "float"
                                                 for c in ("observation.state", "action"))
        assert np.allclose(np.array(tab.column("observation.state").to_pylist()), state[seen])
        assert np.allclose(np.array(tab.column("action").to_pylist()), action[seen])
        meta = json.loads((td / "lr/meta/info.json").read_text())
        prov = json.loads((td / "lr/meta/sim_provenance.json").read_text())
        assert meta["robot_type"] == "booster_k1_sim" and "SIMULATED" in meta["note"] and info["total_frames"] == len(seen)
        assert prov["simulated"] and prov["seed"] == 11 and prov["scenario"]["seed"] == 11 and prov["git_sha"]
        vid = td / "lr/videos/chunk-000/observation.images.head_rgb"
        assert (vid / "episode_000000.mp4").exists() or (vid / "episode_000000_frames").exists()
        print("lerobot video_backend:", info["video_backend"])
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(export_lerobot_zip(rec)))
        assert "meta/sim_provenance.json" in z.namelist() and "data/chunk-000/episode_000000.parquet" in z.namelist()

    # Next episode: reset recompiles the model; the Recorder rebinds its renderer.
    obs, _ = env.reset(seed=12)
    rec.begin(obs)
    for _ in range(3):
        act = heuristic(obs)
        obs, rew, term, trunc, info = env.step(act)
        rec.step(act, obs, rew, term, trunc, info)
    assert rec._model is env.model and len(rec.rows) == len(rec.frames) == 3 and rec.scenario["seed"] == 12
    rec.close()
    print("RECORD-SELFTEST-OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "dataset"])
    ap.add_argument("--task", default="follow", choices=["pick", "follow"])
    ap.add_argument("--episodes", type=int, default=1)
    ap.add_argument("--policy", default="heuristic", help="heuristic, or a PPO .zip from train.py")
    ap.add_argument("--format", default="lerobot", choices=["rrd", "lerobot", "jsonl"])
    ap.add_argument("--out", help="output dir; one k1sim_ep_<seed> file/dir per episode")
    ap.add_argument("--scenario", help="replay this exact scenario JSON (one episode)")
    ap.add_argument("--localmap", help="Local Map domain id: randomize episodes on that real site's map")
    ap.add_argument("--seed", type=int, default=0, help="first episode seed (seeds seed..seed+N-1)")
    ap.add_argument("--no-frames", action="store_true", help="skip head-POV rendering")
    a = ap.parse_args()
    if a.cmd == "selftest":
        selftest()
    else:
        if not a.out or a.episodes < 1:
            ap.error("dataset needs --out DIR and --episodes >= 1")
        dataset(a)
