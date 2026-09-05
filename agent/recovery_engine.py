"""
agent/recovery_engine.py
------------------------
Core recovery engine:
  1. Runs deterministic guardrails (circuit breakers).
  2. If passed, diagnoses root cause via LLM (OpenAI) or offline rule-based fallback.
  3. Determines the optimal recovery action and generates personalised messaging.

Pydantic models:
  - ActionType          → Enum of possible recovery actions
  - ActionParams        → Backoff, expiry, messaging details
  - RecoveryPlan        → Full structured output per payment event
  - RecoveryEngineOutput → Wraps RecoveryPlan + guardrail meta

Usage:
    engine = RecoveryEngine()
    output = engine.evaluate(payment_entity)
"""

from __future__ import annotations

import json
import os
import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from agent.guardrails import GuardrailResult, GuardrailStatus, run_guardrails

# ──────────────────────────────────────────────
# Pydantic Schemas
# ──────────────────────────────────────────────


class ActionType(str, Enum):
    SILENT_NETWORK_RETRY = "SILENT_NETWORK_RETRY"
    DUNNING_PAYMENT_LINK = "DUNNING_PAYMENT_LINK"
    MANUAL_ESCALATION = "MANUAL_ESCALATION"
    NO_ACTION_HALTED = "NO_ACTION_HALTED"


class ActionParams(BaseModel):
    backoff_minutes: Optional[int] = Field(
        None, description="Minutes to wait before retrying silently"
    )
    expiry_hours: Optional[int] = Field(
        None, description="Hours until payment link expires"
    )
    suggested_payment_method: Optional[str] = Field(
        None, description="Suggested alternative payment method for the customer"
    )
    customer_message: Optional[str] = Field(
        None, description="Personalised SMS/WhatsApp message to send to the customer"
    )
    escalation_reason: Optional[str] = Field(
        None, description="Reason for manual escalation (ops team note)"
    )


class RecoveryPlan(BaseModel):
    payment_id: str
    error_code: str
    amount_inr: float
    customer_name: str
    root_cause_diagnosis: str = Field(
        description="AI or rule-based explanation of why the payment failed"
    )
    confidence_score: float = Field(
        ge=0.0, le=1.0, description="Confidence in root-cause diagnosis (0-1)"
    )
    recommended_action: ActionType
    action_params: ActionParams
    audit_reasoning: str = Field(
        description="Step-by-step reasoning chain for this recovery decision"
    )
    diagnosed_by: str = Field(
        description="'llm' or 'rule_engine' — indicates which system made the diagnosis"
    )


class RecoveryEngineOutput(BaseModel):
    payment_id: str
    event_timestamp: int
    guardrail_status: str
    halt_reason: Optional[str] = None
    protected_amount_inr: float = 0.0
    guardrail_notes: list[str] = Field(default_factory=list)
    recovery_plan: Optional[RecoveryPlan] = None
    processing_time_ms: float = 0.0

    def to_audit_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        if d.get("recovery_plan"):
            d["recovery_plan"]["recommended_action"] = d["recovery_plan"][
                "recommended_action"
            ]
        return d


# ──────────────────────────────────────────────
# Rule-Based Fallback Engine
# ──────────────────────────────────────────────

