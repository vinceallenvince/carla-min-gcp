#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  gnupg2 \
  nvidia-driver-580-server \
  vulkan-tools \
  python3-venv

if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  arch="$(dpkg --print-architecture)"
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${codename} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if ! command -v nvidia-ctk >/dev/null 2>&1; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
fi

sudo nvidia-ctk runtime configure --runtime=docker
sudo mkdir -p /etc/cdi
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo systemctl enable --now docker
sudo systemctl restart docker

echo "GPU on host:"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

echo "Vulkan renderer on host:"
vulkaninfo --summary 2>&1 | grep -A3 '^GPU0:'

echo "GPU in Docker:"
sudo docker run --rm --device=nvidia.com/gpu=all \
  ubuntu:22.04 nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

sudo docker pull carlasim/carla:0.10.0
sudo docker rm -f carla-server >/dev/null 2>&1 || true
sudo docker run --detach \
  --name=carla-server \
  --restart=unless-stopped \
  --device=nvidia.com/gpu=all \
  --network=host \
  carlasim/carla:0.10.0 \
  bash CarlaUnreal.sh -RenderOffScreen -nosound

echo "CARLA container started."
