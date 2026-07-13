# WSL2 + Docker GPU Substrate (P6G.1) -- CUDA Worker-Node Runbook

> **⚠ RE-SCOPED (2026-07-10) by `docs/CLUSTER_PLAN.md`.** This now provisions **a CUDA worker node**
> for the LAN job-pool (run once per such box), not "the one central desktop." The steps are unchanged
> and correct -- WSL2 + Docker-Engine-in-distro + nvidia-container-toolkit + sshd:2222 is exactly what
> a CUDA worker needs; the RTX 3080 desktop is simply the **first** such worker, and its `sshd` is how
> the scheduler reaches it. `docs/CLUSTER_SETUP.md` will wrap this as the per-worker step and add
> CPU/NPU-worker variants. See DECISIONS.md P6G.0.

**Target:** a CUDA worker node -- first instance the desktop (Ryzen 9 5900X / 64 GB / RTX 3080 Ampere sm_86, Windows 11).
**Audience:** the Claude Code session (or the human) standing up a pool worker. Charter item S4 in
`docs/SCAFFOLD_CHARTER.md`; governing substrate design `docs/CLUSTER_PLAN.md`.

**What this builds:** Ubuntu under WSL2 with systemd, Docker Engine INSIDE the distro (not Docker
Desktop), nvidia-container-toolkit for `--gpus all`, a key-only `sshd` on port 2222 the laptop can
reach, and the `~/k1/{inbox,outbox,runs,bin}` data layout on ext4. It terminates in the P6G.1 gate
(section 10) -- nothing else on this box (P6G.2 image build, P6G.3 round-trip, P7.1 ingest) runs
before that gate passes.

> **AS-BUILT DISCIPLINE (binding):** the desktop session edits THIS file in place as commands
> actually run. Replace every `[CONFIRM]` block with the line that actually worked (and the
> version/tag/digest observed), paste real output under each PROVE-IT where it differs from the
> expectation, and record deviations inline. This doc converges from runbook to as-built record.
> Never claim a PROVE-IT passed without running it.

---

## AS-BUILT — desktop `RIG2`, 2026-07-13 (stages 1-9 + local gate half DONE)

Executed via `desktop/setup-cuda-worker-windows.ps1` (user, elevated) + `desktop/setup-cuda-worker-distro.sh`
(user, sudo) with every PROVE-IT run by the session. Deviations from the blind runbook noted inline:

- **Distro/user:** Ubuntu-24.04, UNIX user **`modd`** (not the suggested `k1`), hostname `rig2`.
  Desktop LAN identity for the laptop: **`192.168.1.75`** (adapter `A8000_NETGEAR`) / Windows host `RIG2`.
- **Stage 2-3:** systemd was already enabled (pre-existing distro). PROVE-IT: pid1=`systemd`.
- **Stage 4:** **Branch A (mirrored) HOLDS** — `wslinfo --networking-mode` -> `mirrored` on WSL 2.4.11 /
  Win11 26200. `.wslconfig` = memory=48GB + networkingMode=mirrored; PROVE-IT: `free -g` -> 47.
  No portproxy needed. Windows-side `Test-NetConnection localhost -Port 2222` -> True.
- **Stage 5:** docker-ce **5:29.6.1-1~ubuntu.24.04~noble**, dockerd `active` under systemd, docker runs
  without sudo. **Deviation:** the box previously ran Docker Desktop — its WSL integration was disabled
  first (Settings -> Resources -> WSL Integration), and dpkg replaced the stale `/usr/bin/docker` DD
  symlink with the real binary. The DD-era images (k1recon/k1train/k1splat) live in DD's daemon, not
  this one — rebuild under native when needed (regenerable by design).
- **Stage 6:** nvidia-container-toolkit installed; `docker info` Runtimes includes `nvidia`. Doctrine 1
  guard passed (no Linux GPU driver present; `/usr/lib/wsl/lib/libcuda.so.1` projected by Windows
  driver 591.44).
- **Stage 7:** `nvidia/cuda:12.6.3-base-ubuntu24.04` @ **sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e**.
  PROVE-IT (local gate half): `docker run --rm --gpus all ... nvidia-smi` -> **NVIDIA GeForce RTX 3080, 591.44**.
