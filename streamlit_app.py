"""
streamlit_app.py
----------------
RecoverAI — Full Multi-Scenario Streamlit Dashboard

Tabs:
  1. 📊 Revenue Overview    — KPI cards + revenue waterfall + scenario distribution
  2. 💳 Payment Failures    — Inspect & triage payment.failed events
  3. 🛒 Checkout Recovery   — Abandoned cart recovery with stage-level insights
  4. 🔄 Subscriptions       — Failed subscription dunning + mandate retry schedule
  5. 📄 Receivables         — B2B overdue invoice chaser with escalation tiers
  6. 📜 Audit Trail         — Unified audit log with explainability inspector
  7. 🛡️ Circuit Breakers    — Guardrail reference + architecture diagram

Run:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────
st.set_page_config(
    page_title="RecoverAI — AI Revenue Recovery",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────
# CSS
# ──────────────────────────────────────────────
st.markdown(
    """
<style>
  .rzp-header {
    background: linear-gradient(135deg, #072654 0%, #1a1a2e 60%, #2563eb 100%);
    padding: 1.4rem 2rem; border-radius: 12px; margin-bottom: 1.5rem;
  }
  .rzp-header h1 { color: #fff; margin: 0; font-size: 2rem; letter-spacing: -0.5px; }
  .rzp-header p  { color: #a0aec0; margin: 0.25rem 0 0; font-size: 0.95rem; }

  .badge { padding: 2px 8px; border-radius: 6px; font-size: 0.78rem; font-weight: 600; }
  .badge-halted  { background:#7f1d1d; color:#fca5a5; }
  .badge-actioned{ background:#14532d; color:#86efac; }
  .badge-error   { background:#431407; color:#fdba74; }
  .badge-retry   { background:#1e3a5f; color:#93c5fd; }
  .badge-link    { background:#3b1f5e; color:#c4b5fd; }
  .badge-cart    { background:#1e3a5f; color:#7dd3fc; }
  .badge-sub     { background:#1c3029; color:#6ee7b7; }
  .badge-legal   { background:#422006; color:#fcd34d; }
  .badge-manual  { background:#431407; color:#fdba74; }

  div[data-testid="stExpander"] {
    border: 1px solid #334155 !important; border-radius: 8px !important; margin-bottom: 0.5rem;
  }
  .log-entry { font-family: monospace; font-size: 0.82rem; color: #94a3b8; padding: 2px 0; }
  .scenario-pill {
    display: inline-block; padding: 1px 6px; border-radius: 4px;
    font-size: 0.72rem; font-weight: 600; margin-left: 4px;
  }
  .sc-pay { background: #1e3a5f; color: #93c5fd; }
  .sc-cart { background: #1c3029; color: #6ee7b7; }
  .sc-sub  { background: #2e1a5e; color: #c4b5fd; }
  .sc-inv  { background: #3d2008; color: #fcd34d; }
</style>
""",
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
FIXTURE_FILES = {
    "payment.failed":      "fixtures/failed_payments_batch.json",
    "checkout.abandoned":  "fixtures/checkout_abandonments.json",
    "subscription.failed": "fixtures/subscription_failures.json",
    "invoice.overdue":     "fixtures/overdue_invoices.json",
}
AUDIT_PATH = "audit_trail.json"

ACTION_BADGES = {
    "SILENT_NETWORK_RETRY":       '<span class="badge badge-retry">🔄 Silent Retry</span>',
    "DUNNING_PAYMENT_LINK":       '<span class="badge badge-link">🔗 Payment Link</span>',
    "MANUAL_ESCALATION":          '<span class="badge badge-manual">📋 Escalate</span>',
    "CART_REMINDER_SMS":          '<span class="badge badge-cart">📱 Cart Reminder</span>',
    "DISCOUNT_OFFER_LINK":        '<span class="badge badge-cart">🏷️ Discount Link</span>',
    "PAYMENT_METHOD_ASSIST":      '<span class="badge badge-cart">💳 Method Assist</span>',
    "RETRY_IMMEDIATE":            '<span class="badge badge-sub">⚡ Retry Now</span>',
    "RETRY_SCHEDULED":            '<span class="badge badge-sub">📅 Retry Scheduled</span>',
    "PAYMENT_METHOD_UPDATE":      '<span class="badge badge-sub">🔄 Update Method</span>',
    "GRACE_PERIOD_EXTENSION":     '<span class="badge badge-sub">⏳ Grace Period</span>',
    "SUSPENSION_WARNING":         '<span class="badge badge-manual">⚠️ Suspension Warn</span>',
    "GENTLE_REMINDER":            '<span class="badge badge-legal">📧 Reminder</span>',
    "PAYMENT_PLAN_OFFER":         '<span class="badge badge-legal">📋 Pay Plan</span>',
    "ACCOUNT_MANAGER_ESCALATION": '<span class="badge badge-manual">👤 AM Escalation</span>',
    "LEGAL_WARNING":              '<span class="badge badge-legal">⚖️ Legal Warning</span>',
    "COLLECTIONS_HANDOFF":        '<span class="badge badge-legal">🏛️ Collections</span>',
    "NO_ACTION_HALTED":           '<span class="badge badge-halted">🛑 Halted</span>',
}

SCENARIO_EMOJI = {
    "payment_failure":      "💳",
    "checkout_abandonment": "🛒",
    "subscription_failure": "🔄",
    "receivables_overdue":  "📄",
}


# ──────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────

@st.cache_data(ttl=5)
def load_audit() -> dict | None:
    if not os.path.exists(AUDIT_PATH):
        return None
    with open(AUDIT_PATH, encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=60)
def load_fixtures_by_type(path: str) -> list[dict] | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_metrics(records: list[dict]) -> dict:
    total = len(records)
    total_at_risk = sum(r["amount_at_risk_inr"] for r in records)
    total_protected = sum(r.get("protected_amount_inr", 0) for r in records)
    total_recoverable = sum(r.get("recoverable_target_inr", 0) for r in records)
    recovery_pct = (total_recoverable / total_at_risk * 100) if total_at_risk else 0

    halted = [r for r in records if r["status"] == "HALTED"]
    actioned = [r for r in records if r["status"] == "ACTIONED"]

    by_scenario: dict[str, dict] = {}
    for r in records:
        sc = r.get("scenario", "unknown")
        if sc not in by_scenario:
            by_scenario[sc] = {"total": 0, "at_risk": 0.0, "recoverable": 0.0, "halted": 0}
        by_scenario[sc]["total"] += 1
        by_scenario[sc]["at_risk"] += r["amount_at_risk_inr"]
        by_scenario[sc]["recoverable"] += r.get("recoverable_target_inr", 0)
        if r["status"] == "HALTED":
            by_scenario[sc]["halted"] += 1

    action_dist: dict[str, int] = {}
    for r in records:
        a = r.get("recommended_action", "NO_ACTION_HALTED")
        action_dist[a] = action_dist.get(a, 0) + 1

    halt_dist: dict[str, int] = {}
    for r in halted:
        reason = r.get("halt_reason") or "UNKNOWN"
        halt_dist[reason] = halt_dist.get(reason, 0) + 1

    avg_conf = (
        sum(r["confidence_score"] for r in actioned if r.get("confidence_score"))
        / max(len(actioned), 1)
    )

    return dict(
        total=total, total_at_risk=total_at_risk, total_protected=total_protected,
        total_recoverable=total_recoverable, recovery_pct=recovery_pct,
        halted=len(halted), actioned=len(actioned),
        by_scenario=by_scenario, action_dist=action_dist,
        halt_dist=halt_dist, avg_conf=avg_conf,
    )


# ──────────────────────────────────────────────
# Header + Sidebar
# ──────────────────────────────────────────────
st.markdown(
    """
<div class="rzp-header">
  <h1>⚡ RecoverAI</h1>
  <p>Autonomous Revenue Recovery Agent &nbsp;·&nbsp; Razorpay AI Buildathon – Track 03: AI Revenue Recovery</p>
</div>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### ⚙️ Configuration")
    rzp_mode = "🟢 Live" if os.getenv("RAZORPAY_KEY_ID") else "🟡 Mock (no API keys)"
    llm_mode = "🟢 LLM (OpenAI)" if os.getenv("OPENAI_API_KEY") else "🟡 Rule Engine (offline)"
    st.info(f"**Razorpay:** {rzp_mode}")
    st.info(f"**AI Diagnosis:** {llm_mode}")
    st.markdown("---")
    st.markdown("### 📂 Scenarios Covered")
    st.markdown(
        """
- 💳 Payment Failure Recovery
- 🛒 Checkout Abandonment
- 🔄 Subscription Dunning
- 📄 B2B Receivables Chaser
"""
    )
    st.markdown("---")
    st.markdown("### 🛡️ Active Guardrails")
    st.markdown(
        """
- `HALT_DISPUTE_RAISED`
- `HALT_RISK_POLICY`
- `HALT_MAX_RETRIES_EXCEEDED`
- `HALT_COOLDOWN_ACTIVE`
- `HALT_SUBSCRIPTION_CANCELLED`
- `HALT_MANDATE_REVOKED`
- `HALT_MAX_DUNNING_REACHED`
- `HALT_CART_EXPIRED`
- `HALT_OPTED_OUT`
- `HALT_LEGAL_PROCEEDINGS`
- `HALT_DISPUTED`
"""
    )
    st.markdown("---")
    st.markdown("### 🚀 Quick Start")
    st.code("python3 generate_fixtures.py\npython3 batch_evaluator.py\nstreamlit run app.py", language="bash")


# ──────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────
(
    tab_overview, tab_pay, tab_cart, tab_sub, tab_inv,
    tab_audit, tab_circuit,
) = st.tabs([
    "📊 Revenue Overview",
    "💳 Payments",
    "🛒 Checkout",
    "🔄 Subscriptions",
    "📄 Receivables",
    "📜 Audit Trail",
    "🛡️ Circuit Breakers",
])


# ══════════════════════════════════════════════
# TAB 1: Revenue Overview
# ══════════════════════════════════════════════
with tab_overview:
    audit_data = load_audit()

    if audit_data is None:
        st.warning(
            "No audit trail yet. Run **Batch Recovery** from the Audit Trail tab or "
            "run `python3 batch_evaluator.py` in your terminal.",
            icon="⚠️",
        )
        # Show fixture availability
        st.markdown("#### 📁 Fixture Status")
        for label, path in FIXTURE_FILES.items():
            exists = os.path.exists(path)
            st.markdown(f"{'✅' if exists else '❌'} `{path}`")
    else:
        records = audit_data["records"]
        m = compute_metrics(records)

        # ── Hero KPI row ──────────────────────────
        st.markdown("### 💰 Revenue At-a-Glance")
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Events Processed", f"{m['total']}")
        col2.metric("Total At Risk", f"₹{m['total_at_risk']:,.0f}")
        col3.metric("Protected (Halted)", f"₹{m['total_protected']:,.0f}", f"{m['halted']} events")
        col4.metric("Recoverable Target", f"₹{m['total_recoverable']:,.0f}")
        col5.metric("Recovery Rate", f"{m['recovery_pct']:.1f}%", f"{m['actioned']} actioned")

        st.markdown("---")

        # ── Revenue Waterfall ──────────────────────
        st.markdown("### 🌊 Revenue Waterfall")
        try:
            import plotly.graph_objects as go

            unrecoverable = m["total_at_risk"] - m["total_recoverable"] - m["total_protected"]

            fig = go.Figure(go.Waterfall(
                name="Revenue Recovery",
                orientation="v",
                measure=["absolute", "relative", "relative", "total"],
                x=["Total At Risk", "Protected\n(Fraud/Disputes)", "Recoverable\nTarget", "Unrecoverable\nRemainder"],
                textposition="outside",
                text=[
                    f"₹{m['total_at_risk']:,.0f}",
                    f"-₹{m['total_protected']:,.0f}",
                    f"+₹{m['total_recoverable']:,.0f}",
                    f"₹{unrecoverable:,.0f}",
                ],
                y=[m["total_at_risk"], -m["total_protected"], m["total_recoverable"], 0],
                connector={"line": {"color": "rgb(63, 63, 63)"}},
                increasing={"marker": {"color": "#22c55e"}},
                decreasing={"marker": {"color": "#ef4444"}},
                totals={"marker": {"color": "#3b82f6"}},
            ))
            fig.update_layout(
                title="Revenue Recovery Waterfall (₹)",
                plot_bgcolor="#0f172a",
                paper_bgcolor="#0f172a",
                font={"color": "#f1f5f9"},
                height=350,
                margin=dict(l=20, r=20, t=50, b=20),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            st.info("Install plotly for the waterfall chart: `pip install plotly`")

        st.markdown("---")

        # ── Per-scenario bar chart ──────────────────
        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("### 🗂️ Per-Scenario Breakdown")
            sc_labels = {
                "payment_failure": "💳 Payments",
                "checkout_abandonment": "🛒 Checkout",
                "subscription_failure": "🔄 Subscriptions",
                "receivables_overdue": "📄 Receivables",
            }
            try:
                import plotly.express as px
                sc_data = []
                for sc, stats in m["by_scenario"].items():
                    sc_data.append({
                        "Scenario": sc_labels.get(sc, sc),
                        "At Risk (₹)": stats["at_risk"],
                        "Recoverable (₹)": stats["recoverable"],
                    })
                fig2 = px.bar(
                    sc_data, x="Scenario",
                    y=["At Risk (₹)", "Recoverable (₹)"],
                    barmode="group",
                    color_discrete_map={"At Risk (₹)": "#ef4444", "Recoverable (₹)": "#22c55e"},
                    height=300,
                )
                fig2.update_layout(
                    plot_bgcolor="#0f172a", paper_bgcolor="#0f172a",
                    font={"color": "#f1f5f9"}, margin=dict(l=10, r=10, t=30, b=10),
                    legend=dict(orientation="h", y=-0.3),
                )
                st.plotly_chart(fig2, use_container_width=True)
            except ImportError:
                for sc, stats in m["by_scenario"].items():
                    st.markdown(f"**{sc_labels.get(sc, sc)}:** ₹{stats['recoverable']:,.0f} / ₹{stats['at_risk']:,.0f}")

        with col_b:
            st.markdown("### 🎯 Action Distribution")
            try:
                import plotly.express as px
                act_labels = {
                    "SILENT_NETWORK_RETRY": "Silent Retry",
                    "DUNNING_PAYMENT_LINK": "Payment Link",
                    "CART_REMINDER_SMS": "Cart Reminder",
                    "DISCOUNT_OFFER_LINK": "Discount Link",
                    "PAYMENT_METHOD_ASSIST": "Method Assist",
                    "RETRY_IMMEDIATE": "Sub Retry (Now)",
                    "RETRY_SCHEDULED": "Sub Retry (Sched)",
                    "PAYMENT_METHOD_UPDATE": "Method Update",
                    "GRACE_PERIOD_EXTENSION": "Grace Period",
                    "SUSPENSION_WARNING": "Suspension Warn",
                    "GENTLE_REMINDER": "Gentle Reminder",
                    "PAYMENT_PLAN_OFFER": "Payment Plan",
                    "ACCOUNT_MANAGER_ESCALATION": "AM Escalation",
                    "LEGAL_WARNING": "Legal Warning",
                    "COLLECTIONS_HANDOFF": "Collections",
                    "NO_ACTION_HALTED": "Halted",
                    "MANUAL_ESCALATION": "Manual Escalate",
                }
                pie_data = [
                    {"Action": act_labels.get(a, a), "Count": c}
                    for a, c in m["action_dist"].items()
                ]
                fig3 = px.pie(
                    pie_data, names="Action", values="Count",
                    color_discrete_sequence=px.colors.qualitative.Set3, height=300,
                )
                fig3.update_layout(
                    plot_bgcolor="#0f172a", paper_bgcolor="#0f172a",
                    font={"color": "#f1f5f9"}, margin=dict(l=0, r=0, t=30, b=0),
                    showlegend=True, legend=dict(font=dict(size=9)),
                )
                fig3.update_traces(textposition="inside", textinfo="percent+label")
                st.plotly_chart(fig3, use_container_width=True)
            except ImportError:
                for action, count in sorted(m["action_dist"].items(), key=lambda x: -x[1]):
                    st.markdown(f"**{action}:** {count}")


# ══════════════════════════════════════════════
# TAB 2: Payment Failures
# ══════════════════════════════════════════════
with tab_pay:
    st.markdown("### 💳 Payment Failure Events")
    fixtures = load_fixtures_by_type(FIXTURE_FILES["payment.failed"])
    if fixtures is None:
        st.warning("Run `python3 generate_fixtures.py` first.", icon="⚠️")
    else:
        st.success(f"**{len(fixtures)}** payment.failed webhook events loaded.")
        filter_code = st.selectbox(
            "Filter by Error Code", ["ALL"] + sorted(set(
                fx["payload"]["payment"]["entity"]["error_code"] for fx in fixtures
            ))
        )
        filtered = [
            fx for fx in fixtures
            if filter_code == "ALL" or fx["payload"]["payment"]["entity"]["error_code"] == filter_code
        ]
        for fx in filtered:
            e = fx["payload"]["payment"]["entity"]
            n = e.get("notes", {})
            amt = e["amount"] / 100
            label = (
                f"{'⚖️' if n.get('dispute_raised') else ''}{'🚨' if e['error_code']=='FRAUD_SUSPECTED' else ''}"
                f" {e['id']}  ·  {n.get('customer_name','?')}  ·  ₹{amt:,.0f}  ·  {e['error_code']}"
            )
            with st.expander(label):
                c1, c2, c3 = st.columns(3)
                c1.metric("Amount", f"₹{amt:,.2f}")
                c2.metric("Attempt #", str(n.get("attempt_count", 0)))
                c3.metric("Dispute", "Yes 🚨" if n.get("dispute_raised") else "No")
                st.markdown(f"**Error:** `{e['error_code']}` — {e.get('error_description','')}")
                st.markdown(f"**Method:** `{e.get('method','-')}` | **Bank:** `{e.get('bank') or 'N/A'}`")


# ══════════════════════════════════════════════
# TAB 3: Checkout Abandonment
# ══════════════════════════════════════════════
with tab_cart:
    st.markdown("### 🛒 Checkout Abandonment Events")
    fixtures = load_fixtures_by_type(FIXTURE_FILES["checkout.abandoned"])
    if fixtures is None:
        st.warning("Run `python3 generate_fixtures.py` first.", icon="⚠️")
    else:
        st.success(f"**{len(fixtures)}** checkout.abandoned events loaded.")
        filter_stage = st.selectbox(
            "Filter by Funnel Stage",
            ["ALL", "CART", "ADDRESS", "PAYMENT", "OTP", "REVIEW"],
            key="cart_stage",
        )
        filtered = [
            fx for fx in fixtures
            if filter_stage == "ALL" or fx.get("funnel_stage") == filter_stage
        ]
        for fx in filtered:
            cart = fx.get("cart", {})
            customer = fx.get("customer", {})
            label = (
                f"{'🚫' if fx.get('opted_out_marketing') else ''}"
                f" {fx['session_id']}  ·  {customer.get('name','?')}  ·  "
                f"₹{cart.get('total_amount_inr',0):,.0f}  ·  Stage: {fx.get('funnel_stage','?')}"
            )
            with st.expander(label):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Cart Value", f"₹{cart.get('total_amount_inr',0):,.0f}")
                c2.metric("Stage", fx.get("funnel_stage", "?"))
                c3.metric("Reminders Sent", str(fx.get("reminder_count", 0)))
                c4.metric("Opted Out", "Yes 🚫" if fx.get("opted_out_marketing") else "No")
                st.markdown(f"**Device:** `{fx.get('device','?')}` | **Items:** {cart.get('item_count', '?')}")
                if cart.get("items"):
                    for item in cart["items"]:
                        st.markdown(f"  - {item['qty']}× {item['name']} @ ₹{item['price']:,}")


# ══════════════════════════════════════════════
# TAB 4: Subscriptions
# ══════════════════════════════════════════════
with tab_sub:
    st.markdown("### 🔄 Subscription Failure Events")
    fixtures = load_fixtures_by_type(FIXTURE_FILES["subscription.failed"])
    if fixtures is None:
        st.warning("Run `python3 generate_fixtures.py` first.", icon="⚠️")
    else:
        st.success(f"**{len(fixtures)}** subscription.failed events loaded.")
        for fx in fixtures:
            plan = fx.get("plan", {})
            customer = fx.get("customer", {})
            mrr = plan.get("amount_inr", 0)
            label = (
                f"{'❌' if fx.get('cancelled') else '🚫' if fx.get('mandate_revoked') else ''}"
                f" {fx['subscription_id']}  ·  {customer.get('name','?')}  ·  "
                f"₹{mrr:,.0f}/mo  ·  {plan.get('name','?')}  ·  Attempt #{fx.get('dunning_attempt',0)}"
            )
            with st.expander(label):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("MRR", f"₹{mrr:,.0f}")
                c2.metric("Failure Reason", fx.get("failure_reason", "?"))
                c3.metric("Dunning Attempt", str(fx.get("dunning_attempt", 0)))
                c4.metric("Status",
                    "Cancelled ❌" if fx.get("cancelled") else
                    "Mandate Revoked 🚫" if fx.get("mandate_revoked") else
                    "Past Due ⚠️")

                # Show mandate retry schedule
                if not fx.get("cancelled") and not fx.get("mandate_revoked"):
                    from agent.scenarios.subscription_recovery import generate_retry_schedule
                    schedule = generate_retry_schedule(
                        fx.get("dunning_attempt", 0), fx.get("failure_reason", "")
                    )
                    if schedule:
                        st.markdown("**📅 Mandate Retry Schedule:**")
                        for slot in schedule:
                            st.markdown(
                                f"  - Attempt **{slot.attempt_number}** → "
                                f"`{slot.scheduled_at_iso[:16]}` — {slot.reason}"
                            )


# ══════════════════════════════════════════════
# TAB 5: Receivables
# ══════════════════════════════════════════════
with tab_inv:
    st.markdown("### 📄 B2B Overdue Invoice Events")
    fixtures = load_fixtures_by_type(FIXTURE_FILES["invoice.overdue"])
    if fixtures is None:
        st.warning("Run `python3 generate_fixtures.py` first.", icon="⚠️")
    else:
        st.success(f"**{len(fixtures)}** invoice.overdue events loaded.")
        filter_tier = st.selectbox("Filter by Customer Tier", ["ALL", "SMB", "ENTERPRISE", "STARTUP"], key="inv_tier")
        filtered = [
            fx for fx in fixtures
            if filter_tier == "ALL" or fx.get("customer", {}).get("tier") == filter_tier
        ]
        for fx in filtered:
            customer = fx.get("customer", {})
            label = (
                f"{'⚖️' if fx.get('in_legal_proceedings') else ''}{'❗' if fx.get('disputed') else ''}"
                f" {fx['invoice_id']}  ·  {customer.get('company','?')}  ·  "
                f"₹{fx.get('amount_inr',0):,.0f}  ·  {fx.get('days_overdue',0)}d overdue  ·  "
                f"[{customer.get('tier','?')}]"
            )
            with st.expander(label):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Invoice Amount", f"₹{fx.get('amount_inr',0):,.0f}")
                c2.metric("Days Overdue", str(fx.get("days_overdue", 0)))
                c3.metric("Customer Tier", customer.get("tier", "?"))
                c4.metric("Status",
                    "Legal Proceedings ⚖️" if fx.get("in_legal_proceedings") else
                    "Disputed ❗" if fx.get("disputed") else
                    "Overdue ⚠️")
                st.markdown(
                    f"**Contact:** {customer.get('name','?')} | "
                    f"**Payment History:** {fx.get('payment_history','?')} | "
                    f"**Previous Reminders:** {fx.get('previous_reminders',0)}"
                )


# ══════════════════════════════════════════════
# TAB 6: Audit Trail + Batch Runner
# ══════════════════════════════════════════════
with tab_audit:
    st.markdown("### 📜 Audit Trail & Batch Recovery Runner")

    # ── Batch runner ─────────────────────────────
    all_fixtures_exist = all(os.path.exists(p) for p in FIXTURE_FILES.values())

    if not all_fixtures_exist:
        st.error("Fixture files missing. Run `python3 generate_fixtures.py` first.", icon="❌")
    else:
        col_btn, col_info = st.columns([1, 3])
        with col_btn:
            run_clicked = st.button("⚡ Run Batch Recovery", type="primary", use_container_width=True)
        with col_info:
            st.markdown(
                f"Processes **all {sum(len(json.load(open(p))) for p in FIXTURE_FILES.values() if os.path.exists(p))} events** "
                "across 4 revenue-risk scenarios through guardrails → diagnosis → recovery action."
            )

        if run_clicked:
            from agent.orchestrator import RevenueRecoveryOrchestrator, RecoveryResult
            from batch_evaluator import build_audit_record, aggregate, SCENARIO_LABELS

            import json as _json

            orchestrator = RevenueRecoveryOrchestrator(prefer_llm=True)
            all_events: list[dict] = []
            for path in FIXTURE_FILES.values():
                with open(path) as f:
                    all_events.extend(_json.load(f))

            progress = st.progress(0, text="Initialising…")
            log_ph = st.empty()
            log_lines: list[str] = []
            results: list[RecoveryResult] = []
            audit_records: list[dict] = []

            sc_icons = {
                "payment_failure": "💳",
                "checkout_abandonment": "🛒",
                "subscription_failure": "🔄",
                "receivables_overdue": "📄",
                "unknown": "❓",
            }

            for idx, event in enumerate(all_events):
                result = orchestrator.process(event)
                sc_icon = sc_icons.get(result.scenario.value, "❓")
                status_icon = "🛡️" if result.status.value == "HALTED" else "✅"
                log_lines.append(
                    f"{status_icon} [{idx+1:02d}] {sc_icon} {result.event_id[:18]:<18}  "
                    f"{result.status.value:<8}  {result.recommended_action:<32}  ₹{result.amount_at_risk_inr:>8,.0f}"
                )
                log_ph.markdown(
                    "\n".join(f'<div class="log-entry">{l}</div>' for l in log_lines[-20:]),
                    unsafe_allow_html=True,
                )
                progress.progress((idx + 1) / len(all_events), text=f"{idx+1}/{len(all_events)}")
                results.append(result)
                audit_records.append(build_audit_record(result, event))
                time.sleep(0.03)

            # Save
            from agent.razorpay_client import RazorpayRecoveryClient
            from agent.orchestrator import ScenarioType
            with open(AUDIT_PATH, "w") as f:
                _json.dump({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "total_records": len(audit_records),
                    "scenarios_covered": [s.value for s in ScenarioType if s != ScenarioType.UNKNOWN],
                    "razorpay_mode": RazorpayRecoveryClient().mode,
                    "records": audit_records,
                }, f, indent=2)

            progress.progress(1.0, text="✅ Batch complete!")
            st.success(f"✅ Processed {len(results)} events. Audit trail saved.", icon="🎉")

            m = aggregate(results)
            kc1, kc2, kc3, kc4 = st.columns(4)
            kc1.metric("Total At Risk", f"₹{m.total_at_risk:,.0f}")
            kc2.metric("Protected", f"₹{m.total_protected:,.0f}", f"{m.halted_count} halted")
            kc3.metric("Recoverable", f"₹{m.total_recoverable:,.0f}")
            kc4.metric("Recovery Rate", f"{m.recovery_rate:.1f}%")
            load_audit.clear()

    st.markdown("---")

    # ── Audit Inspector ───────────────────────────
    audit_data = load_audit()
    if audit_data:
        st.markdown("### 🔍 Audit Record Inspector")
        records = audit_data["records"]

        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            filter_sc = st.selectbox(
                "Scenario", ["ALL"] + list(set(r.get("scenario","") for r in records)), key="aud_sc"
            )
        with fc2:
            filter_status = st.selectbox("Status", ["ALL", "ACTIONED", "HALTED", "ERROR"], key="aud_st")
        with fc3:
            filter_action = st.selectbox(
                "Action",
                ["ALL"] + sorted(set(r.get("recommended_action","") for r in records)),
                key="aud_act",
            )

        filtered = records
        if filter_sc != "ALL":
            filtered = [r for r in filtered if r.get("scenario") == filter_sc]
        if filter_status != "ALL":
            filtered = [r for r in filtered if r.get("status") == filter_status]
        if filter_action != "ALL":
            filtered = [r for r in filtered if r.get("recommended_action") == filter_action]

        st.markdown(f"*Showing {len(filtered)} of {len(records)} records*")

        for rec in filtered:
            sc = rec.get("scenario", "unknown")
            sc_icon = SCENARIO_EMOJI.get(sc, "❓")
            badge_html = ACTION_BADGES.get(rec.get("recommended_action", "NO_ACTION_HALTED"), "")
            label = (
                f"{sc_icon} {rec['event_id'][:20]}  ·  "
                f"₹{rec['amount_at_risk_inr']:,.0f}  ·  {rec.get('scenario', '?')}"
            )
            with st.expander(label):
                hc1, hc2, hc3 = st.columns(3)
                hc1.markdown(f"**Action**<br>{badge_html}", unsafe_allow_html=True)
                hc2.metric("At Risk", f"₹{rec['amount_at_risk_inr']:,.2f}")
                hc3.metric("Recoverable", f"₹{rec.get('recoverable_target_inr',0):,.2f}")

                if rec.get("halt_reason"):
                    st.error(
                        f"🛡️ **Circuit Breaker:** `{rec['halt_reason']}`\n\n"
                        + "\n".join(f"• {n}" for n in rec.get("guardrail_notes", [])),
                        icon="🛑",
                    )

                if rec.get("diagnosis"):
                    st.info(f"**🤖 Diagnosis:** {rec['diagnosis']}")

                if rec.get("customer_message"):
                    st.success(f"**💬 Customer Message:**\n{rec['customer_message']}", icon="💬")

                if rec.get("recovery_link"):
                    st.markdown(f"**🔗 Recovery Link:** [{rec['recovery_link']}]({rec['recovery_link']})")

                if rec.get("audit_reasoning"):
                    st.markdown(f"**📝 Reasoning:** {rec['audit_reasoning']}")

                if rec.get("confidence_score") is not None:
                    st.markdown(
                        f"**Confidence:** `{rec['confidence_score']*100:.0f}%` | "
                        f"**Diagnosed by:** `{rec.get('diagnosed_by','-')}` | "
                        f"**Processed in:** `{rec.get('processing_time_ms',0):.1f}ms`"
                    )


# ══════════════════════════════════════════════
# TAB 7: Circuit Breakers
# ══════════════════════════════════════════════
with tab_circuit:
    st.markdown("### 🛡️ Deterministic Circuit Breakers")
    st.markdown(
        "Hard safety boundaries enforced **before** any AI call, LLM inference, "
        "or Razorpay API interaction. These are never delegated to AI judgment."
    )

    st.markdown("#### 💳 Payment Failure Guardrails")
    col_a, col_b = st.columns(2)
    with col_a:
        st.error("""
#### ⚖️ HALT_DISPUTE_RAISED
**Trigger:** `notes.dispute_raised == True`

Active chargeback/dispute — recovery messaging could constitute legal coercion.
Frozen, escalated to Disputes team.
""", icon="⚖️")
        st.error("""
#### 🚨 HALT_RISK_POLICY
**Trigger:** `error_code == "FRAUD_SUSPECTED"`

Fraud-flagged transaction — sending a recovery link facilitates fraud.
Quarantined for manual risk review.
""", icon="🚨")
    with col_b:
        st.warning("""
#### 🔁 HALT_MAX_RETRIES_EXCEEDED
**Trigger:** `attempt_count >= 3`

Three failed attempts = persistent issue. Automated retry will fail.
Routes to human-managed recovery queue.
""", icon="🔁")
        st.warning("""
#### ⏳ HALT_COOLDOWN_ACTIVE (Payment)
**Trigger:** `last_contacted_timestamp` < 24h ago

Anti-spam / TRAI DND compliance.
Recovery message suppressed until cooldown expires.
""", icon="⏳")

    st.markdown("---")
    st.markdown("#### 🛒 Checkout Guardrails")
    col_c, col_d = st.columns(2)
    with col_c:
        st.warning("**HALT_CART_TOO_SMALL** — Cart value < ₹150. Recovery cost > expected revenue.", icon="💰")
        st.warning("**HALT_OPTED_OUT** — Customer opted out of marketing. TRAI compliance.", icon="🚫")
    with col_d:
        st.warning("**HALT_MAX_REMINDERS** — Already sent 2 reminders. Cart fatigue risk.", icon="📱")
        st.warning("**HALT_CART_EXPIRED** — Cart > 7 days old. Customer intent too stale.", icon="🕐")

    st.markdown("---")
    st.markdown("#### 🔄 Subscription Guardrails")
    col_e, col_f = st.columns(2)
    with col_e:
        st.warning("**HALT_SUBSCRIPTION_CANCELLED** — Cannot recover a cancelled subscription.", icon="❌")
        st.warning("**HALT_MANDATE_REVOKED** — Bank mandate explicitly revoked. Requires customer re-auth.", icon="🔐")
    with col_f:
        st.warning("**HALT_MAX_DUNNING_REACHED** — 4 dunning attempts hit. Suspend & escalate.", icon="🛑")

    st.markdown("---")
    st.markdown("#### 📄 Receivables Guardrails")
    col_g, col_h = st.columns(2)
    with col_g:
        st.error("**HALT_LEGAL_PROCEEDINGS** — Invoice in active litigation. Legal team owns comms.", icon="⚖️")
        st.error("**HALT_DISPUTED** — Formally disputed invoice. Dispute team must resolve first.", icon="❗")
    with col_h:
        st.warning("**HALT_BELOW_THRESHOLD** — Invoice < ₹1,000. Chase cost > recovery value.", icon="💰")
        st.warning("**HALT_COOLDOWN_ACTIVE** (Receivables) — Chased within last 5 days.", icon="⏳")

    st.markdown("---")
    st.markdown("### 🤖 AI vs. Deterministic Boundary")
    st.markdown("""
| Layer | Owner | What |
|---|---|---|
| **All Circuit Breakers** | 🔒 Deterministic Code | Fraud, disputes, legal, max retries, cooldowns |
| **Root Cause Diagnosis** | 🤖 AI / Rule Engine | Why did this fail? |
| **Recovery Action Selection** | 🤖 AI / Rule Engine | Retry? Link? Escalate? Payment plan? |
| **Customer Messaging** | 🤖 AI / Rule Engine | Personalised SMS/WhatsApp/email content |
| **Mandate Retry Schedule** | 🔒 Deterministic Code | Weekend-safe, month-end-safe backoff dates |
| **Payment Link Creation** | 🔒 Razorpay API / Mock | Link URL, expiry, notification settings |
| **Audit Logging** | 🔒 Deterministic Code | Always-on, tamper-proof, structured |

> **Core principle:** AI decides *how* to recover; deterministic code decides *whether* to act.
""")

    st.markdown("---")
    st.markdown("### 📐 System Architecture")
    st.code("""
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
                    │  (per scenario)       │
                    └──────────┬──────────┘
                         HALTED│  PASSED
                               │
                    ┌──────────▼──────────┐
                    │  🤖 AI DIAGNOSIS     │  ← LLM or Rule Engine fallback
                    │  Root cause + action │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  🔗 Recovery Action  │  ← Razorpay Payment Link / Retry
                    │  + Customer Message  │    / Payment Plan / AM Escalation
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  📜 audit_trail.json  │  ← Every decision logged
                    │  🖥️  Streamlit UI      │
                    └──────────────────────┘
""", language="text")
