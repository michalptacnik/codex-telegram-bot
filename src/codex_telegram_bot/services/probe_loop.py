"""Probe-first tool selection loop.

The ProbeLoop adds a lightweight PROBE step before any tool execution:

1. PROBE step: send a mini-inference asking the model whether tools are needed.
2. Parse PROBE output:
   - ``NO_TOOLS\\n<answer>``  → return the answer directly (no tools run).
   - ``NEED_TOOLS {...}``     → extract ``allowed_tools``, run one tool loop.
3. Hard tool gating: any tool not in ``allowed_tools`` is blocked; one REPAIR
   attempt is made; if still invalid the loop exits and a final answer is
   generated from whatever observations exist.

Tool catalog injected at PROBE time is capped at ``CATALOG_BUDGET_CHARS``
to keep the prompt small.  Tool schemas injected for the execution step are
capped at ``SCHEMA_BUDGET_CHARS``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from codex_telegram_bot.domain.contracts import ProviderAdapter
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.tools.base import ToolContext, ToolRegistry, ToolRequest

logger = logging.getLogger(__name__)

CATALOG_BUDGET_CHARS = 200
SCHEMA_BUDGET_CHARS = 800
MAX_PROBE_OUTPUT_CHARS = 1200
MAX_TOOL_STEPS = 10

_PROBE_SYSTEM_TEMPLATE = (
    "You are a task router. Decide whether tool execution is needed.\n"
    "Available tools: {catalog}\n\n"
    "Output EXACTLY ONE of:\n"
    "  NO_TOOLS\\n<your complete answer>\n"
    "or:\n"
    '  NEED_TOOLS {{"tools":["tool1","tool2"],"goal":"...","max_steps":N,"done_when":"..."}}\n\n'
    "Rules:\n"
    "- Prefer NO_TOOLS for factual, explanatory, or conversational prompts.\n"
    "- Use NEED_TOOLS only when file I/O, shell commands, git, or external\n"
    "  calls are truly required to answer correctly.\n"
    "- List only tools from the catalog; never invent new tool names.\n"
    "- Never add explanations outside the required format.\n"
)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    kind: str  # "NO_TOOLS" | "NEED_TOOLS"
    answer: str = ""  # direct answer when kind == "NO_TOOLS"
    tools: List[str] = field(default_factory=list)
    goal: str = ""
    max_steps: int = 3
    done_when: str = ""


@dataclass
class ProbeRunResult:
    answer: str
    probe: ProbeResult
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    warning: str = ""


# ---------------------------------------------------------------------------
# Tool catalog / schema helpers
# ---------------------------------------------------------------------------

def build_tool_catalog(registry: ToolRegistry, budget: int = CATALOG_BUDGET_CHARS) -> str:
    """Build a compact ≤budget chars comma-separated tool name list."""
    names = registry.names()
    if not names:
        return "(none)"
    catalog = ", ".join(names)
    if len(catalog) <= budget:
        return catalog
    result = ""
    for name in names:
        candidate = (result + ", " + name) if result else name
        if len(candidate) > budget - 4:
            result = candidate[:budget - 4] + "..."
            break
        result = candidate
    return result or names[0]


def build_tool_schemas(
    registry: ToolRegistry,
    allowed: Set[str],
    budget: int = SCHEMA_BUDGET_CHARS,
) -> List[Dict[str, Any]]:
    """Build minimal JSON schemas for ``allowed`` tools, respecting char budget."""
    schemas: List[Dict[str, Any]] = []
    used = 0
    for name in sorted(allowed):
        tool = registry.get(name)
        if not tool:
            continue
        schema = _make_tool_schema(tool)
        encoded = json.dumps(schema)
        if used + len(encoded) > budget:
            break
        schemas.append(schema)
        used += len(encoded)
    return schemas


def _make_tool_schema(tool: Any) -> Dict[str, Any]:
    name = getattr(tool, "name", "") or ""
    doc = getattr(tool, "description", "") or getattr(tool, "__doc__", "") or ""
    description = doc.strip().split("\n")[0][:200] if doc else ""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    }


def tool_schema_line(name: str, tool: Any) -> str:
    """Return a one-line schema description for inline prompt injection."""
    doc = getattr(tool, "description", "") or getattr(tool, "__doc__", "") or ""
    description = doc.strip().split("\n")[0][:120] if doc else ""
    return f"  {name}: {description}" if description else f"  {name}"


# ---------------------------------------------------------------------------
# PROBE step
# ---------------------------------------------------------------------------

async def probe(
    prompt: str,
    provider: ProviderAdapter,
    catalog: str,
    correlation_id: str = "",
) -> ProbeResult:
    """Run the PROBE step: ask the model to decide if tools are needed."""
    system = _PROBE_SYSTEM_TEMPLATE.format(catalog=catalog)
    probe_prompt = system + "\n\nUser task:\n" + prompt[:3000]
    messages = [{"role": "user", "content": probe_prompt}]
    raw = await provider.generate(
        messages=messages,
        stream=False,
        correlation_id=correlation_id,
    )
    raw = (raw or "").strip()[:MAX_PROBE_OUTPUT_CHARS]
    log_json(logger, "probe.result.raw", run_id=correlation_id, preview=raw[:200])
    return _parse_probe_output(raw)


def _parse_probe_output(raw: str) -> ProbeResult:
    """Parse PROBE model output into a ProbeResult."""
    if raw.startswith("NO_TOOLS"):
        answer = raw[len("NO_TOOLS"):].lstrip("\n").strip()
        return ProbeResult(kind="NO_TOOLS", answer=answer)
    if raw.startswith("NEED_TOOLS"):
        rest = raw[len("NEED_TOOLS"):].strip()
        m = re.search(r"\{.*\}", rest, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group())
                tools = [str(t).strip() for t in (obj.get("tools") or []) if str(t).strip()]
                return ProbeResult(
                    kind="NEED_TOOLS",
                    tools=tools,
                    goal=str(obj.get("goal") or "").strip(),
                    max_steps=max(1, int(obj.get("max_steps") or 3)),
                    done_when=str(obj.get("done_when") or "").strip(),
                )
            except Exception:
                pass
        logger.warning("probe: malformed NEED_TOOLS output — falling back to NO_TOOLS")
        return ProbeResult(kind="NO_TOOLS", answer=raw)
    # Unexpected format: treat as direct answer
    logger.warning("probe: unexpected output format — treating as NO_TOOLS answer")
    return ProbeResult(kind="NO_TOOLS", answer=raw)


# ---------------------------------------------------------------------------
# Main ProbeLoop service
# ---------------------------------------------------------------------------

class ProbeLoop:
    """Wraps a provider with PROBE-first tool selection and hard tool gating.

    Usage::

        loop = ProbeLoop(provider=provider, tool_registry=registry)
        result = await loop.run(prompt="...", workspace_root=Path(...))
        print(result.answer)
    """

    def __init__(
        self,
        provider: ProviderAdapter,
        tool_registry: ToolRegistry,
        max_repair_attempts: int = 1,
    ) -> None:
        self._provider = provider
        self._registry = tool_registry
        self._max_repair = max_repair_attempts

    def tool_catalog(self) -> str:
        return build_tool_catalog(self._registry)

    async def run(
        self,
        prompt: str,
        workspace_root: Optional[Path] = None,
        correlation_id: str = "",
        allowed_tools_override: Optional[Set[str]] = None,
    ) -> ProbeRunResult:
        """Run PROBE → tool gate → tool loop → final answer."""
        ws = workspace_root or Path.cwd()

        catalog = self.tool_catalog()
        result = await probe(
            prompt=prompt,
            provider=self._provider,
            catalog=catalog,
            correlation_id=correlation_id,
        )
        log_json(logger, "probe.decision", run_id=correlation_id, kind=result.kind,
                 tools=result.tools)

        if result.kind == "NO_TOOLS":
            return ProbeRunResult(answer=result.answer, probe=result)

        # NEED_TOOLS path
        allowed: Set[str] = allowed_tools_override or set(result.tools)
        # Hard gate: only keep tools that are actually registered
        registered = set(self._registry.names())
        allowed = allowed & registered

        if not allowed:
            # No valid tools — fall through to a direct answer
            final = await self._provider.generate(
                messages=[{"role": "user", "content": prompt}],
                correlation_id=correlation_id,
            )
            return ProbeRunResult(
                answer=final or "",
                probe=result,
                warning="probe requested tools but none were registered",
            )

        ctx = ToolContext(workspace_root=ws)
        tool_results: List[Dict[str, Any]] = []
        steps_remaining = min(result.max_steps, MAX_TOOL_STEPS)

        for _step in range(steps_remaining):
            tool_schemas = build_tool_schemas(self._registry, allowed)
            schema_lines = [tool_schema_line(s["name"], self._registry.get(s["name"])) for s in tool_schemas]
            tool_info = "\n".join(schema_lines)

            tool_prompt = (
                f"Goal: {result.goal or prompt}\n"
                f"Allowed tools (ONLY these):\n{tool_info}\n"
                f"Previous results:\n{_format_tool_results(tool_results)}\n\n"
                f"Respond with EXACTLY ONE of:\n"
                f"  !tool {{\"name\": \"<tool>\", \"args\": {{...}}}}\n"
                f"  DONE: <your final answer>  (use when: {result.done_when or 'goal is complete'})"
            )
            resp = await self._provider.generate(
                messages=[{"role": "user", "content": tool_prompt}],
                correlation_id=correlation_id,
            )
            resp = (resp or "").strip()

            # Check for DONE signal
            if resp.upper().startswith("DONE"):
                colon_pos = resp.find(":")
                answer = resp[colon_pos + 1:].strip() if colon_pos != -1 else resp[4:].strip()
                return ProbeRunResult(
                    answer=answer,
                    probe=result,
                    tool_results=tool_results,
                )

            # Parse !tool directive
            tool_call = _parse_tool_directive(resp)
            if not tool_call:
                # One REPAIR attempt
                repair_resp = await self._provider.generate(
                    messages=[{"role": "user", "content": (
                        f"Your last response was not a valid tool call:\n{resp[:300]}\n\n"
                        f"Respond with EXACTLY: !tool {{\"name\": \"<tool>\", \"args\": {{...}}}}\n"
                        f"Or: DONE: <final answer>"
                    )}],
                    correlation_id=correlation_id,
                )
                tool_call = _parse_tool_directive((repair_resp or "").strip())
                if not tool_call:
                    logger.warning("probe_loop: REPAIR failed — exiting tool loop")
                    break

            tool_name = tool_call.get("name", "")

            # Hard tool gate
            if tool_name not in allowed:
                tool_results.append({
                    "tool": tool_name,
                    "error": f"BLOCKED: '{tool_name}' not in allowed_tools",
                })
                log_json(logger, "probe_loop.tool.blocked", run_id=correlation_id, tool=tool_name)
                break

            # Execute the tool
            tool = self._registry.get(tool_name)
            if not tool:
                tool_results.append({"tool": tool_name, "error": "tool not found in registry"})
                break
            try:
                tr = tool.run(
                    ToolRequest(name=tool_name, args=dict(tool_call.get("args") or {})),
                    ctx,
                )
                tool_results.append({
                    "tool": tool_name,
                    "ok": tr.ok,
                    "output": tr.output[:800],
                })
                log_json(logger, "probe_loop.tool.executed", run_id=correlation_id,
                         tool=tool_name, ok=tr.ok)
                if not tr.ok:
                    break
            except Exception as exc:
                tool_results.append({"tool": tool_name, "error": str(exc)})
                logger.exception("probe_loop: tool execution error for %s", tool_name)
                break

        # Generate final answer from observations
        obs = _format_tool_results(tool_results)
        final_prompt = (
            f"Task: {result.goal or prompt}\n"
            f"Tool observations:\n{obs}\n\n"
            "Provide a complete answer based on the above results."
        )
        final = await self._provider.generate(
            messages=[{"role": "user", "content": final_prompt}],
            correlation_id=correlation_id,
        )
        return ProbeRunResult(
            answer=final or "",
            probe=result,
            tool_results=tool_results,
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_tool_directive(text: str) -> Optional[Dict[str, Any]]:
    """Parse ``!tool {"name": "...", "args": {...}}`` from model output."""
    if not text.startswith("!tool "):
        return None
    body = text[len("!tool "):].strip()
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            name = str(obj.get("name") or obj.get("tool") or "").strip()
            args = obj.get("args")
            if name and isinstance(args, dict):
                return {"name": name, "args": args}
    except Exception:
        pass
    return None


def _format_tool_results(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "(none)"
    parts: List[str] = []
    for r in results:
        tool = r.get("tool", "?")
        if "error" in r:
            parts.append(f"[{tool}] ERROR: {r['error']}")
        else:
            status = "ok" if r.get("ok") else "failed"
            parts.append(f"[{tool}] {status}: {r.get('output', '')[:400]}")
    return "\n".join(parts)
