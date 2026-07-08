# Persist `jetson_clocks` across reboot (K1 follow perf)

`jetson_clocks` pins the Orin GPU/CPU/EMC clocks to the current nvpmodel's **max**. On this robot it
buys the follow **~20–28% lower loop p99** (see `docs/LOOP_BASELINE.md`, Capture A unpinned vs B
pinned) and eliminates most of the pose-latency tail — but it **does not survive a reboot**, so every
power-cycle silently regresses the loop until someone re-runs `sudo jetson_clocks`. This session was
bitten by exactly that (Capture A was an accidental unpinned run right after a reboot).

`k1-jetson-clocks.service` re-pins automatically at every boot.

## Tradeoff — this is a power/thermal decision (your call)

Pinning holds clocks at MAX whenever the robot is **on**, not just during a follow → higher idle power
draw, more heat, louder fan. That's fine for a **demo/dev robot powered off between sessions**;
reconsider it for an always-on deployment. A more power-conservative alternative (not provided here,
more moving parts): a NOPASSWD sudoers entry for `jetson_clocks` so the follow launcher pins at
session start and unpins on exit.

## Install (on the robot, once)

```bash
sudo cp /home/booster/k1-jetson-clocks.service /etc/systemd/system/   # or from this repo dir
sudo systemctl daemon-reload
sudo systemctl enable --now k1-jetson-clocks.service
```

## Verify

```bash
# min == max  ->  pinned
cat /sys/class/devfreq/17000000.gpu/min_freq /sys/class/devfreq/17000000.gpu/max_freq
systemctl status k1-jetson-clocks.service
```

## Uninstall

```bash
sudo systemctl disable --now k1-jetson-clocks.service
sudo rm /etc/systemd/system/k1-jetson-clocks.service
sudo systemctl daemon-reload
```
