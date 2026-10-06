"""Local web control UI for the K1 warehouse sim: watch, drive, break, import and export episodes.

One SimThread owns the env, the mujoco.Renderer and the episode Recorder: OpenGL contexts are
thread-bound, and env.model/env.data are rebuilt on every reset. HTTP handler threads never
touch them. They push commands onto a queue and read a published snapshot (state dict + latest
JPEG). Scenario and MJCF exports run in the SimThread; episode exports get a snapshot of the
recording there and are encoded on the HTTP thread (seconds and ~1 GB at the recording cap).

Manual driving goes through env.step exactly like a policy, so the bridge clamps, link latency
and the staleness watchdog all apply. A server-side deadman zeroes the manual action 300 ms
after the last drive message (a closed tab or a dropped Wi-Fi link stops the robot). Manual
driving runs at real time at most, so that is 300 ms of sim time too.

Usage:  python web.py [--host 127.0.0.1] [--port 8765] [--model runs/x/model.zip]
        python web.py selftest      -> WEB-SELFTEST-OK
"""
import argparse, copy, io, json, math, os, pathlib, queue, socket, tempfile, threading, time, traceback
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# numpy (via k1_warehouse) BEFORE torch: the lazy PPO load would otherwise hit "OMP: Error #15".
import numpy as np
import mujoco
from PIL import Image

import record, sim_io
from k1_warehouse import (CTRL_DT, I_SCAN, I_SEEN, I_STALE, I_STAND, N_RAYS, PLAN_MIN_H, RACK_GROUPS, RAY_FOV, RAY_MAX,
                          K1WarehouseEnv, heuristic, render_option)

HERE = pathlib.Path(__file__).resolve().parent
TASKS, DRIVERS, CAMERAS = ("pick", "follow"), ("auto", "manual", "policy"), ("chase", "top", "head")
VIEWS = ("detailed", "sensor")        # sensor: the proxies the depth rays, detector and planner see
SPEEDS, FAULTS = (0.5, 1, 2, 4, 8), ("stall05", "stall15", "swap")
EXPORTS = {"scenario": ("application/json", ".scenario.json"), "mjcf": ("application/zip", "_mjcf.zip"),
           "jsonl": ("application/x-ndjson", ".jsonl"), "rrd": ("application/octet-stream", ".rrd"),
           "lerobot": ("application/zip", "_lerobot.zip")}
DEADMAN_S = 0.3                       # manual action -> 0 this long after the last drive message
FPS, VIDEO_W, VIDEO_H = 15, 640, 480
MAX_BODY = 8 << 20                    # POST body cap (bytes)
SEED_MAX = 2 ** 63                    # sim_io.validate_scenario's bound, so every episode exports
# ponytail: episodes end after 6 min of sim, so the recording always holds the whole episode
# (~16 KB/step of head RGB + depth, ~58 MB/episode, current + previous kept). Generated episodes
# almost all end sooner; spill to disk if long imports need it.
REC_MAX_STEPS = 3600
LOOPBACK = ("127.0.0.1", "localhost", "::1")
EXPORT_SLOT = threading.Semaphore(1)  # one episode encode at a time (~0.9 GB peak each at the cap)


def _outcome(task, info, terminated):
    if info["falls"]:
        why = "Robot fell"
    elif info["human_contacts"]:
        why = "Touched a person"
    elif terminated:
        why = "All goals reached"
    elif task == "follow":
        why = f"Time up, {info['in_band'] / max(1, info['steps']):.0%} of the time in the 1-2 m band"
    else:
        why = "Time up"
    return {"success": bool(info["success"]), "reason": why}


