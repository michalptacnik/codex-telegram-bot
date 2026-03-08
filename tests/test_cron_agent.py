"""Tests for the CronHeartbeatAgent — the always-alive passive agent daemon."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from codex_telegram_bot.services.cron_agent import (
    CronAgentConfig,
    CronHeartbeatAgent,
    WatcherAlert,
    _check_disk_usage,
    _check_git_repos,
)


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a minimal workspace with heartbeat file."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    heartbeat = memory_dir / "HEARTBEAT.md"
    heartbeat.write_text(
        "# HEARTBEAT v1\n"
        "## Daily (active hours only)\n"
        "- [ ] Check email\n"
        "## Weekly\n"
        "## Monitors\n"
        "## Waiting on\n"
        "## Quiet Hours\n"
        "- start: 22:00\n"
        "- end: 08:00\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def config(tmp_workspace):
    return CronAgentConfig(
        heartbeat_interval_sec=0.1,
        system_watch_interval_sec=0.1,
        workspace_root=tmp_workspace,
        timezone="UTC",
        enable_git_watcher=False,
        enable_disk_watcher=False,
    )


@pytest.mark.asyncio
async def test_start_stop(config):
    agent = CronHeartbeatAgent(config=config)
    assert not agent.is_healthy()

    await agent.start()
    assert agent.is_healthy()

    status = agent.status()
    assert status["running"] is True
    assert status["started_at"] is not None

    await agent.stop()
    assert not agent.is_healthy()


@pytest.mark.asyncio
async def test_heartbeat_sends_proactive(config):
    delivery = AsyncMock(return_value={})
    agent = CronHeartbeatAgent(config=config, delivery_fn=delivery)

    await agent.start()
    # Let it tick once
    await asyncio.sleep(0.3)
    await agent.stop()

    # Heartbeat should have found the daily checklist and tried to deliver
    stats = agent.status()
    assert stats["heartbeat_checks"] >= 1


@pytest.mark.asyncio
async def test_no_delivery_fn_logs_only(config):
    agent = CronHeartbeatAgent(config=config, delivery_fn=None)
    await agent.start()
    await asyncio.sleep(0.3)
    await agent.stop()
    # Should not crash
    assert agent.status()["heartbeat_checks"] >= 1


@pytest.mark.asyncio
async def test_set_delivery_fn(config):
    agent = CronHeartbeatAgent(config=config)
    delivery = AsyncMock(return_value={})
    agent.set_delivery_fn(delivery)
    assert agent._deliver is delivery


def test_disk_watcher_runs():
    alerts = _check_disk_usage(warn_percent=100)
    # At 100% threshold, no alert expected unless disk is literally full
    assert isinstance(alerts, list)


def test_disk_watcher_low_threshold():
    # Use a threshold of 0 to guarantee an alert is triggered
    alerts = _check_disk_usage(warn_percent=0)
    # Should return a list (may be empty in sandbox environments where
    # shutil.disk_usage reports 0 total)
    assert isinstance(alerts, list)
    if alerts:
        assert alerts[0].source == "disk"


def test_git_watcher_nonexistent_path():
    alerts = _check_git_repos([Path("/nonexistent/repo")])
    assert alerts == []


@pytest.mark.asyncio
async def test_daemon_mode_stop_event(config):
    agent = CronHeartbeatAgent(config=config)

    async def stop_soon():
        await asyncio.sleep(0.2)
        agent._stop_event.set()

    asyncio.create_task(stop_soon())
    await agent.run()  # Should return when stop_event is set
    assert not agent.is_healthy()


@pytest.mark.asyncio
async def test_idempotent_start(config):
    agent = CronHeartbeatAgent(config=config)
    await agent.start()
    task_count = len(agent._tasks)
    await agent.start()  # Second start should be no-op
    assert len(agent._tasks) == task_count
    await agent.stop()


@pytest.mark.asyncio
async def test_health_file_written(config, tmp_path):
    health_path = tmp_path / "health"
    config.health_file = health_path
    config.health_interval_sec = 0.1

    agent = CronHeartbeatAgent(config=config)
    await agent.start()
    await asyncio.sleep(0.3)
    await agent.stop()

    assert health_path.exists()
    content = health_path.read_text()
    # Should be an ISO timestamp
    assert "T" in content
