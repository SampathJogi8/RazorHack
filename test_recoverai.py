"""
Unit tests for RecoverAI
Tests NLP PTP extraction, guardrails circuit breakers, scenario orchestration, and REST APIs.
"""

import unittest
import json
from agent.guardrails import run_guardrails, GuardrailStatus, HaltReason
from agent.scenarios.receivables_chaser import run_receivables_guardrails
from agent.scenarios.ptp_tracker import extract_promise_from_text, CustomerIntent, PromiseRecord
from agent.orchestrator import RevenueRecoveryOrchestrator, ScenarioType
from fastapi.testclient import TestClient
from api import app


class TestGuardrails(unittest.TestCase):
    def test_fraud_halt(self):
        entity = {
            "id": "pay_test_fraud",
            "amount": 5000000,
            "error_code": "FRAUD_SUSPECTED",
            "notes": {}
        }
        res = run_guardrails(entity)
        self.assertEqual(res.status, GuardrailStatus.HALTED)
        self.assertEqual(res.halt_reason, HaltReason.RISK_POLICY)

    def test_dispute_halt(self):
        entity = {
            "id": "pay_test_dispute",
            "amount": 250000,
            "error_code": "BAD_REQUEST",
            "notes": {"dispute_raised": True}
        }
        res = run_guardrails(entity)
        self.assertEqual(res.status, GuardrailStatus.HALTED)
        self.assertEqual(res.halt_reason, HaltReason.DISPUTE_RAISED)

    def test_legal_halt(self):
        event = {
            "event_type": "invoice.overdue",
            "invoice_id": "INV-TEST-001",
            "amount_inr": 100000,
            "in_legal_proceedings": True,
            "days_overdue": 45,
            "customer": {"tier": "ENTERPRISE"}
        }
        res = run_receivables_guardrails(event)
        self.assertFalse(res.passed)
        self.assertEqual(res.halt_reason, "HALT_LEGAL_PROCEEDINGS")


class TestPTPTracker(unittest.TestCase):
    def test_promise_extraction(self):
        res = extract_promise_from_text(
            message="We will transfer ₹85,000 by Friday 4 PM.",
            invoice_id="INV-2024-1001",
            invoice_amount=85000.0,
            customer_name="Amit Saxena",
            customer_company="BlueSky Analytics",
        )
        self.assertEqual(res.intent, CustomerIntent.PROMISE_MADE)
        self.assertEqual(res.promised_amount_inr, 85000.0)
        self.assertIsNotNone(res.promised_date_iso)
        self.assertGreaterEqual(res.confidence_score, 0.8)

    def test_dispute_extraction(self):
        res = extract_promise_from_text(
            message="We are disputing this charge, goods were damaged.",
            invoice_id="INV-2024-1002",
            customer_name="Ravi Kumar",
        )
        self.assertEqual(res.intent, CustomerIntent.DISPUTE_RAISED)
        self.assertEqual(res.status.value, "DISPUTED")

    def test_refusal_extraction(self):
        res = extract_promise_from_text(
            message="Stop messaging me this is harassment unsubscribe immediately.",
            invoice_id="INV-2024-1003",
            customer_name="Vendor Lead",
        )
        self.assertEqual(res.intent, CustomerIntent.HARD_REFUSAL)
        self.assertEqual(res.recovery_action, "HALT_OPTED_OUT")


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.orch = RevenueRecoveryOrchestrator(prefer_llm=False)

    def test_process_clean_payment_failure(self):
        event = {
            "event_type": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_clean_test",
                        "amount": 150000,
                        "error_code": "GATEWAY_ERROR",
                        "error_description": "Payment failed due to temporary network error",
                        "notes": {"attempt_count": 0}
                    }
                }
            }
        }
        res = self.orch.process(event)
        self.assertEqual(res.status.value, "ACTIONED")
        self.assertGreater(res.recoverable_target_inr, 0)
        self.assertIsNotNone(res.recovery_link)


class TestAPIEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_status_endpoint(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("fixtures_ready", data)

    def test_metrics_endpoint(self):
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("available"))
        self.assertGreater(data.get("total_recoverable"), 0)

    def test_ptp_extract_endpoint(self):
        payload = {"message": "Will pay 25000 by tomorrow morning"}
        response = self.client.post("/api/ptp/extract", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["promised_amount_inr"], 25000.0)
        self.assertEqual(data["intent"], "PROMISE_MADE")

    def test_case_action_endpoint(self):
        payload = {
            "case_id": "INV-2024-1001",
            "scenario": "receivables_overdue",
            "action": "PAYMENT_LINK_WHATSAPP",
            "channel": "whatsapp"
        }
        response = self.client.post("/api/case/action", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("payment_link", data)



class TestAuthModule(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_demo_accounts(self):
        response = self.client.get("/api/auth/demo-accounts")
        self.assertEqual(response.status_code, 200)
        demos = response.json()
        self.assertGreaterEqual(len(demos), 3)
        emails = [d["email"] for d in demos]
        self.assertIn("ops@recoverai.io", emails)
        self.assertIn("cfo@razorpay.com", emails)

    def test_signin_success(self):
        payload = {"email": "ops@recoverai.io", "password": "recovery123"}
        response = self.client.post("/api/auth/signin", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("token", data)
        self.assertEqual(data["user"]["name"], "Priya Sharma")

    def test_signin_invalid_password(self):
        payload = {"email": "ops@recoverai.io", "password": "wrongpassword"}
        response = self.client.post("/api/auth/signin", json=payload)
        self.assertEqual(response.status_code, 401)

    def test_signup_and_me_and_logout(self):
        import uuid
        test_email = f"test_{uuid.uuid4().hex[:6]}@example.com"
        signup_payload = {
            "name": "Kunal Shah",
            "email": test_email,
            "company": "CRED Commerce",
            "role": "Chief Executive Officer",
            "password": "secretpassword123"
        }
        res_signup = self.client.post("/api/auth/signup", json=signup_payload)
        self.assertEqual(res_signup.status_code, 200)
        data_signup = res_signup.json()
        token = data_signup["token"]
        self.assertEqual(data_signup["user"]["name"], "Kunal Shah")

        # Test /api/auth/me
        res_me = self.client.get(f"/api/auth/me?token={token}")
        self.assertEqual(res_me.status_code, 200)
        self.assertEqual(res_me.json()["user"]["email"], test_email)

        # Test action stamping with this user
        action_payload = {
            "case_id": "INV-2024-1002",
            "scenario": "receivables_overdue",
            "action": "PAYMENT_LINK_WHATSAPP",
            "channel": "whatsapp",
            "approved_by": "Kunal Shah",
            "approved_role": "Chief Executive Officer"
        }
        res_action = self.client.post("/api/case/action", json=action_payload)
        self.assertEqual(res_action.status_code, 200)
        self.assertIn("Kunal Shah", res_action.json()["message"])

        # Test logout
        res_logout = self.client.post("/api/auth/logout", json={"token": token})
        self.assertEqual(res_logout.status_code, 200)

        # Confirm token is invalidated
        res_me_after = self.client.get(f"/api/auth/me?token={token}")
        self.assertEqual(res_me_after.status_code, 401)



class TestLLMIntegration(unittest.TestCase):
    def test_status_reports_llm_mode(self):
        client = TestClient(app)
        res = client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("llm_mode", data)
        self.assertIn("llm_model", data)
        self.assertIn("llm_live", data)

    def test_live_llm_or_fallback_diagnosis(self):
        from agent.recovery_engine import RecoveryEngine
        engine = RecoveryEngine(prefer_llm=True)
        payment_entity = {
            "id": "pay_test_llm_001",
            "amount": 499900,
            "error_code": "BAD_REQUEST_PAYMENT_TIMED_OUT",
            "error_description": "Payment was timed out",
            "error_source": "customer",
            "error_step": "payment_authentication",
            "error_reason": "payment_cancelled",
            "method": "card",
            "notes": {
                "customer_name": "Vikram Malhotra",
                "attempt_count": 1,
                "item_description": "Enterprise Annual License"
            }
        }
        result = engine.evaluate(payment_entity, "https://rzp.io/i/test_link_123")
        self.assertEqual(result.guardrail_status, "PASSED")
        self.assertIsNotNone(result.recovery_plan)
        self.assertIn(result.recovery_plan.diagnosed_by, ["llm", "rule_engine"])
        self.assertGreater(result.recovery_plan.confidence_score, 0.5)


class TestCSVIngestion(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_list_csv_samples(self):
        res = self.client.get("/api/csv/samples")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("samples", data)
        self.assertGreaterEqual(len(data["samples"]), 4)

    def test_download_csv_sample(self):
        res = self.client.get("/api/csv/download/razorpay_failed_payments.csv")
        self.assertEqual(res.status_code, 200)
        self.assertIn("payment_id", res.text)

    def test_run_csv_sample(self):
        payload = {"sample_name": "razorpay_failed_payments.csv", "prefer_llm": False}
        res = self.client.post("/api/csv/run-sample", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["scenario_detected"], "payment_failure")
        self.assertEqual(data["total_rows"], 10)
        self.assertGreater(data["total_recoverable_inr"], 0)

    def test_upload_csv(self):
        csv_content = """payment_id,amount,currency,status,method,bank,error_code,error_description,customer_name,email,contact,item_description,attempt_count,dispute_raised
pay_TEST_99,1999.00,INR,failed,upi,HDFC,GATEWAY_ERROR,Bank server timeout,Test User,test@example.com,+919876543210,Test Order,0,false"""
        files = {"file": ("test.csv", csv_content, "text/csv")}
        res = self.client.post("/api/csv/upload", files=files, data={"prefer_llm": "false"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_rows"], 1)


if __name__ == "__main__":
    unittest.main()


