"""Train a K1 warehouse policy with PPO, then score it against the scripted baseline.

    python train.py --task pick --steps 2000000          # train + eval, writes runs/<task>_<time>/
    python train.py --task pick --eval runs/x/model.zip  # eval only
    python k1_warehouse.py view --model runs/x/model.zip # watch it
    python train.py --task follow --world scn.json       # train on one fixed map (e.g. an imported
    python train.py --task follow --localmap exact-lab   #   Local Map site), episodes still randomized

Eval uses the SAME fixed seeds for policy and baseline, so the numbers compare 1:1.
Output action is [vx, vyaw] in [-1,1] -> the bridge clamps (see action_to_cmd), which is the
TRAIN_CONTRACT action shape; a policy that beats the baseline here is a candidate for
shadow-mode on the robot, not for driving it.
"""
import argparse, json, pathlib, time

# numpy (via k1_warehouse) BEFORE torch: on an anaconda base, torch-first loads a second
# libiomp5md.dll and aborts with "OMP: Error #15".
from k1_warehouse import K1WarehouseEnv, evaluate, heuristic

from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.vec_env import SubprocVecEnv

HERE = pathlib.Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="pick", choices=["pick", "follow"])
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
    env_kwargs = dict(task=a.task, world=world)

    if a.eval:
        model, out = PPO.load(a.eval, device="cpu"), pathlib.Path(a.eval).parent
    else:
        out = HERE / "runs" / f"{a.task}_{time.strftime('%Y%m%d_%H%M%S')}"
        venv = make_vec_env(K1WarehouseEnv, n_envs=a.envs, seed=0, vec_env_cls=SubprocVecEnv,
                            env_kwargs=env_kwargs)
        # ponytail: SB3 defaults + a longer horizon; tune once there is a learning curve to tune against.
        model = PPO("MlpPolicy", venv, n_steps=1024, batch_size=512, gamma=0.995,
                    device="cpu", verbose=1)
        model.learn(a.steps)
        model.save(out / "model")
        venv.close()

    env = K1WarehouseEnv(**env_kwargs)
    report = dict(task=a.task, world=(world or {}).get("name", "generated (new map per episode)"),
                  policy=evaluate(env, lambda o: model.predict(o, deterministic=True)[0], a.episodes),
                  baseline=evaluate(env, heuristic, a.episodes))
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
