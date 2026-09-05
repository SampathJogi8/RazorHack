"""
generate_fixtures.py
---------------------
Generates all revenue-risk fixture files for the RazorRevive demo.

Output files:
  fixtures/failed_payments_batch.json   — 25 payment.failed webhook events
  fixtures/checkout_abandonments.json   — 15 checkout abandonment sessions
  fixtures/subscription_failures.json   — 15 subscription failure events
  fixtures/overdue_invoices.json        — 10 B2B overdue invoice events

Run:
    python3 generate_fixtures.py
"""

import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

random.seed(42)

# ─────────────────────────────────────────────
# Shared reference data
# ─────────────────────────────────────────────
CUSTOMER_NAMES = [
    "Arjun Sharma", "Priya Mehta", "Rohan Verma", "Sneha Iyer", "Karan Patel",
    "Anjali Singh", "Vikram Nair", "Deepa Krishnan", "Aakash Gupta", "Ritu Joshi",
    "Manish Agarwal", "Pooja Rao", "Suresh Kumar", "Neha Bhat", "Rahul Tiwari",
    "Kavita Desai", "Amit Saxena", "Shweta Pandey", "Nitin Chaudhary", "Divya Menon",
    "Sanjay Pillai", "Meera Nambiar", "Rajesh Mishra", "Aditi Banerjee", "Vivek Dubey",
]

COMPANY_NAMES = [
    "Zephyr Logistics Pvt Ltd", "BlueSky Analytics", "GreenTech Solutions",
    "InnovateCo Systems", "TrustBridge Fintech", "NovaSoft Enterprises",
    "PeakPath Consulting", "RapidRetail Ltd", "CloudNine SaaS", "BrightEdge Media",
]

def make_phone() -> str:
    return f"+91{random.randint(7000000000, 9999999999)}"

def make_email(name: str) -> str:
    slug = name.lower().replace(" ", ".").replace("-", "")
    domains = ["gmail.com", "outlook.com", "yahoo.in", "icloud.com"]
    return f"{slug}{random.randint(1, 99)}@{random.choice(domains)}"

def ts_ago(days=0, hours=0, minutes=0) -> int:
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    return int((datetime.now(timezone.utc) - delta).timestamp())


# ═══════════════════════════════════════════════
# 1. Payment Failures (25 events)
# ═══════════════════════════════════════════════

DECLINE_CODES = [
    {
        "code": "GATEWAY_ERROR",
        "description": "Payment processing failed due to a gateway communication error",
        "source": "gateway", "step": "payment_authorization",
        "reason": "gateway_error", "network_code": "GW501",
    },
    {
        "code": "BAD_REQUEST_PAYMENT_TIMED_OUT",
        "description": "Payment session timed out before user could complete authentication",
        "source": "customer", "step": "payment_authentication",
        "reason": "payment_timed_out", "network_code": "TO408",
    },
    {
        "code": "BAD_REQUEST_PAYMENT_DECLINED_BY_BANK",
        "description": "Payment declined by the issuing bank",
        "source": "bank", "step": "payment_authorization",
        "reason": "payment_declined_by_bank", "network_code": "DO5",
    },
    {
        "code": "FRAUD_SUSPECTED",
        "description": "Transaction flagged by fraud risk engine — high-risk velocity pattern detected",
        "source": "internal", "step": "payment_authorization",
        "reason": "fraud_suspected", "network_code": "FR001",
    },
    {
        "code": "CARD_EXPIRED",
        "description": "Card has expired. Customer must update their payment method.",
        "source": "customer", "step": "payment_validation",
        "reason": "card_expired", "network_code": "CE14",
    },
]

