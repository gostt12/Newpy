"""
api/webhooks.py
────────────────
Inbound webhook endpoints for Chapa, Stripe, and PayPal.

Security
────────
  • Every handler verifies the provider signature BEFORE touching the DB.
  • Raw request body is used for verification; never trust parsed JSON.
  • Idempotency: duplicate provider_reference values are silently skipped.
"""

from typing import Any

import stripe as stripe_lib
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from config.settings import get_settings
from models.orm import PaymentProvider, Transaction, TransactionStatus, User
from services.wallet_service import WalletService
from utils.logger import get_logger
from utils.security import verify_chapa_webhook, verify_paypal_webhook, verify_stripe_webhook

router = APIRouter(prefix="/webhook", tags=["webhooks"])
logger = get_logger("webhooks")
settings = get_settings()

stripe_lib.api_key = settings.stripe_secret_key.get_secret_value()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _get_user_by_telegram_id(db: AsyncSession, telegram_id: int) -> User | None:
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def _is_duplicate(db: AsyncSession, provider: PaymentProvider, reference: str) -> bool:
    """Return True if we already processed this provider reference."""
    result = await db.execute(
        select(Transaction).where(
            Transaction.provider == provider,
            Transaction.provider_reference == reference,
            Transaction.status == TransactionStatus.COMPLETED,
        )
    )
    return result.scalar_one_or_none() is not None


# ──────────────────────────────────────────────────────────────────────────────
# Chapa webhook  (ETB)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/chapa", status_code=status.HTTP_200_OK)
async def chapa_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    chapa_signature: str = Header(alias="Chapa-Signature"),
):
    """
    Chapa sends a POST with HMAC-SHA256 signature in `Chapa-Signature` header.
    Event type is determined by the `event` field in the payload.
    """
    raw_body = await request.body()

    try:
        payload: dict[str, Any] = verify_chapa_webhook(raw_body, chapa_signature)
    except ValueError as exc:
        logger.warning("chapa_signature_rejected", detail=str(exc))
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = payload.get("event", "")
    if event_type not in ("charge.success",):
        return {"status": "ignored", "event": event_type}

    ref = payload.get("tx_ref") or payload.get("reference", "")
    if await _is_duplicate(db, PaymentProvider.CHAPA, ref):
        logger.info("chapa_duplicate_skipped", ref=ref)
        return {"status": "duplicate"}

    # Resolve user from metadata (telegram_id is embedded in tx_ref or metadata)
    metadata: dict = payload.get("meta", {}) or {}
    telegram_id = int(metadata.get("telegram_id", 0))
    user = await _get_user_by_telegram_id(db, telegram_id) if telegram_id else None
    if user is None:
        logger.error("chapa_user_not_found", ref=ref, telegram_id=telegram_id)
        raise HTTPException(status_code=404, detail="User not found")

    wallet_svc = WalletService(db)
    tx = await wallet_svc.process_chapa_deposit(user, payload)
    logger.info("chapa_deposit_processed", tx_id=tx.id, user_id=user.id)
    return {"status": "ok", "tx_id": tx.id}


# ──────────────────────────────────────────────────────────────────────────────
# Stripe webhook  (USD)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe_signature: str = Header(alias="Stripe-Signature"),
):
    """
    Stripe sends a POST with signature in `Stripe-Signature` header.
    We handle `payment_intent.succeeded` events.
    """
    raw_body = await request.body()

    try:
        event = verify_stripe_webhook(raw_body, stripe_signature)
    except ValueError as exc:
        logger.warning("stripe_signature_rejected", detail=str(exc))
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] != "payment_intent.succeeded":
        return {"status": "ignored", "event": event["type"]}

    intent = event.data.object
    ref = intent.id

    if await _is_duplicate(db, PaymentProvider.STRIPE, ref):
        logger.info("stripe_duplicate_skipped", ref=ref)
        return {"status": "duplicate"}

    telegram_id = int(intent.metadata.get("telegram_id", 0))
    user = await _get_user_by_telegram_id(db, telegram_id) if telegram_id else None
    if user is None:
        logger.error("stripe_user_not_found", intent_id=ref, telegram_id=telegram_id)
        raise HTTPException(status_code=404, detail="User not found")

    wallet_svc = WalletService(db)
    tx = await wallet_svc.process_stripe_deposit(user, event)
    logger.info("stripe_deposit_processed", tx_id=tx.id, user_id=user.id)
    return {"status": "ok", "tx_id": tx.id}


# ──────────────────────────────────────────────────────────────────────────────
# PayPal webhook  (USD)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/paypal", status_code=status.HTTP_200_OK)
async def paypal_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    paypal_transmission_id: str = Header(alias="PAYPAL-TRANSMISSION-ID", default=""),
    paypal_transmission_time: str = Header(alias="PAYPAL-TRANSMISSION-TIME", default=""),
    paypal_cert_url: str = Header(alias="PAYPAL-CERT-URL", default=""),
    paypal_auth_algo: str = Header(alias="PAYPAL-AUTH-ALGO", default=""),
    paypal_transmission_sig: str = Header(alias="PAYPAL-TRANSMISSION-SIG", default=""),
):
    raw_body = await request.body()
    event_body = raw_body.decode()

    try:
        verify_paypal_webhook(
            transmission_id=paypal_transmission_id,
            timestamp=paypal_transmission_time,
            webhook_id=settings.paypal_webhook_id,
            event_body=event_body,
            cert_url=paypal_cert_url,
            actual_sig=paypal_transmission_sig,
            auth_algo=paypal_auth_algo,
        )
    except ValueError as exc:
        logger.warning("paypal_signature_rejected", detail=str(exc))
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json
    payload: dict = json.loads(event_body)
    event_type = payload.get("event_type", "")

    if event_type not in ("PAYMENT.CAPTURE.COMPLETED", "PAYMENT.SALE.COMPLETED"):
        return {"status": "ignored", "event": event_type}

    resource = payload.get("resource", {})
    ref = resource.get("id", "")
    if await _is_duplicate(db, PaymentProvider.PAYPAL, ref):
        logger.info("paypal_duplicate_skipped", ref=ref)
        return {"status": "duplicate"}

    # telegram_id stored in custom_id or invoice_id field
    telegram_id = int(resource.get("custom_id", 0) or resource.get("invoice_id", 0) or 0)
    user = await _get_user_by_telegram_id(db, telegram_id) if telegram_id else None
    if user is None:
        logger.error("paypal_user_not_found", ref=ref, telegram_id=telegram_id)
        raise HTTPException(status_code=404, detail="User not found")

    wallet_svc = WalletService(db)
    tx = await wallet_svc.process_paypal_deposit(user, payload)
    logger.info("paypal_deposit_processed", tx_id=tx.id, user_id=user.id)
    return {"status": "ok", "tx_id": tx.id}