_RULE_TABLE: dict[str, dict] = {
    "GATEWAY_ERROR": {
        "diagnosis": (
            "Transient gateway communication failure between Razorpay and the "
            "acquiring bank. Likely a temporary network or timeout issue on the "
            "payment processor side, not caused by the customer."
        ),
        "confidence": 0.88,
        "action": ActionType.SILENT_NETWORK_RETRY,
        "backoff_minutes": 15,
        "expiry_hours": None,
        "suggested_method": None,
        "message_template": None,
        "reasoning": (
            "GATEWAY_ERROR indicates a recoverable infrastructure fault. "
            "Silent retry after 15 minutes is appropriate — no customer contact "
            "needed, no user-side fix required."
        ),
    },
    "BAD_REQUEST_PAYMENT_TIMED_OUT": {
        "diagnosis": (
            "Payment session expired before the customer completed 3DS/OTP "
            "authentication. This is a user-side abandonment or network latency "
            "issue, not a bank decline."
        ),
        "confidence": 0.82,
        "action": ActionType.DUNNING_PAYMENT_LINK,
        "backoff_minutes": None,
        "expiry_hours": 24,
        "suggested_method": "upi",
        "message_template": (
            "Hi {name}, your payment of ₹{amount} for {item} timed out. "
            "Complete it quickly via this secure link (valid 24h): {link}. "
            "Try UPI for a faster checkout!"
        ),
        "reasoning": (
            "Timeout indicates the customer was present but did not complete auth. "
            "A fresh payment link with a UPI suggestion minimises friction "
            "and has high recovery probability."
        ),
    },
    "BAD_REQUEST_PAYMENT_DECLINED_BY_BANK": {
        "diagnosis": (
            "Issuing bank declined the transaction. Common causes: insufficient "
            "balance, daily card limit exceeded, or bank security policy triggered "
            "for an unusual transaction pattern."
        ),
        "confidence": 0.79,
        "action": ActionType.DUNNING_PAYMENT_LINK,
        "backoff_minutes": None,
        "expiry_hours": 48,
        "suggested_method": "upi",
        "message_template": (
            "Hi {name}, your payment of ₹{amount} for {item} was declined by your bank. "
            "This can happen due to card limits or security holds. "
            "Please try again via a different method using this link (valid 48h): {link}"
        ),
        "reasoning": (
            "Bank decline is often card-specific. Offering a fresh payment link "
            "that accepts UPI/netbanking lets the customer bypass the declined card "
            "and recover the transaction."
        ),
    },
    "FRAUD_SUSPECTED": {
        "diagnosis": (
            "High-risk transaction flagged by Razorpay's fraud detection engine. "
            "Velocity anomaly or pattern mismatch detected."
        ),
        "confidence": 0.95,
        "action": ActionType.MANUAL_ESCALATION,
        "backoff_minutes": None,
        "expiry_hours": None,
        "suggested_method": None,
        "message_template": None,
        "reasoning": (
            "FRAUD_SUSPECTED is blocked by guardrails — this fallback path is "
            "only reached in unexpected edge cases. Escalate for manual review."
        ),
    },
    "CARD_EXPIRED": {
        "diagnosis": (
            "The customer's card has passed its expiry date. The bank will not "
            "authorise any transaction on an expired card regardless of balance."
        ),
        "confidence": 0.97,
        "action": ActionType.DUNNING_PAYMENT_LINK,
        "backoff_minutes": None,
        "expiry_hours": 72,
        "suggested_method": "upi",
        "message_template": (
            "Hi {name}, your payment of ₹{amount} for {item} failed because your "
            "card has expired. No worries — complete your payment securely via "
            "UPI or a new card using this link (valid 72h): {link}"
        ),
        "reasoning": (
            "Card expiry is a permanent, user-correctable issue. Silent retry "
            "will never succeed. A payment link with UPI option gives the customer "
            "an immediate path to complete payment."
        ),
    },
}

_DEFAULT_RULE = {
    "diagnosis": "Unknown payment failure. Root cause could not be determined from available data.",
    "confidence": 0.50,
    "action": ActionType.MANUAL_ESCALATION,
    "backoff_minutes": None,
    "expiry_hours": None,
    "suggested_method": None,
    "message_template": None,
    "reasoning": "Unrecognised error code — defaulting to manual escalation for human review.",
}