ITEM_DESCRIPTIONS = [
    "Annual SaaS Subscription", "E-commerce Order #ORD-2024", "Hotel Booking – Goa",
    "Flight Ticket – DEL to BOM", "Premium Gym Membership", "Online Course – ML Bootcamp",
    "OTT Subscription Renewal", "Insurance Premium Q3", "EdTech Module Pack",
    "Software License Renewal", "Freelance Invoice #INV-871", "Marketplace Product Bundle",
    "Event Registration – TechConf 2024", "Home Loan EMI Prepayment", "Restaurant Pre-order",
    "Fashion Apparel Order", "Digital Marketing Package", "Cloud Storage Plan – 1TB",
    "Gaming Top-up Bundle", "Healthcare Consultation Booking", "Furniture EMI Installment",
    "Mutual Fund SIP Payment", "Wedding Venue Advance", "EV Booking Deposit",
    "Home Renovation Services",
]

AMOUNTS_PAISE = [
    49900, 129900, 349900, 599900, 79900, 249900, 99900, 189900, 1499900,
    299900, 4999900, 149900, 19900, 999900, 74900, 399900, 89900, 199900,
    29900, 549900, 2999900, 59900, 8999900, 499900, 174900,
]


def generate_payment_fixtures() -> list[dict]:
    distribution = [0]*7 + [1]*5 + [2]*5 + [3]*4 + [4]*4
    fixtures = []
    for i, code_idx in enumerate(distribution):
        decline = DECLINE_CODES[code_idx]
        name = CUSTOMER_NAMES[i]
        amount = AMOUNTS_PAISE[i]
        description = ITEM_DESCRIPTIONS[i]
        method = random.choice(["card", "upi", "netbanking", "card", "upi"])
        bank = random.choice(["HDFC", "ICICI", "SBI", "AXIS", None])
        created_at = ts_ago(days=random.randint(0, 7), hours=random.randint(0, 23))

        if code_idx == 3:
            attempt_count = random.choice([0, 1, 2])
        elif i in (0, 5, 10, 15, 20):
            attempt_count = 3
        elif i in (2, 7, 12):
            attempt_count = 2
        else:
            attempt_count = random.randint(0, 1)

        dispute_raised = i in (3, 9, 18)
        last_contacted = None
        if attempt_count > 0:
            offset_hours = random.choice([1, 6, 12, 20, 25, 30, 48])
            last_contacted = created_at - (offset_hours * 3600)

        fixtures.append({
            "entity": "event",
            "account_id": "acc_razorrevive_demo",
            "event": "payment.failed",
            "event_type": "payment.failed",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{uuid.uuid4().hex[:14]}",
                        "entity": "payment",
                        "amount": amount,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": f"order_{uuid.uuid4().hex[:14]}",
                        "method": method,
                        "amount_refunded": 0,
                        "description": description,
                        "bank": bank,
                        "email": make_email(name),
                        "contact": make_phone(),
                        "notes": {
                            "customer_name": name,
                            "customer_phone": make_phone(),
                            "attempt_count": attempt_count,
                            "dispute_raised": dispute_raised,
                            "last_contacted_timestamp": last_contacted,
                            "item_description": description,
                        },
                        "error_code": decline["code"],
                        "error_description": decline["description"],
                        "error_source": decline["source"],
                        "error_step": decline["step"],
                        "error_reason": decline["reason"],
                        "created_at": created_at,
                    }
                }
            },
            "created_at": created_at,
        })
    return fixtures


# ═══════════════════════════════════════════════
# 2. Checkout Abandonments (15 events)
# ═══════════════════════════════════════════════

