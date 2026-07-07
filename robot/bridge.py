"""Bridge -- the process wrapper around the compiled loco_follow_bridge (P3.1, --drive only).

Extracted verbatim from follow_person_k1.py (pure move, no logic change). Zero coupling: only
stdlib (subprocess/threading/os/time), no follow globals/helpers/sibling classes. The C++ bridge
it wraps is the safety floor; this Python side only frames commands to it and reaps it cleanly.
"""
import os
import subprocess
import threading
import time


class Bridge:
    def __init__(self, path, extra_env=None):
        self.path = path
        self.proc = None
        self._lock = threading.Lock()
        self.extra_env = extra_env or None

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
        reply_box = {}

        def _read():
            try:
                reply_box["line"] = self.proc.stdout.readline()
            except Exception as e:  # noqa: BLE001
                reply_box["err"] = str(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if "line" not in reply_box:
            return False, "<no-reply/timeout>"
        line = (reply_box["line"] or "").strip()
        if not line:
            return False, "<eof>"
        parts = line.split()
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