class SimThread(threading.Thread):
    def __init__(self, model_path=None):
        super().__init__(name="sim", daemon=True)
        self.q = queue.Queue(maxsize=256)       # bounded: a flooding client gets 503, the sim keeps time
        self.cv = threading.Condition()         # guards the published snapshot below
        self.state, self.jpeg, self.frame_n = {}, b"", 0
        self.ready, self.stopping = threading.Event(), False
        self.model_path, self.ppo = model_path, None
        # Everything below is touched by this thread only.
        self.task, self.seed, self.world = "pick", 0, None
        self.env = self.scenario = self.obs = self.renderer = self.vd = self.rec = self.last_rec = None
        self.driver, self.camera, self.view, self.speed = "auto", "chase", "detailed", 1.0
        self.paused = self.done = False
        self.outcome = self.error = None
        self.manual, self.manual_t, self.action = np.zeros(2), 0.0, np.zeros(2)
        self.drive_seq = -1                     # newest drive seq applied; never reset, so late drives stay dropped
        self.cam = mujoco.MjvCamera()
        self.next_step = self.next_frame = 0.0
        self.dirty = True                       # re-render even while paused

    # ------------------------------------------------------------------ HTTP-thread side
    def submit(self, op, arg=None, wait=True):
        """Queue a command; returns a Future for its result (raises queue.Full when swamped)."""
        fut = Future() if wait else None
        self.q.put_nowait((op, arg, fut))
        return fut

    def snapshot(self):
        with self.cv:
            return self.state

    def wait_frame(self, last, timeout=1.0):
        with self.cv:
            self.cv.wait_for(lambda: self.frame_n != last or self.stopping, timeout)
            return self.jpeg, self.frame_n

    def stop(self):
        self.stopping = True
        with self.cv:
            self.cv.notify_all()
        self.join(10)

    # ------------------------------------------------------------------ sim-thread side
    def run(self):
        try:
            self._start(self.task, None, seed=self.seed)
            self._publish()
        finally:
            self.ready.set()
        try:
            while not self.stopping:
                now = time.monotonic()
                running = not (self.paused or self.done)
                due = min(self.next_step if running else math.inf,
                          self.next_frame if running or self.dirty else math.inf)
                try:
                    op, arg, fut = self.q.get(timeout=min(max(0.0, due - now), 0.25))
                except queue.Empty:
                    pass
                else:
                    self._command(op, arg, fut)
                    continue
                now = time.monotonic()
                try:
                    if running and now >= self.next_step:
                        self._step()
                        # Real time x speed; when the sim can't keep up, drop the debt instead of bursting.
                        # Teleop is capped at 1x: the deadman counts wall time, so above 1x the robot
                        # would keep the last command for DEADMAN_S x speed of sim time.
                        rate = min(self.speed, 1.0) if self.driver == "manual" else self.speed
                        self.next_step = max(self.next_step + CTRL_DT / rate, now - 0.2)
                    if (running or self.dirty) and now >= self.next_frame:
                        self._render()
                        self.next_frame = now + 1 / FPS
                except Exception as e:
                    traceback.print_exc()
                    self.paused, self.error = True, f"sim paused on an error: {type(e).__name__}: {e}"
                    self._publish()
        finally:
            if self.renderer:
                self.renderer.close()
            if self.rec:
                self.rec.close()
            while not self.q.empty():
                fut = self.q.get_nowait()[2]
                if fut:
                    fut.set_exception(RuntimeError("server is shutting down"))

    def _command(self, op, arg, fut):
        res = err = None
        try:
            res = self._do(op, arg)
        except Exception as e:
            err = e
            if not isinstance(e, (ValueError, RuntimeError)):
                traceback.print_exc()
        self.dirty = True
        self._publish()                         # before the reply, so the caller reads the new state
        if fut:
            fut.set_exception(err) if err else fut.set_result(res)

    def _do(self, op, a):
        if op == "drive":
            if a["seq"] is not None:
                if a["seq"] <= self.drive_seq:
                    return                      # delivered late: a newer message (maybe a stop) already won
                self.drive_seq = a["seq"]
            self.manual, self.manual_t = a["action"], a["t"]
        elif op == "reset":
            self._start(a.get("task", self.task), self.world, seed=a.get("seed", self.seed))
        elif op == "next":
            self._start(self.task, self.world, seed=min(self.seed + 1, SEED_MAX))
        elif op == "replay":
            self._start(self.task, self.world, scenario=self.scenario)
        elif op == "scenario":                  # validated upload: its world becomes the fixed map
            self._start(a["task"], a["world"], scenario=a)
        elif op == "world":                     # Local Map world, or None = back to generated maps
            self._start(self.task, a, seed=self.seed)
        elif op in ("pause", "resume"):
            if op == "resume" and self.done:
                raise ValueError("the episode has ended: Reset, Next or Replay")
            self.paused, self.next_step = op == "pause", time.monotonic()
            if op == "resume":
                self.error = None
        elif op == "step":
            if self.done:
                raise ValueError("the episode has ended: Reset, Next or Replay")
            self.paused = True
            self._step()
            self.error = None
        elif op == "speed":
            self.speed, self.next_step = a, time.monotonic()
        elif op == "camera":
            self.camera = a
        elif op == "view":
            self.view = a
        elif op == "driver":
            if a == "policy":
                self._load_policy()
            self.driver, self.manual = a, np.zeros(2)
        elif op == "fault":
            self._fault(a)
        elif op == "export":
            return self._export(a)

    def _start(self, task, world, seed=None, scenario=None):
        """Reset onto a (task, world) and start running. The env is reused unless either changed."""
        env = self.env
        if env is None or env.task != task or env.fixed_world is not world:
            env = K1WarehouseEnv(task=task, world=world, visuals=True)
        try:
            obs, _ = env.reset(seed=seed, options={"scenario": scenario} if scenario else None)
        except Exception:
            if env is self.env:
                self.done = self.paused = True  # half-reset env: never step it, keep showing the old frame
            raise
        # Close BOTH old GL contexts before creating new ones: mujoco.Renderer.close() frees its GL
        # objects in whatever context is current, so closing one after a new renderer went current
        # wipes the new renderer's buffers (every frame black after the first reset).
        if self.renderer:
            self.renderer.close()
        if self.rec is not None:
            self.rec.close()                    # its renderer only; rows and frames stay exportable
            if self.rec.rows:
                # Exporters never read rec.env: let the old map's env (grid, distance fields) go.
                self.rec.env = self.rec._vd = None
                self.last_rec = self.rec
        # The renderer is bound to this model; keep the matching data for rendering (self.vd).
        self.renderer, self.vd = mujoco.Renderer(env.model, VIDEO_H, VIDEO_W, max_geom=env.model.ngeom + 1000), env.data
        self.head = env.model.body("k1/head_realsense_rgb_link").id
        self.rec = record.Recorder(env, frame_size=(320, 240), frames=True)
        self.rec.begin(obs)
        self.rec.faults = []                    # injected faults: the scenario + actions alone no longer replay
        self.env, self.task, self.world, self.obs = env, env.task, world, obs
        self.scenario, self.seed = env.scenario, int(env.scenario["seed"])
        self.done = self.paused = False
        self.outcome = self.error = None
        self.manual, self.action = np.zeros(2), np.zeros(2)
        self.next_step, self.dirty = time.monotonic(), True

    def _load_policy(self):
        if self.ppo is None:
            if not self.model_path:
                raise ValueError("no policy loaded: start web.py with --model runs/<run>/model.zip")
            try:
                from stable_baselines3 import PPO
                ppo = PPO.load(self.model_path, device="cpu")
            except Exception as e:
                raise RuntimeError(f"could not load the policy {self.model_path}: {e}") from None
            want = (ppo.observation_space.shape, ppo.action_space.shape)
            got = (self.env.observation_space.shape, self.env.action_space.shape)
            if want != got:
                raise ValueError(f"the policy expects obs/action shapes {want}, this sim gives {got}")
            self.ppo = ppo
        return self.ppo

    def _step(self):
        if self.driver == "manual":
            if time.monotonic() - self.manual_t > DEADMAN_S:
                self.manual = np.zeros(2)       # deadman: the operator went quiet
            a = self.manual
        elif self.driver == "policy":
            a = self._load_policy().predict(self.obs, deterministic=True)[0]
        else:
            a = heuristic(self.obs)
        self.action = np.asarray(a, np.float32)
        try:
            obs, rew, term, trunc, info = self.env.step(self.action)
        except Exception:
            self.done = self.paused = True      # half-stepped env: never step it again; Reset/Next/Replay
            raise
        full = len(self.rec.rows) >= REC_MAX_STEPS - 1      # this row fills the recording: end here
        self.rec.step(self.action, obs, rew, term, trunc or full, info)   # gym meaning: cut by a limit
        self.obs = obs
        if term or trunc or full:
            self.done = self.paused = True      # auto-pause on the outcome banner
            self.outcome = _outcome(self.env.task, info, term) if term or trunc else \
                {"success": False, "reason": f"Recording limit reached ({REC_MAX_STEPS * CTRL_DT:.0f} s of sim)"}
        self._publish()

    def _fault(self, kind):
        env = self.env
        if self.done:
            raise ValueError("the episode has ended: Reset, Next or Replay")
        if kind == "swap":
            if env.task != "follow":
                raise ValueError("Swap target ID is a follow-task fault")
            others = [k for k in range(1, len(env.workers)) if k != env.belief_k]
            if not others:
                raise ValueError("no other worker in this episode to steal the lock")
            # ponytail: first candidate, not a random one -- env.rng belongs to the episode.
            env.belief_k = others[0]
            env.stats["id_swaps"] += 1
        else:                                   # command-link stall: 0.5 s = zero tier, 1.5 s = prepare tier
            env.burst_until = max(env.burst_until, env.t + (0.5 if kind == "stall05" else 1.5))
        # The scenario + actions no longer replay this episode: record what was done, before which
        # row, so exports can say so and a replayer can reapply it (set burst_until / belief_k).
        self.rec.faults.append({"i": len(self.rec.rows), "t": float(env.t), "kind": kind,
                                "until": None if kind == "swap" else float(env.burst_until),
                                "belief_k": int(env.belief_k)})
        self.rec._pre[0].setdefault("faults", []).append(kind)   # on the row it lands before (jsonl)

    def _export(self, kind):
        """-> (bytes or a recording snapshot, content type, filename). Episode exports use the
        current recording, or the previous one right after a reset."""
        rec = self.rec if self.rec.rows or self.last_rec is None else self.last_rec
        scn = self.scenario if kind in ("scenario", "mjcf") else rec.scenario
        ctype, suffix = EXPORTS[kind]
        name = f"k1_{scn['task']}_seed{scn['seed']}{suffix}"
        if kind == "mjcf":                      # needs env.mj_spec: here
            return sim_io.export_mjcf_zip(self.env), ctype, name
        if kind == "scenario":
            with tempfile.TemporaryDirectory() as td:
                p = pathlib.Path(td) / ("export" + suffix)
                sim_io.save_scenario(scn, p)
                return p.read_bytes(), ctype, name
        # Episode exports take seconds at the cap: hand back a snapshot for encode_episode. A
        # shallow copy is enough: rows/frames are append-only and begin() swaps in new lists.
        snap = copy.copy(rec)
        snap.rows, snap.frames, snap.depths, snap.faults = rec.rows[:], rec.frames[:], rec.depths[:], rec.faults[:]
        return snap, ctype, name

    def _render(self):
        self.dirty = False
        env, cam, d = self.env, self.cam, self.vd
        x, y, yaw = (float(v) for v in d.qpos[:3])
        if self.camera == "top":                # whole map in the 4:3 frame (fovy 45 deg), +x to the right
            cam.lookat[:] = [env.L / 2, env.W / 2, 0]
            cam.azimuth, cam.elevation = 90, -89.9
            cam.distance = 1.1 * max(env.W, env.L * VIDEO_H / VIDEO_W) / 2 / math.tan(math.radians(22.5))
        elif self.camera == "chase":
            # 3 m behind the robot, tilted steeper until no wall or rack stands in between, from its
            # middle and from its feet: robots spawn by the dock wall, and a level chase camera there
            # films the back of the wall (or, just grazing over it, hides the robot's legs).
            cam.lookat[:] = [x, y, 0.6]
            cam.azimuth, cam.distance = math.degrees(yaw), 3.0
            cam.elevation = chase_elevation(env.model, d, x, y, yaw)
        else:                                   # head: from the RealSense link, along the robot's heading
            el = record.HEAD_PITCH_DEG
            fwd = np.array([math.cos(math.radians(el)) * math.cos(yaw), math.cos(math.radians(el)) * math.sin(yaw),
                            math.sin(math.radians(el))])
            cam.lookat[:] = d.xpos[self.head] + fwd
            cam.azimuth, cam.elevation, cam.distance = math.degrees(yaw), el, 1.0
        self.renderer.update_scene(d, cam, render_option(head=self.camera == "head", sensor=self.view == "sensor"))
        if self.camera == "head":               # free cameras use fovy 45 deg: widen to the recorded head_rgb FOV
            k = math.tan(math.radians(record.HFOV_DEG) / 2) * VIDEO_H / VIDEO_W / math.tan(math.radians(env.model.vis.global_.fovy) / 2)
            for c in self.renderer.scene.camera:
                c.frustum_bottom, c.frustum_top = c.frustum_bottom * k, c.frustum_top * k
        buf = io.BytesIO()
        Image.fromarray(self.renderer.render()).save(buf, "JPEG", quality=80)
        with self.cv:
            self.jpeg, self.frame_n = buf.getvalue(), self.frame_n + 1
            self.cv.notify_all()

    def _publish(self):
        env, o = self.env, self.obs
        if env is None or o is None:
            return
        f3 = lambda v: [round(float(x), 3) for x in v]
        st = {"t": round(env.t, 2), "t_max": round(env.t_max, 1), "task": self.task, "seed": self.seed,
              "map": self.world["name"] if self.world else None, "driver": self.driver, "camera": self.camera,
              "view": self.view, "speed": self.speed, "paused": self.paused, "done": self.done, "outcome": self.outcome,
              "error": self.error, "policy": bool(self.model_path), "workers": len(env.workers),
              "stats": {k: int(v) for k, v in env.stats.items()}, "pose": f3(env._pose()),
              "flags": {"stale": bool(o[I_STALE]), "standing": bool(o[I_STAND]), "seen": bool(o[I_SEEN])},
              "cmd": f3(env.cmd), "action": f3(self.action),
              "deadman": self.driver == "manual" and time.monotonic() - self.manual_t > DEADMAN_S,
              "scan": {"low": f3(o[I_SCAN:I_SCAN + N_RAYS] * RAY_MAX), "high": f3(o[I_SCAN + N_RAYS:] * RAY_MAX)},
              "fov_deg": round(math.degrees(RAY_FOV)), "ray_max": RAY_MAX, "rec_steps": len(self.rec.rows)}
        with self.cv:
            self.state = st