CART_ITEMS_POOL = [
    [{"name": "Running Shoes – Nike Air", "qty": 1, "price": 4999}],
    [{"name": "Wireless Earbuds", "qty": 1, "price": 2499}, {"name": "Phone Case", "qty": 1, "price": 299}],
    [{"name": "Organic Groceries Bundle", "qty": 3, "price": 650}],
    [{"name": "Home Gym Resistance Bands Set", "qty": 1, "price": 1899}],
    [{"name": "Premium Notebook & Pen Set", "qty": 2, "price": 799}],
    [{"name": "Electric Kettle – 1.5L", "qty": 1, "price": 1299}],
    [{"name": "Children's Books Pack (x5)", "qty": 1, "price": 999}],
    [{"name": "Yoga Mat + Accessories", "qty": 1, "price": 1499}],
    [{"name": "Party Wear Kurta Set", "qty": 1, "price": 2999}],
    [{"name": "SmartWatch – Fitness Tracker", "qty": 1, "price": 6999}],
    [{"name": "Laptop Stand + Keyboard Combo", "qty": 1, "price": 3499}],
    [{"name": "Air Purifier HEPA", "qty": 1, "price": 8999}],
    [{"name": "Baby Clothes Bundle", "qty": 5, "price": 399}],
    [{"name": "Coffee Maker – Drip", "qty": 1, "price": 4499}],
    [{"name": "Vitamin D + Omega-3 Combo", "qty": 2, "price": 899}],
]

FUNNEL_STAGES = ["CART", "ADDRESS", "PAYMENT", "OTP", "REVIEW"]

def generate_checkout_fixtures() -> list[dict]:
    fixtures = []
    for i in range(15):
        name = CUSTOMER_NAMES[i]
        items = CART_ITEMS_POOL[i]
        cart_value = sum(it["qty"] * it["price"] for it in items)
        abandoned_at = ts_ago(
            hours=random.choice([1, 2, 4, 6, 12, 24, 48, 72, 120, 168])
        )
        funnel_stage = FUNNEL_STAGES[i % len(FUNNEL_STAGES)]
        reminder_count = random.choice([0, 0, 0, 1, 1, 2])
        opted_out = i == 4  # one opted-out customer
        last_reminded_at = None
        if reminder_count > 0:
            last_reminded_at = ts_ago(hours=random.choice([2, 5, 10, 26, 50]))

        fixtures.append({
            "event_type": "checkout.abandoned",
            "session_id": f"sess_{uuid.uuid4().hex[:12]}",
            "customer": {
                "name": name,
                "email": make_email(name),
                "phone": make_phone(),
            },
            "cart": {
                "items": items,
                "total_amount_inr": cart_value,
                "currency": "INR",
                "item_count": sum(it["qty"] for it in items),
            },
            "funnel_stage": funnel_stage,
            "abandoned_at": abandoned_at,
            "reminder_count": reminder_count,
            "opted_out_marketing": opted_out,
            "last_reminded_at": last_reminded_at,
            "device": random.choice(["mobile", "mobile", "desktop", "tablet"]),
        })
    return fixtures


# ═══════════════════════════════════════════════
# 3. Subscription Failures (15 events)
# ═══════════════════════════════════════════════

PLANS = [
    {"name": "Pro Monthly", "amount_inr": 999,  "interval": "monthly"},
    {"name": "Business Annual", "amount_inr": 9999, "interval": "yearly"},
    {"name": "Starter Monthly", "amount_inr": 499,  "interval": "monthly"},
    {"name": "Enterprise Plan", "amount_inr": 4999, "interval": "monthly"},
    {"name": "Team Plan", "amount_inr": 2499,  "interval": "monthly"},
]

SUB_FAILURE_REASONS = [
    "INSUFFICIENT_FUNDS", "CARD_EXPIRED", "CARD_DECLINED",
    "BANK_AUTHENTICATION_FAILED", "GATEWAY_ERROR", "NETWORK_TIMEOUT",
]

def generate_subscription_fixtures() -> list[dict]:
    fixtures = []
    for i in range(15):
        name = CUSTOMER_NAMES[i]
        plan = PLANS[i % len(PLANS)]
        failure_reason = SUB_FAILURE_REASONS[i % len(SUB_FAILURE_REASONS)]
        dunning_attempt = i % 5  # 0 to 4 — exercises all guardrail states
        cancelled = (i == 14)   # one cancelled subscription
        mandate_revoked = (i == 13)  # one revoked mandate

        fixtures.append({
            "event_type": "subscription.failed",
            "subscription_id": f"sub_{uuid.uuid4().hex[:12]}",
            "customer": {
                "name": name,
                "email": make_email(name),
                "phone": make_phone(),
            },
            "plan": plan,
            "failure_reason": failure_reason,
            "dunning_attempt": dunning_attempt,
            "cancelled": cancelled,
            "mandate_revoked": mandate_revoked,
            "subscription_status": "past_due",
            "grace_period_ends_at": ts_ago(days=-3),  # 3 days in future
            "failed_at": ts_ago(hours=random.randint(1, 48)),
        })
    return fixtures