def _rule_based_diagnosis(payment_entity: dict, payment_link_url: str) -> RecoveryPlan:
    """Offline deterministic rule-based fallback (no API key required)."""
    notes = payment_entity.get("notes", {})
    error_code = payment_entity.get("error_code", "UNKNOWN")
    amount_inr = payment_entity.get("amount", 0) / 100.0
    customer_name = notes.get("customer_name", "Valued Customer")
    item_desc = notes.get("item_description", "your order")
    payment_id = payment_entity.get("id", "pay_unknown")

    rule = _RULE_TABLE.get(error_code, _DEFAULT_RULE)

    # Render message template
    customer_message = None
    if rule.get("message_template"):
        customer_message = rule["message_template"].format(
            name=customer_name.split()[0],
            amount=f"{amount_inr:,.0f}",
            item=item_desc,
            link=payment_link_url,
        )

    return RecoveryPlan(
        payment_id=payment_id,
        error_code=error_code,
        amount_inr=amount_inr,
        customer_name=customer_name,
        root_cause_diagnosis=rule["diagnosis"],
        confidence_score=rule["confidence"],
        recommended_action=rule["action"],
        action_params=ActionParams(
            backoff_minutes=rule.get("backoff_minutes"),
            expiry_hours=rule.get("expiry_hours"),
            suggested_payment_method=rule.get("suggested_method"),
            customer_message=customer_message,
            escalation_reason=(
                "Manual review required — see audit_reasoning."
                if rule["action"] == ActionType.MANUAL_ESCALATION
                else None
            ),
        ),
        audit_reasoning=rule["reasoning"],
        diagnosed_by="rule_engine",
    )


# ──────────────────────────────────────────────
# LLM Diagnosis (OpenAI-compatible)
# ──────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = """You are RecoverAI, an expert payment recovery agent for Razorpay.
You receive a failed payment event and must output a structured JSON recovery plan.

Your ONLY output must be valid JSON matching this schema:
{
  "root_cause_diagnosis": "<concise technical explanation of why this payment failed>",
  "confidence_score": <float 0.0-1.0>,
  "recommended_action": "<SILENT_NETWORK_RETRY | DUNNING_PAYMENT_LINK | MANUAL_ESCALATION>",
  "backoff_minutes": <int or null>,
  "expiry_hours": <int or null>,
  "suggested_payment_method": "<upi|card|netbanking|null>",
  "customer_message": "<personalised SMS/WhatsApp text or null>",
  "audit_reasoning": "<step-by-step reasoning for your decision>"
}

Rules you MUST follow:
- SILENT_NETWORK_RETRY: Only for transient infrastructure errors (gateway, network). No customer contact.
- DUNNING_PAYMENT_LINK: When customer action is needed. Always include a helpful customer_message.
- MANUAL_ESCALATION: For ambiguous or irreversible errors. Set customer_message=null.
- Never recommend retry for CARD_EXPIRED — the card physically cannot work.
- Be empathetic and clear in customer_message. Use the customer's first name.
- The payment link placeholder is already provided in the context.
"""


def _llm_diagnosis(payment_entity: dict, payment_link_url: str) -> RecoveryPlan | None:
    """
    Attempt LLM-based diagnosis using OpenAI API.
    Returns None if the API call fails or key is not set, triggering fallback.
    """
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import requests

        notes = payment_entity.get("notes", {})
        error_code = payment_entity.get("error_code", "UNKNOWN")
        amount_inr = payment_entity.get("amount", 0) / 100.0
        customer_name = notes.get("customer_name", "Customer")
        item_desc = notes.get("item_description", "order")

        user_message = f"""
Failed Payment Event:
  payment_id: {payment_entity.get('id')}
  error_code: {error_code}
  error_description: {payment_entity.get('error_description')}
  error_source: {payment_entity.get('error_source')}
  error_step: {payment_entity.get('error_step')}
  error_reason: {payment_entity.get('error_reason')}
  payment_method: {payment_entity.get('method')}
  amount_inr: {amount_inr}
  customer_name: {customer_name}
  item_description: {item_desc}
  attempt_count: {notes.get('attempt_count', 0)}
  payment_link: {payment_link_url}
