from __future__ import annotations

import os
import shutil
from typing import Callable, Dict, Iterable, List, Mapping, Sequence

AGENT_TOOLCHAIN_COMMANDS_ENV = "AGENT_TOOLCHAIN_COMMANDS"

DEFAULT_AGENT_TOOLCHAIN_COMMANDS: tuple[str, ...] = (
    "python3",
    "bash",
    "git",
    "curl",
    "wget",
    "jq",
    "rg",
    "fd|fdfind",
    "zip",
    "unzip",
    "tar",
    "rsync",
    "ssh",
    "chromium|chromium-browser|google-chrome",
)

_APT_PACKAGE_BY_COMMAND: Dict[str, str] = {
    "python3": "python3",
    "bash": "bash",
    "git": "git",
    "curl": "curl",
    "wget": "wget",
    "jq": "jq",
    "rg": "ripgrep",
    "fd": "fd-find",
    "fdfind": "fd-find",
    "zip": "zip",
    "unzip": "unzip",
    "tar": "tar",
    "rsync": "rsync",
    "ssh": "openssh-client",
    "chromium": "chromium-browser",
    "chromium-browser": "chromium-browser",
}


def required_agent_toolchain_commands(env: Mapping[str, str] | None = None) -> List[str]:
    source = env if env is not None else os.environ
    raw = str(source.get(AGENT_TOOLCHAIN_COMMANDS_ENV, "") or "").strip()
    if not raw:
        return list(DEFAULT_AGENT_TOOLCHAIN_COMMANDS)
    items: List[str] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if token and token not in items:
            items.append(token)
    return items or list(DEFAULT_AGENT_TOOLCHAIN_COMMANDS)


def find_missing_agent_toolchain_commands(
    command_specs: Sequence[str],
    which: Callable[[str], str | None] = shutil.which,
) -> List[str]:
    missing: List[str] = []
    for spec in command_specs:
        alternatives = [item.strip() for item in str(spec or "").split("|") if item.strip()]
        if not alternatives:
            continue
        if any(which(cmd) for cmd in alternatives):
            continue
        missing.append("|".join(alternatives))
    return missing


def apt_packages_for_missing_commands(missing_specs: Iterable[str]) -> List[str]:
    packages: List[str] = []
    for spec in missing_specs:
        alternatives = [item.strip() for item in str(spec or "").split("|") if item.strip()]
        package = ""
        for cmd in alternatives:
            mapped = _APT_PACKAGE_BY_COMMAND.get(cmd)
            if mapped:
                package = mapped
                break
        if package and package not in packages:
            packages.append(package)
    return packages


def agent_toolchain_status(env: Mapping[str, str] | None = None) -> Dict[str, object]:
    required = required_agent_toolchain_commands(env=env)
    missing = find_missing_agent_toolchain_commands(required)
    packages = apt_packages_for_missing_commands(missing)
    return {
        "required": required,
        "missing": missing,
        "missing_packages_hint": packages,
        "ready": len(missing) == 0,
    }
