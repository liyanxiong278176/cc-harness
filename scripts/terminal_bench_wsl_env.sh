#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]] || ! grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo "ERROR: Terminal-Bench 2.1 must run inside Ubuntu WSL2." >&2
  exit 2
fi
if [[ "${WSL_DISTRO_NAME:-}" != "Ubuntu" ]]; then
  echo "ERROR: expected WSL distribution Ubuntu, found ${WSL_DISTRO_NAME:-unknown}." >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$project_root" != "/mnt/d/agent_learning/cc-harness" ]]; then
  echo "ERROR: expected project at /mnt/d/agent_learning/cc-harness, found $project_root." >&2
  exit 2
fi

export CC_HARNESS_TERMINAL_EXECUTION_BACKEND="wsl2-ubuntu-native-docker.v1"
export PATH="$HOME/.local/bin:$PATH"
export CC_HARNESS_TERMINAL_RUNTIME_ROOT="$HOME/.local/share/cc-harness/terminal-bench-runtime"
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/cc-harness/venv"
export UV_CACHE_DIR="$HOME/.cache/uv"
export XDG_CACHE_HOME="$HOME/.cache"
export TMPDIR="$HOME/.cache/cc-harness/tmp"
export DOCKER_HOST="unix:///var/run/docker.sock"
unset DOCKER_CONTEXT

# WSL2 uses a separate NAT namespace, so Windows localhost is not reachable
# from either Ubuntu or its Docker containers. Route only this Terminal-Bench
# process tree through the Clash listener on the WSL gateway. The dedicated
# Docker config injects the same transport into task, agent, and verifier
# containers without changing official task definitions or timeout settings.
windows_gateway="$(ip route show default | awk '{print $3; exit}')"
if [[ -z "$windows_gateway" ]]; then
  echo "ERROR: cannot resolve the Windows gateway for Terminal-Bench." >&2
  exit 2
fi
export CC_HARNESS_TERMINAL_NETWORK_TRANSPORT="wsl-gateway-http-proxy.v1"
export CC_HARNESS_TERMINAL_PROXY="http://${windows_gateway}:7890"
if ! timeout 5 bash -c "exec 3<>/dev/tcp/${windows_gateway}/7890" 2>/dev/null; then
  echo "ERROR: Clash proxy is not reachable at ${CC_HARNESS_TERMINAL_PROXY}." >&2
  echo "Enable Clash Allow LAN and the scoped Windows firewall rule before evaluation." >&2
  exit 2
fi
export HTTP_PROXY="$CC_HARNESS_TERMINAL_PROXY"
export HTTPS_PROXY="$CC_HARNESS_TERMINAL_PROXY"
export http_proxy="$CC_HARNESS_TERMINAL_PROXY"
export https_proxy="$CC_HARNESS_TERMINAL_PROXY"
unset ALL_PROXY all_proxy
export NO_PROXY="localhost,127.0.0.1,::1,host.docker.internal"
export no_proxy="$NO_PROXY"
export DOCKER_CONFIG="$HOME/.config/cc-harness/docker-terminal-bench"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

mkdir -p \
  "$CC_HARNESS_TERMINAL_RUNTIME_ROOT" \
  "$UV_PROJECT_ENVIRONMENT" \
  "$UV_CACHE_DIR" \
  "$TMPDIR" \
  "$DOCKER_CONFIG"
printf '%s\n' \
  '{' \
  '  "proxies": {' \
  '    "default": {' \
  "      \"httpProxy\": \"${CC_HARNESS_TERMINAL_PROXY}\"," \
  "      \"httpsProxy\": \"${CC_HARNESS_TERMINAL_PROXY}\"," \
  "      \"noProxy\": \"${NO_PROXY}\"" \
  '    }' \
  '  }' \
  '}' >"$DOCKER_CONFIG/config.json"

cd "$project_root"
command -v uv >/dev/null 2>&1 || {
  echo "ERROR: uv is not installed in Ubuntu." >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || {
  echo "ERROR: native Docker is not installed in Ubuntu." >&2
  exit 2
}
[[ "$(command -v docker)" == "/usr/bin/docker" ]] || {
  echo "ERROR: refusing non-native Docker binary: $(command -v docker)" >&2
  exit 2
}
[[ "$(docker context show)" == "default" ]] || {
  echo "ERROR: refusing Docker context $(docker context show)." >&2
  exit 2
}
[[ "$(docker info --format '{{.DockerRootDir}}')" == "/var/lib/docker" ]] || {
  echo "ERROR: refusing Docker daemon outside /var/lib/docker." >&2
  exit 2
}
