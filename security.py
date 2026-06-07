"""
utils/security.py
─────────────────
HMAC-based webhook verification helpers for Chapa, Stripe, and PayPal.
All functions raise ValueError on invalid signatures so callers can
return HTTP 400 / 401 immediately.
"""

import hashlib
import hmac
import json
import time
from typing import Any

import stripe as stripe_lib

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("security")
settings = get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# Chapa
# ──────────────────────────────────────────────────────────────────────────────

def verify_chapa_webhook(payload: bytes, signature_header: str) -> dict[str, Any]:
    """
    Verify a Chapa webhook HMAC-SHA256 signature.

    Chapa sends:  Chapa-Signature: sha256=<hex_digest>
    Raises ValueError if invalid.
    Returns parsed JSON payload on success.
    """
    secret = settings.chapa_webhook_secret.get_secret_value().encode()
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()

    # Header format: "sha256=<hex>"
    parts = signature_header.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        raise ValueError("Malformed Chapa-Signature header")

    received = parts[1]
    if not hmac.compare_digest(expected, received):
        logger.warning("chapa_webhook_signature_mismatch")
        raise ValueError("Chapa webhook signature verification failed")

    return json.loads(payload)


# ──────────────────────────────────────────────────────────────────────────────
# Stripe
# ──────────────────────────────────────────────────────────────────────────────

def verify_stripe_webhook(payload: bytes, sig_header: str) -> stripe_lib.Event:
    """
    Verify a Stripe webhook using the official SDK.
    Raises stripe.error.SignatureVerificationError on failure.
    """
    secret = settings.stripe_webhook_secret.get_secret_value()
    try:
        event = stripe_lib.Webhook.construct_event(payload, sig_header, secret)
        return event
    except stripe_lib.error.SignatureVerificationError as exc:
        logger.warning("stripe_webhook_signature_mismatch", error=str(exc))
        raise ValueError(f"Stripe signature verification failed: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# PayPal
# ──────────────────────────────────────────────────────────────────────────────

def verify_paypal_webhook(
    transmission_id: str,
    timestamp: str,
    webhook_id: str,
    event_body: str,
    cert_url: str,
    actual_sig: str,
    auth_algo: str,
) -> bool:
    """
    PayPal webhook verification (simplified HMAC approach for sandbox/dev).

    In production you should validate against PayPal's certificate chain.
    Returns True on success; raises ValueError on failure.
    """
    expected_message = f"{transmission_id}|{timestamp}|{webhook_id}|{_crc32(event_body)}"
    secret = settings.paypal_client_secret.get_secret_value().encode()
    computed = hmac.new(secret, expected_message.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, actual_sig):
        logger.warning("paypal_webhook_signature_mismatch")
        raise ValueError("PayPal webhook signature verification failed")
    return True


def _crc32(data: str) -> str:
    import binascii
    return str(binascii.crc32(data.encode()) & 0xFFFFFFFF)


# ──────────────────────────────────────────────────────────────────────────────
# Telegram Mini App init-data verification
# ──────────────────────────────────────────────────────────────────────────────

def verify_telegram_init_data(init_data: str) -> dict[str, Any]:
    """
    Validate Telegram Mini App initData to authenticate the user.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Returns parsed data dict on success; raises ValueError on failure.
    """
    from urllib.parse import parse_qsl, unquote
    import base64

    pairs = dict(parse_qsl(unquote(init_data)))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing hash in init_data")

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(pairs.items())
    )

    bot_token = settings.telegram_bot_token.get_secret_value().encode()
    secret_key = hmac.new(b"WebAppData", bot_token, hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise ValueError("Telegram initData hash verification failed")

    # Optionally check timestamp freshness (5 min window)
    auth_date = int(pairs.get("auth_date", 0))
    if time.time() - auth_date > 300:
        raise ValueError("Telegram initData is expired")

    return pairs