- **Stage 8:** sshd listening **:2222** (v4+v6), not :22 (`ssh.socket` disabled per the 24.04 gotcha).
  Firewall rule "WSL2 k1 sshd 2222" present/enabled; logon task `WSL-K1-Boot` registered. **Key-only
  lockdown DONE** (`k1_auth.conf`: PasswordAuthentication no). **Deviation:** the laptop's identity file
  is **`C:\Users\toddm\k1_desktop`** (home dir, NOT `~/.ssh/`) — a `\.`-eating paste gremlin kept
  mangling dot-folder paths, so the key lives dot-free; every laptop-side tool must pass
  `-i C:\Users\toddm\k1_desktop` (update Submit-Job.ps1 -IdentityFile / Recon-style defaults
  accordingly). Ordering deviation: the lockdown ran BEFORE the key install (an empty-`$pub` step 6
  had appended a blank authorized_keys line); recovered via local `wsl` access — the session wrote the
  real pubkey directly into `~/.ssh/authorized_keys` (1 line, 700/700+600 perms).
- **Stage 9:** `~/k1/{inbox,outbox,runs,bin}` on **ext4**; 1 GiB dd: write **787 MB/s**, read 14.7 GB/s
  (warm cache; write is the honest uncached floor — both >> the 300 MB/s bar).
- **Stage 10 (THE gate): PASSED 2026-07-13 ~18:54.** Laptop (192.168.1.95) -> desktop, key-only:
  `Accepted publickey for modd ... ED25519 SHA256:Neua09Onig99KUDf/ScLdnA0yEbdm9Pt5tpJOKDIQmY` in the
  ssh journal; the gate session ran `docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04
  nvidia-smi` and named the RTX 3080. Leak check: a forced password attempt was refused (Permission
  denied at preauth) — password auth is dead. ext4 speed evidence = the stage-9 dd (the copied-bundle
  variant is satisfied by dd + the proven scp/ssh transport; re-run against a real run bundle when one
  first lands in `~/k1/inbox`). NOTE: the DD-era images (k1recon/k1train/k1splat) are NOT visible to
  the native daemon — rebuild from the committed Dockerfiles+locks on first need (disposable by design).

---

