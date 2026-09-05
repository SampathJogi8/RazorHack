"""
agent/csv_ingestor.py
---------------------
Universal CSV Ingestion & Analysis Engine for RecoverAI.
Parses merchant CSV exports (Razorpay, Zoho Books, Tally, Shopify, Stripe, Chargebee,
as well as Kaggle customer churn/abandonment datasets and bank statements with header banners).
Normalizes them into structured revenue-risk events and executes deterministic guardrails
and OpenRouter LLM evaluation with intelligent concurrency.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import uuid
from typing import Any, Optional

from agent.orchestrator import RevenueRecoveryOrchestrator, ScenarioType, RecoveryResult


def _clean_key(key: str) -> str:
    """Normalize CSV header key to snake_case."""
    if not key:
        return ""
    k = key.strip().lower()
    for ch in [" ", "-", "/", ".", "(", ")", "[", "]", "#", ":"]:
        k = k.replace(ch, "_")
    while "__" in k:
        k = k.replace("__", "_")
    return k.strip("_")


def find_csv_header(lines: list[str]) -> tuple[int, list[str]]:
    """
    Scans the first 40 lines of a CSV to locate the true column header line.
    Handles bank statements, Tally reports, and accounting sheets that have
    metadata or titles at lines 1-15 before the actual data columns.
    """
    for i, line in enumerate(lines[:40]):
        if not line.strip():
            continue
        try:
            row = list(csv.reader([line]))[0]
        except Exception:
            continue
        row_lower = [c.lower().strip() for c in row if c]
        if not row_lower:
            continue
        # Look for typical column signatures
        matches = sum(
            1 for c in row_lower
            if any(term in c for term in (
                "amount", "date", "desc", "id", "inv", "pay", "dr", "cr",
                "status", "sl", "name", "email", "total", "balance", "ref", "fee",
                "cart", "churn", "order", "value", "session"
            ))
        )
        if matches >= 2:
            return i, row
    # Fallback to line 0
    first_row = list(csv.reader([lines[0]]))[0] if lines else []
    return 0, first_row


def detect_csv_scenario(headers: list[str]) -> ScenarioType:
    """Intelligently detect the business recovery scenario from CSV headers."""
    h_set = {_clean_key(h) for h in headers}

    # E-Commerce Cart Abandonment / Kaggle Churn Datasets
    if any(k in h_set for k in (
        "cart_abandonment_rate", "cart_abandonment", "abandoned_stage",
        "cart_id", "session_id", "checkout_id", "cart_items_count", "churned", "wishlist_items"
    )):
        return ScenarioType.CHECKOUT_ABANDON

    # Receivables / B2B Invoices / Accounts Receivable
    if any(k in h_set for k in (
        "invoice_id", "invoice_number", "days_overdue", "due_date",
        "in_legal_proceedings", "receivables", "disputed"
    )):
        return ScenarioType.RECEIVABLES

    # Subscription / Involuntary Churn
    if any(k in h_set for k in ("subscription_id", "plan_name", "mandate_revoked", "billing_cycle", "mrr")):
        return ScenarioType.SUBSCRIPTION

    # Payment Gateway Failure / Bank Statements (Default / Razorpay)
    return ScenarioType.PAYMENT_FAILURE


def parse_csv_to_events(csv_text: str, scenario: Optional[str] = None, max_rows: int = 50) -> tuple[ScenarioType, list[dict]]:
    """
    Parse raw CSV text into a list of normalized event dictionaries for the Orchestrator.
    Handles metadata preambles, Kaggle churn datasets, bank statements, and standard merchant exports.
    """
    lines = csv_text.splitlines()
    if not lines:
        return ScenarioType.UNKNOWN, []

    header_idx, raw_headers = find_csv_header(lines)
    headers = [_clean_key(h) for h in raw_headers]
    detected_scenario = ScenarioType(scenario) if scenario else detect_csv_scenario(headers)

    data_lines = lines[header_idx + 1:]
    reader = csv.reader(data_lines)

    events: list[dict] = []
    is_kaggle_churn = "cart_abandonment_rate" in headers or "churned" in headers or "average_order_value" in headers

    for idx, row in enumerate(reader):
        if not any(row):
            continue
        row_dict = {headers[i]: (row[i].strip() if i < len(row) else "") for i in range(len(headers))}

        # ── Scenario: E-Commerce Abandonment & Kaggle Customer Churn ──────────────
        if detected_scenario == ScenarioType.CHECKOUT_ABANDON:
            if is_kaggle_churn:
                # Kaggle Churn Dataset (e.g. ecommerce_customer_churn_dataset.csv)
                is_churned = str(row_dict.get("churned", "")).strip() == "1"
                abandon_rate = float(row_dict.get("cart_abandonment_rate") or 0.0)
                
                # Target at-risk customers: either marked churned or cart abandonment rate > 40%
                if not (is_churned or abandon_rate > 40.0):
                    continue

                aov = float(row_dict.get("average_order_value") or row_dict.get("lifetime_value") or 100.0)
                city = row_dict.get("city") or "Global"
                country = row_dict.get("country") or "India"
                gender = row_dict.get("gender") or "Shopper"
                wishlist = int(float(row_dict.get("wishlist_items") or 1.0))
                
                # Convert to INR if in USD (< 500), otherwise keep as is
                total_inr = round(aov * 83.5, 2) if aov < 1000 else round(aov, 2)

                event = {
                    "event_type": "checkout.abandoned",
                    "session_id": f"churn_{city.lower()}_{idx}",
                    "cart": {
                        "total_amount_inr": total_inr,
                        "items_count": max(wishlist, 1),
                        "items_summary": f"E-Commerce Cart ({gender}, {city})",
                        "coupon_failed": float(row_dict.get("discount_usage_rate") or 0.0) > 35.0,
                    },
                    "customer": {
                        "name": f"{gender} in {city}",
                        "email": f"customer_{idx}@{city.lower().replace(' ', '')}.com",
                        "phone": "+919876543210",
                    },
                    "exit_step": "cart_abandonment" if abandon_rate > 50.0 else "checkout_dropoff",
                    "high_intent": float(row_dict.get("total_purchases") or 0.0) > 8.0,
                }
                events.append(event)

            else:
                # Standard Shopify / WooCommerce cart abandonment export
                raw_amt_str = (
                    row_dict.get("total_amount_inr")
                    or row_dict.get("amount")
                    or row_dict.get("cart_value")
                    or row_dict.get("total")
                    or "0.0"
                )
                clean_amt_str = re.sub(r"[^\d.]", "", str(raw_amt_str)) or "0.0"
                total_val = float(clean_amt_str)
                if total_val <= 0:
                    continue

                items_count = int(row_dict.get("cart_items_count") or row_dict.get("items_count") or 1)
                event = {
                    "event_type": "checkout.abandoned",
                    "session_id": row_dict.get("session_id") or row_dict.get("checkout_id") or f"cart_{uuid.uuid4().hex[:8]}",
                    "cart": {
                        "total_amount_inr": total_val,
                        "items_count": items_count,
                        "items_summary": row_dict.get("items_summary") or "Cart Items",
                        "coupon_failed": str(row_dict.get("coupon_failed", "false")).lower() in ("true", "1", "yes"),
                    },
                    "customer": {
                        "name": row_dict.get("customer_name") or row_dict.get("name") or "Shopper",
                        "email": row_dict.get("email") or "shopper@gmail.com",
                        "phone": row_dict.get("phone") or row_dict.get("contact") or "+919876543210",
                    },
                    "exit_step": row_dict.get("abandoned_stage") or row_dict.get("exit_step") or "payment_step",
                    "high_intent": str(row_dict.get("high_intent", "true")).lower() in ("true", "1", "yes"),
                }
                events.append(event)

        # ── Scenario: B2B Receivables / Overdue Invoices ────────────────────────────
        elif detected_scenario == ScenarioType.RECEIVABLES:
            raw_amt_str = (
                row_dict.get("amount_inr")
                or row_dict.get("amount")
                or row_dict.get("invoice_amount")
                or "0.0"
            )
            clean_amt_str = re.sub(r"[^\d.]", "", str(raw_amt_str)) or "0.0"
            amount_inr = float(clean_amt_str)
            if amount_inr <= 0:
                continue

            event = {
                "event_type": "invoice.overdue",
                "invoice_id": row_dict.get("invoice_id") or row_dict.get("invoice_number") or f"INV-{uuid.uuid4().hex[:6].upper()}",
                "amount_inr": amount_inr,
                "currency": row_dict.get("currency", "INR"),
                "days_overdue": int(row_dict.get("days_overdue") or 15),
                "disputed": str(row_dict.get("disputed", "false")).lower() in ("true", "1", "yes"),
                "in_legal_proceedings": str(row_dict.get("in_legal_proceedings", "false")).lower() in ("true", "1", "yes"),
                "previous_reminders": int(row_dict.get("previous_reminders") or 1),
                "payment_history": row_dict.get("payment_history") or "good",
                "customer": {
                    "name": row_dict.get("customer_name") or row_dict.get("name") or "Finance Manager",
                    "company": row_dict.get("company") or row_dict.get("customer_company") or "Enterprise Client",
                    "tier": (row_dict.get("tier") or "ENTERPRISE").upper(),
                    "email": row_dict.get("email") or "finance@client.com",
                    "phone": row_dict.get("phone") or row_dict.get("contact") or "+919876543210",
                },
            }
            events.append(event)

        # ── Scenario: Subscription Billing / Recurring Involuntary Churn ─────────
        elif detected_scenario == ScenarioType.SUBSCRIPTION:
            raw_amt_str = row_dict.get("amount_inr") or row_dict.get("amount") or "0.0"
            clean_amt_str = re.sub(r"[^\d.]", "", str(raw_amt_str)) or "0.0"
            amount_inr = float(clean_amt_str)
            if amount_inr <= 0:
                continue

            event = {
                "event_type": "subscription.failed",
                "subscription_id": row_dict.get("subscription_id") or f"sub_{uuid.uuid4().hex[:8]}",
                "plan": {
                    "name": row_dict.get("plan_name") or "SaaS Subscription",
                    "amount_inr": amount_inr,
                    "billing_cycle": row_dict.get("billing_cycle") or "monthly",
                },
                "customer": {
                    "name": row_dict.get("customer_name") or "Subscriber",
                    "email": row_dict.get("email") or "subscriber@corp.in",
                    "phone": row_dict.get("phone") or "+919876543210",
                },
                "retry_count": int(row_dict.get("retry_count") or 1),
                "decline_code": row_dict.get("decline_code") or "insufficient_funds",
                "card_brand": row_dict.get("card_brand") or "Visa",
                "mandate_revoked": str(row_dict.get("mandate_revoked", "false")).lower() in ("true", "1", "yes"),
            }
            events.append(event)

        # ── Scenario: Payment Failures & Bank Statements (Default) ────────────────
        else:
            raw_amt_str = (
                row_dict.get("amount")
                or row_dict.get("amount_inr")
                or row_dict.get("withdrawal_amt")
                or row_dict.get("debit")
                or "0.0"
            )
            clean_amt_str = re.sub(r"[^\d.]", "", raw_amt_str) or "0.0"
            amount_val = float(clean_amt_str)
            if amount_val <= 0:
                continue

            # In Razorpay webhook payloads, amount is in paise (₹1 = 100 paise)
            amount_paise = int(amount_val * 100) if ("." in raw_amt_str or amount_val < 500000) else int(amount_val)

            # Extract customer name & description (handles bank statement UPI descriptions)
            raw_desc = (
                row_dict.get("description")
                or row_dict.get("narration")
                or row_dict.get("item_description")
                or "Order Transaction"
            )
            customer_name = row_dict.get("customer_name") or row_dict.get("name")
            if not customer_name:
                if "/" in raw_desc:
                    parts = [p.strip() for p in raw_desc.split("/") if p.strip()]
                    if len(parts) >= 2 and len(parts[1]) > 2:
                        customer_name = parts[1]
                if not customer_name:
                    customer_name = "Account Holder"

            # Determine error code
            raw_err = (
                row_dict.get("error_code")
                or row_dict.get("status")
                or ""
            ).upper()

            if not raw_err or raw_err in ("FAILED", "FAIL", "CR", "DR"):
                desc_upper = raw_desc.upper()
                if "DECLINE" in desc_upper or "REVERSAL" in desc_upper or "CHRG" in desc_upper:
                    raw_err = "BAD_REQUEST_PAYMENT_TIMED_OUT"
                elif "TIMEOUT" in desc_upper or "GATEWAY" in desc_upper:
                    raw_err = "GATEWAY_ERROR"
                elif "INSUFFICIENT" in desc_upper or "BOUNCE" in desc_upper:
                    raw_err = "INSUFFICIENT_FUNDS"
                else:
                    raw_err = "BAD_REQUEST_PAYMENT_TIMED_OUT"

            event = {
                "entity": "event",
                "event": "payment.failed",
                "event_type": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": (
                                row_dict.get("payment_id")
                                or row_dict.get("chq_ref_no")
                                or row_dict.get("ref_no")
                                or row_dict.get("id")
                                or f"pay_csv_{uuid.uuid4().hex[:8]}"
                            ),
                            "amount": amount_paise,
                            "currency": row_dict.get("currency", "INR"),
                            "status": "failed",
                            "method": row_dict.get("method") or ("upi" if "UPI" in raw_desc.upper() else "card"),
                            "bank": row_dict.get("bank") or "HDFC",
                            "email": row_dict.get("email") or "customer@example.com",
                            "contact": row_dict.get("contact") or row_dict.get("phone") or "+919876543210",
                            "error_code": raw_err,
                            "error_description": row_dict.get("error_description") or raw_desc,
                            "error_source": row_dict.get("error_source", "gateway"),
                            "error_step": row_dict.get("error_step", "payment_authorization"),
                            "error_reason": row_dict.get("error_reason", "payment_failed"),
                            "notes": {
                                "customer_name": customer_name,
                                "item_description": raw_desc[:50],
                                "attempt_count": int(row_dict.get("attempt_count") or 1),
                                "dispute_raised": str(row_dict.get("dispute_raised", "false")).lower() in ("true", "1", "yes"),
                            },
                        }
                    }
                },
            }
            events.append(event)

        if len(events) >= max_rows:
            break

    return detected_scenario, events


def process_csv_content(
    csv_text: str,
    scenario: Optional[str] = None,
    prefer_llm: bool = True,
    update_audit_trail: bool = True,
    max_rows: int = 50,
) -> dict[str, Any]:
    """
    Complete ingestion pipeline with intelligent LLM tiering:
    - Parses CSV with header scanning (supporting Kaggle churn datasets & bank statements).
    - Caps processing at max_rows (default 50) to guarantee instant responses under 3s.
    - Runs OpenRouter LLM on top rows, and high-speed rule engine on the rest.
    """
    detected_scenario, events = parse_csv_to_events(csv_text, scenario, max_rows=max_rows)
    if not events:
        return {
            "success": False,
            "error": "Could not identify recoverable transaction rows with amounts in this CSV.",
            "total_rows": 0,
        }

    # Intelligent Tiering: run LLM on up to 5 rows, and high-speed rule engine for the rest
    orch_llm = RevenueRecoveryOrchestrator(prefer_llm=True) if prefer_llm else None
    orch_rules = RevenueRecoveryOrchestrator(prefer_llm=False)

    results: list[RecoveryResult] = []
    for idx, event in enumerate(events):
        if prefer_llm and idx < 5:
            res = orch_llm.process(event)
        else:
            res = orch_rules.process(event)
        results.append(res)

    total_at_risk = sum(r.amount_at_risk_inr for r in results)
    total_protected = sum(r.protected_amount_inr for r in results)
    total_recoverable = sum(r.recoverable_target_inr for r in results)
    actioned = [r for r in results if r.status.value == "ACTIONED"]
    halted = [r for r in results if r.status.value == "HALTED"]

    itemized = []
    for r in results:
        itemized.append({
            "event_id": r.event_id,
            "scenario": r.scenario.value,
            "status": r.status.value,
            "halt_reason": r.halt_reason,
            "guardrail_notes": r.guardrail_notes,
            "amount_at_risk_inr": r.amount_at_risk_inr,
            "protected_amount_inr": r.protected_amount_inr,
            "recoverable_target_inr": r.recoverable_target_inr,
            "recommended_action": r.recommended_action,
            "customer_message": r.customer_message,
            "recovery_link": r.recovery_link,
            "confidence_score": r.confidence_score,
            "diagnosis": r.diagnosis,
            "audit_reasoning": r.audit_reasoning,
            "diagnosed_by": r.diagnosed_by,
        })

    # Append to audit_trail.json
    if update_audit_trail and os.path.exists("audit_trail.json"):
        try:
            with open("audit_trail.json", "r+", encoding="utf-8") as f:
                audit_data = json.load(f)
                existing_records = audit_data.get("records", [])
                existing_records.extend([r.to_audit_dict() for r in results])
                audit_data["records"] = existing_records
                audit_data["total_records"] = len(existing_records)
                f.seek(0)
                json.dump(audit_data, f, indent=2)
                f.truncate()
        except Exception:
            pass

    return {
        "success": True,
        "scenario_detected": detected_scenario.value,
        "total_rows": len(results),
        "total_at_risk_inr": total_at_risk,
        "total_protected_inr": total_protected,
        "total_recoverable_inr": total_recoverable,
        "recovery_rate_pct": round((total_recoverable / total_at_risk * 100), 1) if total_at_risk else 0.0,
        "actioned_count": len(actioned),
        "halted_count": len(halted),
        "results": itemized,
    }
