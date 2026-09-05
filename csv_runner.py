#!/usr/bin/env python3
"""
csv_runner.py — Command-line interface to test real CSV files in RecoverAI.
Usage:
    python3 csv_runner.py sample_csvs/razorpay_failed_payments.csv
    python3 csv_runner.py path/to/your/export.csv --llm
"""

import argparse
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from agent.csv_ingestor import process_csv_content


def main():
    parser = argparse.ArgumentParser(description="RecoverAI Real CSV Ingestion Runner")
    parser.add_argument("file", help="Path to the CSV file to analyze")
    parser.add_argument("--scenario", choices=["payment_failure", "receivables_overdue", "checkout_abandonment", "subscription_failure"], default=None, help="Force scenario (default: auto-detect)")
    parser.add_argument("--llm", action="store_true", default=True, help="Enable OpenRouter / OpenAI LLM diagnosis")
    parser.add_argument("--no-audit", action="store_true", help="Don't write to audit_trail.json")
    args = parser.parse_args()

    csv_path = Path(args.file)
    if not csv_path.exists():
        print(f"❌ Error: File not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    print(f"\n📂 Ingesting CSV: {csv_path.name}")
    print("=" * 60)

    content = csv_path.read_text(encoding="utf-8")
    result = process_csv_content(
        csv_text=content,
        scenario=args.scenario,
        prefer_llm=args.llm,
        update_audit_trail=not args.no_audit,
    )

    if not result.get("success"):
        print(f"❌ Ingestion failed: {result.get('error')}")
        sys.exit(1)

    print(f"✅ Auto-Detected Scenario:  {result['scenario_detected'].upper()}")
    print(f"📊 Total Rows Processed:    {result['total_rows']}")
    print(f"💸 Total Revenue At Risk:   ₹{result['total_at_risk_inr']:,.2f}")
    print(f"🛡️  Protected (Halted):      ₹{result['total_protected_inr']:,.2f} ({result['halted_count']} events)")
    print(f"🎯 Recoverable Target:      ₹{result['total_recoverable_inr']:,.2f} ({result['actioned_count']} actioned)")
    print(f"📈 Recovery Target Rate:    {result['recovery_rate_pct']}%\n")

    print("🔍 Itemized Decision Breakdown:")
    print("-" * 60)
    for idx, item in enumerate(result["results"][:10], 1):
        status_icon = "🟢" if item["status"] == "ACTIONED" else "🛑"
        print(f"{idx}. {status_icon} [{item['event_id']}] ₹{item['amount_at_risk_inr']:,.2f} | Status: {item['status']}")
        if item["status"] == "HALTED":
            print(f"   Reason: {item['halt_reason']}")
            if item.get("guardrail_notes"):
                print(f"   Circuit Breaker: {', '.join(item['guardrail_notes'])}")
        else:
            print(f"   Recommended Action: {item['recommended_action']}")
            print(f"   Root Cause: {item['diagnosis']}")
            print(f"   Recovery Link: {item['recovery_link']}")
            if item.get("customer_message"):
                print(f"   Personalized Message: \"{item['customer_message']}\"")
        print()

    if len(result["results"]) > 10:
        print(f"... and {len(result['results']) - 10} more rows.")


if __name__ == "__main__":
    main()
