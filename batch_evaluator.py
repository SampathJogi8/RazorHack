"""
batch_evaluator.py
------------------
Multi-scenario batch processor for RazorRevive.

Loads all fixture files, routes events through the unified orchestrator,
saves a structured audit_trail.json, and prints a rich terminal summary
showing measured revenue recovery across all 4 scenarios.

Run:
    python3 generate_fixtures.py   # first time only
    python3 batch_evaluator.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.orchestrator import RevenueRecoveryOrchestrator, RecoveryResult, ScenarioType

load_dotenv()
console = Console()

# ──────────────────────────────────────────────
# Fixture files
# ──────────────────────────────────────────────
FIXTURE_FILES = {
    "payment.failed":       "fixtures/failed_payments_batch.json",
    "checkout.abandoned":   "fixtures/checkout_abandonments.json",
    "subscription.failed":  "fixtures/subscription_failures.json",
    "invoice.overdue":      "fixtures/overdue_invoices.json",
}

AUDIT_PATH = "audit_trail.json"

SCENARIO_LABELS = {
    ScenarioType.PAYMENT_FAILURE:  "💳 Payment Failure",
    ScenarioType.CHECKOUT_ABANDON: "🛒 Checkout Abandonment",
    ScenarioType.SUBSCRIPTION:     "🔄 Subscription Failure",
    ScenarioType.RECEIVABLES:      "📄 B2B Receivables",
}


# ──────────────────────────────────────────────
# Loaders
# ──────────────────────────────────────────────

def load_all_fixtures() -> list[dict]:
    """Load all fixture files. Exits if any are missing."""
    missing = [p for p in FIXTURE_FILES.values() if not os.path.exists(p)]
    if missing:
        console.print(
            f"[red]❌ Missing fixture files:[/red] {missing}\n"
            "Run [bold]python3 generate_fixtures.py[/bold] first.",
        )
        sys.exit(1)

    all_events = []
    for label, path in FIXTURE_FILES.items():
        with open(path, encoding="utf-8") as f:
            events = json.load(f)
        console.print(f"  [dim]Loaded {len(events):>2} events from {path}[/dim]")
        all_events.extend(events)
    return all_events


# ──────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────

def aggregate(results: list[RecoveryResult]) -> dict:
    total_events = len(results)
    total_at_risk = sum(r.amount_at_risk_inr for r in results)
    total_protected = sum(r.protected_amount_inr for r in results)

    halted = [r for r in results if r.status.value == "HALTED"]
    actioned = [r for r in results if r.status.value == "ACTIONED"]
    errors = [r for r in results if r.status.value == "ERROR"]

    total_recoverable = sum(r.recoverable_target_inr for r in actioned)
    recovery_rate = (total_recoverable / total_at_risk * 100) if total_at_risk else 0

    # Per-scenario breakdown
    by_scenario: dict[str, dict] = {}
    for r in results:
        skey = r.scenario.value
        if skey not in by_scenario:
            by_scenario[skey] = {
                "total": 0, "halted": 0, "actioned": 0,
                "at_risk": 0.0, "recoverable": 0.0, "protected": 0.0,
            }
        by_scenario[skey]["total"] += 1
        by_scenario[skey]["at_risk"] += r.amount_at_risk_inr
        by_scenario[skey]["recoverable"] += r.recoverable_target_inr
        by_scenario[skey]["protected"] += r.protected_amount_inr
        if r.status.value == "HALTED":
            by_scenario[skey]["halted"] += 1
        elif r.status.value == "ACTIONED":
            by_scenario[skey]["actioned"] += 1

    # Action distribution
    action_dist: dict[str, int] = {}
    action_value: dict[str, float] = {}
    for r in results:
        a = r.recommended_action
        action_dist[a] = action_dist.get(a, 0) + 1
        action_value[a] = action_value.get(a, 0.0) + r.recoverable_target_inr

    # Halt distribution
    halt_dist: dict[str, int] = {}
    for r in halted:
        reason = r.halt_reason or "UNKNOWN"
        halt_dist[reason] = halt_dist.get(reason, 0) + 1

    avg_conf = (
        sum(r.confidence_score for r in actioned if r.confidence_score)
        / max(len(actioned), 1)
    )
    avg_ms = sum(r.processing_time_ms for r in results) / max(total_events, 1)

    return dict(
        total_events=total_events,
        total_at_risk=total_at_risk,
        total_protected=total_protected,
        total_recoverable=total_recoverable,
        recovery_rate=recovery_rate,
        halted_count=len(halted),
        actioned_count=len(actioned),
        error_count=len(errors),
        by_scenario=by_scenario,
        action_dist=action_dist,
        action_value=action_value,
        halt_dist=halt_dist,
        avg_confidence=round(avg_conf, 3),
        avg_processing_ms=round(avg_ms, 1),
    )


# ──────────────────────────────────────────────
# Terminal output helpers
# ──────────────────────────────────────────────

def print_header():
    console.print()
    console.print(
        Panel.fit(
            "[bold magenta]⚡ RazorRevive — Multi-Scenario Batch Evaluation[/bold magenta]\n"
            "[dim]AI Revenue Recovery Agent  ·  Razorpay Buildathon Track 03[/dim]",
            border_style="magenta",
        )
    )
    console.print()


def print_progress(idx: int, total: int, result: RecoveryResult):
    scenario_emoji = {
        ScenarioType.PAYMENT_FAILURE: "💳",
        ScenarioType.CHECKOUT_ABANDON: "🛒",
        ScenarioType.SUBSCRIPTION: "🔄",
        ScenarioType.RECEIVABLES: "📄",
    }.get(result.scenario, "❓")

    status_icon = "🛡️ " if result.status.value == "HALTED" else ("❌" if result.status.value == "ERROR" else "✅")
    halt_tag = f" [{result.halt_reason}]" if result.halt_reason else ""

    console.print(
        f"  [{idx:>2}/{total}] {status_icon} {scenario_emoji} "
        f"[cyan]{result.event_id[:20]:<20}[/cyan]  "
        f"[yellow]{result.status.value:<8}[/yellow]  "
        f"[green]{result.recommended_action:<30}[/green]  "
        f"₹{result.amount_at_risk_inr:>9,.0f}"
        f"[dim]{halt_tag}[/dim]"
    )


def print_summary(m: dict, razorpay_mode: str):
    console.print()

    # ── Revenue Waterfall (KPI overview) ─────────
    kpi = Table(
        title="📊 Revenue Recovery Summary",
        box=box.ROUNDED,
        title_style="bold cyan",
        header_style="bold white on dark_blue",
    )
    kpi.add_column("Metric", style="bold", min_width=38)
    kpi.add_column("Value", justify="right", min_width=22)

    kpi.add_row("Total Events Processed", str(m["total_events"]))
    kpi.add_row(
        "🔴  Total Revenue At Risk",
        f"[bold red]₹{m['total_at_risk']:>12,.2f}[/bold red]",
    )
    kpi.add_row(
        "🛡️   Protected (Fraud / Disputes / Legal)",
        f"[bold yellow]₹{m['total_protected']:>12,.2f}[/bold yellow]",
    )
    kpi.add_row(
        "🟢  Recoverable Revenue Target",
        f"[bold green]₹{m['total_recoverable']:>12,.2f}[/bold green]",
    )
    kpi.add_row(
        "📈  Recovery Rate",
        f"[bold green]{m['recovery_rate']:.1f}%[/bold green]",
    )
    kpi.add_row("✅  Events Actioned",      str(m["actioned_count"]))
    kpi.add_row("🛑  Events Halted",        str(m["halted_count"]))
    kpi.add_row("⚡  Avg Confidence Score", f"{m['avg_confidence']*100:.1f}%")
    kpi.add_row("⏱️   Avg Processing Time",  f"{m['avg_processing_ms']:.1f} ms")
    kpi.add_row("🔗  Razorpay Client Mode", razorpay_mode.upper())
    console.print(kpi)

    # ── Per-Scenario Breakdown ────────────────────
    console.print()
    sc_table = Table(
        title="🗂️  Per-Scenario Revenue Breakdown",
        box=box.SIMPLE_HEAVY,
        title_style="bold blue",
        header_style="bold",
    )
    sc_table.add_column("Scenario", style="cyan", min_width=28)
    sc_table.add_column("Events", justify="right")
    sc_table.add_column("At Risk (₹)", justify="right")
    sc_table.add_column("Recoverable (₹)", justify="right")
    sc_table.add_column("Halted", justify="right")

    scenario_display = {
        "payment_failure":       "💳 Payment Failures",
        "checkout_abandonment":  "🛒 Checkout Abandonment",
        "subscription_failure":  "🔄 Subscription Failures",
        "receivables_overdue":   "📄 B2B Receivables",
    }

    for skey, stats in m["by_scenario"].items():
        label = scenario_display.get(skey, skey)
        sc_table.add_row(
            label,
            str(stats["total"]),
            f"₹{stats['at_risk']:,.0f}",
            f"[green]₹{stats['recoverable']:,.0f}[/green]",
            f"[yellow]{stats['halted']}[/yellow]",
        )
    console.print(sc_table)

    # ── Action Distribution ───────────────────────
    console.print()
    act_table = Table(
        title="🎯 Recovery Action Distribution",
        box=box.SIMPLE_HEAVY,
        title_style="bold green",
        header_style="bold",
    )
    act_table.add_column("Action", style="cyan", min_width=38)
    act_table.add_column("Events", justify="right")
    act_table.add_column("Revenue Target (₹)", justify="right")

    action_icons = {
        "SILENT_NETWORK_RETRY":       "🔄 Silent Network Retry",
        "DUNNING_PAYMENT_LINK":       "🔗 Payment Link (Dunning)",
        "MANUAL_ESCALATION":          "📋 Manual Escalation",
        "CART_REMINDER_SMS":          "📱 Cart Reminder SMS",
        "DISCOUNT_OFFER_LINK":        "🏷️  Discount Offer Link",
        "PAYMENT_METHOD_ASSIST":      "💳 Payment Method Assist",
        "RETRY_IMMEDIATE":            "⚡ Subscription Retry (Immediate)",
        "RETRY_SCHEDULED":            "📅 Subscription Retry (Scheduled)",
        "PAYMENT_METHOD_UPDATE":      "🔄 Payment Method Update",
        "GRACE_PERIOD_EXTENSION":     "⏳ Grace Period Extension",
        "SUSPENSION_WARNING":         "⚠️  Suspension Warning",
        "GENTLE_REMINDER":            "📧 Receivables Gentle Reminder",
        "PAYMENT_PLAN_OFFER":         "📋 Payment Plan Offer",
        "ACCOUNT_MANAGER_ESCALATION": "👤 Account Manager Escalation",
        "LEGAL_WARNING":              "⚖️  Legal Warning",
        "COLLECTIONS_HANDOFF":        "🏛️  Collections Handoff",
        "NO_ACTION_HALTED":           "🛑 No Action (Halted)",
    }

    for action, count in sorted(m["action_dist"].items(), key=lambda x: -x[1]):
        label = action_icons.get(action, action)
        val = m["action_value"].get(action, 0)
        act_table.add_row(label, str(count), f"₹{val:,.2f}")
    console.print(act_table)

    # ── Circuit Breaker Triggers ──────────────────
    if m["halt_dist"]:
        console.print()
        halt_table = Table(
            title="🛡️  Circuit Breaker Triggers",
            box=box.SIMPLE_HEAVY,
            title_style="bold yellow",
            header_style="bold",
        )
        halt_table.add_column("Halt Reason", style="yellow", min_width=38)
        halt_table.add_column("Count", justify="right")
        for reason, count in sorted(m["halt_dist"].items(), key=lambda x: -x[1]):
            halt_table.add_row(reason, str(count))
        console.print(halt_table)

    console.print(
        f"\n[bold green]✅ Audit trail saved → [cyan]{AUDIT_PATH}[/cyan][/bold green]\n"
    )


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def build_audit_record(result: RecoveryResult, event: dict) -> dict:
    d = result.to_audit_dict()
    d["scenario_label"] = SCENARIO_LABELS.get(result.scenario, result.scenario.value)
    d.pop("raw_plan", None)  # keep audit lean; full plan in raw_plan if needed
    return d


def main():
    print_header()
    console.print("[dim]Loading all fixture files…[/dim]")
    all_events = load_all_fixtures()
    total = len(all_events)
    console.print(f"\n[bold]Total events to process: {total}[/bold]\n")

    orchestrator = RevenueRecoveryOrchestrator(prefer_llm=True)
    from agent.razorpay_client import RazorpayRecoveryClient
    razorpay = RazorpayRecoveryClient()

    results: list[RecoveryResult] = []
    audit_records: list[dict] = []

    for idx, event in enumerate(all_events, start=1):
        result = orchestrator.process(event)
        print_progress(idx, total, result)
        audit_records.append(build_audit_record(result, event))
        results.append(result)
        time.sleep(0.015)  # visual pacing

    # Save audit trail
    with open(AUDIT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_records": len(audit_records),
                "scenarios_covered": [s.value for s in ScenarioType if s != ScenarioType.UNKNOWN],
                "razorpay_mode": razorpay.mode,
                "records": audit_records,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    m = aggregate(results)
    print_summary(m, razorpay.mode)


if __name__ == "__main__":
    main()