"""

        # Support both OpenAI and OpenAI-compatible endpoints (Auto-detect OpenRouter)
        base_url = os.getenv("OPENAI_BASE_URL", "")
        if not base_url:
            if api_key and api_key.startswith("sk-or-"):
                base_url = "https://openrouter.ai/api/v1"
            else:
                base_url = "https://api.openai.com/v1"
        model = os.getenv("OPENAI_MODEL", "")
        if not model:
            model = "openai/gpt-4o-mini" if "openrouter" in base_url.lower() else "gpt-4o-mini"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8080",
            "X-Title": "RecoverAI Agent",
        }

        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.2,
                "max_tokens": 600,
                "response_format": {"type": "json_object"},
            },
            timeout=15.0,
        )

        if response.status_code != 200:
            return None

        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        parsed = json.loads(content.strip())

        payment_id = payment_entity.get("id", "pay_unknown")

        # Build customer message with link injected
        customer_message = parsed.get("customer_message")
        if customer_message and "{link}" in customer_message:
            customer_message = customer_message.replace("{link}", payment_link_url)

        return RecoveryPlan(
            payment_id=payment_id,
            error_code=error_code,
            amount_inr=amount_inr,
            customer_name=customer_name,
            root_cause_diagnosis=parsed.get("root_cause_diagnosis", "LLM diagnosis unavailable"),
            confidence_score=float(parsed.get("confidence_score", 0.7)),
            recommended_action=ActionType(parsed.get("recommended_action", "MANUAL_ESCALATION")),
            action_params=ActionParams(
                backoff_minutes=parsed.get("backoff_minutes"),
                expiry_hours=parsed.get("expiry_hours"),
                suggested_payment_method=parsed.get("suggested_payment_method"),
                customer_message=customer_message,
                escalation_reason=(
                    "LLM-recommended manual review."
                    if parsed.get("recommended_action") == "MANUAL_ESCALATION"
                    else None
                ),
            ),
            audit_reasoning=parsed.get("audit_reasoning", "LLM reasoning not available."),
            diagnosed_by="llm",
        )

    except Exception:
        return None  # Silently fall back to rule engine


# ──────────────────────────────────────────────
# Public: RecoveryEngine
# ──────────────────────────────────────────────


class RecoveryEngine:
    """
    Orchestrates guardrails → LLM diagnosis (with rule-based fallback) → RecoveryPlan.

    Usage:
        engine = RecoveryEngine()
        output = engine.evaluate(payment_entity, payment_link_url)
    """

    def __init__(self, prefer_llm: bool = True):
        self.prefer_llm = prefer_llm

    def evaluate(
        self,
        payment_entity: dict,
        payment_link_url: str = "https://rzp.io/i/pending",
    ) -> RecoveryEngineOutput:
        t0 = time.time()
        payment_id = payment_entity.get("id", "pay_unknown")
        event_ts = payment_entity.get("created_at", int(time.time()))

        # ── Step 1: Circuit Breakers ──────────────────────
        guardrail: GuardrailResult = run_guardrails(payment_entity)

        if guardrail.halted:
            elapsed_ms = round((time.time() - t0) * 1000, 2)
            return RecoveryEngineOutput(
                payment_id=payment_id,
                event_timestamp=event_ts,
                guardrail_status=guardrail.status.value,
                halt_reason=guardrail.halt_reason.value if guardrail.halt_reason else None,
                protected_amount_inr=guardrail.protected_amount_inr,
                guardrail_notes=guardrail.audit_notes,
                recovery_plan=None,
                processing_time_ms=elapsed_ms,
            )

        # ── Step 2: Diagnosis ─────────────────────────────
        plan: RecoveryPlan | None = None

        if self.prefer_llm:
            plan = _llm_diagnosis(payment_entity, payment_link_url)

        if plan is None:
            plan = _rule_based_diagnosis(payment_entity, payment_link_url)

        elapsed_ms = round((time.time() - t0) * 1000, 2)

        return RecoveryEngineOutput(
            payment_id=payment_id,
            event_timestamp=event_ts,
            guardrail_status=guardrail.status.value,
            halt_reason=None,
            protected_amount_inr=0.0,
            guardrail_notes=guardrail.audit_notes,
            recovery_plan=plan,
            processing_time_ms=elapsed_ms,
        )
