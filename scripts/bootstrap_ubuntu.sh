#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bootstrap_ubuntu.sh [--user|--system] [options]

Options:
  --user                  Install as user service (default)
  --system                Install as system service
  --workdir DIR           Repository path (default: current directory)
  --config-dir DIR        Config directory
  --venv-dir DIR          Python virtualenv path
  --entrypoint CMD        Override service entrypoint (default: <venv>/bin/codex-telegram-bot)
  --skip-apt              Skip apt dependency installation
  --no-enable             Do not enable/start the service after install
  --dry-run               Print commands without executing
  -h, --help              Show this help
EOF
}

MODE="user"
WORKDIR="$(pwd)"
CONFIG_DIR="${HOME}/.config/codex-telegram-bot"
VENV_DIR="${HOME}/.local/share/codex-telegram-bot/.venv"
ENTRYPOINT=""
SKIP_APT="false"
ENABLE_SERVICE="true"
DRY_RUN="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE="user"; shift ;;
    --system) MODE="system"; shift ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --venv-dir) VENV_DIR="$2"; shift 2 ;;
    --entrypoint) ENTRYPOINT="$2"; shift 2 ;;
    --skip-apt) SKIP_APT="true"; shift ;;
    --no-enable) ENABLE_SERVICE="false"; shift ;;
    --dry-run) DRY_RUN="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

WORKDIR="$(cd "$WORKDIR" && pwd)"
INSTALL_SERVICE_SCRIPT="$WORKDIR/scripts/install_service.sh"

if [[ ! -f "$INSTALL_SERVICE_SCRIPT" ]]; then
  echo "Missing installer: $INSTALL_SERVICE_SCRIPT" >&2
  exit 1
fi

if [[ -z "$ENTRYPOINT" ]]; then
  ENTRYPOINT="$VENV_DIR/bin/codex-telegram-bot"
fi

SUDO=""
if command -v sudo >/dev/null 2>&1 && [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  SUDO="sudo"
fi

run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] $*"
    return 0
  fi
  "$@"
}

run_cmd_maybe_sudo() {
  if [[ -n "$SUDO" ]]; then
    run_cmd $SUDO "$@"
  else
    run_cmd "$@"
  fi
}

ensure_ubuntu() {
  if [[ ! -f /etc/os-release ]]; then
    echo "Cannot detect OS. /etc/os-release not found." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    echo "This installer targets Ubuntu. Detected ID=${ID:-unknown}." >&2
    exit 1
  fi
}

install_deps() {
  ensure_ubuntu
  run_cmd_maybe_sudo apt-get update
  run_cmd_maybe_sudo apt-get install -y python3 python3-venv python3-pip systemd curl git
}

prepare_venv() {
  mkdir -p "$(dirname "$VENV_DIR")"
  if [[ ! -d "$VENV_DIR" ]]; then
    run_cmd python3 -m venv "$VENV_DIR"
  fi
  run_cmd "$VENV_DIR/bin/pip" install --upgrade pip wheel setuptools
  run_cmd "$VENV_DIR/bin/pip" install -e "$WORKDIR"
}

install_service() {
  local mode_flag="--user"
  if [[ "$MODE" == "system" ]]; then
    mode_flag="--system"
  fi
  if [[ "$MODE" == "system" ]]; then
    run_cmd_maybe_sudo "$INSTALL_SERVICE_SCRIPT" "$mode_flag" \
      --workdir "$WORKDIR" \
      --config-dir "$CONFIG_DIR" \
      --entrypoint "$ENTRYPOINT"
  else
    run_cmd "$INSTALL_SERVICE_SCRIPT" "$mode_flag" \
      --workdir "$WORKDIR" \
      --config-dir "$CONFIG_DIR" \
      --entrypoint "$ENTRYPOINT"
  fi
}

enable_service() {
  if [[ "$ENABLE_SERVICE" != "true" ]]; then
    echo "Service enable/start skipped (--no-enable)."
    return 0
  fi
  if [[ "$MODE" == "system" ]]; then
    run_cmd_maybe_sudo systemctl enable --now codex-telegram-bot
  else
    run_cmd systemctl --user enable --now codex-telegram-bot
  fi
}

if [[ "$SKIP_APT" != "true" ]]; then
  install_deps
fi
prepare_venv
install_service
enable_service

cat <<EOF
Bootstrap complete.
Mode: $MODE
Workdir: $WORKDIR
Config dir: $CONFIG_DIR
Venv: $VENV_DIR
Entrypoint: $ENTRYPOINT
EOF