# ---------------------------------------------------------------------- HTTP
def _num(body, k):
    v = body.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        raise ValueError(f"drive: {k} must be a finite number")
    return float(min(max(v, -1), 1))


def parse_cmd(body, data_root):
    """Validate one /api/cmd body -> (sim op, arg). Raises ValueError with a readable message."""
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    op = body.get("op")

    def pick(choices):
        v = body.get("value")
        if isinstance(v, bool) or v not in choices:
            raise ValueError(f"{op}: value must be one of {list(choices)}")
        return v
    if op in ("next", "replay", "pause", "resume", "step"):
        return op, None
    if op == "reset":
        a = {}
        if body.get("task") is not None:
            if body["task"] not in TASKS:
                raise ValueError(f"reset: task must be one of {list(TASKS)}")
            a["task"] = body["task"]
        if body.get("seed") is not None:
            s = body["seed"]
            if isinstance(s, bool) or not isinstance(s, int):
                raise ValueError("reset: seed must be an integer")
            a["seed"] = min(max(s, 0), SEED_MAX)
        return op, a
    if op == "drive":
        # seq: the page's increasing counter. Each drive is its own connection, so a forward sent
        # before a release can arrive after it; the sim drops it. Optional: without it, last arrival wins.
        s = body.get("seq")
        if s is not None and (isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 2 ** 53):
            raise ValueError("drive: seq must be an integer in [0, 2**53]")
        return op, {"action": np.array([_num(body, "vx"), _num(body, "vyaw")]), "t": time.monotonic(), "seq": s}
    if op == "speed":
        return op, float(pick(SPEEDS))
    if op in ("driver", "camera", "view", "fault"):
        return op, pick({"driver": DRIVERS, "camera": CAMERAS, "view": VIEWS, "fault": FAULTS}[op])
    if op == "import_localmap":
        return "world", sim_io.localmap_to_world(body.get("domain"), data_root)   # it validates the id
    if op == "random_maps":
        return "world", None
    raise ValueError(f"unknown op {op!r}")


