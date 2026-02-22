from dataclasses import dataclass
from typing import Sequence


VALID_POLICY_PROFILES = {"strict", "balanced", "trusted"}

_HIGH_RISK_FLAGS = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--danger-full-access",
    "--sandbox=danger-full-access",
    "--yolo",
}
_MEDIUM_RISK_FLAGS = {
    "--sandbox=workspace-write",
    "--sandbox=read-only",
}
_RESTRICTED_BINARIES = {"codex"}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    risk_tier: str
    reason: str


class ExecutionPolicyEngine:
    def evaluate(self, argv: Sequence[str], policy_profile: str = "balanced") -> PolicyDecision:
        profile = self.normalize_profile(policy_profile)
        if not argv:
            return PolicyDecision(allowed=False, risk_tier="high", reason="Empty command.")

        command = (argv[0] or "").strip()
        if not command:
            return PolicyDecision(allowed=False, risk_tier="high", reason="Invalid command.")

        risk_tier = self._risk_tier(argv)
        if profile in {"strict", "balanced"} and command not in _RESTRICTED_BINARIES:
            return PolicyDecision(
                allowed=False,
                risk_tier="high",
                reason=f"Command '{command}' is not allowed in '{profile}' profile.",
            )
        if profile == "strict" and risk_tier in {"medium", "high"}:
            return PolicyDecision(
                allowed=False,
                risk_tier=risk_tier,
                reason=f"Risk tier '{risk_tier}' denied by strict profile.",
            )
        if profile == "balanced" and risk_tier == "high":
            return PolicyDecision(
                allowed=False,
                risk_tier=risk_tier,
                reason="High-risk command denied by balanced profile.",
            )
        return PolicyDecision(allowed=True, risk_tier=risk_tier, reason="Allowed.")

    def normalize_profile(self, policy_profile: str) -> str:
        normalized = (policy_profile or "").strip().lower()
        if normalized not in VALID_POLICY_PROFILES:
            return "balanced"
        return normalized

    def _risk_tier(self, argv: Sequence[str]) -> str:
        for token in argv:
            if token in _HIGH_RISK_FLAGS:
                return "high"
        for token in argv:
            if token in _MEDIUM_RISK_FLAGS:
                return "medium"
        return "low"
