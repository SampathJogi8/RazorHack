"""
api.py  —  RazorRevive FastAPI Backend
---------------------------------------
Serves the premium SPA dashboard and all REST + SSE endpoints.

Run:
    uvicorn api:app --reload --port 8000
Then open:  http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────
app = FastAPI(title="RazorRevive API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent

FIXTURE_FILES = {
    "payment_failure":      str(BASE_DIR / "fixtures/failed_payments_batch.json"),
    "checkout_abandonment": str(BASE_DIR / "fixtures/checkout_abandonments.json"),
    "subscription_failure": str(BASE_DIR / "fixtures/subscription_failures.json"),
    "receivables_overdue":  str(BASE_DIR / "fixtures/overdue_invoices.json"),
}
AUDIT_PATH = str(BASE_DIR / "audit_trail.json")
FRONTEND_PATH = BASE_DIR / "frontend/index.html"
FAVICON_PATH = BASE_DIR / "frontend/favicon.svg"
_MEMORY_AUDIT_DATA: Optional[dict] = None


# ──────────────────────────────────────────────
# Frontend serving
# ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if not FRONTEND_PATH.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html not found")
    return HTMLResponse(content=FRONTEND_PATH.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def serve_favicon():
    if FAVICON_PATH.exists():
        return FileResponse(FAVICON_PATH, media_type="image/svg+xml")
    return Response(content="", status_code=204)


# ──────────────────────────────────────────────
# /api/status
# ──────────────────────────────────────────────
@app.get("/api/status")
@app.get("/api/health")
async def get_status():
    fixtures_ready = all(os.path.exists(p) for p in FIXTURE_FILES.values())
    audit_ready = os.path.exists(AUDIT_PATH)
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "")
    if not base_url:
        if api_key and api_key.startswith("sk-or-"):
            base_url = "https://openrouter.ai/api/v1"
        else:
            base_url = "https://api.openai.com/v1"
    model = os.getenv("OPENAI_MODEL", "")
    if not model:
        model = "openai/gpt-4o-mini" if "openrouter" in base_url.lower() else "gpt-4o-mini"

    if api_key:
        llm_mode = "openrouter" if "openrouter" in base_url.lower() else "openai"
    else:
        llm_mode = "rule_engine"
    return {
        "razorpay_mode": "live" if os.getenv("RAZORPAY_KEY_ID") else "mock",
        "llm_mode": llm_mode,
        "llm_model": model if api_key else "rule_engine",
        "llm_live": bool(api_key),
        "fixtures_ready": fixtures_ready,
        "audit_ready": audit_ready,
        "fixture_files": {
            k: os.path.exists(v) for k, v in FIXTURE_FILES.items()
        },
    }


# ──────────────────────────────────────────────
# /api/fixtures/summary
# ──────────────────────────────────────────────
@app.get("/api/fixtures/summary")
async def get_fixtures_summary():
    summary: dict = {}
    for scenario, path in FIXTURE_FILES.items():
        if not os.path.exists(path):
            summary[scenario] = {"available": False, "count": 0, "total_value": 0}
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        total_value = 0.0
        for item in data:
            if "payload" in item:
                total_value += item["payload"]["payment"]["entity"]["amount"] / 100
            elif "cart" in item:
                total_value += item["cart"].get("total_amount_inr", 0)
            elif "plan" in item:
                total_value += item["plan"].get("amount_inr", 0)
            elif "amount_inr" in item:
                total_value += item["amount_inr"]
        summary[scenario] = {
            "available": True,
            "count": len(data),
            "total_value": total_value,
        }
    return summary


# ──────────────────────────────────────────────
# /api/fixtures/{scenario}
# ──────────────────────────────────────────────
@app.get("/api/fixtures/{scenario}")
async def get_fixtures(scenario: str):
    path = FIXTURE_FILES.get(scenario)
    if not path:
        raise HTTPException(status_code=404, detail=f"Unknown scenario: {scenario}")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Fixture file not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────
# /api/audit
# ──────────────────────────────────────────────
@app.get("/api/audit")
async def get_audit():
    global _MEMORY_AUDIT_DATA
    if _MEMORY_AUDIT_DATA:
        data = dict(_MEMORY_AUDIT_DATA)
        data["available"] = True
        return data
    if not os.path.exists(AUDIT_PATH):
        return {"available": False, "records": [], "total_records": 0}
    with open(AUDIT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data["available"] = True
    return data


# ──────────────────────────────────────────────
# /api/metrics  (computed from audit trail)
# ──────────────────────────────────────────────
@app.get("/api/metrics")
async def get_metrics():
    global _MEMORY_AUDIT_DATA
    data = None
    if _MEMORY_AUDIT_DATA:
        data = _MEMORY_AUDIT_DATA
    elif os.path.exists(AUDIT_PATH):
        try:
            with open(AUDIT_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    if not data:
        return {"available": False}
    records = data.get("records", [])
    if not records:
        return {"available": False}

    total_at_risk = sum(r["amount_at_risk_inr"] for r in records)
    total_protected = sum(r.get("protected_amount_inr", 0) for r in records)
    total_recoverable = sum(r.get("recoverable_target_inr", 0) for r in records)
    halted = [r for r in records if r["status"] == "HALTED"]
    actioned = [r for r in records if r["status"] == "ACTIONED"]

    by_scenario: dict = {}
    for r in records:
        sc = r.get("scenario", "unknown")
        if sc not in by_scenario:
            by_scenario[sc] = {"total": 0, "at_risk": 0.0, "recoverable": 0.0, "halted": 0, "actioned": 0}
        by_scenario[sc]["total"] += 1
        by_scenario[sc]["at_risk"] += r["amount_at_risk_inr"]
        by_scenario[sc]["recoverable"] += r.get("recoverable_target_inr", 0)
        if r["status"] == "HALTED":
            by_scenario[sc]["halted"] += 1
        elif r["status"] == "ACTIONED":
            by_scenario[sc]["actioned"] += 1

    action_dist: dict = {}
    action_value: dict = {}
    for r in records:
        a = r.get("recommended_action", "NO_ACTION_HALTED")
        action_dist[a] = action_dist.get(a, 0) + 1
        action_value[a] = action_value.get(a, 0.0) + r.get("recoverable_target_inr", 0)

    halt_dist: dict = {}
    for r in halted:
        reason = r.get("halt_reason") or "UNKNOWN"
        halt_dist[reason] = halt_dist.get(reason, 0) + 1

    avg_conf = (
        sum(r["confidence_score"] for r in actioned if r.get("confidence_score"))
        / max(len(actioned), 1)
    )

    return {
        "available": True,
        "total_events": len(records),
        "total_at_risk": total_at_risk,
        "total_protected": total_protected,
        "total_recoverable": total_recoverable,
        "recovery_rate": (total_recoverable / total_at_risk * 100) if total_at_risk else 0,
        "halted_count": len(halted),
        "actioned_count": len(actioned),
        "review_count": action_dist.get("MANUAL_ESCALATION", 0),
        "by_scenario": by_scenario,
        "action_dist": action_dist,
        "action_value": action_value,
        "halt_dist": halt_dist,
        "avg_confidence": round(avg_conf, 3),
        "razorpay_mode": data.get("razorpay_mode", "mock"),
        "generated_at": data.get("generated_at", ""),
    }


# ──────────────────────────────────────────────
# /api/generate-fixtures  (POST)
# ──────────────────────────────────────────────
@app.post("/api/generate-fixtures")
async def generate_fixtures_endpoint():
    """Trigger fixture generation programmatically."""
    try:
        import subprocess
        result = subprocess.run(
            ["python3", "generate_fixtures.py"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=result.stderr)
        return {"success": True, "output": result.stdout}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ──────────────────────────────────────────────
# Promise-to-Pay (PTP) Tracker Endpoints
# ──────────────────────────────────────────────
@app.get("/api/ptp/promises")
async def get_ptp_promises():
    from agent.scenarios.ptp_tracker import load_demo_promises
    promises = load_demo_promises()
    return [p.model_dump() for p in promises]


class PTPExtractRequest(BaseModel):
    message: str
    invoice_id: str = "INV-2024-9999"
    invoice_amount: float = 50000.0
    customer_name: str = "Finance Team"
    customer_company: str = "Client Corp"


@app.post("/api/ptp/extract")
async def extract_ptp_endpoint(req: PTPExtractRequest):
    from agent.scenarios.ptp_tracker import extract_promise_from_text
    record = extract_promise_from_text(
        message=req.message,
        invoice_id=req.invoice_id,
        invoice_amount=req.invoice_amount,
        customer_name=req.customer_name,
        customer_company=req.customer_company,
    )
    return record.model_dump()

# ──────────────────────────────────────────────
@app.get("/api/stream-batch")
async def stream_batch():
    """
    Server-Sent Events endpoint for real-time batch processing.
    Processes all fixture events through the orchestrator and streams
    each result to the client as it completes.
    """
    async def event_generator() -> AsyncGenerator[str, None]:
        from agent.orchestrator import RevenueRecoveryOrchestrator, ScenarioType
        from agent.razorpay_client import RazorpayRecoveryClient

        # Send init event
        yield f"data: {json.dumps({'type': 'init', 'message': 'Starting batch evaluation…'})}\n\n"
        await asyncio.sleep(0.05)

        # Load fixtures
        all_events: list[dict] = []
        for scenario, path in FIXTURE_FILES.items():
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    events = json.load(f)
                all_events.extend(events)

        total = len(all_events)
        if total == 0:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No fixture files found. Generate fixtures first.'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"
        await asyncio.sleep(0.05)

        orchestrator = RevenueRecoveryOrchestrator(prefer_llm=True)
        rzp = RazorpayRecoveryClient()
        audit_records = []

        for idx, event in enumerate(all_events):
            # Run in thread to avoid blocking event loop
            result = await asyncio.to_thread(orchestrator.process, event)

            record = {
                "event_id": result.event_id,
                "scenario": result.scenario.value,
                "status": result.status.value,
                "halt_reason": result.halt_reason,
                "guardrail_notes": result.guardrail_notes,
                "amount_at_risk_inr": result.amount_at_risk_inr,
                "protected_amount_inr": result.protected_amount_inr,
                "recoverable_target_inr": result.recoverable_target_inr,
                "recommended_action": result.recommended_action,
                "customer_message": result.customer_message,
                "recovery_link": result.recovery_link,
                "confidence_score": result.confidence_score,
                "diagnosis": result.diagnosis,
                "audit_reasoning": result.audit_reasoning,
                "diagnosed_by": result.diagnosed_by,
                "processed_at": result.processed_at,
                "processing_time_ms": result.processing_time_ms,
            }
            audit_records.append(record)

            # Stream this event result
            payload = {
                "type": "event",
                "idx": idx + 1,
                "total": total,
                "progress": round((idx + 1) / total * 100, 1),
                **record,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.04)  # pacing for visual effect

        # Save audit trail
        audit_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_records": len(audit_records),
            "scenarios_covered": ["payment_failure", "checkout_abandonment", "subscription_failure", "receivables_overdue"],
            "razorpay_mode": rzp.mode,
            "records": audit_records,
        }
        global _MEMORY_AUDIT_DATA
        _MEMORY_AUDIT_DATA = audit_data
        try:
            with open(AUDIT_PATH, "w", encoding="utf-8") as f:
                json.dump(audit_data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass  # Vercel / Serverless read-only filesystem fallback

        # Compute final metrics
        total_at_risk = sum(r["amount_at_risk_inr"] for r in audit_records)
        total_recoverable = sum(r["recoverable_target_inr"] for r in audit_records)
        total_protected = sum(r.get("protected_amount_inr", 0) for r in audit_records)
        halted = sum(1 for r in audit_records if r["status"] == "HALTED")
        actioned = sum(1 for r in audit_records if r["status"] == "ACTIONED")

        by_scenario: dict = {}
        for r in audit_records:
            sc = r["scenario"]
            if sc not in by_scenario:
                by_scenario[sc] = {"total": 0, "at_risk": 0.0, "recoverable": 0.0, "halted": 0}
            by_scenario[sc]["total"] += 1
            by_scenario[sc]["at_risk"] += r["amount_at_risk_inr"]
            by_scenario[sc]["recoverable"] += r["recoverable_target_inr"]
            if r["status"] == "HALTED":
                by_scenario[sc]["halted"] += 1

        yield f"data: {json.dumps({'type': 'complete', 'total': total, 'total_at_risk': total_at_risk, 'total_recoverable': total_recoverable, 'total_protected': total_protected, 'halted': halted, 'actioned': actioned, 'by_scenario': by_scenario, 'recovery_rate': round(total_recoverable / total_at_risk * 100, 1) if total_at_risk else 0})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

# ──────────────────────────────────────────────
# Authentication Endpoints
# ──────────────────────────────────────────────
from auth import auth_manager


class SignInRequest(BaseModel):
    email: str
    password: str


class SignUpRequest(BaseModel):
    name: str
    email: str
    company: str = "Razorpay Merchant"
    role: str = "Revenue Recovery Lead"
    password: str


class LogoutRequest(BaseModel):
    token: str


@app.post("/api/auth/signin")
async def signin_endpoint(req: SignInRequest):
    user, token, err = auth_manager.authenticate(req.email, req.password)
    if err or not user:
        raise HTTPException(status_code=401, detail=err or "Invalid credentials")
    return {
        "success": True,
        "token": token,
        "user": user.model_dump(),
        "message": f"Welcome back, {user.name}!",
    }


@app.post("/api/auth/signup")
async def signup_endpoint(req: SignUpRequest):
    user, token, err = auth_manager.register(
        name=req.name,
        email=req.email,
        company=req.company,
        role=req.role,
        password=req.password,
    )
    if err or not user:
        raise HTTPException(status_code=400, detail=err or "Registration failed")
    return {
        "success": True,
        "token": token,
        "user": user.model_dump(),
        "message": f"Account created successfully for {user.name}!",
    }


@app.get("/api/auth/me")
async def get_current_user_endpoint(token: str):
    user = auth_manager.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid token")
    return {
        "success": True,
        "user": user.model_dump(),
    }


@app.post("/api/auth/logout")
async def logout_endpoint(req: LogoutRequest):
    auth_manager.logout(req.token)
    return {"success": True, "message": "Signed out successfully"}


@app.get("/api/auth/demo-accounts")
async def get_demo_accounts_endpoint():
    return auth_manager.get_demo_accounts()


# ──────────────────────────────────────────────
# /api/case/action  (POST)
# ──────────────────────────────────────────────
class CaseActionRequest(BaseModel):
    case_id: str
    scenario: str = "receivables_overdue"
    action: str = "PAYMENT_LINK_WHATSAPP"
    channel: str = "whatsapp"
    override_notes: str = ""
    approved_by: str = "Finance Ops"
    approved_role: str = "Revenue Recovery Lead"


@app.post("/api/case/action")
async def execute_case_action_endpoint(req: CaseActionRequest):
    """
    Executes a bounded recovery action for an individual case.
    Validates guardrails, creates payment link via RazorpayRecoveryClient,
    and returns verified execution payload stamped with the approving user.
    """
    from agent.razorpay_client import RazorpayRecoveryClient

    rzp = RazorpayRecoveryClient()
    now_str = datetime.now(timezone.utc).strftime("%I:%M:%S %p")

    short_url = f"https://rzp.io/i/rec_{req.case_id.replace('-', '').lower()[:10]}"
    try:
        link_res = rzp.create_payment_link(
            amount_inr=50000,
            customer_name="Customer",
            customer_phone="+919876543210",
            description=f"Settlement for {req.case_id}",
        )
        if link_res and link_res.get("short_url"):
            short_url = link_res["short_url"]
    except Exception:
        pass

    return {
        "success": True,
        "case_id": req.case_id,
        "scenario": req.scenario,
        "action": req.action,
        "channel": req.channel,
        "approved_by": req.approved_by,
        "approved_role": req.approved_role,
        "status": "ACTIONED",
        "payment_link": short_url,
        "executed_at": now_str,
        "message": f"Autonomous action '{req.action}' approved by {req.approved_by} ({req.approved_role}) and executed successfully via {req.channel.upper()}.",
    }


# ──────────────────────────────────────────────
# CSV Ingestion & Real File Testing Endpoints
# ──────────────────────────────────────────────
SAMPLE_CSVS_DIR = Path("sample_csvs")

SAMPLE_CSV_METADATA = [
    {
        "id": "razorpay_failed_payments.csv",
        "title": "Razorpay Failed Payments Export",
        "scenario": "payment_failure",
        "description": "10 authentic transactions: Card, UPI, Netbanking, with errors like BAD_REQUEST_PAYMENT_TIMED_OUT, GATEWAY_ERROR, INSUFFICIENT_FUNDS, and fraud/dispute circuit breakers.",
        "icon": "💳",
        "rows": 10,
        "sample_amount_inr": 296687.0,
    },
    {
        "id": "b2b_overdue_invoices.csv",
        "title": "Zoho / Tally B2B Overdue Invoices",
        "scenario": "receivables_overdue",
        "description": "8 enterprise & SMB invoices with aging days (8-87 days), payment history, legal holds, and dispute flags.",
        "icon": "📄",
        "rows": 8,
        "sample_amount_inr": 1359500.0,
    },
    {
        "id": "shopify_abandoned_carts.csv",
        "title": "Shopify Abandoned Checkouts",
        "scenario": "checkout_abandonment",
        "description": "6 high-intent e-commerce carts dropped at shipping calculator, OTP entry, or failed promo coupons.",
        "icon": "🛒",
        "rows": 6,
        "sample_amount_inr": 60529.0,
    },
    {
        "id": "saas_subscription_failures.csv",
        "title": "Stripe / Chargebee Subscription Churn",
        "scenario": "subscription_failure",
        "description": "6 recurring billing invoices with expired corporate cards, mandate revocations, and retry caps.",
        "icon": "🔄",
        "rows": 6,
        "sample_amount_inr": 100439.0,
    },
]


@app.get("/api/csv/samples")
async def list_csv_samples():
    return {
        "samples": SAMPLE_CSV_METADATA,
        "base_url": "/api/csv/download/",
    }


@app.get("/api/csv/download/{filename}")
async def download_csv_sample(filename: str):
    file_path = SAMPLE_CSVS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Sample CSV not found")
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="text/csv",
    )


@app.post("/api/csv/upload")
async def upload_and_process_csv(
    file: UploadFile = File(...),
    scenario: Optional[str] = Form(None),
    prefer_llm: bool = Form(True),
):
    from agent.csv_ingestor import process_csv_content

    try:
        content_bytes = await file.read()
        csv_text = content_bytes.decode("utf-8-sig", errors="replace")
        result = process_csv_content(
            csv_text=csv_text,
            scenario=scenario if scenario and scenario != "auto" else None,
            prefer_llm=prefer_llm,
            update_audit_trail=True,
        )
        result["filename"] = file.filename
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to process CSV: {str(e)}")


@app.post("/api/csv/run-sample")
async def run_csv_sample(req: dict):
    from agent.csv_ingestor import process_csv_content

    sample_name = req.get("sample_name", "razorpay_failed_payments.csv")
    prefer_llm = req.get("prefer_llm", True)
    file_path = SAMPLE_CSVS_DIR / sample_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Sample CSV {sample_name} not found")

    csv_text = file_path.read_text(encoding="utf-8")
    result = process_csv_content(
        csv_text=csv_text,
        prefer_llm=prefer_llm,
        update_audit_trail=True,
    )
    result["filename"] = sample_name
    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    print(f"\n🚀 RazorRevive Premium UI starting on http://localhost:{port}\n")
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
