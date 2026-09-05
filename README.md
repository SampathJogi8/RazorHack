# RazorRevive — AI Revenue Recovery Agent
## Razorpay AI Buildathon 2024 · Track 03: AI Revenue Recovery

---

### 🎯 What it solves

Revenue loss rarely happens in one place. RazorRevive is a **multi-scenario autonomous recovery agent** that detects revenue at risk across four failure modes and executes bounded, compliant recovery workflows:

| Scenario | Revenue Leak | Recovery |
|---|---|---|
| 💳 **Payment Failures** | Gateway errors, bank declines, expired cards, fraud | Silent retry / dunning payment link / escalation |
| 🛒 **Checkout Abandonment** | Cart drop-off at OTP, payment, address, review | Stage-aware cart recovery messages + discount offers |
| 🔄 **Subscription Failures** | Failed eNACH mandates, card declines, expired cards | Mandate retry sequencer with backoff / payment method update |
| 📄 **B2B Receivables** | Overdue invoices (30 / 60 / 90 / 120+ days) | Tiered chaser: gentle reminder → payment plan → legal warning |

**The bar:** Measured money recovered across a batch, with compliant escalation, stopping rules, and a complete audit trail.

---

### 🏗️ Architecture & Data Flow

```
┌────────────────────────────────────────────────────────────────────┐
│          Revenue-Risk Events (4 Scenarios)                          │
│  💳 payment.failed  🛒 checkout.abandoned                          │
│  🔄 subscription.failed  📄 invoice.overdue                        │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│         ORCHESTRATOR (agent/orchestrator.py)                      │
│         Event-type router → Scenario Handler                      │
└────┬──────────────────┬─────────────────┬──────────────┬─────────┘
     │                  │                 │              │
     ▼                  ▼                 ▼              ▼
 💳 Payment       🛒 Checkout       🔄 Subscription  📄 Receivables
 recovery_engine  checkout_recovery subscription_    receivables_
                               recovery         chaser
     │                  │                 │              │
     └──────────────────┴─────────────────┴──────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  🛡️  GUARDRAILS       │  ← Always first, always deterministic
                    └──────────┬──────────┘
                         HALTED│  PASSED
                               │
                    ┌──────────▼──────────┐
                    │  🤖 AI DIAGNOSIS     │  ← LLM or Rule Engine fallback
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  🔗 Recovery Action  │  ← Payment Link / Retry / Plan
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  📜 audit_trail.json  │
                    │  🖥️  Streamlit UI      │
                    └──────────────────────┘
```

---

### 📁 File Structure

```
razorpay/
├── requirements.txt
├── .env.example
├── api.py                           # FastAPI server & SSE streaming endpoint
├── frontend/
│   └── index.html                   # Premium single-page application (Modern UI)
├── generate_fixtures.py             # Generates all 4 scenario fixture files
├── batch_evaluator.py               # Multi-scenario CLI batch runner
├── app.py                           # Streamlit dashboard (7 tabs)
├── agent/
│   ├── guardrails.py                # Payment failure circuit breakers
│   ├── recovery_engine.py           # LLM + rule-based payment diagnosis
│   ├── razorpay_client.py           # Razorpay Payment Links API client
│   ├── orchestrator.py              # Unified event router → scenarios
│   └── scenarios/
│       ├── checkout_recovery.py     # Checkout abandonment (5 funnel stages)
│       ├── subscription_recovery.py # Subscription dunning + mandate retry sequencer
│       └── receivables_chaser.py    # B2B invoice chaser (30/60/90/120d tiers)
└── fixtures/
    ├── failed_payments_batch.json   # 25 payment.failed events
    ├── checkout_abandonments.json   # 15 checkout.abandoned events
    ├── subscription_failures.json   # 15 subscription.failed events
    └── overdue_invoices.json        # 10 invoice.overdue events
```

---

### 🤖 AI vs. Deterministic: Clear Separation

| Layer | Owner | What |
|---|---|---|
| **All Circuit Breakers** | 🔒 Deterministic Code | Fraud, disputes, legal, max retries, cooldowns |
| **Root Cause Diagnosis** | 🤖 AI / Rule Engine | Why did this fail? |
| **Recovery Action Selection** | 🤖 AI / Rule Engine | Retry? Link? Escalate? Payment plan? |
| **Customer Messaging** | 🤖 AI / Rule Engine | Personalised SMS/WhatsApp/email content |
| **Mandate Retry Schedule** | 🔒 Deterministic Code | Weekend-safe, month-end-safe backoff |
| **Payment Link Creation** | 🔒 Razorpay API / Mock | Link URL, expiry, notification |
| **Audit Logging** | 🔒 Deterministic Code | Always-on, structured audit trail |

> **Core principle:** AI decides *how* to recover; deterministic code decides *whether* to act.

---

### 🛡️ Circuit Breakers (11 Stopping Rules)

