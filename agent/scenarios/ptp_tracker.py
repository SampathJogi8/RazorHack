"""
agent/scenarios/ptp_tracker.py
--------------------------------
Promise-to-Pay (PTP) NLP Extraction & Closed-Loop Tracking Engine.

When customers reply to dunning notices or payment links (via WhatsApp, SMS,
or Email), this engine:
1. Analyzes intent: PROMISE_MADE, DISPUTE_RAISED, EXTENSION_REQUEST, HARD_REFUSAL.
2. Extracts structured entities: Promise Amount (₹), Promised Due Date (ISO),
   Payment Rail (NEFT/RTGS, UPI, Card), and Confidence Score.
3. Enforces compliance stopping rules:
   - Customer says "Stop messaging" -> HALT_OPTED_OUT
   - Customer disputes invoice -> HALT_DISPUTED
4. Schedules closed-loop verification:
   - On Promise Date: Checks if payment arrived.
   - If YES -> Stop workflow, mark RECOVERED.
   - If NO -> Dispatch respectful reminder (Bounded: Max 1 follow-up).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CustomerIntent(str, Enum):
    PROMISE_MADE       = "PROMISE_MADE"
    DISPUTE_RAISED     = "DISPUTE_RAISED"
    EXTENSION_REQUEST  = "EXTENSION_REQUEST"
    HARD_REFUSAL       = "HARD_REFUSAL"
    GENERAL_QUERY      = "GENERAL_QUERY"


class PromiseFulfillmentStatus(str, Enum):
    PENDING   = "PENDING"
    HONORED   = "HONORED"
    BROKEN    = "BROKEN"
    DISPUTED  = "DISPUTED"


class PromiseRecord(BaseModel):
    promise_id: str
    invoice_id: str
    customer_name: str
    customer_company: str
    raw_message: str
    intent: CustomerIntent
    promised_amount_inr: float
    promised_date_iso: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    status: PromiseFulfillmentStatus = PromiseFulfillmentStatus.PENDING
    recovery_action: str
    automated_followup_message: Optional[str] = None
    audit_notes: list[str] = Field(default_factory=list)
    created_at_iso: str = ""


# ──────────────────────────────────────────────
# PTP Extraction Logic
# ──────────────────────────────────────────────

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_REFUSAL_KEYWORDS = [
    "stop messaging", "unsubscribe", "opt out", "dont contact",
    "don't contact", "harassment", "spam", "leave me alone", "not interested"
]

_DISPUTE_KEYWORDS = [
    "dispute", "disputing", "disputed", "incorrect amount", "wrong bill", "didn't receive",
    "did not receive", "defective", "fraud", "not our invoice", "already paid"
]


def extract_promise_from_text(
    message: str,
    invoice_id: str = "INV-2024-9999",
    invoice_amount: float = 50000.0,
    customer_name: str = "Finance Team",
    customer_company: str = "Client Corp",
) -> PromiseRecord:
    """
    Extracts structured promise-to-pay commitment from unstructured customer text.
    Handles dates ('next Friday', 'Sep 10', 'tomorrow', 'Monday'), amounts,
    disputes, and opt-out stopping rules.
    """
    msg_clean = message.lower().strip()
    now = datetime.now(timezone.utc)
    audit_notes: list[str] = []

    # 1. Check Hard Refusal / Opt-Out stopping rule
    if any(kw in msg_clean for kw in _REFUSAL_KEYWORDS):
        return PromiseRecord(
            promise_id=f"ptp_{invoice_id[-6:]}_{int(now.timestamp())}",
            invoice_id=invoice_id,
            customer_name=customer_name,
            customer_company=customer_company,
            raw_message=message,
            intent=CustomerIntent.HARD_REFUSAL,
            promised_amount_inr=0.0,
            promised_date_iso="",
            confidence_score=0.98,
            status=PromiseFulfillmentStatus.DISPUTED,
            recovery_action="HALT_OPTED_OUT",
            audit_notes=["Customer requested communication halt. Stopping rule enforced immediately."],
            created_at_iso=now.isoformat(),
        )

    # 2. Check Formal Dispute stopping rule
    if any(kw in msg_clean for kw in _DISPUTE_KEYWORDS):
        return PromiseRecord(
            promise_id=f"ptp_{invoice_id[-6:]}_{int(now.timestamp())}",
            invoice_id=invoice_id,
            customer_name=customer_name,
            customer_company=customer_company,
            raw_message=message,
            intent=CustomerIntent.DISPUTE_RAISED,
            promised_amount_inr=0.0,
            promised_date_iso="",
            confidence_score=0.94,
            status=PromiseFulfillmentStatus.DISPUTED,
            recovery_action="HALT_DISPUTED",
            audit_notes=["Customer formally disputed charges. Routed to dispute resolution queue."],
            created_at_iso=now.isoformat(),
        )

    # 3. Extract Amount
    promised_amount = invoice_amount
    amount_match = re.search(r'(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d{2})?)', msg_clean)
    if not amount_match:
        amount_match = re.search(r'\b([1-9]\d{3,6}(?:\.\d{2})?)\b', msg_clean)
    if amount_match:
        try:
            promised_amount = float(amount_match.group(1).replace(",", ""))
            audit_notes.append(f"Explicit amount extracted: ₹{promised_amount:,.2f}")
        except ValueError:
            pass
    elif "lakh" in msg_clean or "lac" in msg_clean:
        lakh_match = re.search(r'([\d\.]+)\s*(?:lakh|lac)', msg_clean)
        if lakh_match:
            try:
                promised_amount = float(lakh_match.group(1)) * 100000.0
                audit_notes.append(f"Lakh amount extracted: ₹{promised_amount:,.2f}")
            except ValueError:
                pass
    elif "half" in msg_clean:
        promised_amount = round(invoice_amount / 2.0, 2)
        audit_notes.append(f"Partial payment commitment: 50% (₹{promised_amount:,.2f})")

    # 4. Extract Promised Date
    target_date = now + timedelta(days=5) # default fallback
    confidence = 0.85

    if "tomorrow" in msg_clean:
        target_date = now + timedelta(days=1)
        audit_notes.append("Target date extracted: Tomorrow")
        confidence = 0.95
    elif "friday" in msg_clean:
        days_ahead = (4 - now.weekday()) % 7
        target_date = now + timedelta(days=days_ahead if days_ahead > 0 else 7)
        audit_notes.append("Target date extracted: Upcoming Friday")
        confidence = 0.92
    elif "monday" in msg_clean:
        days_ahead = (0 - now.weekday()) % 7
        target_date = now + timedelta(days=days_ahead if days_ahead > 0 else 7)
        audit_notes.append("Target date extracted: Upcoming Monday")
        confidence = 0.92
    elif "next week" in msg_clean:
        target_date = now + timedelta(days=7)
        audit_notes.append("Target date extracted: Next week (+7d)")
        confidence = 0.80
    elif "end of month" in msg_clean or "month end" in msg_clean:
        target_date = now + timedelta(days=14)
        audit_notes.append("Target date extracted: Month-end")
        confidence = 0.78
    else:
        # Match pattern like "10th", "sep 15", "15 sep"
        day_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', msg_clean)
        if day_match:
            day_num = int(day_match.group(1))
            if 1 <= day_num <= 31:
                try:
                    target_date = now.replace(day=day_num)
                    if target_date < now:
                        target_date += timedelta(days=30)
                    audit_notes.append(f"Target calendar day extracted: {day_num}")
                    confidence = 0.90
                except ValueError:
                    target_date = now + timedelta(days=7)

    date_iso = target_date.strftime("%Y-%m-%d")

    followup_msg = (
        f"Hi {customer_name.split()[0]}, confirming your payment commitment of "
        f"₹{promised_amount:,.0f} by {target_date.strftime('%d %b %Y')} for Invoice #{invoice_id[-6:]}. "
        f"We've paused reminders until then! Complete early via: https://rzp.io/i/inv_{invoice_id[-6:]}"
    )

    return PromiseRecord(
        promise_id=f"ptp_{invoice_id[-6:]}_{int(now.timestamp())}",
        invoice_id=invoice_id,
        customer_name=customer_name,
        customer_company=customer_company,
        raw_message=message,
        intent=CustomerIntent.PROMISE_MADE,
        promised_amount_inr=promised_amount,
        promised_date_iso=date_iso,
        confidence_score=confidence,
        status=PromiseFulfillmentStatus.PENDING,
        recovery_action="PROMISE_REGISTERED",
        automated_followup_message=followup_msg,
        audit_notes=audit_notes,
        created_at_iso=now.isoformat(),
    )


# ──────────────────────────────────────────────
# Built-in Demo Promises for Evaluation
# ──────────────────────────────────────────────

SAMPLE_CUSTOMER_RESPONSES = [
    {
        "invoice_id": "INV-2024-1000",
        "company": "Zephyr Logistics Pvt Ltd",
        "name": "Arjun Sharma",
        "amount": 15000.0,
        "reply": "Sorry for the delay! We will release ₹15,000 this Friday by 3 PM via RTGS.",
    },
    {
        "invoice_id": "INV-2024-1001",
        "company": "BlueSky Analytics",
        "name": "Priya Mehta",
        "amount": 85000.0,
        "reply": "Under internal CFO approval. Expect payment of ₹85,000 next Monday.",
    },
    {
        "invoice_id": "INV-2024-1002",
        "company": "GreenTech Solutions",
        "name": "Rohan Verma",
        "amount": 4500.0,
        "reply": "We are facing a brief cash crunch. Can clear half ₹2,250 tomorrow and rest next week.",
    },
    {
        "invoice_id": "INV-2024-1004",
        "company": "TrustBridge Fintech",
        "name": "Karan Patel",
        "amount": 32000.0,
        "reply": "Payment scheduled for the 15th after our batch run clears.",
    },
    {
        "invoice_id": "INV-2024-1006",
        "company": "PeakPath Consulting",
        "name": "Vikram Nair",
        "amount": 700000.0,
        "reply": "We are disputing line item #3 on this bill. Do not charge until rectified.",
    },
    {
        "invoice_id": "INV-2024-1007",
        "company": "RapidRetail Ltd",
        "name": "Deepa Krishnan",
        "amount": 6800.0,
        "reply": "Stop messaging me this is harassment unsubscribe immediately.",
    }
]


def load_demo_promises() -> list[PromiseRecord]:
    """Generates extracted PTP records from realistic customer messages."""
    records = []
    for item in SAMPLE_CUSTOMER_RESPONSES:
        p = extract_promise_from_text(
            message=item["reply"],
            invoice_id=item["invoice_id"],
            invoice_amount=item["amount"],
            customer_name=item["name"],
            customer_company=item["company"],
        )
        records.append(p)
    return records
