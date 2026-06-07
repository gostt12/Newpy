"""
services/wallet_service.py
───────────────────────────
Wallet Management — the single source of truth for all balance mutations.

Responsibilities
────────────────
  • Get-or-create wallets per user / currency
  • Credit (deposit, refund, sale proceeds)
  • Debit (withdrawal, boost purchase, escrow hold)
  • Lock / unlock funds during escrow lifecycle
  • Sync with Chapa (ETB), Stripe (USD), and PayPal (USD) deposit webhooks
  • Record every movement as a Transaction row
"""

import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.orm import (
    Currency,
    PaymentProvider,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    Wallet,
)
from utils.logger import get_logger

logger = get_logger("wallet_service")
settings = get_settings()

PLATFORM_FEE_RATE = Decimal("0.05")   # 5 % platform fee on sales


class InsufficientFundsError(Exception):
    """Raised when a debit would result in negative available balance."""


class WalletService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Wallet helpers ────────────────────────────────────────────────────────

    async def get_or_create_wallet(self, user: User, currency: Currency) -> Wallet:
        """Return the user's wallet for the given currency, creating it if needed."""
        result = await self._db.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.currency == currency)
        )
        wallet = result.scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(user_id=user.id, currency=currency, balance=Decimal("0"), locked_balance=Decimal("0"))
            self._db.add(wallet)
            await self._db.flush()
            logger.info("wallet_created", user_id=user.id, currency=currency)
        return wallet

    async def get_balance(self, user: User, currency: Currency) -> dict[str, float]:
        wallet = await self.get_or_create_wallet(user, currency)
        return {
            "balance": float(wallet.balance),
            "locked": float(wallet.locked_balance),
            "available": float(wallet.available_balance),
            "currency": currency.value,
        }

    # ── Credit ────────────────────────────────────────────────────────────────

    async def credit(
        self,
        user: User,
        amount: Decimal,
        currency: Currency,
        tx_type: TransactionType,
        provider: PaymentProvider,
        *,
        provider_reference: str | None = None,
        provider_payload: Any = None,
        reference_type: str | None = None,
        reference_id: str | None = None,
        description: str | None = None,
    ) -> Transaction:
        """Add funds to a user wallet and record the transaction."""
        wallet = await self.get_or_create_wallet(user, currency)

        fee = (amount * PLATFORM_FEE_RATE).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        net = amount - fee if tx_type == TransactionType.SALE_CREDIT else amount

        wallet.balance = Decimal(str(wallet.balance)) + net
        tx = self._build_tx(
            user_id=user.id,
            wallet_id=wallet.id,
            tx_type=tx_type,
            status=TransactionStatus.COMPLETED,
            currency=currency,
            amount=amount,
            fee=fee if tx_type == TransactionType.SALE_CREDIT else Decimal("0"),
            net_amount=net,
            provider=provider,
            provider_reference=provider_reference,
            provider_payload=provider_payload,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
        )
        self._db.add(tx)
        await self._db.flush()
        logger.info(
            "wallet_credited",
            user_id=user.id,
            amount=float(net),
            currency=currency,
            tx_id=tx.id,
        )
        return tx

    # ── Debit ─────────────────────────────────────────────────────────────────

    async def debit(
        self,
        user: User,
        amount: Decimal,
        currency: Currency,
        tx_type: TransactionType,
        provider: PaymentProvider,
        *,
        provider_reference: str | None = None,
        provider_payload: Any = None,
        reference_type: str | None = None,
        reference_id: str | None = None,
        description: str | None = None,
    ) -> Transaction:
        """Deduct funds from available balance. Raises InsufficientFundsError if needed."""
        wallet = await self.get_or_create_wallet(user, currency)
        available = Decimal(str(wallet.available_balance))

        if amount > available:
            raise InsufficientFundsError(
                f"Required {amount} {currency} but available {available}"
            )

        wallet.balance = Decimal(str(wallet.balance)) - amount
        tx = self._build_tx(
            user_id=user.id,
            wallet_id=wallet.id,
            tx_type=tx_type,
            status=TransactionStatus.COMPLETED,
            currency=currency,
            amount=amount,
            fee=Decimal("0"),
            net_amount=amount,
            provider=provider,
            provider_reference=provider_reference,
            provider_payload=provider_payload,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
        )
        self._db.add(tx)
        await self._db.flush()
        logger.info("wallet_debited", user_id=user.id, amount=float(amount), currency=currency)
        return tx

    # ── Escrow lock / unlock ──────────────────────────────────────────────────

    async def lock_for_escrow(
        self, user: User, amount: Decimal, currency: Currency, escrow_order_id: str
    ) -> Transaction:
        """Lock buyer funds for an escrow order (holds them, can't be spent)."""
        wallet = await self.get_or_create_wallet(user, currency)
        available = Decimal(str(wallet.available_balance))

        if amount > available:
            raise InsufficientFundsError(
                f"Cannot lock {amount} {currency}: available {available}"
            )

        wallet.locked_balance = Decimal(str(wallet.locked_balance)) + amount
        tx = self._build_tx(
            user_id=user.id,
            wallet_id=wallet.id,
            tx_type=TransactionType.ESCROW_HOLD,
            status=TransactionStatus.COMPLETED,
            currency=currency,
            amount=amount,
            fee=Decimal("0"),
            net_amount=amount,
            provider=PaymentProvider.INTERNAL,
            reference_type="escrow",
            reference_id=escrow_order_id,
            description=f"Escrow hold for order {escrow_order_id[:8]}",
        )
        self._db.add(tx)
        await self._db.flush()
        logger.info("escrow_locked", user_id=user.id, order_id=escrow_order_id, amount=float(amount))
        return tx

    async def release_escrow_to_seller(
        self, buyer: User, seller: User, amount: Decimal, currency: Currency, escrow_order_id: str
    ) -> tuple[Transaction, Transaction]:
        """Deduct locked funds from buyer and credit (minus fee) to seller."""
        buyer_wallet = await self.get_or_create_wallet(buyer, currency)

        # Remove the locked amount from buyer
        buyer_wallet.balance = Decimal(str(buyer_wallet.balance)) - amount
        buyer_wallet.locked_balance = Decimal(str(buyer_wallet.locked_balance)) - amount

        buyer_tx = self._build_tx(
            user_id=buyer.id,
            wallet_id=buyer_wallet.id,
            tx_type=TransactionType.ESCROW_RELEASE,
            status=TransactionStatus.COMPLETED,
            currency=currency,
            amount=amount,
            fee=Decimal("0"),
            net_amount=amount,
            provider=PaymentProvider.INTERNAL,
            reference_type="escrow",
            reference_id=escrow_order_id,
        )
        self._db.add(buyer_tx)

        # Credit seller (minus platform fee)
        seller_tx = await self.credit(
            seller,
            amount,
            currency,
            TransactionType.SALE_CREDIT,
            PaymentProvider.INTERNAL,
            reference_type="escrow",
            reference_id=escrow_order_id,
            description=f"Sale proceeds for order {escrow_order_id[:8]}",
        )

        await self._db.flush()
        logger.info("escrow_released", order_id=escrow_order_id, amount=float(amount))
        return buyer_tx, seller_tx

    async def refund_escrow_to_buyer(
        self, buyer: User, amount: Decimal, currency: Currency, escrow_order_id: str
    ) -> Transaction:
        """Unlock and refund locked escrow funds back to buyer."""
        wallet = await self.get_or_create_wallet(buyer, currency)
        wallet.locked_balance = Decimal(str(wallet.locked_balance)) - amount

        tx = self._build_tx(
            user_id=buyer.id,
            wallet_id=wallet.id,
            tx_type=TransactionType.ESCROW_REFUND,
            status=TransactionStatus.COMPLETED,
            currency=currency,
            amount=amount,
            fee=Decimal("0"),
            net_amount=amount,
            provider=PaymentProvider.INTERNAL,
            reference_type="escrow",
            reference_id=escrow_order_id,
            description=f"Escrow refund for order {escrow_order_id[:8]}",
        )
        self._db.add(tx)
        await self._db.flush()
        logger.info("escrow_refunded", user_id=buyer.id, order_id=escrow_order_id)
        return tx

    # ── Payment-provider deposit handlers ────────────────────────────────────

    async def process_chapa_deposit(
        self, user: User, verified_payload: dict[str, Any]
    ) -> Transaction:
        """Credit wallet after a verified Chapa webhook."""
        amount = Decimal(str(verified_payload["amount"]))
        ref = verified_payload.get("tx_ref") or verified_payload.get("reference")
        return await self.credit(
            user,
            amount,
            Currency.ETB,
            TransactionType.DEPOSIT,
            PaymentProvider.CHAPA,
            provider_reference=ref,
            provider_payload=json.dumps(verified_payload),
            description="Chapa deposit",
        )

    async def process_stripe_deposit(
        self, user: User, stripe_event: Any
    ) -> Transaction:
        """Credit wallet after a verified Stripe PaymentIntent/Charge event."""
        intent = stripe_event.data.object
        amount = Decimal(str(intent.amount_received)) / Decimal("100")  # cents → dollars
        return await self.credit(
            user,
            amount,
            Currency.USD,
            TransactionType.DEPOSIT,
            PaymentProvider.STRIPE,
            provider_reference=intent.id,
            provider_payload=json.dumps(dict(intent)),
            description="Stripe deposit",
        )

    async def process_paypal_deposit(
        self, user: User, paypal_event: dict[str, Any]
    ) -> Transaction:
        """Credit wallet after a verified PayPal webhook."""
        resource = paypal_event.get("resource", {})
        amount = Decimal(str(resource.get("amount", {}).get("total", "0")))
        ref = resource.get("id")
        return await self.credit(
            user,
            amount,
            Currency.USD,
            TransactionType.DEPOSIT,
            PaymentProvider.PAYPAL,
            provider_reference=ref,
            provider_payload=json.dumps(paypal_event),
            description="PayPal deposit",
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _build_tx(
        *,
        user_id: str,
        wallet_id: str | None,
        tx_type: TransactionType,
        status: TransactionStatus,
        currency: Currency,
        amount: Decimal,
        fee: Decimal,
        net_amount: Decimal,
        provider: PaymentProvider,
        provider_reference: str | None = None,
        provider_payload: Any = None,
        reference_type: str | None = None,
        reference_id: str | None = None,
        description: str | None = None,
    ) -> Transaction:
        return Transaction(
            user_id=user_id,
            wallet_id=wallet_id,
            type=tx_type,
            status=status,
            currency=currency,
            amount=float(amount),
            fee=float(fee),
            net_amount=float(net_amount),
            provider=provider,
            provider_reference=provider_reference,
            provider_payload=provider_payload if isinstance(provider_payload, str) else json.dumps(provider_payload) if provider_payload else None,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
        )