| Scenario | Breaker | Trigger |
|---|---|---|
| Payment | `HALT_DISPUTE_RAISED` | Active chargeback |
| Payment | `HALT_RISK_POLICY` | FRAUD_SUSPECTED error code |
| Payment | `HALT_MAX_RETRIES_EXCEEDED` | attempt_count ≥ 3 |
| Payment | `HALT_COOLDOWN_ACTIVE` | Last contact < 24h |
| Checkout | `HALT_CART_TOO_SMALL` | Cart value < ₹150 |
| Checkout | `HALT_OPTED_OUT` | Marketing opt-out |
| Checkout | `HALT_MAX_REMINDERS` | Reminders sent ≥ 2 |
| Checkout | `HALT_CART_EXPIRED` | Cart > 7 days old |
| Subscription | `HALT_SUBSCRIPTION_CANCELLED` | Already cancelled |
| Subscription | `HALT_MANDATE_REVOKED` | Mandate explicitly revoked |
| Subscription | `HALT_MAX_DUNNING_REACHED` | dunning_attempt ≥ 4 |
| Receivables | `HALT_LEGAL_PROCEEDINGS` | Active litigation |
| Receivables | `HALT_DISPUTED` | Formally disputed |
| Receivables | `HALT_BELOW_THRESHOLD` | Invoice < ₹1,000 |

---

### 🔄 Mandate Retry Sequencer

For failed subscriptions, the retry sequencer generates an optimised retry schedule:
- **Exponential backoff:** 1h → 24h → 72h → 168h between attempts
- **Weekend-aware:** Skips Saturday/Sunday (low bank liquidity)
- **Month-end-aware:** Avoids last 3 days of month (low-balance period)
- **Time-of-day targeting:** Schedules retries at 10am IST (bank liquidity peak)
- **Hard cap:** Maximum 4 dunning attempts, then suspend + escalate

---

### ⚡ Quickstart: Run in 2 Commands

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Launch the Premium UI Dashboard (FastAPI + Modern Web App)
python3 api.py
# 👉 Open: http://localhost:8080
```

#### Other Run Modes:
- **Streamlit Dashboard**: `streamlit run app.py` (opens `http://localhost:8501`)
- **CLI Batch Evaluator**: `python3 batch_evaluator.py` (rich terminal UI)
- **Re-generate Fixtures**: `python3 generate_fixtures.py` (65 realistic events)

**With API keys (optional):**
```bash
cp .env.example .env
# Fill in RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, OPENAI_API_KEY
```

All 4 scenarios run fully offline with mock payment links and rule-based diagnosis if no keys are set.


---

### 📊 Expected Batch Output (65 events)

```
📊 Revenue Recovery Summary
┌────────────────────────────────────┬───────────────┐
│ Total Events Processed             │            65 │
│ Total Revenue At Risk              │   ₹X,XX,XXX   │
│ Protected (Fraud/Disputes/Legal)   │   ₹XX,XXX     │
│ Recoverable Revenue Target         │   ₹X,XX,XXX   │
│ Recovery Rate                      │         XX.X% │
└────────────────────────────────────┴───────────────┘

Per-Scenario: Payments | Checkout | Subscriptions | Receivables
Action Distribution: Silent Retry | Payment Links | Cart Reminders |
                     Mandate Retry | Payment Plans | Legal Warnings | ...
```

---

### 💥 What Broke During Development & How We Fixed It

1. **Multi-scenario routing by event_type string** — Payment webhooks use `event: "payment.failed"` while new scenarios use `event_type`. Fixed with dual-field detection in orchestrator.

2. **Mandate retry sequencer scheduling to weekends** — First implementation just added hours blindly. Fixed with `_is_weekend()` + `_is_month_end()` forward-push loop.

3. **Enterprise receivables receiving automated legal warnings** — Business rule violation: enterprise accounts must never get automated legal threats. Fixed with tier-specific action override in escalation logic.

4. **LLM timeout blocking multi-scenario batch** — Single slow LLM call could stall 65 events. Fixed: 15s `httpx` timeout + silent fallback to rule engine on any exception.

5. **Streamlit `st.cache_data` not refreshing audit trail after batch run** — Cache persisted stale `None`. Fixed: explicit `load_audit.clear()` called after batch completion.

6. **Cooldown check crashing on `None` timestamp** — First-time abandons have no `last_reminded_at`. Fixed: `if last_ts is None: return None` early exit in all cooldown checks.

---

### 🏆 Evaluation Criteria

| Criterion | RazorRevive Response |
|---|---|
| **Don't just identify** | Executes recovery workflows — payment links, retry schedules, payment plans, customer messages |
| **Measured money recovered** | Batch reports ₹ recoverable target per scenario + overall recovery rate % |
| **Compliant escalation** | 11 circuit breakers, TRAI/DND compliance, legal proceedings halt, Enterprise tier protection |
| **Stopping rules** | Hard caps: max retries, max dunning, cart expiry, cooldowns, fraud halt |
| **Audit trail** | Every event → structured `audit_trail.json` with diagnosis, reasoning, confidence, action |
| **AI Revenue Recovery** | 4 scenarios × root-cause AI diagnosis × personalised recovery messaging |

---

*Built with ❤️ for Razorpay AI Buildathon 2024.*
