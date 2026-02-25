"""Mission planner and task decomposition (EPIC 6, issue #77).

Given a high-level goal string, the planner calls the provider to produce
an ordered list of concrete steps (a MissionPlan).  If the provider is
unavailable the planner falls back to a single-step passthrough plan so
the execution loop can always proceed.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Sequence

from codex_telegram_bot.domain.contracts import ProviderAdapter
from codex_telegram_bot.domain.missions import MissionPlan, MissionStep

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a task decomposition engine. Given a high-level goal, output a JSON
array of steps. Each step is an object with:
  - "index": integer starting at 0
  - "description": a single, concrete, actionable sentence
  - "tool_hint": one of "shell_exec", "read_file", "write_file", "git_status",
    "git_diff", "git_log", "git_add", "git_commit", "ssh_detect", or "" if unknown

Return ONLY the JSON array. No prose, no markdown fences. Example:
[
  {"index": 0, "description": "List files in the workspace", "tool_hint": "shell_exec"},
  {"index": 1, "description": "Read the README", "tool_hint": "read_file"}
]
""".strip()

_MAX_STEPS = 20
_FALLBACK_TOOL_HINT = "shell_exec"


def _extract_json_array(text: str) -> Optional[list]:
    """Try to extract the first JSON array from text (handles extra prose)."""
    # Strip markdown fences
    text = re.sub(r"```[a-z]*\n?", "", text).strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _parse_steps(raw: list) -> List[MissionStep]:
    steps: List[MissionStep] = []
    for item in raw[:_MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        idx = int(item.get("index", len(steps)))
        desc = str(item.get("description") or "").strip()
        hint = str(item.get("tool_hint") or "").strip()
        if not desc:
            continue
        steps.append(MissionStep(index=idx, description=desc, tool_hint=hint))
    return steps


class MissionPlanner:
    """Decompose a mission goal into an ordered list of steps."""

    def __init__(self, provider: ProviderAdapter) -> None:
        self._provider = provider

    async def plan(self, mission_id: str, goal: str, context: Optional[dict] = None) -> MissionPlan:
        """Call the provider to decompose goal into steps.

        Falls back to a single passthrough step on error.
        """
        user_message = goal
        if context:
            user_message = f"Context:\n{json.dumps(context, indent=2)}\n\nGoal: {goal}"
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        try:
            raw_output = await self._provider.generate(messages=messages, stream=False)
        except Exception as exc:
            logger.warning("mission=%s planner provider error: %s", mission_id, exc)
            return self._fallback_plan(mission_id, goal)

        parsed = _extract_json_array(raw_output or "")
        if not parsed:
            logger.warning(
                "mission=%s planner returned non-parseable output, using fallback",
                mission_id,
            )
            return self._fallback_plan(mission_id, goal)

        steps = _parse_steps(parsed)
        if not steps:
            return self._fallback_plan(mission_id, goal)

        return MissionPlan(mission_id=mission_id, goal=goal, steps=steps)

    def plan_from_steps(
        self,
        mission_id: str,
        goal: str,
        step_descriptions: Sequence[str],
    ) -> MissionPlan:
        """Build a plan directly from a list of description strings (no LLM call)."""
        steps = [
            MissionStep(index=i, description=desc.strip(), tool_hint="")
            for i, desc in enumerate(step_descriptions)
            if desc.strip()
        ]
        return MissionPlan(mission_id=mission_id, goal=goal, steps=steps or [
            MissionStep(index=0, description=goal, tool_hint=_FALLBACK_TOOL_HINT)
        ])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_plan(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(
            mission_id=mission_id,
            goal=goal,
            steps=[MissionStep(index=0, description=goal, tool_hint=_FALLBACK_TOOL_HINT)],
        )
