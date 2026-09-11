"""Bridge -- the process wrapper around the compiled loco_follow_bridge (P3.1, --drive only).

Extracted from follow_person_k1.py (P3.1 pure move), then FIXED 2026-07-08 after the first
armed-heartbeat drive on the robot exposed two latent protocol bugs (see command_expect_ok /
_drain_stdout). Zero coupling: only stdlib (subprocess/threading/queue/os/time), no follow
globals/helpers/sibling classes. The C++ bridge it wraps is the safety floor; this Python side
only frames commands to it and reaps it cleanly.
"""
import os
import queue
import subprocess
import threading
import time

from common import log


class Bridge:
    def __init__(self, path, extra_env=None):
        self.path = path
        self.proc = None
        self._lock = threading.Lock()
        self.extra_env = extra_env or None
        self._rx = queue.Queue(maxsize=1000)   # bridge stdout lines (drained continuously)
        self._safety_last = (0.0, "", 0)       # (t, category, count) -- throttles BRIDGE-SAFETY repeats

    def start(self):
        env = None
        if self.extra_env:
            env = dict(os.environ); env.update(self.extra_env)
        self.proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            env=env,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()

    # The C++ safety floor reports every intervention on this pipe -- WATCHDOG staleness zeroes,
    # HB deadman trips, kPrepare demotions. Historically all of it was drained into _rx and
    # discarded by the reply matcher, so the floor acted INVISIBLY: a session could be velocity-
    # zeroed or demoted repeatedly with no trace in k1_follow.err, and post-incident the honest
    # answer to "did the watchdog fire?" was "unobservable" rather than yes or no.
    # An intervention is a first-class safety event, so it gets logged. Repeats of the same
    # category are throttled to 1/s (a stale condition re-emits every tick) with a count, so a
    # sustained trip is one legible line per second rather than a flood.
    _SAFETY = ("WATCHDOG", "HB-STALE", "ERR ")

    def _note_safety(self, s):
        cat = s.split(None, 2)[0] if s else ""
        now = time.monotonic()
        last_t, last_cat, n = self._safety_last
        if cat == last_cat and (now - last_t) < 1.0:
            self._safety_last = (last_t, cat, n + 1)
            return
        if n > 1 and last_cat:
            log("BRIDGE-SAFETY %s x%d suppressed in the last %.1fs" % (last_cat, n - 1, now - last_t))
        self._safety_last = (now, cat, 1)
        log("BRIDGE-SAFETY %s" % s)

    def _drain_stdout(self):
        """Continuously read bridge stdout into _rx. TWO jobs: (a) feed command_expect_ok's
        reply matcher; (b) keep the pipe empty -- the bridge echoes 'OK v ...' for EVERY 10Hz
        velocity tick, and with no reader the 64KB pipe fills in ~3.5 min, blocking the C++
        emit() and freezing the follow (the C++ watchdog then stands the robot: fail-safe,
        but the run dies). Drop-oldest on overflow: newest lines are the ones a live
        command_expect_ok could be waiting for."""
        try:
            for line in self.proc.stdout:
                _s = line.strip()
                if _s.startswith(self._SAFETY):
                    try:
                        self._note_safety(_s)
                    except Exception:  # noqa: BLE001 -- logging must never break the drain
                        pass
                try:
                    self._rx.put_nowait(line)
                except queue.Full:
                    try:
                        self._rx.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._rx.put_nowait(line)
                    except queue.Full:
                        pass
        except Exception:  # noqa: BLE001  (pipe closed at shutdown)
            pass

    def _send(self, cmd):
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return None
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                return None
            return cmd

    def command_expect_ok(self, cmd, timeout=4.0):
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return False, "<bridge-dead>"
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as e:
                return False, "<write-failed:%s>" % e
        # Scan for THIS command's reply line ('OK <cmd> ... 0' / 'ERR <cmd> ...'), SKIPPING
        # unsolicited lines: the startup handshake 'OK ready 0', the armed-deadman banner
        # 'OK hb-required 1 file=... stale=... prep=...', HB-STALE notices, and 'OK v' echoes.
        # The old reader took the FIRST line and required last-token '0', so with the hb banner
        # present every reply was off-by-one ('ping' consumed the handshake, 'prep' consumed the
        # banner -> 'prep=1500' != '0' -> DRIVE-ABORT, first armed drive 2026-07-08). In the
        # tethered path the misalignment was invisible (every line starts OK and ends 0).
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "<no-reply/timeout>"
            try:
                line = self._rx.get(timeout=remaining)
            except queue.Empty:
                return False, "<no-reply/timeout>"
            line = (line or "").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("OK", "ERR") and parts[1] == cmd:
                ok = len(parts) >= 3 and parts[0] == "OK" and parts[-1] == "0"
                return ok, line

    def send_velocity(self, vx, vy, vyaw):
        self._send("v %.4f %.4f %.4f" % (vx, vy, vyaw))

    def stop(self):
        self._send("stop")

    def prep(self):
        self._send("prep")      # ChangeMode(kPrepare) -- stable stand

    def walk(self):
        self._send("walk")      # ChangeMode(kWalking)

    def send_head(self, pitch, yaw):
        """Point the head (radians, absolute). Fire-and-forget like velocity -- it is streamed
        at tracking rate. The bridge clamps yaw to +/-0.60 rad and pitch to +/-0.35 rad as the
        last line of defence, and re-centres the head on shutdown."""
        self._send("head %.4f %.4f" % (pitch, yaw))

    def quit(self):
        self._send("quit")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def shutdown(self, join_timeout=3.0):
        try:
            if self.alive():
                self.stop()
                self.quit()
                t0 = time.time()
                while self.alive() and (time.time() - t0) < join_timeout:
                    time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.alive():
                self.proc.terminate()
                t0 = time.time()
                while self.alive() and (time.time() - t0) < 1.0:
                    time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.alive():
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
