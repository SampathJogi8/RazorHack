"""
agent/scenarios/subscription_recovery.py
------------------------------------------
Subscription Failure Recovery Scenario.

Handles failed recurring subscription payments (card declines, mandate failures,
expired cards, revoked eNACH mandates).

Includes a Mandate Retry Sequencer that generates an optimised retry schedule
with exponential backoff, avoiding weekends and end-of-month low-balance periods.

Guardrails (deterministic, pre-AI):
  - Subscription already cancelled → HALT_SUBSCRIPTION_CANCELLED
  - Dunning attempts >= 4 → HALT_MAX_DUNNING_REACHED (suspend)
  - Mandate explicitly revoked → HALT_MANDATE_REVOKED
  - Failure flagged as fraud → HALT_RISK_POLICY

Recovery actions:
  - RETRY_IMMEDIATE         → Transient error; retry right now
  - RETRY_SCHEDULED         → Retry in N hours with optimised schedule
  - PAYMENT_METHOD_UPDATE   → Request new card/UPI from customer
  - GRACE_PERIOD_EXTENSION  → Extend access while customer updates method
  - SUSPENSION_WARNING      → Final dunning notice before account suspend
  - NO_ACTION_HALTED        → Guardrail blocked
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
MAX_DUNNING_ATTEMPTS: int = 4
RETRY_BACKOFF_HOURS = [1, 24, 72, 168]   # 1h, 1d, 3d, 7d
GRACE_PERIOD_DAYS: int = 3


# ──────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────

class SubscriptionAction(str, Enum):
    RETRY_IMMEDIATE        = "RETRY_IMMEDIATE"
    RETRY_SCHEDULED        = "RETRY_SCHEDULED"
    PAYMENT_METHOD_UPDATE  = "PAYMENT_METHOD_UPDATE"
    GRACE_PERIOD_EXTENSION = "GRACE_PERIOD_EXTENSION"
    SUSPENSION_WARNING     = "SUSPENSION_WARNING"
    NO_ACTION_HALTED       = "NO_ACTION_HALTED"


@dataclass
class SubGuardrailResult:
    passed: bool
    halt_reason: Optional[str] = None
    audit_notes: list[str] = field(default_factory=list)


class RetrySlot(BaseModel):
    attempt_number: int
    scheduled_at_iso: str
    scheduled_at_ts: int
    reason: str


class SubscriptionRecoveryPlan(BaseModel):
    subscription_id: str
    customer_name: str
    plan_name: str
    mrr_inr: float
    failure_reason: str
    dunning_attempt: int
    diagnosis: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    recommended_action: SubscriptionAction
    retry_schedule: list[RetrySlot] = Field(default_factory=list)
    update_link: Optional[str] = None
    grace_period_days: Optional[int] = None
    customer_message: Optional[str] = None
    audit_reasoning: str
    diagnosed_by: str


# ──────────────────────────────────────────────
# Mandate Retry Sequencer
# ──────────────────────────────────────────────

def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5  # Saturday=5, Sunday=6


def _is_month_end(dt: datetime) -> bool:
    """Avoid last 3 days of month — low-balance period."""
    import calendar
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    return dt.day >= last_day - 2


def _next_safe_window(from_dt: datetime, offset_hours: int) -> datetime:
    """
    Add offset_hours to from_dt and then push forward past weekends
    and month-end windows to find the safest retry time.
    """
    candidate = from_dt + timedelta(hours=offset_hours)
    # Push forward until it's a safe window (max 3 iterations)
    for _ in range(3):
        if _is_weekend(candidate) or _is_month_end(candidate):
            candidate += timedelta(days=1)
        else:
            break
    # Target 10am IST (04:30 UTC) — banks most liquid mid-morning
    candidate = candidate.replace(hour=4, minute=30, second=0, microsecond=0)
    return candidate


def generate_retry_schedule(
    dunning_attempt: int,
    failure_reason: str,
) -> list[RetrySlot]:
    """
    Generate the remaining retry slots starting from dunning_attempt.

    For GATEWAY_ERROR / transient errors, first slot is immediate (1h).
    For card/method errors, first slot is delayed (24h).
    """
    now = datetime.now(timezone.utc)
    slots: list[RetrySlot] = []

    transient_errors = {"GATEWAY_ERROR", "NETWORK_ERROR", "BANK_TIMEOUT"}
    start_idx = dunning_attempt  # already made `dunning_attempt` attempts

    backoffs = RETRY_BACKOFF_HOURS[start_idx:]
    if failure_reason in transient_errors:
        backoffs = [1] + backoffs  # immediate retry for transient

    for i, hours in enumerate(backoffs):
        attempt_num = start_idx + i + 1
        if attempt_num > MAX_DUNNING_ATTEMPTS:
            break
        slot_dt = _next_safe_window(now, hours)
        slots.append(
            RetrySlot(
                attempt_number=attempt_num,
                scheduled_at_iso=slot_dt.isoformat(),
                scheduled_at_ts=int(slot_dt.timestamp()),
                reason=f"Attempt {attempt_num}: backoff after {hours}h — "
                       f"targeting mid-morning bank liquidity window",
            )
        )

    return slots


# ──────────────────────────────────────────────
# Guardrails
# ──────────────────────────────────────────────

def run_subscription_guardrails(event: dict) -> SubGuardrailResult:
    cancelled = event.get("cancelled", False)
    dunning_attempt = int(event.get("dunning_attempt", 0))
    failure_reason = event.get("failure_reason", "")
    mandate_revoked = event.get("mandate_revoked", False)

    if cancelled:
        return SubGuardrailResult(
            passed=False,
            halt_reason="HALT_SUBSCRIPTION_CANCELLED",
            audit_notes=["Subscription is already cancelled — no recovery action possible."],
        )

    if mandate_revoked:
        return SubGuardrailResult(
            passed=False,
            halt_reason="HALT_MANDATE_REVOKED",
            audit_notes=[
                "eNACH/NACH mandate has been explicitly revoked by the customer. "
                "Automated retry would fail at the bank level. Requires customer action."
            ],
        )

    if dunning_attempt >= MAX_DUNNING_ATTEMPTS:
        return SubGuardrailResult(
            passed=False,
            halt_reason="HALT_MAX_DUNNING_REACHED",
            audit_notes=[
                f"dunning_attempt={dunning_attempt} has reached the maximum "
                f"of {MAX_DUNNING_ATTEMPTS}. Subscription will be suspended. "
                "Escalate to retention team."
            ],
        )

    if "FRAUD" in failure_reason.upper():
        return SubGuardrailResult(
            passed=False,
            halt_reason="HALT_RISK_POLICY",
            audit_notes=[
                f"Failure reason '{failure_reason}' indicates a potential fraud signal. "
                "Automated recovery halted for manual risk review."
            ],
        )

    return SubGuardrailResult(
        passed=True,
        audit_notes=["All subscription guardrails passed."],
    )


# ──────────────────────────────────────────────
# Recovery Rules
# ──────────────────────────────────────────────

_FAILURE_RULES = {
    "INSUFFICIENT_FUNDS": {
        "diagnosis": "Insufficient balance in the linked bank account at the time of mandate "
                     "debit. This is a temporary cash-flow issue — the customer likely has funds "
                     "later in the month.",
        "confidence": 0.85,
        "action": SubscriptionAction.RETRY_SCHEDULED,
        "message": (
            "Hi {name}, your {plan} subscription payment of ₹{amount} couldn't go through "
            "today. We'll retry automatically — no action needed! Or update your payment "
            "method here: {link}"
        ),
        "reasoning": "Insufficient funds is usually a timing issue. Schedule retry "
                     "in a safe bank window. Also send update link as fallback.",
    },
    "CARD_EXPIRED": {
        "diagnosis": "The card linked to this subscription has expired. "
                     "No amount of retries will succeed — customer must update their payment method.",
        "confidence": 0.97,
        "action": SubscriptionAction.PAYMENT_METHOD_UPDATE,
        "message": (
            "Hi {name}, your {plan} subscription (₹{amount}/mo) failed because your card "
            "has expired. Update your payment method to continue uninterrupted: {link}"
        ),
        "reasoning": "Expired card is a permanent, user-correctable failure. "
                     "Immediate payment method update request is the only viable recovery.",
    },
    "CARD_DECLINED": {
        "diagnosis": "Card declined by issuing bank. Could be velocity limit, security hold, "
                     "or insufficient balance. Not the same as expired.",
        "confidence": 0.78,
        "action": SubscriptionAction.RETRY_SCHEDULED,
        "message": (
            "Hi {name}, we couldn't process your {plan} renewal (₹{amount}). "
            "We'll retry in 24 hours. To avoid interruption, you can also update "
            "your payment method: {link}"
        ),
        "reasoning": "Card decline is often transient. Schedule retry with 24h backoff. "
                     "Offer payment method update as parallel path.",
    },
    "BANK_AUTHENTICATION_FAILED": {
        "diagnosis": "3DS/OTP authentication failed or timed out. "
                     "For mandates, this shouldn't happen — may indicate a bank integration issue.",
        "confidence": 0.70,
        "action": SubscriptionAction.RETRY_SCHEDULED,
        "message": (
            "Hi {name}, there was a temporary issue processing your {plan} renewal. "
            "We'll retry shortly. If it keeps failing, update your payment method: {link}"
        ),
        "reasoning": "Auth failure on mandates is unusual — likely transient bank issue. "
                     "Retry with 1h backoff first.",
    },
    "GATEWAY_ERROR": {
        "diagnosis": "Payment gateway infrastructure error — transient and not customer-caused.",
        "confidence": 0.91,
        "action": SubscriptionAction.RETRY_IMMEDIATE,
        "message": None,  # No customer contact needed for silent retry
        "reasoning": "Gateway errors are transient. Silent immediate retry expected to succeed.",
    },
    "NETWORK_TIMEOUT": {
        "diagnosis": "Network timeout between gateway and bank. Transient infrastructure issue.",
        "confidence": 0.88,
        "action": SubscriptionAction.RETRY_IMMEDIATE,
        "message": None,
        "reasoning": "Timeout errors resolve on retry. Immediate silent retry appropriate.",
    },
}

_DEFAULT_FAILURE_RULE = {
    "diagnosis": "Unknown subscription failure reason. Could not determine root cause.",
    "confidence": 0.50,
    "action": SubscriptionAction.GRACE_PERIOD_EXTENSION,
    "message": (
        "Hi {name}, your {plan} subscription had a payment issue. "
        "We've extended your access by {grace}d. Please update your payment method: {link}"
    ),
    "reasoning": "Unknown failure — extend grace period and request payment method update.",
}


def diagnose_subscription(event: dict, update_link: str) -> SubscriptionRecoveryPlan:
    subscription_id = event.get("subscription_id", "sub_unknown")
    customer = event.get("customer", {})
    plan = event.get("plan", {})
    failure_reason = event.get("failure_reason", "UNKNOWN")
    dunning_attempt = int(event.get("dunning_attempt", 0))

    customer_name = customer.get("name", "Valued Customer")
    first_name = customer_name.split()[0]
    plan_name = plan.get("name", "subscription")
    mrr = float(plan.get("amount_inr", 0))

    rule = _FAILURE_RULES.get(failure_reason, _DEFAULT_FAILURE_RULE)

    # Override to SUSPENSION_WARNING if this is the last allowed attempt
    if dunning_attempt == MAX_DUNNING_ATTEMPTS - 1:
        rule = {
            **rule,
            "action": SubscriptionAction.SUSPENSION_WARNING,
            "message": (
                "Hi {name}, this is our final reminder — your {plan} (₹{amount}/mo) "
                "will be suspended in 48h if payment isn't resolved. "
                "Update now to keep your access: {link}"
            ),
            "reasoning": f"dunning_attempt={dunning_attempt} is the final allowed "
                         "attempt before suspension. Escalated to SUSPENSION_WARNING.",
        }

    # Generate retry schedule for retry actions
    retry_schedule: list[RetrySlot] = []
    if rule["action"] in (
        SubscriptionAction.RETRY_IMMEDIATE,
        SubscriptionAction.RETRY_SCHEDULED,
    ):
        retry_schedule = generate_retry_schedule(dunning_attempt, failure_reason)

    # Build customer message
    customer_message = None
    msg_template = rule.get("message")
    if msg_template:
        customer_message = msg_template.format(
            name=first_name,
            plan=plan_name,
            amount=f"{mrr:,.0f}",
            grace=GRACE_PERIOD_DAYS,
            link=update_link,
        )

    return SubscriptionRecoveryPlan(
        subscription_id=subscription_id,
        customer_name=customer_name,
        plan_name=plan_name,
        mrr_inr=mrr,
        failure_reason=failure_reason,
        dunning_attempt=dunning_attempt,
        diagnosis=rule["diagnosis"],
        confidence_score=rule["confidence"],
        recommended_action=rule["action"],
        retry_schedule=retry_schedule,
        update_link=update_link,
        grace_period_days=GRACE_PERIOD_DAYS if rule["action"] == SubscriptionAction.GRACE_PERIOD_EXTENSION else None,
        customer_message=customer_message,
        audit_reasoning=rule["reasoning"],
        diagnosed_by="rule_engine",
    )