class Handler(BaseHTTPRequestHandler):
    # ponytail: HTTP/1.0, one connection per request (~15/s while driving, all loopback). Switch to
    # HTTP/1.1 keep-alive if TIME_WAIT sockets ever matter; errors must then close the connection.
    server_version = "K1Sim/1"
    timeout = 30                                # a stalled body read or a dead /stream viewer frees its thread

    def log_request(self, code="-", size="-"):
        if str(code)[:1] in "45":               # quiet: drive messages and state polls are 15 req/s
            super().log_request(code, size)

    def _send(self, code, body, ctype="application/json", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        mv = memoryview(body)
        for i in range(0, len(mv), 1 << 20):    # 1 MB writes: the 30 s timeout is per write, so a big export over slow Wi-Fi still finishes
            self.wfile.write(mv[i:i + (1 << 20)])

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _ask(self, op, arg=None):
        return self.server.sim.submit(op, arg).result(timeout=300)

    def _guard(self, fn):
        """Run one request; every failure becomes a short JSON error, never a stack trace."""
        try:
            # DNS-rebinding guard: a page from another site may not drive or read the sim, on any bind.
            h = (self.headers.get("Host") or "localhost").lower()
            host = h[1:h.find("]")] if h.startswith("[") else h.rsplit(":", 1)[0]
            if host not in self.server.allowed_hosts:
                return self._json(403, {"error": f"host {host!r} not allowed"})
            # Cross-site GETs (an <img src> on any open page) could otherwise loop exports or streams.
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return self._json(403, {"error": "cross-site request refused"})
            fn()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass                                # client went away
        except queue.Full:
            self._json(503, {"error": "the sim is busy, try again"})
        except TimeoutError:
            self._json(504, {"error": "the sim did not answer in time"})
        except (ValueError, RuntimeError) as e:  # validation, importer and optional-dependency errors
            self._json(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"internal error: {type(e).__name__}: {e}"})

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def _get(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, (HERE / "web.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/state":
            self._json(200, self.server.sim.snapshot())
        elif path == "/stream":
            self._stream()
        elif path == "/api/domains":
            self._json(200, sim_io.list_localmap_domains(self.server.data_root))
        elif path.startswith("/api/export/") and path[12:] in EXPORTS:
            data, ctype, name = self._ask("export", path[12:])
            if not isinstance(data, bytes):     # episode snapshot: encode here, off the sim thread
                data = encode_episode(path[12:], data)
            self._send(200, data, ctype, [("Content-Disposition", f'attachment; filename="{name}"')])
        else:
            self._json(404, {"error": "not found"})

    def _post(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0:
            raise ValueError("bad Content-Length")
        if n > MAX_BODY:
            self.close_connection = True        # the unread body makes the connection unusable
            return self._json(413, {"error": f"request body over {MAX_BODY >> 20} MB"})
        # JSON-only: a cross-site form post cannot set this type without a CORS preflight we never answer.
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        body = json.loads(self.rfile.read(n) or b"null")
        path = self.path.split("?", 1)[0]
        if path == "/api/cmd":
            op, arg = parse_cmd(body, self.server.data_root)
            if op == "drive":                   # fire-and-forget at 10 Hz; the deadman covers loss
                self.server.sim.submit(op, arg, wait=False)
                return self._send(204, b"")
            self._ask(op, arg)
        elif path == "/api/import/scenario":
            self._ask("scenario", sim_io.validate_scenario(body))
        else:
            return self._json(404, {"error": "not found"})
        self._json(200, {"ok": True, "state": self.server.sim.snapshot()})

    def _stream(self):
        """MJPEG: one part per rendered frame. A slow client only ever delays its own thread."""
        sim = self.server.sim
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = 0                                # frame 0 = nothing rendered yet
        while not sim.stopping:
            # A paused sim re-sends its frame once a second: a keepalive, and a closed viewer errors out.
            jpg, last = sim.wait_frame(last)
            if jpg:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(jpg), jpg))


def encode_episode(kind, rec):
    """jsonl / rrd / lerobot bytes of a recording snapshot. Runs on the HTTP thread, one at a time."""
    with EXPORT_SLOT:
        if kind == "lerobot":
            return record.export_lerobot_zip(rec)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            p = pathlib.Path(td) / ("export" + EXPORTS[kind][1])
            (record.export_jsonl if kind == "jsonl" else record.export_rrd)(rec, p)
            return p.read_bytes()


class Server(ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets a second server bind a port in use and never get its requests:
    # off there, so a second web.py fails with "address in use" instead of silently idling.
    allow_reuse_address = os.name != "nt"


class Server6(Server):
    address_family = socket.AF_INET6


def chase_elevation(m, d, x, y, yaw, dist=3.0):
    """Chase camera tilt: the shallowest of -20..-80 deg with no wall or rack between camera and robot,
    checked from the robot's middle (0.6 m) and from just above its knees (PLAN_MIN_H). Not from the
    floor: a ray from there starts inside or under a 0.14 m pallet and would always tilt to top-down."""
    for el in (-20, -35, -50, -65, -80):
        e = math.radians(el)
        back = np.array([-math.cos(e) * math.cos(yaw), -math.cos(e) * math.sin(yaw), -math.sin(e)])
        if not any(0 <= mujoco.mj_ray(m, d, np.array([x, y, z]), back, RACK_GROUPS, 1, -1, np.zeros(1, np.int32)) < dist
                   for z in (0.6, PLAN_MIN_H)):
            return el
    return el


def make_server(host="127.0.0.1", port=8765, model=None, data_root=None):
    """Bind the HTTP server and start the sim (first episode ready on return); caller runs serve_forever."""
    bind = host.strip("[]")
    srv = (Server6 if ":" in bind else Server)((bind, port), Handler)
    # Host names this server answers to: loopback, the bound address, and for a wildcard bind this
    # PC's name and addresses (a LAN browser uses those).
    srv.allowed_hosts = {*LOOPBACK, bind.lower()}
    if bind in ("", "0.0.0.0", "::"):
        name = socket.gethostname()
        srv.allowed_hosts.add(name.lower())
        try:
            srv.allowed_hosts |= {ai[4][0].lower() for ai in socket.getaddrinfo(name, None)}
        except OSError:
            pass
    srv.sim, srv.data_root = SimThread(model), data_root or sim_io.DEFAULT_DATA_ROOT
    srv.sim.start()
    srv.sim.ready.wait(120)
    if not srv.sim.snapshot():              # first episode never built (e.g. no OpenGL): fail now, not 504s later
        srv.server_close()
        raise RuntimeError("the sim did not start; see the traceback above")
    return srv


def selftest():
    import http.client, itertools, urllib.error, urllib.request, zipfile
    global REC_MAX_STEPS

    # Chase camera: the robot over a pallet's edge keeps the shallow view (a floor-level ray started
    # inside the pallet and slammed it top-down); a rack behind the robot does tilt it.
    from k1_warehouse import _static
    def tilt(kind, x, half, z):
        w = {"name": "t", "size": [12, 6], "stations": [], "spawn": [8, 2, 9, 4], "forklift_lane": None,
             "statics": [_static(kind, [x, 3, z], half)]}
        env = K1WarehouseEnv(world=w, n_workers=(0, 0), n_boxes=(0, 0))
        env.reset(seed=0)
        env.data.qpos[:3] = [6.0, 3.0, 0.0]              # facing +x
        mujoco.mj_forward(env.model, env.data)
        return chase_elevation(env.model, env.data, 6.0, 3.0, 0.0)
    assert tilt("pallet", 5.85, [0.5, 0.25, 0.07], 0.07) == -20, "a pallet under the robot must not tilt the chase camera"
    assert tilt("rack", 4.5, [0.3, 1.0, 1.0], 1.0) < -20, "a rack 1.5 m behind the robot must tilt the chase camera"

    with tempfile.TemporaryDirectory() as td:
        srv = make_server("127.0.0.1", 0, data_root=pathlib.Path(td))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"

        def req(path, body=None):
            data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
            r = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r, timeout=300) as f:
                    return f.status, f.headers, f.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read()

        def cmd(op, expect=200, **kw):
            code, _, b = req("/api/cmd", dict(op=op, **kw))
            assert code == expect, (op, kw, code, b)
            return json.loads(b) if b else None

        def state():
            return json.loads(req("/state")[2])

        next_seq = itertools.count(1).__next__

        def drive_for(vx, vyaw, sim_s):
            t_end = state()["t"] + sim_s
            while state()["t"] < t_end:
                cmd("drive", 204, vx=vx, vyaw=vyaw, seq=next_seq())
                time.sleep(0.08)

        def raw_get(path, headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", path, headers=headers)
            code = c.getresponse().status
            c.close()
            return code

        code, h, b = req("/")
        assert code == 200 and h.get_content_type() == "text/html" and b"<html" in b, (code, h)
        assert state()["task"] == "pick" and not state()["done"]
        # Rebinding + cross-site guards; a second server can't co-bind the port (Windows SO_REUSEADDR).
        assert raw_get("/state", {"Host": f"evil.example:{port}"}) == 403
        assert raw_get("/state", {"Host": f"[::1]:{port}"}) == 200
        assert raw_get("/api/export/scenario", {"Sec-Fetch-Site": "cross-site"}) == 403
        try:
            Server(("127.0.0.1", port), Handler).server_close()
            raise AssertionError("a second server bound the busy port")
        except OSError:
            pass

        st = cmd("reset", task="follow", seed=5)["state"]
        assert st["task"] == "follow" and st["seed"] == 5 and st["t"] <= 0.2, st
        cmd("speed", value=2)
        cmd("speed", 400, value=3)
        # Manual: hold forward ~1.5 s of sim time, go quiet -> the server deadman zeroes the action.
        # Teleop runs at <= 1x whatever the speed, so the wall-clock deadman is <= 300 ms of sim.
        p0 = cmd("driver", value="manual")["state"]["pose"]
        w0, t0 = time.monotonic(), state()["t"]
        drive_for(1.0, 0.0, 1.5)
        st = state()
        assert st["action"] == [1.0, 0.0] and not st["deadman"], st
        assert st["t"] - t0 <= time.monotonic() - w0 + 0.35, ("teleop ran faster than real time", st["t"] - t0)
        time.sleep(DEADMAN_S + 0.4)
        st = state()
        assert st["deadman"] and st["action"] == [0.0, 0.0], st
        assert math.dist(p0[:2], st["pose"][:2]) > 0.02 and not st["done"], (p0, st)
        # A forward delivered after a newer stop (older seq) is dropped.
        s_old, s_new = next_seq(), next_seq()
        cmd("drive", 204, vx=0, vyaw=0, seq=s_new)
        cmd("drive", 204, vx=1, vyaw=0, seq=s_old)
        time.sleep(0.2)                                  # >= 1 step, < the deadman
        assert state()["action"] == [0.0, 0.0], state()

        # Pause + step = exactly one 0.1 s control tick.
        s1 = cmd("pause")["state"]
        time.sleep(0.3)
        assert state()["t"] == s1["t"], "sim advanced while paused"
        s2 = cmd("step")["state"]
        assert s2["paused"] and s2["stats"]["steps"] == s1["stats"]["steps"] + 1, (s1, s2)
        assert abs(s2["t"] - s1["t"] - CTRL_DT) < 1e-6, (s1["t"], s2["t"])

        # One MJPEG part is a whole JPEG, and every camera draws a real picture after a reset (not
        # the black frame a mis-ordered GL context close produces), in both views; the sensor view
        # is a different picture (proxies instead of the visual layer).
        def frame():
            with urllib.request.urlopen(base + "/stream", timeout=30) as f:
                assert f.headers.get_content_type() == "multipart/x-mixed-replace"
                assert f.readline().strip() == b"--frame"
                hdr = {}
                while line := f.readline().strip():
                    k, v = line.split(b":", 1)
                    hdr[k.strip().lower()] = v.strip()
                jpg = f.read(int(hdr[b"content-length"]))
            assert hdr[b"content-type"] == b"image/jpeg" and jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"
            return np.asarray(Image.open(io.BytesIO(jpg)))
        for c in CAMERAS:
            cmd("camera", value=c)
            px = []
            for v in VIEWS:
                assert cmd("view", value=v)["state"]["view"] == v
                time.sleep(0.2)
                px.append(frame())
                assert px[-1].shape == (VIDEO_H, VIDEO_W, 3) and px[-1].std() > 10, (c, v, px[-1].std())
            assert np.abs(px[0].astype(int) - px[1]).mean() > 3, (c, "sensor view draws the detailed picture")
        cmd("view", 400, value="xray")
        cmd("view", value="detailed")
        assert record.decode_rgb(srv.sim.rec.frames[-1]).std() > 10, "Recorder frames went black"
        assert state()["error"] is None, state()["error"]
        # Paused, so the head scene is settled: it covers what the recorded head_rgb covers.
        gc = srv.sim.renderer.scene.camera[0]
        hfov = 2 * math.degrees(math.atan(gc.frustum_top / gc.frustum_near * VIDEO_W / VIDEO_H))
        assert abs(hfov - record.HFOV_DEG) < 0.5, (hfov, record.HFOV_DEG)
        # Chase camera 1 m from the dock wall: facing down the aisle it tilts up over the wall,
        # facing the wall it stays level. (Paused: the sim thread is idle; nothing steps this pose.)
        cmd("camera", value="chase")
        for yaw, steep in ((0.0, True), (math.pi, False)):
            srv.sim.env.data.qpos[:3] = [1.0, srv.sim.env.aisles_y[0], yaw]
            cmd("view", value="detailed")                # any command re-renders
            time.sleep(0.2)
            assert (srv.sim.cam.elevation < -20) == steep, (yaw, srv.sim.cam.elevation)

        # Exports: scenario round-trips through the validator; episode + scene bundles download.
        code, h, b = req("/api/export/scenario")
        assert code == 200 and "attachment" in h["Content-Disposition"], (code, b[:200])
        scn = sim_io.validate_scenario(json.loads(b))
        assert scn["task"] == "follow" and scn["seed"] == 5
        code, h, b = req("/api/export/jsonl")
        assert code == 200 and b and all(json.loads(ln) for ln in b.splitlines() if ln.strip()), (code, b[:200])
        code, h, b = req("/api/export/mjcf")
        assert code == 200, (code, b[:300])
        assert any(n.endswith("scene.xml") for n in zipfile.ZipFile(io.BytesIO(b)).namelist())
        assert req("/api/export/nope")[0] == 404

        # A step that throws inside env.step leaves a half-stepped env: the episode ends there.
        def boom(a):
            raise ValueError("injected step failure")
        srv.sim.env.step = boom                          # paused: the sim thread is idle
        cmd("step", 400)
        del srv.sim.env.step
        assert state()["done"] and cmd("resume", 400)

        # Import the exported scenario back: same task/seed, its world is now the fixed map.
        cmd("reset", task="pick", seed=9)
        code, _, b = req("/api/import/scenario", json.dumps(scn).encode())
        st = json.loads(b)["state"] if code == 200 else b
        assert code == 200 and st["task"] == "follow" and st["seed"] == 5 and st["map"] == scn["world"]["name"], st
        code, _, b = req("/api/import/scenario", {"format": "nope"})
        assert code == 400 and json.loads(b)["error"], (code, b)

        # Validation: unknown op, non-finite numbers, oversized body.
        code, _, b = req("/api/cmd", {"op": "selfdestruct"})
        assert code == 400 and "unknown op" in json.loads(b)["error"], (code, b)
        assert req("/api/cmd", b'{"op": "drive", "vx": NaN, "vyaw": 0}')[0] == 400
        assert req("/api/cmd", {"op": "drive", "vx": 0, "vyaw": 0, "seq": "7"})[0] == 400
        assert req("/api/cmd", {"op": "reset", "seed": "5"})[0] == 400
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.putrequest("POST", "/api/import/scenario")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(MAX_BODY + 1))
        c.endheaders()
        assert c.getresponse().status == 413
        c.close()

        # Faults: a 1.5 s link stall while turning trips the watchdog's prepare tier.
        cmd("driver", value="manual")
        prep0 = state()["stats"]["prep_events"]
        drive_for(0.0, 1.0, 0.6)
        cmd("fault", value="stall15")
        drive_for(0.0, 1.0, 2.5)
        st = state()
        assert st["stats"]["prep_events"] > prep0 and not st["done"], st
        # The fault is in the recording, so the export says the episode was tampered with.
        rows = [json.loads(ln) for ln in req("/api/export/jsonl")[2].splitlines()[1:]]
        assert [r["faults"] for r in rows if "faults" in r] == [["stall15"]], [r.get("faults") for r in rows]
        assert [f["kind"] for f in srv.sim.rec.faults] == ["stall15"], srv.sim.rec.faults
        # Swap target ID: needs a distractor; a pick episode refuses it.
        env = K1WarehouseEnv(task="follow")
        seed = next(s for s in range(200) if len(env.make_scenario(s)["episode"]["workers"]) > 1)
        cmd("reset", task="follow", seed=seed)
        st = cmd("pause")["state"]                       # paused: no natural ReID swaps in between
        assert cmd("fault", value="swap")["state"]["stats"]["id_swaps"] == st["stats"]["id_swaps"] + 1
        cmd("reset", task="pick", seed=1)
        cmd("fault", 400, value="swap")

        # Recording cap: the episode ends on a truncated row, with the reason on the banner. The
        # finished recording moved to last_rec without its env (the old map's grid can be freed).
        cap, REC_MAX_STEPS = REC_MAX_STEPS, 12
        try:
            cmd("driver", value="auto")
            cmd("speed", value=8)
            cmd("reset", task="pick", seed=1)
            t_end = time.monotonic() + 60
            while not state()["done"] and time.monotonic() < t_end:
                time.sleep(0.05)
        finally:
            REC_MAX_STEPS = cap
        st = state()
        assert st["done"] and st["rec_steps"] == 12 and "Recording limit" in st["outcome"]["reason"], st
        last = json.loads(req("/api/export/jsonl")[2].splitlines()[-1])
        assert last["truncated"] and not last["terminated"], last
        assert srv.sim.last_rec.env is None

        # A policy trained on another env is refused at load time; the driver stays as it was.
        from stable_baselines3 import PPO
        PPO("MlpPolicy", "Pendulum-v1", n_steps=64, device="cpu").save(pathlib.Path(td) / "pendulum.zip")
        srv.sim.model_path = str(pathlib.Path(td) / "pendulum.zip")
        code, _, b = req("/api/cmd", {"op": "driver", "value": "policy"})
        assert code == 400 and "shapes" in json.loads(b)["error"] and state()["driver"] == "auto", (code, b)
        srv.sim.model_path = None

        # Local Map: a domain written into the temp data root lists, imports and runs.
        sim_io.world_to_localmap(scn["world"], "selftest_map", data_root=pathlib.Path(td))
        doms = json.loads(req("/api/domains")[2])
        assert any(d["id"] == "selftest_map" for d in doms), doms
        st = cmd("import_localmap", domain="selftest_map")["state"]
        assert st["map"] and st["task"] == "pick" and not st["done"], st
        cmd("import_localmap", 400, domain="no_such_domain")
        assert cmd("random_maps")["state"]["map"] is None

        srv.shutdown()
        srv.server_close()
        srv.sim.stop()
        assert not srv.sim.is_alive(), "sim thread did not stop"
    print("WEB-SELFTEST-OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["selftest"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", help="PPO .zip from train.py (enables the Policy driver)")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    srv = make_server(a.host, a.port, a.model)
    host = f"[{a.host.strip('[]')}]" if ":" in a.host else a.host
    print(f"K1 sim UI on http://{host}:{srv.server_address[1]}/  (Ctrl+C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        srv.sim.stop()


if __name__ == "__main__":
    main()
