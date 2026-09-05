"""
agent/razorpay_client.py
------------------------
Razorpay Payment Links client.

- If RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET are set: calls the real Razorpay
  Payment Links API (test mode).
- Otherwise: returns a fully-formed deterministic mock link so the demo runs
  without any credentials.

Razorpay API docs: https://razorpay.com/docs/api/payment-links/
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
RAZORPAY_PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links"
REQUEST_TIMEOUT_SECONDS = 10


# ──────────────────────────────────────────────
# Mock link generator (offline / no-key mode)
# ──────────────────────────────────────────────

def _mock_payment_link(payment_id: str, amount_inr: float) -> dict:
    """
    Deterministically generate a realistic mock Razorpay payment link.
    The link slug is derived from the payment_id so it is stable across runs.
    """
    slug = hashlib.md5(payment_id.encode()).hexdigest()[:8]
    short_id = f"recov_{slug}"
    return {
        "id": f"plink_{slug}",
        "short_url": f"https://rzp.io/i/{short_id}",
        "status": "created",
        "amount": int(amount_inr * 100),
        "currency": "INR",
        "description": f"Recovery link for {payment_id}",
        "mock": True,
    }


# ──────────────────────────────────────────────
# RazorpayRecoveryClient
# ──────────────────────────────────────────────

class RazorpayRecoveryClient:
    """
    Creates Razorpay Payment Links for dunning / recovery campaigns.

    Instantiation auto-detects credentials from environment:
        RAZORPAY_KEY_ID      → Razorpay API Key (test_ prefix for test mode)
        RAZORPAY_KEY_SECRET  → Razorpay API Secret

    If not set, all calls return mock responses without any network I/O.
    """

    def __init__(self):
        self.key_id: Optional[str] = os.getenv("RAZORPAY_KEY_ID")
        self.key_secret: Optional[str] = os.getenv("RAZORPAY_KEY_SECRET")
        self.live: bool = bool(self.key_id and self.key_secret)

    @property
    def mode(self) -> str:
        return "live" if self.live else "mock"

    def create_payment_link(
        self,
        payment_id: str,
        amount_inr: float,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        description: str,
        expiry_hours: int = 24,
        notify_sms: bool = True,
        notify_email: bool = True,
    ) -> dict:
        """
        Create a Razorpay Payment Link for recovery dunning.

        Args:
            payment_id:     Original failed payment ID (used as reference).
            amount_inr:     Amount in Indian Rupees (will be converted to paise).
            customer_name:  Customer display name.
            customer_email: Customer email for Razorpay notification.
            customer_phone: Customer phone (E.164 format, e.g. +919876543210).
            description:    Line item / product description.
            expiry_hours:   Hours until the link expires (default 24h).
            notify_sms:     Send SMS notification via Razorpay (if live).
            notify_email:   Send email notification via Razorpay (if live).

        Returns:
            dict with at minimum: id, short_url, status, amount, currency, mock (bool).
        """
        if not self.live:
            return _mock_payment_link(payment_id, amount_inr)

        # ── Real Razorpay API call ────────────────────────
        expire_by = int(time.time()) + (expiry_hours * 3600)
        amount_paise = int(round(amount_inr * 100))

        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": description[:255],  # API limit
            "customer": {
                "name": customer_name,
                "email": customer_email,
                "contact": customer_phone,
            },
            "notify": {
                "sms": notify_sms,
                "email": notify_email,
            },
            "reminder_enable": True,
            "notes": {
                "original_payment_id": payment_id,
                "recovery_source": "razorrevive",
            },
            "callback_url": os.getenv(
                "RAZORPAY_CALLBACK_URL", "https://razorrevive.demo/callback"
            ),
            "callback_method": "get",
            "expire_by": expire_by,
        }

        try:
            resp = requests.post(
                RAZORPAY_PAYMENT_LINKS_URL,
                json=payload,
                auth=HTTPBasicAuth(self.key_id, self.key_secret),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            data["mock"] = False
            return data

        except requests.exceptions.Timeout:
            # Gateway timeout — return mock to keep batch processing alive
            fallback = _mock_payment_link(payment_id, amount_inr)
            fallback["error"] = "Razorpay API timeout — mock link returned"
            return fallback

        except requests.exceptions.HTTPError as exc:
            fallback = _mock_payment_link(payment_id, amount_inr)
            fallback["error"] = f"Razorpay API error {exc.response.status_code}: {exc.response.text[:200]}"
            return fallback

        except Exception as exc:
            fallback = _mock_payment_link(payment_id, amount_inr)
            fallback["error"] = f"Unexpected error: {str(exc)}"
            return fallback
