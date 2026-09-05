"""
agent/scenarios/receivables_chaser.py
--------------------------------------
B2B Receivables Chaser Scenario.

Handles overdue invoices across different customer tiers (Enterprise, SMB,
Startup) and escalation stages based on days overdue.

Guardrails (deterministic, pre-AI):
  - Invoice already in legal proceedings → HALT_LEGAL_PROCEEDINGS
  - Invoice formally disputed → HALT_DISPUTED
  - Amount below minimum chase threshold → HALT_BELOW_THRESHOLD
  - Already chased this week → HALT_COOLDOWN_ACTIVE

Escalation tiers by days overdue:
  0–30d  → GENTLE_REMINDER
  31–60d → PAYMENT_PLAN_OFFER
  61–90d → ACCOUNT_MANAGER_ESCALATION
  91–120d → LEGAL_WARNING
  120d+  → COLLECTIONS_HANDOFF

Customer tier modifiers:
  ENTERPRISE → Always escalate to Account Manager, never automated legal warning
  SMB        → Standard escalation ladder
  STARTUP    → Offer payment plans earlier (cashflow empathy)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
MIN_CHASE_AMOUNT_INR: float = 1000.0
CHASE_COOLDOWN_DAYS: int = 5            # Don't chase same invoice twice in 5 days
LEGAL_WARNING_THRESHOLD_DAYS: int = 90
COLLECTIONS_THRESHOLD_DAYS: int = 120


# ──────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────

class ChaserAction(str, Enum):
    GENTLE_REMINDER             = "GENTLE_REMINDER"
    PAYMENT_PLAN_OFFER          = "PAYMENT_PLAN_OFFER"
    ACCOUNT_MANAGER_ESCALATION  = "ACCOUNT_MANAGER_ESCALATION"
    LEGAL_WARNING               = "LEGAL_WARNING"
    COLLECTIONS_HANDOFF         = "COLLECTIONS_HANDOFF"
    NO_ACTION_HALTED            = "NO_ACTION_HALTED"


class PaymentPlanSlot(BaseModel):
    installment: int
    amount_inr: float
    due_date_iso: str


class ChaserPlan(BaseModel):
    invoice_id: str
    customer_name: str
    customer_company: str
    customer_tier: str
    invoice_amount_inr: float
    days_overdue: int
    diagnosis: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    recommended_action: ChaserAction
    payment_plan: list[PaymentPlanSlot] = Field(default_factory=list)
    payment_link: Optional[str] = None
    customer_message: str
    escalation_note: Optional[str] = Field(None, description="Internal ops team note")
    audit_reasoning: str
    diagnosed_by: str


@dataclass
class ChaserGuardrailResult:
    passed: bool
    halt_reason: Optional[str] = None
    audit_notes: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Guardrails
# ──────────────────────────────────────────────

def run_receivables_guardrails(event: dict) -> ChaserGuardrailResult:
    in_legal = event.get("in_legal_proceedings", False)
    disputed = event.get("disputed", False)
    amount_inr = float(event.get("amount_inr", 0))
    last_chased_ts = event.get("last_chased_at")

    if in_legal:
        return ChaserGuardrailResult(
            passed=False,
            halt_reason="HALT_LEGAL_PROCEEDINGS",
            audit_notes=[
                "Invoice is in active legal proceedings. "
                "All automated outreach suspended — legal team manages communication."
            ],
        )

    if disputed:
        return ChaserGuardrailResult(
            passed=False,
            halt_reason="HALT_DISPUTED",
            audit_notes=[
                "Invoice is formally disputed by the customer. "
                "Chasing would escalate the dispute — route to dispute resolution team."
            ],
        )

    if amount_inr < MIN_CHASE_AMOUNT_INR:
        return ChaserGuardrailResult(
            passed=False,
            halt_reason="HALT_BELOW_THRESHOLD",
            audit_notes=[
                f"Invoice amount ₹{amount_inr:.0f} is below the minimum chase "
                f"threshold ₹{MIN_CHASE_AMOUNT_INR:.0f}. Recovery cost > expected revenue."
            ],
        )

    if last_chased_ts:
        days_since = (int(time.time()) - int(last_chased_ts)) / 86400
        if days_since < CHASE_COOLDOWN_DAYS:
            return ChaserGuardrailResult(
                passed=False,
                halt_reason="HALT_COOLDOWN_ACTIVE",
                audit_notes=[
                    f"Invoice was last chased {days_since:.1f}d ago. "
                    f"{CHASE_COOLDOWN_DAYS}d cooldown active to avoid relationship damage."
                ],
            )

    return ChaserGuardrailResult(
        passed=True,
        audit_notes=["All receivables guardrails passed."],
    )


# ──────────────────────────────────────────────
# Payment Plan Generator
# ──────────────────────────────────────────────

def generate_payment_plan(
    invoice_amount: float, installments: int
) -> list[PaymentPlanSlot]:
    """Split invoice into equal monthly installments."""
    from datetime import datetime, timedelta, timezone

    per_installment = round(invoice_amount / installments, 2)
    plan = []
    now = datetime.now(timezone.utc)

    for i in range(1, installments + 1):
        due = now + timedelta(days=30 * i)
        # Last installment adjusts for rounding
        amount = (
            round(invoice_amount - per_installment * (installments - 1), 2)
            if i == installments
            else per_installment
        )
        plan.append(
            PaymentPlanSlot(
                installment=i,
                amount_inr=amount,
                due_date_iso=due.strftime("%Y-%m-%d"),
            )
        )
    return plan


# ──────────────────────────────────────────────
# Diagnosis Engine
# ──────────────────────────────────────────────

def _get_escalation_level(
    days_overdue: int, customer_tier: str
) -> tuple[ChaserAction, str, float]:
    """Return (action, diagnosis, confidence) based on days overdue and tier."""
    tier = customer_tier.upper()

    if days_overdue >= COLLECTIONS_THRESHOLD_DAYS:
        return (
            ChaserAction.COLLECTIONS_HANDOFF,
            f"Invoice is {days_overdue} days overdue — beyond internal recovery threshold. "
            "Collections or write-off evaluation required.",
            0.92,
        )

    if days_overdue >= LEGAL_WARNING_THRESHOLD_DAYS:
        if tier == "ENTERPRISE":
            # Never send automated legal warnings to enterprise — human only
            return (
                ChaserAction.ACCOUNT_MANAGER_ESCALATION,
                f"Enterprise invoice {days_overdue}d overdue. "
                "Automated legal warning suppressed — routing to Account Manager.",
                0.88,
            )
        return (
            ChaserAction.LEGAL_WARNING,
            f"Invoice is {days_overdue} days overdue. Legal notice threshold crossed. "
            "Formal demand letter being prepared.",
            0.85,
        )

    if days_overdue >= 31:
        if tier == "STARTUP":
            # Startups get payment plans earlier — cashflow empathy
            return (
                ChaserAction.PAYMENT_PLAN_OFFER,
                f"Invoice is {days_overdue}d overdue. Customer is a startup — "
                "payment plan offered to prevent relationship damage.",
                0.80,
            )
        if tier == "ENTERPRISE":
            return (
                ChaserAction.ACCOUNT_MANAGER_ESCALATION,
                f"Enterprise invoice {days_overdue}d overdue. Escalating to Account Manager "
                "to protect the relationship.",
                0.83,
            )
        return (
            ChaserAction.PAYMENT_PLAN_OFFER,
            f"Invoice is {days_overdue}d overdue. Offering structured payment plan "
            "to maximise recovery without legal friction.",
            0.78,
        )

    # 1–30 days overdue
    return (
        ChaserAction.GENTLE_REMINDER,
        f"Invoice is {days_overdue}d overdue. Early-stage — gentle reminder appropriate. "
        "Customer has likely overlooked or deprioritised.",
        0.75,
    )


_MESSAGES = {
    ChaserAction.GENTLE_REMINDER: (
        "Hi {name}, this is a gentle reminder that Invoice #{inv_id} for ₹{amount} "
        "from {company} was due {days}d ago. Please pay via: {link}"
    ),
    ChaserAction.PAYMENT_PLAN_OFFER: (
        "Hi {name}, we understand cashflow can be tight. Invoice #{inv_id} (₹{amount}) "
        "is {days}d overdue. We'd like to offer a flexible payment plan — "
        "let's resolve this together. Pay the first installment: {link}"
    ),
    ChaserAction.ACCOUNT_MANAGER_ESCALATION: (
        "Hi {name}, our accounts team has flagged Invoice #{inv_id} (₹{amount}) as "
        "{days}d overdue. Your Account Manager will reach out shortly to discuss a resolution."
    ),
    ChaserAction.LEGAL_WARNING: (
        "IMPORTANT: Invoice #{inv_id} (₹{amount}) is {days} days overdue. "
        "This is a formal demand for payment. Failure to pay within 7 days may result "
        "in legal action. Pay immediately: {link}"
    ),
    ChaserAction.COLLECTIONS_HANDOFF: (
        "Final Notice — Invoice #{inv_id} (₹{amount}) is {days}d overdue. "
        "This account has been referred to our collections partner. "
        "Contact us immediately to resolve: {link}"
    ),
}

_ESCALATION_NOTES = {
    ChaserAction.ACCOUNT_MANAGER_ESCALATION: (
        "ENTERPRISE account — do NOT send automated legal threats. "
        "Account Manager to call within 24h and negotiate resolution."
    ),
    ChaserAction.LEGAL_WARNING: (
        "Formal demand letter to be prepared by legal team. "
        "7-day payment window before filing."
    ),
    ChaserAction.COLLECTIONS_HANDOFF: (
        "Hand off to collections partner. "
        "Internal recovery exhausted — evaluate write-off vs. litigation."
    ),
}

_REASONING = {
    ChaserAction.GENTLE_REMINDER: "0–30d overdue: gentle reminder maximises response rate without relationship damage.",
    ChaserAction.PAYMENT_PLAN_OFFER: "31–60d overdue: payment plan reduces friction and improves cash recovery vs. writing off.",
    ChaserAction.ACCOUNT_MANAGER_ESCALATION: "Enterprise account — relationship > automation. AM-managed recovery protects LTV.",
    ChaserAction.LEGAL_WARNING: "90d+ overdue: formal legal demand is required escalation. Final automated step.",
    ChaserAction.COLLECTIONS_HANDOFF: "120d+ overdue: internal recovery exhausted. Collections/write-off evaluation required.",
}


def diagnose_receivable(event: dict, payment_link: str) -> ChaserPlan:
    invoice_id = event.get("invoice_id", "inv_unknown")
    customer = event.get("customer", {})
    amount_inr = float(event.get("amount_inr", 0))
    days_overdue = int(event.get("days_overdue", 0))
    customer_tier = customer.get("tier", "SMB")

    customer_name = customer.get("name", "Finance Team")
    first_name = customer_name.split()[0]
    company = customer.get("company", "your company")

    action, diagnosis, confidence = _get_escalation_level(days_overdue, customer_tier)

    # Generate payment plan for PAYMENT_PLAN_OFFER
    installments_map = {"STARTUP": 3, "SMB": 2, "ENTERPRISE": 3}
    payment_plan: list[PaymentPlanSlot] = []
    if action == ChaserAction.PAYMENT_PLAN_OFFER:
        num_installments = installments_map.get(customer_tier.upper(), 2)
        payment_plan = generate_payment_plan(amount_inr, num_installments)

    # Build message
    msg_template = _MESSAGES.get(action, "")
    customer_message = msg_template.format(
        name=first_name,
        inv_id=invoice_id[-6:],  # Last 6 chars for brevity
        amount=f"{amount_inr:,.0f}",
        days=days_overdue,
        company="RazorRevive",
        link=payment_link,
    )

    return ChaserPlan(
        invoice_id=invoice_id,
        customer_name=customer_name,
        customer_company=company,
        customer_tier=customer_tier,
        invoice_amount_inr=amount_inr,
        days_overdue=days_overdue,
        diagnosis=diagnosis,
        confidence_score=confidence,
        recommended_action=action,
        payment_plan=payment_plan,
        payment_link=payment_link,
        customer_message=customer_message,
        escalation_note=_ESCALATION_NOTES.get(action),
        audit_reasoning=_REASONING.get(action, ""),
        diagnosed_by="rule_engine",
    )