# ═══════════════════════════════════════════════
# 4. Overdue Invoices (10 events)
# ═══════════════════════════════════════════════

INVOICE_AMOUNTS = [15000, 85000, 4500, 250000, 32000, 12000, 700000, 6800, 45000, 180000]
OVERDUE_DAYS = [15, 45, 75, 130, 28, 92, 60, 8, 110, 38]
TIERS = ["SMB", "ENTERPRISE", "STARTUP", "ENTERPRISE", "SMB",
         "STARTUP", "SMB", "STARTUP", "SMB", "ENTERPRISE"]

def generate_receivables_fixtures() -> list[dict]:
    fixtures = []
    for i in range(10):
        company = COMPANY_NAMES[i]
        contact_name = CUSTOMER_NAMES[i + 15]
        amount_inr = INVOICE_AMOUNTS[i]
        days_overdue = OVERDUE_DAYS[i]
        tier = TIERS[i]
        in_legal = (i == 3)      # one in legal proceedings
        disputed = (i == 8)      # one disputed
        last_chased_days_ago = random.choice([None, 3, 7, 14, 2])
        last_chased_at = ts_ago(days=last_chased_days_ago) if last_chased_days_ago else None

        fixtures.append({
            "event_type": "invoice.overdue",
            "invoice_id": f"INV-2024-{str(1000 + i).zfill(4)}",
            "customer": {
                "name": contact_name,
                "company": company,
                "tier": tier,
                "email": make_email(contact_name),
                "phone": make_phone(),
            },
            "amount_inr": amount_inr,
            "currency": "INR",
            "due_date": ts_ago(days=days_overdue),
            "days_overdue": days_overdue,
            "in_legal_proceedings": in_legal,
            "disputed": disputed,
            "previous_reminders": random.randint(0, 4),
            "last_chased_at": last_chased_at,
            "payment_history": random.choice(["good", "good", "good", "spotty", "poor"]),
        })
    return fixtures


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════

def main():
    os.makedirs("fixtures", exist_ok=True)

    generators = [
        ("fixtures/failed_payments_batch.json", generate_payment_fixtures, "payment.failed"),
        ("fixtures/checkout_abandonments.json", generate_checkout_fixtures, "checkout.abandoned"),
        ("fixtures/subscription_failures.json", generate_subscription_fixtures, "subscription.failed"),
        ("fixtures/overdue_invoices.json", generate_receivables_fixtures, "invoice.overdue"),
    ]

    total_value = 0.0
    total_events = 0

    print("\n🚀 RazorRevive — Fixture Generator")
    print("=" * 50)

    for path, generator_fn, event_label in generators:
        fixtures = generator_fn()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fixtures, f, indent=2, ensure_ascii=False)

        # Compute value
        event_value = 0.0
        for fx in fixtures:
            if "payload" in fx:
                event_value += fx["payload"]["payment"]["entity"]["amount"] / 100
            elif "cart" in fx:
                event_value += fx["cart"].get("total_amount_inr", 0)
            elif "plan" in fx:
                event_value += fx["plan"].get("amount_inr", 0)
            elif "amount_inr" in fx:
                event_value += fx["amount_inr"]

        total_value += event_value
        total_events += len(fixtures)
        print(f"  ✅ {event_label:<35} → {len(fixtures):>2} events  |  ₹{event_value:>10,.2f}  →  {path}")

    print("=" * 50)
    print(f"  📦 Total: {total_events} events  |  ₹{total_value:,.2f} revenue at risk")
    print()


if __name__ == "__main__":
    main()
