"""
agent/guardrails.py
--------------------
Deterministic circuit breakers that execute BEFORE any LLM call or payment
link creation.  These are hard safety boundaries — not subject to AI judgment.

Circuit breakers (evaluated in priority order):
  1. HALT_DISPUTE_RAISED          — customer has an open chargeback / dispute
  2. HALT_RISK_POLICY             — transaction flagged as FRAUD_SUSPECTED
  3. HALT_MAX_RETRIES_EXCEEDED    — attempt_count >= 3 (payment fatigue limit)
  4. HALT_COOLDOWN_ACTIVE         — customer contacted within the last 24 hours

If all checks pass, the breaker returns status=PASSED and recovery proceeds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
MAX_RETRY_ATTEMPTS: int = 3
CONTACT_COOLDOWN_SECONDS: int = 24 * 3600  # 24 hours
FRAUD_ERROR_CODES: frozenset[str] = frozenset({"FRAUD_SUSPECTED"})


# ──────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────
class GuardrailStatus(str, Enum):
    PASSED = "PASSED"
    HALTED = "HALTED"


class HaltReason(str, Enum):
    DISPUTE_RAISED = "HALT_DISPUTE_RAISED"
    RISK_POLICY = "HALT_RISK_POLICY"
    MAX_RETRIES_EXCEEDED = "HALT_MAX_RETRIES_EXCEEDED"
    COOLDOWN_ACTIVE = "HALT_COOLDOWN_ACTIVE"


@dataclass
class GuardrailResult:
    """Immutable result returned by the circuit-breaker chain."""

    status: GuardrailStatus
    halt_reason: HaltReason | None = None
    protected_amount_inr: float = 0.0
    audit_notes: list[str] = field(default_factory=list)

    # Convenience properties
    @property
    def halted(self) -> bool:
        return self.status == GuardrailStatus.HALTED

    @property
    def passed(self) -> bool:
        return self.status == GuardrailStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "halt_reason": self.halt_reason.value if self.halt_reason else None,
            "protected_amount_inr": self.protected_amount_inr,
            "audit_notes": self.audit_notes,
        }


# ──────────────────────────────────────────────
# Individual breaker functions
# ──────────────────────────────────────────────

def _check_dispute(notes: dict, amount_inr: float) -> GuardrailResult | None:
    """Halt if the customer has raised a chargeback / dispute."""
    if notes.get("dispute_raised", False):
        return GuardrailResult(
            status=GuardrailStatus.HALTED,
            halt_reason=HaltReason.DISPUTE_RAISED,
            protected_amount_inr=amount_inr,
            audit_notes=[
                "Customer has an active dispute/chargeback. "
                "Recovery workflows are suspended to avoid legal exposure. "
                "Escalate to Disputes team."
            ],
        )
    return None


def _check_fraud(error_code: str, amount_inr: float) -> GuardrailResult | None:
    """Halt if the error is fraud-related — zero tolerance policy."""
    if error_code in FRAUD_ERROR_CODES:
        return GuardrailResult(
            status=GuardrailStatus.HALTED,
            halt_reason=HaltReason.RISK_POLICY,
            protected_amount_inr=amount_inr,
            audit_notes=[
                f"Error code '{error_code}' is flagged under Risk Policy. "
                "No automated recovery allowed. "
                "Transaction quarantined for manual risk review."
            ],
        )
    return None


def _check_max_retries(notes: dict, amount_inr: float) -> GuardrailResult | None:
    """Halt if attempt_count has reached or exceeded the maximum retry cap."""
    attempt_count = int(notes.get("attempt_count", 0))
    if attempt_count >= MAX_RETRY_ATTEMPTS:
        return GuardrailResult(
            status=GuardrailStatus.HALTED,
            halt_reason=HaltReason.MAX_RETRIES_EXCEEDED,
            protected_amount_inr=0.0,  # Not protecting fraud — escaping retry loop
            audit_notes=[
                f"attempt_count={attempt_count} has reached the hard cap of "
                f"{MAX_RETRY_ATTEMPTS}. "
                "Automated recovery halted to prevent payment fatigue and "
                "customer spam. Route to MANUAL_ESCALATION queue."
            ],
        )
    return None


def _check_cooldown(notes: dict, amount_inr: float) -> GuardrailResult | None:
    """Halt if the customer was already contacted in the last 24 hours."""
    last_ts = notes.get("last_contacted_timestamp")
    if last_ts is None:
        return None  # Never contacted — safe to proceed

    now = int(time.time())
    seconds_since = now - int(last_ts)
    if seconds_since < CONTACT_COOLDOWN_SECONDS:
        hours_remaining = round((CONTACT_COOLDOWN_SECONDS - seconds_since) / 3600, 1)
        return GuardrailResult(
            status=GuardrailStatus.HALTED,
            halt_reason=HaltReason.COOLDOWN_ACTIVE,
            protected_amount_inr=0.0,
            audit_notes=[
                f"Customer was last contacted {round(seconds_since/3600, 1)}h ago "
                f"(cooldown window = 24h). "
                f"Recovery messaging suppressed for {hours_remaining}h more. "
                "Re-queue after cooldown expires."
            ],
        )
    return None


# ──────────────────────────────────────────────
# Public API: run_guardrails
# ──────────────────────────────────────────────

def run_guardrails(payment_entity: dict) -> GuardrailResult:
    """
    Evaluate all circuit breakers against a Razorpay payment entity dict.

    Breakers are executed in priority order (most severe first).
    Returns on the first HALT; returns PASSED if all checks clear.

    Args:
        payment_entity: The `payload.payment.entity` dict from the webhook.

    Returns:
        GuardrailResult with status PASSED or HALTED.
    """
    notes: dict = payment_entity.get("notes", {})
    error_code: str = payment_entity.get("error_code", "")
    amount_inr: float = payment_entity.get("amount", 0) / 100.0

    # Priority 1: Dispute (highest severity)
    result = _check_dispute(notes, amount_inr)
    if result:
        return result

    # Priority 2: Fraud risk policy
    result = _check_fraud(error_code, amount_inr)
    if result:
        return result

    # Priority 3: Max retry cap
    result = _check_max_retries(notes, amount_inr)
    if result:
        return result

    # Priority 4: 24-hour contact cooldown
    result = _check_cooldown(notes, amount_inr)
    if result:
        return result

    # All clear
    return GuardrailResult(
        status=GuardrailStatus.PASSED,
        halt_reason=None,
        protected_amount_inr=0.0,
        audit_notes=["All circuit breakers passed. Proceeding to recovery engine."],
    )
