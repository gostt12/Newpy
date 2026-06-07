"""
services/escrow_service.py
───────────────────────────
Escrow Orchestration — manages the full channel sale lifecycle.

State machine
─────────────
  INITIATED
    → PAYMENT_PENDING   (buyer initiates payment)
    → FUNDS_HELD        (payment confirmed, funds locked in buyer wallet)
    → TRANSFER_IN_PROGRESS (seller starts ownership transfer)
    → TRANSFER_VERIFIED (bot/admin verifies transfer completed)
    → COMPLETED         (funds released to seller)

  Any state → DISPUTED  (either party raises dispute)
  Any state → REFUNDED  (admin or timeout refund)
  Any state → CANCELLED (before FUNDS_HELD only)
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config.settings import get_settings
from models.orm import (
    Channel,
    Currency,
    EscrowOrder,
    EscrowStatus,
    PaymentProvider,
    Transaction,
    TransactionType,
    User,
    Wallet,
)
from services.notifier import NotifierService, Templates
from services.wallet_service import InsufficientFundsError, WalletService
from utils.logger import get_logger

logger = get_logger("escrow_service")
settings = get_settings()

ESCROW_EXPIRY_HOURS = 72   # auto-refund after 72 h if transfer not verified


class EscrowError(Exception):
    """Domain-level escrow error."""


class EscrowService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._wallet = WalletService(db)
        self._notifier = NotifierService(db)

    # ── Create ────────────────────────────────────────────────────────────────

    async def create_order(
        self,
        buyer: User,
        channel: Channel,
    ) -> EscrowOrder:
        """Initialise a new escrow order for a channel purchase."""
        if channel.is_sold:
            raise EscrowError("Channel is already sold")
        if channel.owner_id == buyer.id:
            raise EscrowError("Owner cannot buy their own channel")

        seller_result = await self._db.execute(select(User).where(User.id == channel.owner_id))
        seller = seller_result.scalar_one_or_none()
        if seller is None:
            raise EscrowError("Seller not found")

        amount = Decimal(str(channel.price))
        platform_fee = (amount * Decimal("0.05")).quantize(Decimal("0.000001"))
        seller_receives = amount - platform_fee

        order = EscrowOrder(
            channel_id=channel.id,
            buyer_id=buyer.id,
            seller_id=seller.id,
            status=EscrowStatus.PAYMENT_PENDING,
            currency=channel.currency,
            amount=float(amount),
            platform_fee=float(platform_fee),
            seller_receives=float(seller_receives),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=ESCROW_EXPIRY_HOURS),
        )
        self._db.add(order)
        channel.pending_buyer_id = buyer.id
        await self._db.flush()

        logger.info("escrow_order_created", order_id=order.id, channel_id=channel.id)
        return order

    # ── Payment confirmed → lock funds ────────────────────────────────────────

    async def confirm_payment(
        self, order: EscrowOrder, payment_tx: Transaction
    ) -> EscrowOrder:
        """Called after a deposit is verified; locks the buyer's funds."""
        if order.status != EscrowStatus.PAYMENT_PENDING:
            raise EscrowError(f"Cannot confirm payment: order is {order.status}")

        buyer_result = await self._db.execute(select(User).where(User.id == order.buyer_id))
        buyer = buyer_result.scalar_one()
        seller_result = await self._db.execute(select(User).where(User.id == order.seller_id))
        seller = seller_result.scalar_one()
        channel_result = await self._db.execute(select(Channel).where(Channel.id == order.channel_id))
        channel = channel_result.scalar_one()

        lock_tx = await self._wallet.lock_for_escrow(
            buyer,
            Decimal(str(order.amount)),
            Currency(order.currency),
            order.id,
        )

        order.status = EscrowStatus.FUNDS_HELD
        order.payment_transaction_id = payment_tx.id
        await self._db.flush()

        # Notify both parties
        await self._notifier.send(
            buyer,
            Templates.escrow_funds_held(order.id, order.amount, order.currency, channel.title),
            reference_type="escrow",
            reference_id=order.id,
        )
        await self._notifier.send(
            seller,
            Templates.escrow_transfer_requested(order.id, channel.title, buyer.username or "buyer"),
            reference_type="escrow",
            reference_id=order.id,
        )

        logger.info("escrow_funds_held", order_id=order.id)
        return order

    # ── Seller initiates transfer ─────────────────────────────────────────────

    async def initiate_transfer(self, order: EscrowOrder) -> EscrowOrder:
        """Seller confirms they have initiated the channel ownership transfer."""
        if order.status != EscrowStatus.FUNDS_HELD:
            raise EscrowError(f"Cannot initiate transfer: order is {order.status}")

        order.status = EscrowStatus.TRANSFER_IN_PROGRESS
        order.transfer_admin_rights_confirmed = True
        await self._db.flush()

        buyer_result = await self._db.execute(select(User).where(User.id == order.buyer_id))
        buyer = buyer_result.scalar_one()
        channel_result = await self._db.execute(select(Channel).where(Channel.id == order.channel_id))
        channel = channel_result.scalar_one()

        await self._notifier.send(
            buyer,
            Templates.escrow_transfer_verify_prompt(order.id, channel.title),
            reference_type="escrow",
            reference_id=order.id,
        )

        logger.info("transfer_initiated", order_id=order.id)
        return order

    # ── Buyer confirms receipt → release funds ────────────────────────────────

    async def confirm_transfer_and_release(self, order: EscrowOrder) -> EscrowOrder:
        """
        Buyer confirms they received ownership; funds are released to seller.
        The channel's owner_id is updated to the buyer.
        """
        if order.status not in (EscrowStatus.TRANSFER_IN_PROGRESS, EscrowStatus.TRANSFER_VERIFIED):
            raise EscrowError(f"Cannot release: order is {order.status}")

        buyer_result = await self._db.execute(select(User).where(User.id == order.buyer_id))
        buyer = buyer_result.scalar_one()
        seller_result = await self._db.execute(select(User).where(User.id == order.seller_id))
        seller = seller_result.scalar_one()
        channel_result = await self._db.execute(select(Channel).where(Channel.id == order.channel_id))
        channel = channel_result.scalar_one()

        # Release funds
        _, seller_tx = await self._wallet.release_escrow_to_seller(
            buyer,
            seller,
            Decimal(str(order.amount)),
            Currency(order.currency),
            order.id,
        )

        # Transfer channel ownership
        channel.owner_id = buyer.id
        channel.pending_buyer_id = None
        channel.is_listed = False
        channel.is_sold = True
        channel.transfer_verified = True

        order.status = EscrowStatus.COMPLETED
        order.buyer_confirmed_receipt = True
        order.transfer_ownership_confirmed = True
        order.release_transaction_id = seller_tx.id
        order.completed_at = datetime.now(timezone.utc)
        await self._db.flush()

        await self._notifier.send(
            seller,
            Templates.escrow_completed(order.id, order.seller_receives, order.currency, channel.title),
            reference_type="escrow",
            reference_id=order.id,
        )

        logger.info("escrow_completed", order_id=order.id, channel_id=channel.id)
        return order

    # ── Refund ────────────────────────────────────────────────────────────────

    async def refund(self, order: EscrowOrder, reason: str = "") -> EscrowOrder:
        """Refund locked funds to buyer. Allowed in FUNDS_HELD or TRANSFER_IN_PROGRESS."""
        if order.status not in (
            EscrowStatus.FUNDS_HELD,
            EscrowStatus.TRANSFER_IN_PROGRESS,
            EscrowStatus.DISPUTED,
        ):
            raise EscrowError(f"Cannot refund: order is {order.status}")

        buyer_result = await self._db.execute(select(User).where(User.id == order.buyer_id))
        buyer = buyer_result.scalar_one()
        channel_result = await self._db.execute(select(Channel).where(Channel.id == order.channel_id))
        channel = channel_result.scalar_one()

        await self._wallet.refund_escrow_to_buyer(
            buyer, Decimal(str(order.amount)), Currency(order.currency), order.id
        )

        channel.pending_buyer_id = None
        order.status = EscrowStatus.REFUNDED
        order.notes = reason
        await self._db.flush()

        await self._notifier.send(
            buyer,
            Templates.escrow_refunded(order.id, order.amount, order.currency),
            reference_type="escrow",
            reference_id=order.id,
        )

        logger.info("escrow_refunded", order_id=order.id, reason=reason)
        return order

    # ── Dispute ───────────────────────────────────────────────────────────────

    async def raise_dispute(self, order: EscrowOrder, raised_by: User, reason: str) -> EscrowOrder:
        if order.status in (EscrowStatus.COMPLETED, EscrowStatus.REFUNDED, EscrowStatus.CANCELLED):
            raise EscrowError(f"Cannot dispute: order is {order.status}")

        order.status = EscrowStatus.DISPUTED
        order.dispute_reason = f"Raised by {raised_by.id}: {reason}"
        await self._db.flush()
        logger.warning("escrow_disputed", order_id=order.id, user_id=raised_by.id, reason=reason)
        return order

    # ── Expired orders cleanup ────────────────────────────────────────────────

    async def expire_stale_orders(self) -> list[str]:
        """
        Called by the scheduler. Auto-refund orders that have passed their
        expiry time without completing. Returns list of refunded order IDs.
        """
        now = datetime.now(timezone.utc)
        result = await self._db.execute(
            select(EscrowOrder).where(
                EscrowOrder.expires_at < now,
                EscrowOrder.status.in_([
                    EscrowStatus.PAYMENT_PENDING,
                    EscrowStatus.FUNDS_HELD,
                    EscrowStatus.TRANSFER_IN_PROGRESS,
                ]),
            )
        )
        stale = result.scalars().all()
        refunded_ids = []
        for order in stale:
            try:
                if order.status in (EscrowStatus.FUNDS_HELD, EscrowStatus.TRANSFER_IN_PROGRESS):
                    await self.refund(order, "Auto-refunded: escrow expired")
                else:
                    order.status = EscrowStatus.CANCELLED
                refunded_ids.append(order.id)
            except Exception as exc:
                logger.error("expire_order_failed", order_id=order.id, error=str(exc))

        if refunded_ids:
            logger.info("orders_expired", count=len(refunded_ids))
        return refunded_ids
