#!/usr/bin/env bash
# setup-cuda-worker-distro.sh  (P6G.1 -- in-distro half; docs/WSL2_SUBSTRATE.md sections 3,5-10)
#
# Run WITH sudo INSIDE Ubuntu-24.04, AFTER setup-cuda-worker-windows.ps1's wsl --shutdown:
#   sudo bash desktop/setup-cuda-worker-distro.sh
#
# Installs NATIVE Docker Engine (not Docker Desktop) + nvidia-container-toolkit (the ONLY NVIDIA thing
# in the distro -- NEVER a Linux GPU driver, doctrine 1), a key-only sshd on 2222, and ~/k1 on ext4.
# Vendor apt lines are current as of 2026-07 but DRIFT -- the [CONFIRM] echoes point at the source of truth.
set -euo pipefail

USER_REAL="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$USER_REAL" | cut -d: -f6)"
PORT="${K1_SSHD_PORT:-2222}"
CUDA_BASE="${K1_CUDA_BASE:-nvidia/cuda:12.6.3-base-ubuntu24.04}"   # [CONFIRM] tag at hub.docker.com/r/nvidia/cuda/tags

echo "== 0. Doctrine guard: refuse if a Linux NVIDIA driver is installed in the distro =="
if dpkg -l 2>/dev/null | grep -Eq '^ii\s+(nvidia-driver-|cuda-drivers)'; then
  echo "FATAL: a Linux NVIDIA driver is installed in the distro -- it shadows /usr/lib/wsl/lib and breaks"
  echo "       GPU access (WSL2_SUBSTRATE doctrine 1). Remove it; the driver lives on Windows ONLY." >&2
  exit 1
fi
echo "  ok (Windows driver projects CUDA via /usr/lib/wsl/lib):"; ls /usr/lib/wsl/lib/libcuda.so.1

echo "== 1. systemd sanity (set by wsl.conf; this box already has it) =="
[ "$(ps -p 1 -o comm=)" = systemd ] || { echo "FATAL: pid1 is not systemd -- set [boot] systemd=true in /etc/wsl.conf + wsl --shutdown" >&2; exit 1; }

echo "== 2. Native Docker Engine (docker-ce) -- [CONFIRM] https://docs.docker.com/engine/install/ubuntu/ =="
if ! dpkg -l docker-ce 2>/dev/null | grep -q '^ii'; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else echo "  docker-ce already installed"; fi
usermod -aG docker "$USER_REAL"
systemctl enable --now docker

echo "== 3. nvidia-container-toolkit -- [CONFIRM] docs.nvidia.com 'nvidia-container-toolkit install guide' =="
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
else echo "  nvidia-ctk already installed"; fi
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

echo "== 4. sshd on ${PORT}, key-only. Install the laptop pubkey FIRST (below), THEN disable passwords =="
apt-get install -y openssh-server
printf 'Port %s\n' "$PORT" > /etc/ssh/sshd_config.d/k1_port.conf
systemctl disable --now ssh.socket 2>/dev/null || true   # 24.04 socket-activation pins :22 otherwise
systemctl enable --now ssh.service
systemctl restart ssh
echo "  sshd listening:"; ss -tlnp | grep ":${PORT}" || echo "  (WARN: not listening on ${PORT} -- check journalctl -u ssh)"

echo "== 5. Data layout on ext4 (NEVER /mnt/c -- 9P murders frame I/O) =="
sudo -u "$USER_REAL" mkdir -p "$USER_HOME"/k1/{inbox,outbox,runs,bin}
df -T "$USER_HOME/k1" | awk 'NR==2{print "  ~/k1 filesystem:",$2}'

echo "== 6. CUDA base + LOCAL half of the P6G.1 gate =="
sudo -u "$USER_REAL" docker pull "$CUDA_BASE"
echo "  docker runtimes:"; sudo -u "$USER_REAL" docker info --format '{{json .Runtimes}}' | grep -o nvidia || echo "  (WARN: nvidia runtime not registered)"
echo "  GPU-in-container (--gpus all):"
sudo -u "$USER_REAL" docker run --rm --gpus all "$CUDA_BASE" nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || {
  echo "FATAL: --gpus all failed. Check: Windows driver, nvidia-ctk runtime configure, systemctl restart docker." >&2; exit 1; }

cat <<EOF

DISTRO-HALF-OK. Remaining (each side once):
  * Log out/in of the distro (or 'newgrp docker') so the docker group applies without sudo.
  * From the LAPTOP: generate a key + install it, THEN lock sshd to key-only:
      # laptop:
      ssh-keygen -t ed25519 -f "\$env:USERPROFILE\\.ssh\\k1_desktop" -C laptop-to-desktop
      \$pub = Get-Content "\$env:USERPROFILE\\.ssh\\k1_desktop.pub"
      ssh -p ${PORT} ${USER_REAL}@<desktop> "mkdir -p ~/.ssh; chmod 700 ~/.ssh; echo '\$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
      # then, back in this distro:
      printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\nPubkeyAuthentication yes\\n' | sudo tee /etc/ssh/sshd_config.d/k1_auth.conf
      sudo systemctl restart ssh
  * P6G.1 GATE (from the laptop): ssh -p ${PORT} -i <key> ${USER_REAL}@<desktop> "docker run --rm --gpus all ${CUDA_BASE} nvidia-smi"
    must name the RTX 3080, and an ext4 read-speed check on a copied bundle -- record as-built in WSL2_SUBSTRATE.md.
EOF
