"""Train a K1 warehouse policy with PPO, then score it against the scripted baseline.

    python train.py --task pick --steps 2000000          # train + eval, writes runs/<task>_<time>_<pid>/
    python train.py --eval runs/x/model.zip              # eval only: task from the run's eval.json,
                                                         #   writes eval_<time>_<pid>.json beside it
    python k1_warehouse.py view --model runs/x/model.zip # watch it
    python train.py --task follow --world scn.json       # train on one fixed map (e.g. an imported
    python train.py --task follow --localmap exact-lab   #   Local Map site), episodes still randomized

Eval uses the SAME fixed seeds for policy and baseline, so the numbers compare 1:1.
Output action is [vx, vyaw] in [-1,1] -> the bridge clamps (see action_to_cmd), which is the
TRAIN_CONTRACT action shape; a policy that beats the baseline here is a candidate for
shadow-mode on the robot, not for driving it.

    python train.py selftest                             -> TRAIN-SELFTEST-OK (no training)
"""
import argparse, json, os, pathlib, sys, time

# numpy (via k1_warehouse) BEFORE torch: on an anaconda base, torch-first loads a second
# libiomp5md.dll and aborts with "OMP: Error #15".
from k1_warehouse import K1WarehouseEnv, evaluate, heuristic

from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.vec_env import SubprocVecEnv

HERE = pathlib.Path(__file__).resolve().parent
GENERATED = "generated (new map per episode)"


def _run_dir(task, root=HERE / "runs"):
    """A fresh run dir. The pid keeps concurrent same-second launches apart; exist_ok=False makes
    any remaining collision fail loudly instead of two runs overwriting one model.zip/eval.json."""
    out = root / f"{task}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    out.mkdir(parents=True, exist_ok=False)
    return out


def _eval_target(out, task, world_name):
    """Eval-only: (task, report path). Task defaults to the one in the run's training report;
    a task/world that differs from it is refused. The training eval.json is never overwritten."""
    prev = json.loads((out / "eval.json").read_text()) if (out / "eval.json").exists() else {}
    task = task or prev.get("task", "pick")
    for k, v in (("task", task), ("world", world_name)):
        if k in prev and prev[k] != v:
            raise ValueError(f"{out} was trained with {k}={prev[k]!r}, refusing to evaluate with {v!r}")
    return task, out / f"eval_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["pick", "follow"],
                    help="default: pick for training; the run's own task for --eval")
    ap.add_argument("--steps", type=int, default=2_000_000)
    ap.add_argument("--envs", type=int, default=8)
    ap.add_argument("--episodes", type=int, default=50, help="eval episodes")
    ap.add_argument("--eval", help="skip training; evaluate this model .zip")
    ap.add_argument("--world", help="scenario .json whose world (map) every episode uses")
    ap.add_argument("--localmap", help="Local Map domain id to import as the fixed world")
    a = ap.parse_args()

    world = None
    if a.world or a.localmap:
        import sim_io
        world = sim_io.load_scenario(a.world)["world"] if a.world else sim_io.localmap_to_world(a.localmap)
    world_name = (world or {}).get("name", GENERATED)

    if a.eval:
        out = pathlib.Path(a.eval).parent
        try:
            a.task, report_path = _eval_target(out, a.task, world_name)
        except ValueError as e:
            ap.error(str(e))
    else:
        a.task = a.task or "pick"
        out = _run_dir(a.task)
        report_path = out / "eval.json"
    env_kwargs = dict(task=a.task, world=world)

    if a.eval:
        model = PPO.load(a.eval, device="cpu")
    else:
        venv = make_vec_env(K1WarehouseEnv, n_envs=a.envs, seed=0, vec_env_cls=SubprocVecEnv,
                            env_kwargs=env_kwargs)
        # ponytail: SB3 defaults + a longer horizon; tune once there is a learning curve to tune against.
        model = PPO("MlpPolicy", venv, n_steps=1024, batch_size=512, gamma=0.995,
                    device="cpu", verbose=1)
        model.learn(a.steps)
        model.save(out / "model")
        venv.close()

    env = K1WarehouseEnv(**env_kwargs)
    report = dict(task=a.task, world=world_name,
                  policy=evaluate(env, lambda o: model.predict(o, deterministic=True)[0], a.episodes),
                  baseline=evaluate(env, heuristic, a.episodes))
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        # concurrent launches: the pid is in the name, and a same-name collision raises (never reuses).
        strftime, time.strftime = time.strftime, lambda *_: "T"
        try:
            run = _run_dir("pick", d / "runs")
            assert run.name == f"pick_T_{os.getpid()}" and run.is_dir(), run
            try:
                _run_dir("pick", d / "runs")
                raise AssertionError("run dir reused")
            except FileExistsError:
                pass
        finally:
            time.strftime = strftime
        # --eval: inherits the run's task, refuses a task/world mismatch, never targets eval.json.
        (run / "eval.json").write_text(json.dumps(dict(task="follow", world=GENERATED)))
        task, path = _eval_target(run, None, GENERATED)
        assert (task == "follow" and path.parent == run and path.name.startswith("eval_")
                and path.name != "eval.json"), (task, path)
        for args in (("pick", GENERATED), (None, "site-a")):
            try:
                _eval_target(run, *args)
                raise AssertionError(f"mismatch accepted: {args}")
            except ValueError:
                pass
        assert _eval_target(d, None, GENERATED)[0] == "pick"   # no training report: old default
    print("TRAIN-SELFTEST-OK")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["selftest"] else main()
