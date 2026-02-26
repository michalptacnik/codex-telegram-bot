import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
ALLOWLIST_KEY = "ALLOWLIST"

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "codex-telegram-bot"


@dataclass
class Config:
    token: str
    allowlist: Optional[List[int]]
    config_dir: Path
    env_path: Path


def load_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    except Exception as exc:
        print(f"Failed to read .env: {exc}", file=sys.stderr)
    return data


def write_env_file(path: Path, data: Dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in data.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"Failed to write .env: {exc}", file=sys.stderr)


def get_env_value(key: str, env_file: Dict[str, str]) -> Optional[str]:
    return os.environ.get(key) or env_file.get(key)


def apply_env_defaults(env_file: Dict[str, str], target_env: Optional[Dict[str, str]] = None) -> int:
    """Populate missing process env vars from .env-style mapping.

    Existing environment values are never overwritten.
    Returns the number of keys applied.
    """
    target = target_env if target_env is not None else os.environ  # type: ignore[assignment]
    applied = 0
    for raw_key, raw_value in (env_file or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if key in target and str(target.get(key) or "").strip():
            continue
        target[key] = str(raw_value or "")
        applied += 1
    return applied


def parse_allowlist(raw: Optional[str]) -> Optional[List[int]]:
    if not raw:
        return None
    ids: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids or None


def get_env_path(config_dir: Path) -> Path:
    return config_dir / ".env"


def load_env_with_fallback(config_dir: Path) -> Dict[str, str]:
    env_path = get_env_path(config_dir)
    data = load_env_file(env_path)
    if data:
        return data

    # Legacy fallbacks: project root or current working directory
    legacy_paths = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for legacy in legacy_paths:
        legacy_data = load_env_file(legacy)
        if legacy_data:
            return legacy_data
    return {}


def ensure_onboarding(config_dir: Path) -> Dict[str, str]:
    env_path = get_env_path(config_dir)
    env_file = load_env_file(env_path)
    token = get_env_value(TOKEN_KEY, env_file)
    allowlist = get_env_value(ALLOWLIST_KEY, env_file)

    updated = False

    if not token:
        token = input("Enter Telegram Bot Token: ").strip()
        env_file[TOKEN_KEY] = token
        updated = True

    if allowlist is None:
        allowlist_input = input(
            "Enter allowed Telegram user ID(s), comma separated. Leave blank to allow everyone: "
        ).strip()
        if not allowlist_input:
            print(
                "WARNING: No allowlist set. Anyone who finds your bot can use your Codex CLI and burn tokens."
            )
            confirm = input("Type YES to continue: ").strip()
            if confirm != "YES":
                print("Aborted.")
                sys.exit(1)
        env_file[ALLOWLIST_KEY] = allowlist_input
        updated = True

    if updated:
        write_env_file(env_path, env_file)

    return env_file


def purge_env(config_dir: Path) -> None:
    env_path = get_env_path(config_dir)
    try:
        if env_path.exists():
            env_path.unlink()
    except Exception as exc:
        print(f"Failed to purge .env: {exc}", file=sys.stderr)


def reinstall_env(config_dir: Path) -> None:
    env_path = get_env_path(config_dir)
    env_file = load_env_file(env_path)
    if TOKEN_KEY in env_file:
        del env_file[TOKEN_KEY]
        write_env_file(env_path, env_file)


def load_config(config_dir: Path) -> Config:
    env_file = ensure_onboarding(config_dir)
    token = get_env_value(TOKEN_KEY, env_file)
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        sys.exit(1)

    allowlist_raw = get_env_value(ALLOWLIST_KEY, env_file)
    allowlist = parse_allowlist(allowlist_raw)

    return Config(
        token=token,
        allowlist=allowlist,
        config_dir=config_dir,
        env_path=get_env_path(config_dir),
    )
