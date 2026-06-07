"""
services/notifier.py
─────────────────────
Telegram Bot API notifier.

Sends HTML-formatted messages to users and persists every attempt
in the notifications table for audit and retry purposes.
Uses httpx for non-blocking HTTP calls.
"""

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import get_settings
from models.orm import Notification, NotificationStatus, User
from utils.logger import get_logger

logger = get_logger("notifier")
settings = get_settings()

_BASE_URL = f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}"

# ── Pre-built message templates ───────────────────────────────────────────────

class Templates:
    @staticmethod
    def deposit_success(amount: float, currency: str, balance: float) -> str:
        return (
            f"✅ <b>Deposit Confirmed</b>\n\n"
            f"Amount: <b>{amount:,.2f} {currency}</b>\n"
            f"New Balance: <b>{balance:,.2f} {currency}</b>"
        )

    @staticmethod
    def withdrawal_success(amount: float, currency: str) -> str:
        return (
            f"💸 <b>Withdrawal Processed</b>\n\n"
            f"Amount: <b>{amount:,.2f} {currency}</b>\n"
            f"Your funds are on their way."
        )

    @staticmethod
    def withdrawal_failed(amount: float, currency: str, reason: str) -> str:
        return (
            f"❌ <b>Withdrawal Failed</b>\n\n"
            f"Amount: <b>{amount:,.2f} {currency}</b>\n"
            f"Reason: {reason}\n"
            f"Please contact support if this continues."
        )

    @staticmethod
    def escrow_funds_held(order_id: str, amount: float, currency: str, channel_title: str) -> str:
        return (
            f"🔒 <b>Escrow Funds Held</b>\n\n"
            f"Order: <code>{order_id[:8]}…</code>\n"
            f"Channel: <b>{channel_title}</b>\n"
            f"Amount: <b>{amount:,.2f} {currency}</b>\n\n"
            f"Funds are securely held. The seller will now initiate the channel transfer."
        )

    @staticmethod
    def escrow_transfer_requested(order_id: str, channel_title: str, buyer_username: str) -> str:
        return (
            f"📢 <b>Transfer Requested</b>\n\n"
            f"Order: <code>{order_id[:8]}…</code>\n"
            f"Channel: <b>{channel_title}</b>\n"
            f"Buyer: @{buyer_username}\n\n"
            f"Please transfer admin rights to the buyer now."
        )

    @staticmethod
    def escrow_transfer_verify_prompt(order_id: str, channel_title: str) -> str:
        return (
            f"🔍 <b>Confirm Channel Transfer</b>\n\n"
            f"Order: <code>{order_id[:8]}…</code>\n"
            f"Channel: <b>{channel_title}</b>\n\n"
            f"Have you received full admin/owner rights? "
            f"Tap <b>Confirm</b> in the app to release funds to the seller."
        )

    @staticmethod
    def escrow_completed(order_id: str, amount: float, currency: str, channel_title: str) -> str:
        return (
            f"🎉 <b>Sale Complete!</b>\n\n"
            f"Order: <code>{order_id[:8]}…</code>\n"
            f"Channel: <b>{channel_title}</b>\n"
            f"Received: <b>{amount:,.2f} {currency}</b>\n\n"
            f"Funds have been released to your wallet."
        )

    @staticmethod
    def escrow_refunded(order_id: str, amount: float, currency: str) -> str:
        return (
            f"↩️ <b>Escrow Refunded</b>\n\n"
            f"Order: <code>{order_id[:8]}…</code>\n"
            f"Amount: <b>{amount:,.2f} {currency}</b>\n\n"
            f"Funds have been returned to your wallet."
        )

    @staticmethod
    def boost_activated(channel_title: str, boost_type: str, expires_at: datetime) -> str:
        return (
            f"🚀 <b>Boost Activated!</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n"
            f"Type: {boost_type.replace('_', ' ').title()}\n"
            f"Expires: {expires_at.strftime('%d %b %Y %H:%M UTC')}"
        )

    @staticmethod
    def boost_expiring_soon(channel_title: str, hours_left: int) -> str:
        return (
            f"⏳ <b>Boost Expiring Soon</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n"
            f"Expires in: <b>{hours_left} hours</b>\n\n"
            f"Renew now to keep your channel featured."
        )

    @staticmethod
    def boost_expired(channel_title: str) -> str:
        return (
            f"⏰ <b>Boost Expired</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n"
            f"Your boost has ended. Renew to continue promoting."
        )


# ── Core notifier ─────────────────────────────────────────────────────────────

class NotifierService:
    """
    Sends Telegram messages and records each attempt in the DB.

    Usage
    ─────
        notifier = NotifierService(db_session)
        await notifier.send(user, "Hello <b>world</b>!", reference_type="escrow", reference_id="abc")
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def send(
        self,
        user: User,
        message: str,
        *,
        parse_mode: str = "HTML",
        reference_type: str | None = None,
        reference_id: str | None = None,
    ) -> Notification:
        """Queue, send, and record a Telegram message."""
        notification = Notification(
            user_id=user.id,
            telegram_id=user.telegram_id,
            message_text=message,
            parse_mode=parse_mode,
            reference_type=reference_type,
            reference_id=reference_id,
            status=NotificationStatus.QUEUED,
        )
        self._db.add(notification)
        await self._db.flush()

        try:
            await self._dispatch(user.telegram_id, message, parse_mode)
            notification.status = NotificationStatus.SENT
            notification.sent_at = datetime.now(timezone.utc)
            logger.info("notification_sent", tg_id=user.telegram_id, ref=reference_id)
        except Exception as exc:
            notification.status = NotificationStatus.FAILED
            notification.failure_reason = str(exc)
            notification.retry_count += 1
            logger.error("notification_failed", tg_id=user.telegram_id, error=str(exc))

        return notification

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _dispatch(self, chat_id: int, text: str, parse_mode: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_BASE_URL}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
