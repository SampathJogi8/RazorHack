"""
agent/orchestrator.py
----------------------
Unified Revenue Recovery Orchestrator.

Routes incoming revenue-risk events to the appropriate scenario handler
based on event_type, and returns a unified RecoveryResult for audit logging.

Supported event types:
  payment.failed           → Payment Failure Recovery (agent/recovery_engine.py)
  checkout.abandoned       → Checkout Abandonment Recovery
  subscription.failed      → Subscription Failure Recovery
  invoice.overdue          → B2B Receivables Chaser

Usage:
    orchestrator = RevenueRecoveryOrchestrator()
    result = orchestrator.process(event)
    batch = orchestrator.run_batch(events)
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

# ──────────────────────────────────────────────
# Unified result types
# ──────────────────────────────────────────────

class ScenarioType(str, Enum):
    PAYMENT_FAILURE  = "payment_failure"
    CHECKOUT_ABANDON = "checkout_abandonment"
    SUBSCRIPTION     = "subscription_failure"
    RECEIVABLES      = "receivables_overdue"
    UNKNOWN          = "unknown"


class RecoveryStatus(str, Enum):
    HALTED   = "HALTED"
    ACTIONED = "ACTIONED"
    ERROR    = "ERROR"


class RecoveryResult(BaseModel):
    """Unified output for every revenue-risk event regardless of scenario."""
    event_id: str
    scenario: ScenarioType
    status: RecoveryStatus
    halt_reason: Optional[str] = None
    guardrail_notes: list[str] = Field(default_factory=list)

    # Revenue impact
    amount_at_risk_inr: float = 0.0
    protected_amount_inr: float = 0.0       # halted from fraud/dispute
    recoverable_target_inr: float = 0.0     # actioned events

    # Action taken
    recommended_action: str = "NO_ACTION_HALTED"
    customer_message: Optional[str] = None
    recovery_link: Optional[str] = None
    confidence_score: Optional[float] = None

    # Explainability
    diagnosis: Optional[str] = None
    audit_reasoning: Optional[str] = None
    diagnosed_by: Optional[str] = None

    # Raw plan (scenario-specific)
    raw_plan: Optional[dict[str, Any]] = None

    # Timing
    processed_at: str = ""
    processing_time_ms: float = 0.0

    def to_audit_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ──────────────────────────────────────────────
# Mock recovery link generator (shared across scenarios)
# ──────────────────────────────────────────────

def _mock_link(event_id: str, scenario: str) -> str:
    slug = hashlib.md5(f"{scenario}:{event_id}".encode()).hexdigest()[:8]
    return f"https://rzp.io/i/{scenario[:3]}_{slug}"


# ──────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────

class RevenueRecoveryOrchestrator:
    """
    Routes revenue-risk events to scenario handlers and returns unified results.

    Initialize once, call process() for individual events or run_batch() for lists.
    """

    def __init__(self, prefer_llm: bool = True):
        self.prefer_llm = prefer_llm
        # Lazy import scenario modules to avoid circular deps
        self._payment_engine = None
        self._razorpay_client = None

    def _get_payment_engine(self):
        if not self._payment_engine:
            from agent.recovery_engine import RecoveryEngine
            self._payment_engine = RecoveryEngine(prefer_llm=self.prefer_llm)
        return self._payment_engine

    def _get_razorpay_client(self):
        if not self._razorpay_client:
            from agent.razorpay_client import RazorpayRecoveryClient
            self._razorpay_client = RazorpayRecoveryClient()
        return self._razorpay_client

    def _detect_scenario(self, event: dict) -> ScenarioType:
        event_type = event.get("event_type") or event.get("event", "")
        if "payment.failed" in event_type or "payment_failed" in event_type:
            return ScenarioType.PAYMENT_FAILURE
        if "checkout" in event_type or "abandoned" in event_type:
            return ScenarioType.CHECKOUT_ABANDON
        if "subscription" in event_type:
            return ScenarioType.SUBSCRIPTION
        if "invoice" in event_type or "overdue" in event_type:
            return ScenarioType.RECEIVABLES
        return ScenarioType.UNKNOWN

    def _get_event_id(self, event: dict, scenario: ScenarioType) -> str:
        """Extract or derive a stable event identifier."""
        if scenario == ScenarioType.PAYMENT_FAILURE:
            try:
                return event["payload"]["payment"]["entity"]["id"]
            except (KeyError, TypeError):
                pass
        elif scenario == ScenarioType.CHECKOUT_ABANDON:
            return event.get("session_id", f"sess_{hashlib.md5(str(event).encode()).hexdigest()[:8]}")
        elif scenario == ScenarioType.SUBSCRIPTION:
            return event.get("subscription_id", f"sub_{hashlib.md5(str(event).encode()).hexdigest()[:8]}")
        elif scenario == ScenarioType.RECEIVABLES:
            return event.get("invoice_id", f"inv_{hashlib.md5(str(event).encode()).hexdigest()[:8]}")
        return f"evt_{hashlib.md5(str(event).encode()).hexdigest()[:8]}"

    # ── Scenario handlers ──────────────────────────────────────────────────

    def _handle_payment_failure(self, event: dict) -> RecoveryResult:
        from agent.recovery_engine import ActionType

        entity = event.get("payload", {}).get("payment", {}).get("entity", event)
        notes = entity.get("notes", {})
        amount_inr = entity.get("amount", 0) / 100.0
        event_id = entity.get("id", "pay_unknown")
        event_ts = entity.get("created_at", int(time.time()))

        # Create payment link
        rzp = self._get_razorpay_client()
        link_data = rzp.create_payment_link(
            payment_id=event_id,
            amount_inr=amount_inr,
            customer_name=notes.get("customer_name", "Customer"),
            customer_email=entity.get("email", "customer@example.com"),
            customer_phone=entity.get("contact", "+910000000000"),
            description=notes.get("item_description", "Order"),
            expiry_hours=24,
        )
        link_url = link_data.get("short_url", _mock_link(event_id, "pay"))

        output = self._get_payment_engine().evaluate(entity, link_url)
        plan = output.recovery_plan

        status = RecoveryStatus.HALTED if output.halt_reason else RecoveryStatus.ACTIONED
        recoverable = 0.0
        protected = output.protected_amount_inr

        if plan and plan.recommended_action in (
            ActionType.SILENT_NETWORK_RETRY,
            ActionType.DUNNING_PAYMENT_LINK,
        ):
            recoverable = amount_inr

        return RecoveryResult(
            event_id=event_id,
            scenario=ScenarioType.PAYMENT_FAILURE,
            status=status,
            halt_reason=output.halt_reason,
            guardrail_notes=output.guardrail_notes,
            amount_at_risk_inr=amount_inr,
            protected_amount_inr=protected,
            recoverable_target_inr=recoverable,
            recommended_action=plan.recommended_action.value if plan else "NO_ACTION_HALTED",
            customer_message=plan.action_params.customer_message if plan else None,
            recovery_link=link_url if plan else None,
            confidence_score=plan.confidence_score if plan else None,
            diagnosis=plan.root_cause_diagnosis if plan else None,
            audit_reasoning=plan.audit_reasoning if plan else None,
            diagnosed_by=plan.diagnosed_by if plan else None,
            raw_plan=plan.model_dump() if plan else None,
            processing_time_ms=output.processing_time_ms,
        )

    def _handle_checkout_abandonment(self, event: dict) -> RecoveryResult:
        from agent.scenarios.checkout_recovery import (
            run_checkout_guardrails,
            diagnose_checkout,
            CheckoutAction,
        )

        session_id = event.get("session_id", "sess_unknown")
        cart = event.get("cart", {})
        amount_inr = cart.get("total_amount_inr", 0.0)
        recovery_link = _mock_link(session_id, "cart")

        guardrail = run_checkout_guardrails(event)
        if not guardrail.passed:
            return RecoveryResult(
                event_id=session_id,
                scenario=ScenarioType.CHECKOUT_ABANDON,
                status=RecoveryStatus.HALTED,
                halt_reason=guardrail.halt_reason,
                guardrail_notes=guardrail.audit_notes,
                amount_at_risk_inr=amount_inr,
                protected_amount_inr=0.0,
                recoverable_target_inr=0.0,
                recommended_action="NO_ACTION_HALTED",
            )

        plan = diagnose_checkout(event, recovery_link)
        recoverable = amount_inr if plan.recommended_action != CheckoutAction.NO_ACTION_HALTED else 0.0

        return RecoveryResult(
            event_id=session_id,
            scenario=ScenarioType.CHECKOUT_ABANDON,
            status=RecoveryStatus.ACTIONED,
            guardrail_notes=guardrail.audit_notes,
            amount_at_risk_inr=amount_inr,
            protected_amount_inr=0.0,
            recoverable_target_inr=recoverable,
            recommended_action=plan.recommended_action.value,
            customer_message=plan.customer_message,
            recovery_link=plan.recovery_link,
            confidence_score=plan.confidence_score,
            diagnosis=plan.diagnosis,
            audit_reasoning=plan.audit_reasoning,
            diagnosed_by=plan.diagnosed_by,
            raw_plan=plan.model_dump(),
        )

    def _handle_subscription_failure(self, event: dict) -> RecoveryResult:
        from agent.scenarios.subscription_recovery import (
            run_subscription_guardrails,
            diagnose_subscription,
            SubscriptionAction,
        )

        subscription_id = event.get("subscription_id", "sub_unknown")
        plan_data = event.get("plan", {})
        mrr = float(plan_data.get("amount_inr", 0))
        update_link = _mock_link(subscription_id, "sub")

        guardrail = run_subscription_guardrails(event)
        if not guardrail.passed:
            return RecoveryResult(
                event_id=subscription_id,
                scenario=ScenarioType.SUBSCRIPTION,
                status=RecoveryStatus.HALTED,
                halt_reason=guardrail.halt_reason,
                guardrail_notes=guardrail.audit_notes,
                amount_at_risk_inr=mrr,
                protected_amount_inr=0.0,
                recoverable_target_inr=0.0,
                recommended_action="NO_ACTION_HALTED",
            )

        plan = diagnose_subscription(event, update_link)
        recoverable = mrr if plan.recommended_action not in (
            SubscriptionAction.NO_ACTION_HALTED,
            SubscriptionAction.SUSPENSION_WARNING,
        ) else 0.0

        return RecoveryResult(
            event_id=subscription_id,
            scenario=ScenarioType.SUBSCRIPTION,
            status=RecoveryStatus.ACTIONED,
            guardrail_notes=guardrail.audit_notes,
            amount_at_risk_inr=mrr,
            protected_amount_inr=0.0,
            recoverable_target_inr=recoverable,
            recommended_action=plan.recommended_action.value,
            customer_message=plan.customer_message,
            recovery_link=plan.update_link,
            confidence_score=plan.confidence_score,
            diagnosis=plan.diagnosis,
            audit_reasoning=plan.audit_reasoning,
            diagnosed_by=plan.diagnosed_by,
            raw_plan=plan.model_dump(),
        )

    def _handle_receivables(self, event: dict) -> RecoveryResult:
        from agent.scenarios.receivables_chaser import (
            run_receivables_guardrails,
            diagnose_receivable,
            ChaserAction,
        )

        invoice_id = event.get("invoice_id", "inv_unknown")
        amount_inr = float(event.get("amount_inr", 0))
        payment_link = _mock_link(invoice_id, "inv")

        guardrail = run_receivables_guardrails(event)
        if not guardrail.passed:
            return RecoveryResult(
                event_id=invoice_id,
                scenario=ScenarioType.RECEIVABLES,
                status=RecoveryStatus.HALTED,
                halt_reason=guardrail.halt_reason,
                guardrail_notes=guardrail.audit_notes,
                amount_at_risk_inr=amount_inr,
                protected_amount_inr=0.0,
                recoverable_target_inr=0.0,
                recommended_action="NO_ACTION_HALTED",
            )

        plan = diagnose_receivable(event, payment_link)
        recoverable = amount_inr if plan.recommended_action not in (
            ChaserAction.COLLECTIONS_HANDOFF,
            ChaserAction.NO_ACTION_HALTED,
        ) else 0.0

        return RecoveryResult(
            event_id=invoice_id,
            scenario=ScenarioType.RECEIVABLES,
            status=RecoveryStatus.ACTIONED,
            guardrail_notes=guardrail.audit_notes,
            amount_at_risk_inr=amount_inr,
            protected_amount_inr=0.0,
            recoverable_target_inr=recoverable,
            recommended_action=plan.recommended_action.value,
            customer_message=plan.customer_message,
            recovery_link=plan.payment_link,
            confidence_score=plan.confidence_score,
            diagnosis=plan.diagnosis,
            audit_reasoning=plan.audit_reasoning,
            diagnosed_by=plan.diagnosed_by,
            raw_plan=plan.model_dump(),
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def process(self, event: dict) -> RecoveryResult:
        """Process a single revenue-risk event and return a unified RecoveryResult."""
        t0 = time.time()
        scenario = self._detect_scenario(event)

        try:
            if scenario == ScenarioType.PAYMENT_FAILURE:
                result = self._handle_payment_failure(event)
            elif scenario == ScenarioType.CHECKOUT_ABANDON:
                result = self._handle_checkout_abandonment(event)
            elif scenario == ScenarioType.SUBSCRIPTION:
                result = self._handle_subscription_failure(event)
            elif scenario == ScenarioType.RECEIVABLES:
                result = self._handle_receivables(event)
            else:
                result = RecoveryResult(
                    event_id=self._get_event_id(event, scenario),
                    scenario=ScenarioType.UNKNOWN,
                    status=RecoveryStatus.ERROR,
                    guardrail_notes=[f"Unknown event type: {event.get('event_type', '?')}"],
                    recommended_action="NO_ACTION_HALTED",
                )
        except Exception as exc:
            result = RecoveryResult(
                event_id=self._get_event_id(event, scenario),
                scenario=scenario,
                status=RecoveryStatus.ERROR,
                guardrail_notes=[f"Processing error: {str(exc)}"],
                recommended_action="NO_ACTION_HALTED",
                audit_reasoning=f"Exception: {str(exc)}",
            )

        elapsed = round((time.time() - t0) * 1000, 2)
        result.processing_time_ms = elapsed
        result.processed_at = datetime.now(timezone.utc).isoformat()
        return result

    def run_batch(self, events: list[dict]) -> list[RecoveryResult]:
        """Process a batch of events and return all results."""
        return [self.process(event) for event in events]
