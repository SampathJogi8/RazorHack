"""
agent/scenarios/checkout_recovery.py
--------------------------------------
Checkout Abandonment Recovery Scenario.

Handles sessions where customers added items to cart but dropped off at various
funnel stages (address, payment selection, OTP, review confirmation).

Guardrails (deterministic, checked before any AI/messaging):
  - Cart value below minimum threshold → HALT_CART_TOO_SMALL
  - Customer has opted out of marketing → HALT_OPTED_OUT
  - Reminder count >= 2 → HALT_MAX_REMINDERS
  - Cart abandoned > 7 days → HALT_CART_EXPIRED
  - Duplicate reminder within 4 hours → HALT_COOLDOWN_ACTIVE

Recovery actions:
  - CART_REMINDER_SMS       → Simple link-back reminder for recent abandons
  - DISCOUNT_OFFER_LINK     → Time-sensitive coupon for high-value carts
  - PAYMENT_METHOD_ASSIST   → Payment-stage drop-offs need method guidance
  - NO_ACTION_HALTED        → Guardrail blocked
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
MIN_CART_VALUE_INR: float = 150.0       # Don't chase sub-₹150 carts
MAX_REMINDERS: int = 2                   # Max recovery messages per cart
CART_EXPIRY_DAYS: int = 7               # Carts older than 7d are too cold
COOLDOWN_SECONDS: int = 4 * 3600        # 4-hour anti-spam window

FUNNEL_STAGES = ["BROWSE", "CART", "ADDRESS", "PAYMENT", "OTP", "REVIEW"]


# ──────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────

class CheckoutAction(str, Enum):
    CART_REMINDER_SMS     = "CART_REMINDER_SMS"
    DISCOUNT_OFFER_LINK   = "DISCOUNT_OFFER_LINK"
    PAYMENT_METHOD_ASSIST = "PAYMENT_METHOD_ASSIST"
    NO_ACTION_HALTED      = "NO_ACTION_HALTED"


@dataclass
class CheckoutGuardrailResult:
    passed: bool
    halt_reason: Optional[str] = None
    audit_notes: list[str] = field(default_factory=list)


class CheckoutRecoveryPlan(BaseModel):
    session_id: str
    customer_name: str
    cart_value_inr: float
    funnel_stage: str
    diagnosis: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    recommended_action: CheckoutAction
    discount_pct: Optional[int] = Field(None, description="Discount % to offer, if applicable")
    expiry_minutes: Optional[int] = Field(None, description="Minutes until offer expires")
    recovery_link: str
    customer_message: str
    audit_reasoning: str
    diagnosed_by: str


# ──────────────────────────────────────────────
# Guardrails
# ──────────────────────────────────────────────

def run_checkout_guardrails(event: dict) -> CheckoutGuardrailResult:
    """Deterministic safety checks for checkout abandonment events."""
    cart_value = event.get("cart", {}).get("total_amount_inr", 0.0)
    opted_out = event.get("opted_out_marketing", False)
    reminder_count = event.get("reminder_count", 0)
    abandoned_at = event.get("abandoned_at", int(time.time()))
    last_reminded_at = event.get("last_reminded_at")

    age_days = (int(time.time()) - abandoned_at) / 86400

    if cart_value < MIN_CART_VALUE_INR:
        return CheckoutGuardrailResult(
            passed=False,
            halt_reason="HALT_CART_TOO_SMALL",
            audit_notes=[
                f"Cart value ₹{cart_value:.0f} is below minimum threshold "
                f"₹{MIN_CART_VALUE_INR:.0f}. Recovery cost > expected revenue."
            ],
        )

    if opted_out:
        return CheckoutGuardrailResult(
            passed=False,
            halt_reason="HALT_OPTED_OUT",
            audit_notes=[
                "Customer has opted out of marketing communications. "
                "Recovery messaging suppressed to comply with TRAI/DND regulations."
            ],
        )

    if reminder_count >= MAX_REMINDERS:
        return CheckoutGuardrailResult(
            passed=False,
            halt_reason="HALT_MAX_REMINDERS",
            audit_notes=[
                f"Already sent {reminder_count} reminders (max={MAX_REMINDERS}). "
                "Further messaging would cause cart fatigue and opt-outs."
            ],
        )

    if age_days > CART_EXPIRY_DAYS:
        return CheckoutGuardrailResult(
            passed=False,
            halt_reason="HALT_CART_EXPIRED",
            audit_notes=[
                f"Cart is {age_days:.1f} days old (threshold={CART_EXPIRY_DAYS}d). "
                "Customer intent is too stale for effective recovery."
            ],
        )

    if last_reminded_at:
        seconds_since = int(time.time()) - int(last_reminded_at)
        if seconds_since < COOLDOWN_SECONDS:
            hrs_remaining = round((COOLDOWN_SECONDS - seconds_since) / 3600, 1)
            return CheckoutGuardrailResult(
                passed=False,
                halt_reason="HALT_COOLDOWN_ACTIVE",
                audit_notes=[
                    f"Last reminder sent {round(seconds_since/3600,1)}h ago. "
                    f"4h cooldown active — {hrs_remaining}h remaining."
                ],
            )

    return CheckoutGuardrailResult(
        passed=True,
        audit_notes=["All checkout guardrails passed."],
    )


# ──────────────────────────────────────────────
# Recovery Rules
# ──────────────────────────────────────────────

_STAGE_RULES = {
    "OTP": {
        "diagnosis": "Customer reached OTP/3DS authentication but did not complete it. "
                     "Likely a UX friction point — wrong OTP, expired OTP, or distraction.",
        "confidence": 0.87,
        "action": CheckoutAction.CART_REMINDER_SMS,
        "discount_pct": None,
        "expiry_minutes": 30,
        "message": (
            "Hi {name} 👋 You were almost done! Your order of ₹{amount} is saved. "
            "Complete it in one tap: {link} (valid 30 min)"
        ),
        "reasoning": "OTP drop is high-intent — customer was on the final step. "
                     "Urgency messaging (30 min window) drives strong recovery.",
    },
    "PAYMENT": {
        "diagnosis": "Customer reached payment page but did not select a method or "
                     "abandoned after seeing available options. May indicate method unavailability "
                     "or price hesitation.",
        "confidence": 0.78,
        "action": CheckoutAction.PAYMENT_METHOD_ASSIST,
        "discount_pct": None,
        "expiry_minutes": 120,
        "message": (
            "Hi {name}, having trouble paying? Your cart (₹{amount}) is waiting! "
            "Try UPI, cards, or EMI — whatever works for you: {link}"
        ),
        "reasoning": "Payment-stage abandonment often means method friction. "
                     "Highlighting multiple payment options reduces perceived barriers.",
    },
    "REVIEW": {
        "diagnosis": "Customer reviewed their order but did not confirm. "
                     "This is often price hesitation — delivery cost surprise or final total shock.",
        "confidence": 0.72,
        "action": CheckoutAction.DISCOUNT_OFFER_LINK,
        "discount_pct": 5,
        "expiry_minutes": 60,
        "message": (
            "Hi {name} 🎉 Still thinking? Here's 5% off your ₹{amount} order — "
            "exclusively for you! Offer expires in 1 hour: {link}"
        ),
        "reasoning": "Review-stage abandonment is usually price-sensitive. "
                     "A small, time-scoped discount triggers FOMO and closes the conversion.",
    },
    "ADDRESS": {
        "diagnosis": "Customer dropped off at address entry. Could be a UX issue with "
                     "form complexity or no saved address, or simply being interrupted.",
        "confidence": 0.65,
        "action": CheckoutAction.CART_REMINDER_SMS,
        "discount_pct": None,
        "expiry_minutes": 180,
        "message": (
            "Hi {name}, your cart (₹{amount}) is waiting for you! "
            "Pick up where you left off: {link}"
        ),
        "reasoning": "Address-stage abandon is a moderate-intent signal. "
                     "Simple cart reminder is sufficient — no discount needed.",
    },
    "CART": {
        "diagnosis": "Customer added items to cart but never started checkout. "
                     "Low urgency — browsing or wishlist behaviour.",
        "confidence": 0.55,
        "action": CheckoutAction.CART_REMINDER_SMS,
        "discount_pct": None,
        "expiry_minutes": 360,
        "message": (
            "Hi {name}, you left something behind! "
            "Your cart (₹{amount}) is still saved: {link}"
        ),
        "reasoning": "Cart-only abandon is low-intent. Simple reminder with long window.",
    },
}

_DEFAULT_STAGE_RULE = _STAGE_RULES["CART"]


def diagnose_checkout(event: dict, recovery_link: str) -> CheckoutRecoveryPlan:
    """Rule-based checkout abandonment diagnosis and recovery planning."""
    session_id = event.get("session_id", "sess_unknown")
    customer = event.get("customer", {})
    cart = event.get("cart", {})
    funnel_stage = event.get("funnel_stage", "CART").upper()
    cart_value = cart.get("total_amount_inr", 0.0)
    customer_name = customer.get("name", "there")
    first_name = customer_name.split()[0]

    # High-value cart gets a discount regardless of stage
    rule = _STAGE_RULES.get(funnel_stage, _DEFAULT_STAGE_RULE)
    if cart_value >= 5000 and funnel_stage not in ("OTP",):
        rule = {**rule, "action": CheckoutAction.DISCOUNT_OFFER_LINK, "discount_pct": 5}

    message = rule["message"].format(
        name=first_name,
        amount=f"{cart_value:,.0f}",
        link=recovery_link,
    )

    return CheckoutRecoveryPlan(
        session_id=session_id,
        customer_name=customer_name,
        cart_value_inr=cart_value,
        funnel_stage=funnel_stage,
        diagnosis=rule["diagnosis"],
        confidence_score=rule["confidence"],
        recommended_action=rule["action"],
        discount_pct=rule.get("discount_pct"),
        expiry_minutes=rule.get("expiry_minutes"),
        recovery_link=recovery_link,
        customer_message=message,
        audit_reasoning=rule["reasoning"],
        diagnosed_by="rule_engine",
    )
