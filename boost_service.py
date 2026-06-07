"""
services/boost_service.py
──────────────────────────
Boosting & Advertising Module.

Responsibilities
────────────────
  • Purchase a boost (deducts from wallet, creates Boost record, activates)
  • Activate / deactivate boosts
  • Scheduled expiry checks (called by APScheduler every hour)
  • Pre-expiry reminder notifications (24 h before)
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.orm import (
    Boost,
    BoostStatus,
    BoostType,
    Channel,
    Currency,
    PaymentProvider,
    TransactionType,
    User,
)
from services.notifier import NotifierService, Templates
from services.wallet_service import InsufficientFundsError, WalletService
from utils.logger import get_logger

logger = get_logger("boost_service")
settings = get_settings()

# Pricing table: (BoostType, duration_days) → amount in currency
BOOST_PRICING: dict[tuple[str, int], tuple[float, Currency]] = {
    ("channel_promotion", 7):  (500.0,  Currency.ETB),
    ("channel_promotion", 14): (900.0,  Currency.ETB),
    ("channel_promotion", 30): (1500.0, Currency.ETB),
    ("ad_banner", 7):          (10.0,   Currency.USD),
    ("ad_banner", 14):         (18.0,   Currency.USD),
    ("ad_banner", 30):         (30.0,   Currency.USD),
    ("featured_listing", 7):   (350.0,  Currency.ETB),
    ("featured_listing", 14):  (600.0,  Currency.ETB),
    ("featured_listing", 30):  (1000.0, Currency.ETB),
}


class BoostError(Exception):
    pass


class BoostService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._wallet = WalletService(db)
        self._notifier = NotifierService(db)

    # ── Purchase ──────────────────────────────────────────────────────────────

    async def purchase_boost(
        self,
        user: User,
        channel: Channel,
        boost_type: BoostType,
        duration_days: int,
        *,
        metadata: dict | None = None,
    ) -> Boost:
        """
        Deduct the boost price from the user's wallet and activate the boost.
        """
        key = (boost_type.value, duration_days)
        if key not in BOOST_PRICING:
            raise BoostError(f"No pricing defined for {boost_type.value} × {duration_days}d")

        price, currency = BOOST_PRICING[key]
        amount = Decimal(str(price))

        # Deduct from wallet
        try:
            tx = await self._wallet.debit(
                user,
                amount,
                currency,
                TransactionType.BOOST_PURCHASE,
                PaymentProvider.INTERNAL,
                reference_type="boost",
                description=f"{boost_type.value} for {channel.title} ({duration_days}d)",
            )
        except InsufficientFundsError as exc:
            raise BoostError(f"Insufficient funds: {exc}") from exc

        # Create boost record
        import json
        now = datetime.now(timezone.utc)
        boost = Boost(
            user_id=user.id,
            channel_id=channel.id,
            type=boost_type,
            status=BoostStatus.ACTIVE,
            currency=currency,
            amount_paid=float(amount),
            duration_days=duration_days,
            starts_at=now,
            expires_at=now + timedelta(days=duration_days),
            payment_transaction_id=tx.id,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        self._db.add(boost)
        await self._db.flush()

        # Update boost transaction reference
        tx.reference_id = boost.id
        await self._db.flush()

        await self._notifier.send(
            user,
            Templates.boost_activated(channel.title, boost_type.value, boost.expires_at),
            reference_type="boost",
            reference_id=boost.id,
        )

        logger.info(
            "boost_purchased",
            boost_id=boost.id,
            user_id=user.id,
            channel_id=channel.id,
            type=boost_type.value,
            duration=duration_days,
        )
        return boost

    # ── Manual activate / deactivate ──────────────────────────────────────────

    async def activate(self, boost: Boost) -> Boost:
        if boost.status != BoostStatus.PENDING:
            raise BoostError(f"Boost is {boost.status}, cannot activate")
        now = datetime.now(timezone.utc)
        boost.status = BoostStatus.ACTIVE
        boost.starts_at = now
        boost.expires_at = now + timedelta(days=boost.duration_days)
        await self._db.flush()
        logger.info("boost_activated", boost_id=boost.id)
        return boost

    async def cancel(self, boost: Boost, reason: str = "") -> Boost:
        if boost.status not in (BoostStatus.PENDING, BoostStatus.ACTIVE):
            raise BoostError(f"Cannot cancel boost in state {boost.status}")
        boost.status = BoostStatus.CANCELLED
        await self._db.flush()
        logger.info("boost_cancelled", boost_id=boost.id, reason=reason)
        return boost

    # ── Scheduled expiry checks ───────────────────────────────────────────────

    async def expire_boosts(self) -> list[str]:
        """
        Deactivate all boosts that have passed their expiry time.
        Called by the scheduler every hour.
        Returns list of expired boost IDs.
        """
        now = datetime.now(timezone.utc)
        result = await self._db.execute(
            select(Boost).where(
                Boost.status == BoostStatus.ACTIVE,
                Boost.expires_at <= now,
            )
        )
        expired = result.scalars().all()
        expired_ids = []

        for boost in expired:
            boost.status = BoostStatus.EXPIRED
            expired_ids.append(boost.id)

            # Notify user
            try:
                user_result = await self._db.execute(select(User).where(User.id == boost.user_id))
                user = user_result.scalar_one_or_none()
                if user and boost.channel_id:
                    channel_result = await self._db.execute(
                        select(Channel).where(Channel.id == boost.channel_id)
                    )
                    channel = channel_result.scalar_one_or_none()
                    if channel:
                        await self._notifier.send(
                            user,
                            Templates.boost_expired(channel.title),
                            reference_type="boost",
                            reference_id=boost.id,
                        )
            except Exception as exc:
                logger.error("boost_expiry_notify_failed", boost_id=boost.id, error=str(exc))

        await self._db.flush()
        if expired_ids:
            logger.info("boosts_expired", count=len(expired_ids))
        return expired_ids

    async def send_expiry_reminders(self, hours_ahead: int = 24) -> list[str]:
        """
        Send reminders for boosts expiring within `hours_ahead` hours.
        Called by the scheduler.
        """
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=hours_ahead)

        result = await self._db.execute(
            select(Boost).where(
                Boost.status == BoostStatus.ACTIVE,
                Boost.expires_at > now,
                Boost.expires_at <= window_end,
            )
        )
        upcoming = result.scalars().all()
        reminded_ids = []

        for boost in upcoming:
            hours_left = int((boost.expires_at - now).total_seconds() / 3600)
            try:
                user_result = await self._db.execute(select(User).where(User.id == boost.user_id))
                user = user_result.scalar_one_or_none()
                if user and boost.channel_id:
                    channel_result = await self._db.execute(
                        select(Channel).where(Channel.id == boost.channel_id)
                    )
                    channel = channel_result.scalar_one_or_none()
                    if channel:
                        await self._notifier.send(
                            user,
                            Templates.boost_expiring_soon(channel.title, hours_left),
                            reference_type="boost",
                            reference_id=boost.id,
                        )
                        reminded_ids.append(boost.id)
            except Exception as exc:
                logger.error("reminder_failed", boost_id=boost.id, error=str(exc))

        return reminded_ids