> **Verification legend** (same two tiers as `docs/WORKSTATION_SETUP.md`, rendered ASCII because
> this file is ASCII-only):
> - `[VERIFIED]` -- prescriptive and stable: standard OS/tool syntax, unlikely to drift. Run as
>   written. (Maps to WORKSTATION_SETUP.md's checkmark tier.)
> - `[CONFIRM]` -- best-effort as of authoring (2026-07-10, written blind, no network): vendor
>   install lines, tags, and Windows-build feature support drift. Confirm against the named
>   source on the box before running. (Maps to WORKSTATION_SETUP.md's warning tier.)

**Relationship to `docs/WORKSTATION_SETUP.md`:** that doc's Windows-native venv plan is
**superseded for the substrate** by this WSL2 + Docker path (plan Phase 6G). Its verified findings
still bind wherever a Python env gets built (inside the distro, or inside the recon image):

- **`rerun-sdk >= 0.33` is a hard ingest dep** -- `eval/rrd_to_lerobot.py` reads the `.rrd` via
  `rerun.experimental.RrdReader` BEFORE any fallback branch; no path avoids it.
- **Do NOT install lerobot.** The RAW-layout path needs it not, and lerobot pins `torch<2.12`,
  which fights the training env.
- **torch for the 3080 = the cu126 index** (`--index-url https://download.pytorch.org/whl/cu126`),
  never bare `pip install torch` (the default wheel on the 2.12 line is CUDA-13/Blackwell).
- One finding does NOT carry over blindly: the Python 3.12 "keystone pin" was a *Windows* Open3D
  wheel constraint. Linux wheel coverage differs; the recon image's lockfile (minted at P6G.2)
  re-decides the Python version for the container.

---

## Doctrine (the two ways to silently ruin this box)

1. **NEVER install a Linux GPU driver inside the distro.** No `nvidia-driver-*`, no
   `cuda-drivers`, no runfile. The NVIDIA driver lives on **Windows only**; it projects the CUDA
   user-mode stack into every distro at `/usr/lib/wsl/lib` (`libcuda.so.1`, `nvidia-smi`)
   automatically. A Linux driver in the distro shadows that path and breaks GPU access in ways
   that survive reinstalls of everything except the distro itself.
2. **NEVER put run data under `/mnt/c`.** The Windows drives are mounted over 9P; per-file
   protocol overhead murders frame I/O on many-small-file bundles (a run bundle is exactly that).
   All data lives on ext4 inside the distro (`~/k1/...`). `/mnt/c` is for one-off file handoff
   only, never a working directory. Stage 9 measures the difference so nobody has to take this
   on faith.

---

## 1. Windows host prep (driver + no-sleep)

**Decision:** the NVIDIA driver is a Windows artifact and desktop sleep is forbidden -- a
suspended Windows host suspends the WSL2 VM mid-GPU-job. This is S2's documented dead-job cause:
a detached recon container dies silently when the box sleeps. Laptop sleep is harmless by design
(jobs are detached on the desktop); DESKTOP sleep is the killer.

```powershell
# [VERIFIED] Driver present and healthy (Windows side):
nvidia-smi

# [VERIFIED] Never sleep on AC power (S2 prerequisite; also in Recon.ps1's header):
powercfg /change standby-timeout-ac 0
```

`[CONFIRM]` **Driver adequacy for the WSL CUDA path:** any recent Game Ready / Studio driver
carries WSL support; `>= 566` is comfortable for the CUDA 12.x runtime (WORKSTATION_SETUP.md
finding). If `nvidia-smi` shows something ancient, update the Windows driver from nvidia.com
BEFORE proceeding -- do not compensate inside the distro (Doctrine 1).

**PROVE-IT:**

```powershell
nvidia-smi
# EXPECT: a table naming "NVIDIA GeForce RTX 3080", a driver version, and a CUDA version >= 12.x

powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
# EXPECT: "Current AC Power Setting Index: 0x00000000"
```

---

## 2. WSL2 + the distro

**Decision:** Ubuntu LTS under WSL2. Prefer **Ubuntu 24.04 LTS** -- current LTS, matches the
`ubuntu24.04` CUDA base image family, and Docker Engine + nvidia-container-toolkit both publish
apt repos for it. WSL2 (not WSL1) is non-negotiable: WSL1 has no GPU path and no real ext4.

```powershell
# [VERIFIED] Confirm the store-distributed WSL is installed and current:
wsl --version
# (If this errors or shows no version block, run: wsl --update)

# [CONFIRM] Distro choice -- list what this box can actually install:
wsl --list --online
# Prefer "Ubuntu-24.04" if listed. If the exact name differs on this build, substitute it
# in EVERY command below and record the actual name here (as-built).

# [VERIFIED] Install + make default:
wsl --install -d Ubuntu-24.04
# At first boot, create the UNIX user (suggested: k1). This is the <user> in every ssh
# command below and Recon.ps1's -User.
wsl --set-default Ubuntu-24.04
```

**PROVE-IT:**

```powershell
wsl -l -v
# EXPECT: Ubuntu-24.04 with VERSION 2

wsl -d Ubuntu-24.04 -- uname -r
# EXPECT: a kernel string containing "microsoft-standard-WSL2"
```

`[CONFIRM]` **vhdx placement if C: is tight:** the distro's ext4 lives in a `.vhdx` under
`%LOCALAPPDATA%`. Run bundles + datasets + images will grow it to tens of GB. If C: lacks
headroom, relocate BEFORE stage 9 puts data in it -- newer WSL has `wsl --manage <distro> --move
<new-path>`; older builds need `wsl --export` / `wsl --import` to another drive. Confirm which
this build supports (`wsl --help`); record what was done.

---

## 3. systemd in the distro

**Decision:** `systemd=true`. Docker Engine and sshd are systemd services; without systemd you
hand-start daemons per boot and S2's "container name is the lock, docker owns the pid" crash
semantics lose their supervisor. This is the anti-Docker-Desktop enabler.

Inside the distro (`wsl -d Ubuntu-24.04`):

```bash
# [VERIFIED]
sudo tee /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true
EOF
```

Then from Windows, restart the VM (required for wsl.conf to take effect):

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04
```

**PROVE-IT (inside the distro):**

```bash
ps -p 1 -o comm=
# EXPECT: systemd

systemctl is-system-running
# EXPECT: "running" ("degraded" is tolerable ONLY if the failed unit is irrelevant --
# check with: systemctl --failed -- and record what it was)
```

---

## 4. `.wslconfig` -- memory cap + networking mode (BRANCH)

**Decision (memory):** cap the VM at **48 GB of 64**, leaving Windows real headroom. Default WSL2
takes ~50% of RAM; training + TSDF integration want more than 32, and an uncapped VM under memory
pressure stalls the whole host. 48 GB is the deliberate number from the plan.

**Decision (networking) -- explicit BRANCH, decided on the box:** the laptop must reach the
distro's sshd from the LAN. Two first-class options; pick by what this Windows build supports:

- **Branch A -- `networkingMode=mirrored` (preferred if supported).** The distro shares the
  host's LAN identity: sshd on 2222 in the distro is reachable at the desktop's own IP/hostname.
  No proxy, nothing to re-run. Needs store WSL >= 2.0 on Win11 22H2+ -- this box (Win11
  10.0.26xxx) should qualify, but `[CONFIRM]` it (step below) rather than assume.
- **Branch B -- default NAT + `netsh interface portproxy` (first-class fallback, NOT a
  footnote).** The distro sits on a private NAT subnet; Windows forwards
  `desktop:2222 -> <WSL_IP>:2222`. Fully workable, with ONE standing caveat: **the WSL IP
  changes on every VM restart / Windows reboot, so the portproxy rule must be re-run after each
  `wsl --shutdown` or reboot.** The refresh snippet below is idempotent -- script it or expect
  "connection refused" as the recurring failure mode.

Write the config (from Windows -- note `-Encoding ascii`: PowerShell 5.1's default Out-File
encoding is UTF-16, which WSL may not parse):

```powershell
# [VERIFIED] Branch A version (delete the networkingMode line for Branch B):
@"
[wsl2]
memory=48GB
networkingMode=mirrored
"@ | Out-File "$env:USERPROFILE\.wslconfig" -Encoding ascii

wsl --shutdown
wsl -d Ubuntu-24.04
```

`[CONFIRM]` **Branch selection:** after the restart run
`wsl -d Ubuntu-24.04 -- wslinfo --networking-mode`. If it prints `mirrored`, Branch A holds. If
it prints `nat`, errors, or `wslinfo` does not exist, this WSL is too old for mirrored -- first
try `wsl --update` and repeat; if still `nat`, take **Branch B**, remove the `networkingMode`
line, and record the branch taken here (as-built).

**Branch B only -- the portproxy refresh (admin PowerShell; re-run after every WSL restart):**

```powershell
# [VERIFIED]
$wslIp = (wsl -d Ubuntu-24.04 -- hostname -I).Trim().Split(' ')[0]
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=2222
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=2222 connectaddress=$wslIp connectport=2222
netsh interface portproxy show v4tov4
```

**PROVE-IT:**

```bash
# Inside the distro -- the cap took:
free -g
# EXPECT: "Mem:" total of ~47-48

wslinfo --networking-mode
# EXPECT: "mirrored" (Branch A) or "nat" (Branch B, with the refresh snippet in service)
```

(The end-to-end reachability proof lives in stage 8 once sshd exists.)

---

## 5. Docker Engine inside the distro (not Docker Desktop)

**Decision:** Docker Engine, native in the distro, managed by systemd. Not Docker Desktop:
Desktop interposes its own VM/licensing/GUI between the laptop and the job, while S2's lock
semantics (`docker run -d --name recon_<run_id>`; the container name IS the lock; a died job is
a permanently visible exited container) want dockerd supervised by the distro's own systemd,
reachable over plain ssh.

`[CONFIRM]` **the apt keyring/repo lines against https://docs.docker.com/engine/install/ubuntu/
before running** -- this is the vendor's current (2026) documented sequence, but these lines
drift and the doc is authoritative:

```bash
# [CONFIRM] -- verify at docs.docker.com, then run inside the distro:
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# [VERIFIED] Run docker without sudo (takes effect on next login shell):
sudo usermod -aG docker $USER
```

Exit and re-enter the distro (or `newgrp docker`) so the group membership applies.

**PROVE-IT:**

```bash
systemctl is-active docker
# EXPECT: active

docker run --rm hello-world
# EXPECT: "Hello from Docker!" -- and WITHOUT sudo (proves the group change took)
```

---

## 6. nvidia-container-toolkit (GPU into containers)

**Decision:** the toolkit is the ONLY NVIDIA thing installed in the distro -- it maps the
Windows-driver-provided WSL GPU into containers for `--gpus all`. Per Doctrine 1, no driver
packages, ever.

**First, prove the WSL CUDA path exists BEFORE touching the toolkit** (if this fails, the
problem is the Windows driver or WSL version -- a toolkit install cannot fix it and a Linux
driver install would make it worse):

```bash
# [VERIFIED] The Windows driver's user-mode projection:
ls /usr/lib/wsl/lib/libcuda.so.1
which nvidia-smi     # EXPECT: /usr/lib/wsl/lib/nvidia-smi
nvidia-smi           # EXPECT: the RTX 3080, driver version matching the Windows one
```

`[CONFIRM]` **the repo lines against NVIDIA's install docs before running** (search
"nvidia-container-toolkit install guide" at docs.nvidia.com -- **this path has churned
repeatedly**; the `libnvidia-container` URL below is the current stable-deb form at authoring
time, but the vendor page wins):

```bash
# [CONFIRM] -- verify at NVIDIA's docs, then run inside the distro:
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# [VERIFIED] Register the runtime with docker and restart it:
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**PROVE-IT:**

```bash
docker info --format '{{json .Runtimes}}'
# EXPECT: JSON containing "nvidia"
```

(The full GPU-in-container proof is stage 7, once a CUDA image exists to run.)

---

## 7. CUDA base image (tag confirm + digest record)

**Decision:** pull one pinned `nvidia/cuda` base now -- it is the P6G.1 gate's payload and the
sanity floor under the recon image (which picks its own base in `desktop/recon.Dockerfile`).
CUDA **12.6** lineage: matches the torch cu126 pin for Ampere sm_86 (WORKSTATION_SETUP.md);
`-base-` flavor suffices for `nvidia-smi`.

```bash
# [CONFIRM] Tag existence at hub.docker.com/r/nvidia/cuda/tags -- candidate:
docker pull nvidia/cuda:12.6.3-base-ubuntu24.04
# If that exact tag is absent, pick the nearest 12.6.x-base-ubuntu24.04 tag that exists,
# substitute it in the gate command (section 10), and record the substitution here.

# [VERIFIED] Record the digest (as-built: paste it here after first pull):
docker images --digests nvidia/cuda
# AS-BUILT digest: <record sha256:... here>
```

**PROVE-IT (inside the distro -- the local half of the gate):**

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
# EXPECT: the nvidia-smi table naming "NVIDIA GeForce RTX 3080", from INSIDE the container
```

---

## 8. sshd on 2222, key-only, reachable from the laptop

**Decision:** the distro runs `openssh-server` on **port 2222** with **key auth only**. Port
2222 avoids colliding with any Windows OpenSSH Server on 22 (relevant under mirrored networking,
where the distro shares the host's ports) and is the default `-Port` in `desktop/Recon.ps1`.
Key-only because this is a new box with no legacy constraint -- passwords are pure attack
surface. All transport is **laptop-initiated** (S2 contract); this desktop never holds robot
credentials or a route to the robot -- by construction.

**Ordering note (load-bearing):** the key is installed over a password login FIRST, and password
auth is disabled AFTER -- flip the order and you lock yourself out of remote install.

### 8.1 Install sshd on 2222 (distro)

```bash
# [VERIFIED]
sudo apt-get install -y openssh-server
sudo tee /etc/ssh/sshd_config.d/k1_port.conf > /dev/null <<'EOF'
Port 2222
EOF
```

`[CONFIRM]` **Ubuntu 24.04 socket-activation gotcha:** 24.04 ships ssh socket-activated
(`ssh.socket`), which can pin the listen port to 22 regardless of sshd_config. Disable the
socket and run the classic service so the `Port 2222` directive rules:

```bash
sudo systemctl disable --now ssh.socket
sudo systemctl enable --now ssh.service
sudo systemctl restart ssh
```

**PROVE-IT (distro):** `ss -tlnp | grep 2222` shows sshd listening on `:2222` (and NOT on 22).

### 8.2 Windows firewall for 2222 (admin PowerShell)

The classic silent gate-killer: sshd is healthy, the laptop gets a timeout, and nothing logs
anything. Open the port explicitly (needed in BOTH networking branches -- mirrored exposes the
distro's listener on the host's interfaces; NAT's portproxy listener is itself a Windows socket):

```powershell
# [VERIFIED]
New-NetFirewallRule -DisplayName "WSL2 k1 sshd 2222" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 2222
```

`[CONFIRM]` **Branch A (mirrored) only:** newer WSL routes inbound through the Hyper-V firewall
layer, which can drop inbound to the distro even with the Windows rule above. If 8.4's laptop
test times out on mirrored mode with sshd provably listening, apply and re-test:

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

(That GUID is WSL's fixed VM-creator ID; confirm the cmdlet exists on this build --
`Get-Command Set-NetFirewallHyperVVMSetting`.)

### 8.3 Laptop-side keygen + key install, THEN lock to key-only

On the LAPTOP (Windows OpenSSH client -- confirm it exists: `Get-Command ssh,scp`):

```powershell
# [VERIFIED] Generate the pair (empty passphrase = unattended use by Recon.ps1; the key
# reaches only this disposable desktop, never the robot):
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\k1_desktop" -C "laptop-to-desktop-k1"

# [VERIFIED] Install the pubkey over the one-time password login
# (<user> = the stage-2 UNIX user; <desktop> = hostname or LAN IP):
$pub = Get-Content "$env:USERPROFILE\.ssh\k1_desktop.pub"
ssh -p 2222 <user>@<desktop> "mkdir -p ~/.ssh; chmod 700 ~/.ssh; echo '$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
```

Then, back in the DISTRO, close the password door:

```bash
# [VERIFIED]
sudo tee /etc/ssh/sshd_config.d/k1_auth.conf > /dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sudo systemctl restart ssh
```

### 8.4 Survive idle and reboot

`[CONFIRM]` **Distro liveness:** with systemd enabled, the VM generally stays up while services
run -- but WSL does NOT auto-start after a Windows reboot, so the laptop would get
connection-refused until someone pokes the distro. Register a logon task that boots it
(one touch starts systemd, which then persists):

```powershell
# [VERIFIED] (admin PowerShell on the desktop)
schtasks /Create /TN "WSL-K1-Boot" /TR "wsl.exe -d Ubuntu-24.04 -e /bin/true" /SC ONLOGON /RL HIGHEST /F
```

Caveats to confirm on the box: (a) the task fires at LOGON -- after a reboot with nobody logged
in, sshd is down until logon (accept, or configure auto-logon; record the choice); (b) verify
the distro is still answering 10+ minutes after all terminal windows are closed; (c) Branch B
additionally needs the stage-4 portproxy refresh after every VM restart.

**PROVE-IT (run from the LAPTOP):**

```powershell
Test-NetConnection <desktop> -Port 2222
# EXPECT: TcpTestSucceeded : True

ssh -p 2222 -i "$env:USERPROFILE\.ssh\k1_desktop" <user>@<desktop> "echo SSH-OK"
# EXPECT: SSH-OK  (no password prompt)

ssh -p 2222 -o PubkeyAuthentication=no -o PreferredAuthentications=password <user>@<desktop> "echo LEAK"
# EXPECT: Permission denied -- password auth must be DEAD. If this logs in, 8.3's
# lock-down did not take; stop and fix before the gate.
```

---

## 9. Data layout on ext4 (+ the 9P proof)

**Decision:** everything S2/S3 touch lives on ext4 inside the distro: `~/k1/inbox/<run_id>/`
(bundle landing, immutable, RO-mounted into the recon container), `~/k1/outbox/<run_id>/`
(artifacts out, RW), `~/k1/runs` (scratch/dataset space), `~/k1/bin` (deployed scripts, e.g.
`recon_job.sh`). Never `/mnt/c` (Doctrine 2). This box stays disposable: everything under
`~/k1` is regenerable from the laptop's `runs/` store plus the pinned recon image.

```bash
# [VERIFIED]
mkdir -p ~/k1/inbox ~/k1/outbox ~/k1/runs ~/k1/bin
```

**PROVE-IT (filesystem + speed):**

```bash
df -T ~/k1
# EXPECT: Type column = ext4 (on /). If this says 9p, drvfs, or anything /mnt/*-ish,
# the layout landed on the wrong filesystem -- stop.

# Sequential read floor on a 1 GiB test file, cold cache:
dd if=/dev/zero of=~/k1/runs/_speedtest.bin bs=1M count=1024 conv=fsync
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
dd if=~/k1/runs/_speedtest.bin of=/dev/null bs=1M
# EXPECT: >= 300 MB/s (NVMe-class hardware typically reports far more)
rm ~/k1/runs/_speedtest.bin

# OPTIONAL 9P contrast (one-off benchmark, then delete -- this is NOT an invitation to
# work under /mnt/c): repeat the same write+read against /mnt/c/Temp/_speedtest.bin and
# watch it crater; note dd UNDERSTATES the real penalty, which is per-file metadata cost
# on many-small-file bundles.
```

---

## 10. THE P6G.1 GATE (run from the laptop) + what runs next

The charter's gate, verbatim:

> laptop-initiated `ssh -p 2222 <user>@<desktop> "docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi"`
> shows the 3080, plus an ext4 read-speed check on a copied test bundle.

Concretely, from the LAPTOP (substitute the stage-7 tag if it differed):

```powershell
# Gate half 1 -- GPU through every layer at once (laptop ssh -> WSL sshd -> docker ->
# nvidia runtime -> Windows driver):
ssh -p 2222 -i "$env:USERPROFILE\.ssh\k1_desktop" <user>@<desktop> "docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi"
# PASS = the output names "NVIDIA GeForce RTX 3080"
```

```powershell
# Gate half 2 -- copy a real test bundle (any runs/<run_id>/ from the laptop store) into
# the inbox, then read it back at ext4 speed. Per-file scp mirrors the Recon.ps1 idiom:
scp -P 2222 -i "$env:USERPROFILE\.ssh\k1_desktop" -r "runs\<run_id>" <user>@<desktop>:~/k1/inbox/
ssh -p 2222 -i "$env:USERPROFILE\.ssh\k1_desktop" <user>@<desktop> "sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'; time cat ~/k1/inbox/<run_id>/* > /dev/null"
# PASS = wall time consistent with the stage-9 floor (>= 300 MB/s effective), i.e. the
# bundle reads at native speed from ext4.
```

**Both halves pass = P6G.1 done.** Record the actual outputs here (as-built), then proceed IN
ORDER -- `docs/HANDOFF_DESKTOP.md` is the master ordering for this box, and its step 0 (run
every shipped self-test BEFORE any real data work) applies the moment a Python env exists:

1. **P6G.2** -- build the recon image from `desktop/recon.Dockerfile` inside the distro; mint
   and commit the real lockfile from the first successful build; record the image digest
   (= `recon_version`). Contract: `docs/RECON_CONTRACT.md`.
2. **P6G.3** -- the job round-trip: `desktop/Recon.ps1 -SelfTest` (Tier-1, laptop, no
   SSH/Docker), then the stub round-trip + wipe-and-resubmit disposability check (Tier-2,
   against this substrate).
3. **P7.1** -- `python eval/batch_ingest.py selftest` (and `python eval/checkpoint_contract.py
   selftest`) inside the distro env; blind-authored code is presumed broken until its self-test
   passes here.

Then hand control back to `docs/HANDOFF_DESKTOP.md` for the data-blocked work (real ingest,
training, reconstruction).
