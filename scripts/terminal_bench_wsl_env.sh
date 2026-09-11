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
export CC_HARNESS_TERMINAL_GUARD_CLEANUP="${CC_HARNESS_TERMINAL_GUARD_CLEANUP:-1}"
export PATH="$HOME/.local/bin:$PATH"
export CC_HARNESS_TERMINAL_RUNTIME_ROOT="$HOME/.local/share/cc-harness/terminal-bench-runtime"
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/cc-harness/venv"
export UV_CACHE_DIR="$HOME/.cache/uv"
export XDG_CACHE_HOME="$HOME/.cache"
export TMPDIR="$HOME/.cache/cc-harness/tmp"
export DOCKER_HOST="unix:///var/run/docker.sock"
unset DOCKER_CONTEXT

# Harbor's official Docker baseline is public network access.  Do not force a
# host proxy into every task/verifier container: on WSL2 the Windows gateway
# address may be reachable from Ubuntu while still being blocked from the
# native Docker bridge (and a stale Clash listener then turns a valid task into
# a NetworkConnectionError).  Operators who explicitly need Clash can opt in
# with CC_HARNESS_TERMINAL_NETWORK_TRANSPORT=proxy; the default remains the
# official direct/public path.
network_transport="${CC_HARNESS_TERMINAL_NETWORK_TRANSPORT:-direct}"
if [[ -n "${CC_HARNESS_TERMINAL_PROXY:-}" && -z "${CC_HARNESS_TERMINAL_NETWORK_TRANSPORT:-}" ]]; then
  network_transport="proxy"
fi
case "$network_transport" in
  direct|public|direct-public.v1)
    export CC_HARNESS_TERMINAL_NETWORK_TRANSPORT="direct-public.v1"
    unset CC_HARNESS_TERMINAL_PROXY
    unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
    ;;
  proxy|wsl-gateway-http-proxy.v1)
    # Docker bridge containers do not share the WSL NAT gateway.  A proxy
    # reachable as 172.29.x.1 from WSL is therefore usually *not* reachable
    # from a task container.  Docker Desktop exposes the Windows host to
    # containers as host.docker.internal; use that address for the proxy
    # injected by Docker's config.json.  An explicit proxy URL is still
    # honored for users running a different reachable proxy.
    export CC_HARNESS_TERMINAL_NETWORK_TRANSPORT="wsl-gateway-http-proxy.v1"
    export CC_HARNESS_TERMINAL_PROXY="${CC_HARNESS_TERMINAL_PROXY:-http://host.docker.internal:7890}"
    proxy_hostport="${CC_HARNESS_TERMINAL_PROXY#http://}"
    proxy_host="${proxy_hostport%%:*}"
    proxy_port="${proxy_hostport##*:}"
    if [[ "$proxy_hostport" == "$proxy_host" || -z "$proxy_host" || -z "$proxy_port" ]]; then
      echo "ERROR: CC_HARNESS_TERMINAL_PROXY must be an http://host:port URL." >&2
      exit 2
    fi
    # The proxy URL is consumed by Docker task containers.  Do not blindly
    # export it into the WSL supervisor: host.docker.internal resolves to a
    # different address in WSL and may be unreachable there.  Operators that
    # also need a WSL-side proxy can set the separate host-proxy variable.
    host_proxy="${CC_HARNESS_TERMINAL_HOST_PROXY:-}"
    if [[ -n "$host_proxy" ]]; then
      export HTTP_PROXY="$host_proxy"
      export HTTPS_PROXY="$host_proxy"
      export http_proxy="$host_proxy"
      export https_proxy="$host_proxy"
    else
      unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
    fi
    unset ALL_PROXY all_proxy
    ;;
  *)
    echo "ERROR: unsupported CC_HARNESS_TERMINAL_NETWORK_TRANSPORT=${network_transport}; use direct or proxy." >&2
    exit 2
    ;;
esac
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
if [[ "$network_transport" == "proxy" || "$network_transport" == "wsl-gateway-http-proxy.v1" ]]; then
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
else
  # Explicitly replace any previous proxy configuration in this dedicated
  # project-owned Docker config directory.
  printf '%s\n' '{}' >"$DOCKER_CONFIG/config.json"
fi

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
