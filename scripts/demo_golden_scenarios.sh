#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONPATH="${ROOT_DIR}/src"

echo "[1/4] write+verify scenario"
python3 - <<'PY'
import asyncio
import tempfile
from pathlib import Path

from codex_telegram_bot.execution.local_shell import LocalShellRunner

async def main():
    root = Path(tempfile.mkdtemp(prefix="golden-write-"))
    runner = LocalShellRunner()
    create = await runner.run(
        ["bash", "-lc", "mkdir -p demo && printf 'hello\n' > demo/hello.txt"],
        workspace_root=str(root),
        policy_profile="trusted",
    )
    assert create.returncode == 0, create.stderr
    target = root / "demo" / "hello.txt"
    assert target.exists(), "verification failed: file was not created"
    data = target.read_text(encoding="utf-8")
    assert data == "hello\n", f"verification failed: unexpected content: {data!r}"
    print("verified:", str(target))

asyncio.run(main())
PY

echo "[2/4] internet-disabled scenario"
python3 - <<'PY'
import os
import tempfile
from pathlib import Path

from codex_telegram_bot.services.mcp_bridge import McpBridge

os.environ["MCP_DISABLE_HTTP"] = "true"
bridge = McpBridge(workspace_root=Path(tempfile.mkdtemp(prefix="golden-mcp-")))
try:
    bridge.register_server("http://insecure.example.com/mcp", name="blocked")
except ValueError as exc:
    assert "HTTP" in str(exc), str(exc)
    print("verified: insecure MCP HTTP endpoint blocked")
else:
    raise SystemExit("verification failed: insecure MCP endpoint was allowed")
PY

echo "[3/4] email-disabled scenario"
ENABLE_EMAIL_TOOL=0 SMTP_HOST= SMTP_USER= SMTP_APP_PASSWORD= python3 - <<'PY'
from codex_telegram_bot.tools.email import email_tool_enabled

assert email_tool_enabled() is False, "verification failed: email tool should be disabled"
print("verified: SMTP email tool disabled cleanly")
PY

echo "[4/4] sandboxed mode (Docker backend dry-run)"
DOCKER_SANDBOX_DRY_RUN=1 python3 - <<'PY'
import asyncio
import tempfile
from pathlib import Path

from codex_telegram_bot.execution.docker_sandbox import DockerSandboxRunner
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver

async def main():
    root = Path(tempfile.mkdtemp(prefix="golden-docker-"))
    runner = DockerSandboxRunner(profile_resolver=ExecutionProfileResolver(root))
    out = await runner.run(["echo", "sandbox-ok"], workspace_root=str(root), policy_profile="trusted")
    if out.returncode == 126 and "docker binary is not available" in out.stderr:
        print("verified: docker backend selected and blocked cleanly when docker is absent")
        return
    assert out.returncode == 0, out.stderr
    assert "docker run --rm" in out.stdout, out.stdout
    assert "--network none" in out.stdout, out.stdout
    print("verified: docker sandbox command rendered")

asyncio.run(main())
PY

echo "All golden scenarios passed."
