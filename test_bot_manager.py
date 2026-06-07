"""
tests/test_bot_manager.py
──────────────────────────
Comprehensive async unit tests for the Bot Manager.
Uses an in-memory SQLite database (via aiosqlite) so no PostgreSQL is required.
"""

import json
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.database import Base
from models.orm import (
    Boost,
    BoostStatus,
    BoostType,
    Channel,
    Currency,
    EscrowOrder,
    EscrowStatus,
    PaymentProvider,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    Wallet,
)
from services.boost_service import BoostError, BoostService
from services.escrow_service import EscrowError, EscrowService
from services.wallet_service import InsufficientFundsError, WalletService

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Provide a fresh async SQLite session for each test."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _make_user(db: AsyncSession, telegram_id: int, username: str) -> User:
    user = User(telegram_id=telegram_id, username=username, first_name=username.capitalize())
    db.add(user)
    await db.flush()
    return user


async def _make_channel(
    db: AsyncSession, owner: User, price: float = 1000.0, currency: Currency = Currency.ETB
) -> Channel:
    ch = Channel(
        title="Test Channel",
        owner_id=owner.id,
        price=price,
        currency=currency,
        is_listed=True,
    )
    db.add(ch)
    await db.flush()
    return ch


# ──────────────────────────────────────────────────────────────────────────────
# Wallet Service Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestWalletService:
    @pytest.mark.asyncio
    async def test_get_or_create_wallet(self, db_session):
        user = await _make_user(db_session, 111, "alice")
        svc = WalletService(db_session)

        wallet = await svc.get_or_create_wallet(user, Currency.ETB)
        assert wallet.balance == 0
        assert wallet.currency == Currency.ETB

        # idempotent
        wallet2 = await svc.get_or_create_wallet(user, Currency.ETB)
        assert wallet2.id == wallet.id

    @pytest.mark.asyncio
    async def test_credit_deposit(self, db_session):
        user = await _make_user(db_session, 112, "bob")
        svc = WalletService(db_session)

        tx = await svc.credit(
            user, Decimal("500"), Currency.ETB,
            TransactionType.DEPOSIT, PaymentProvider.CHAPA,
            provider_reference="TXN123",
        )
        assert tx.status == TransactionStatus.COMPLETED
        assert float(tx.net_amount) == 500.0

        balance = await svc.get_balance(user, Currency.ETB)
        assert balance["balance"] == 500.0
        assert balance["available"] == 500.0

    @pytest.mark.asyncio
    async def test_debit_success(self, db_session):
        user = await _make_user(db_session, 113, "carol")
        svc = WalletService(db_session)

        await svc.credit(user, Decimal("1000"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)
        await svc.debit(user, Decimal("400"), Currency.ETB, TransactionType.BOOST_PURCHASE, PaymentProvider.INTERNAL)

        balance = await svc.get_balance(user, Currency.ETB)
        assert balance["balance"] == 600.0

    @pytest.mark.asyncio
    async def test_debit_insufficient_funds(self, db_session):
        user = await _make_user(db_session, 114, "dave")
        svc = WalletService(db_session)

        await svc.credit(user, Decimal("100"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        with pytest.raises(InsufficientFundsError):
            await svc.debit(user, Decimal("500"), Currency.ETB, TransactionType.BOOST_PURCHASE, PaymentProvider.INTERNAL)

    @pytest.mark.asyncio
    async def test_escrow_lock_unlock(self, db_session):
        buyer = await _make_user(db_session, 115, "buyer")
        seller = await _make_user(db_session, 116, "seller")
        svc = WalletService(db_session)

        await svc.credit(buyer, Decimal("2000"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        await svc.lock_for_escrow(buyer, Decimal("1000"), Currency.ETB, "order-abc")
        balance = await svc.get_balance(buyer, Currency.ETB)
        assert balance["available"] == 1000.0
        assert balance["locked"] == 1000.0

        # Lock too much raises error
        with pytest.raises(InsufficientFundsError):
            await svc.lock_for_escrow(buyer, Decimal("1500"), Currency.ETB, "order-xyz")

    @pytest.mark.asyncio
    async def test_escrow_release_to_seller(self, db_session):
        buyer = await _make_user(db_session, 117, "buyerX")
        seller = await _make_user(db_session, 118, "sellerX")
        svc = WalletService(db_session)

        await svc.credit(buyer, Decimal("1000"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)
        await svc.lock_for_escrow(buyer, Decimal("1000"), Currency.ETB, "order-release")
        await svc.release_escrow_to_seller(buyer, seller, Decimal("1000"), Currency.ETB, "order-release")

        buyer_bal = await svc.get_balance(buyer, Currency.ETB)
        seller_bal = await svc.get_balance(seller, Currency.ETB)

        assert buyer_bal["balance"] == 0.0
        # Seller receives 95% (5% platform fee)
        assert seller_bal["balance"] == pytest.approx(950.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_chapa_deposit_processing(self, db_session):
        user = await _make_user(db_session, 119, "chapaUser")
        svc = WalletService(db_session)

        payload = {
            "event": "charge.success",
            "amount": "750.00",
            "tx_ref": "CHAPA-TXN-001",
            "meta": {"telegram_id": 119},
        }
        tx = await svc.process_chapa_deposit(user, payload)
        assert tx.status == TransactionStatus.COMPLETED
        assert tx.provider == PaymentProvider.CHAPA
        assert float(tx.net_amount) == 750.0


# ──────────────────────────────────────────────────────────────────────────────
# Escrow Service Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestEscrowService:
    @pytest.mark.asyncio
    async def test_full_escrow_lifecycle(self, db_session):
        buyer = await _make_user(db_session, 201, "buyer1")
        seller = await _make_user(db_session, 202, "seller1")
        channel = await _make_channel(db_session, seller, price=500.0, currency=Currency.ETB)

        wallet_svc = WalletService(db_session)
        await wallet_svc.credit(buyer, Decimal("1000"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        escrow_svc = EscrowService(db_session)

        # Step 1: Create order
        order = await escrow_svc.create_order(buyer, channel)
        assert order.status == EscrowStatus.PAYMENT_PENDING

        # Step 2: Confirm payment (lock funds)
        deposit_tx = await wallet_svc.credit(
            buyer, Decimal("500"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA, provider_reference="pay-001"
        )
        order = await escrow_svc.confirm_payment(order, deposit_tx)
        assert order.status == EscrowStatus.FUNDS_HELD

        # Step 3: Seller initiates transfer
        order = await escrow_svc.initiate_transfer(order)
        assert order.status == EscrowStatus.TRANSFER_IN_PROGRESS

        # Step 4: Buyer confirms receipt → funds released
        order = await escrow_svc.confirm_transfer_and_release(order)
        assert order.status == EscrowStatus.COMPLETED

        # Channel should now belong to buyer
        from sqlalchemy import select
        ch_result = await db_session.execute(select(Channel).where(Channel.id == channel.id))
        updated_ch = ch_result.scalar_one()
        assert updated_ch.owner_id == buyer.id
        assert updated_ch.is_sold is True

    @pytest.mark.asyncio
    async def test_escrow_refund(self, db_session):
        buyer = await _make_user(db_session, 203, "buyer2")
        seller = await _make_user(db_session, 204, "seller2")
        channel = await _make_channel(db_session, seller, price=300.0)

        wallet_svc = WalletService(db_session)
        await wallet_svc.credit(buyer, Decimal("500"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        escrow_svc = EscrowService(db_session)
        order = await escrow_svc.create_order(buyer, channel)

        deposit_tx = await wallet_svc.credit(
            buyer, Decimal("300"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA
        )
        order = await escrow_svc.confirm_payment(order, deposit_tx)
        order = await escrow_svc.refund(order, "Buyer requested refund")

        assert order.status == EscrowStatus.REFUNDED
        balance = await wallet_svc.get_balance(buyer, Currency.ETB)
        assert balance["locked"] == 0.0

    @pytest.mark.asyncio
    async def test_owner_cannot_buy_own_channel(self, db_session):
        owner = await _make_user(db_session, 205, "owner1")
        channel = await _make_channel(db_session, owner)

        escrow_svc = EscrowService(db_session)
        with pytest.raises(EscrowError, match="Owner cannot buy"):
            await escrow_svc.create_order(owner, channel)

    @pytest.mark.asyncio
    async def test_dispute_flow(self, db_session):
        buyer = await _make_user(db_session, 206, "buyer3")
        seller = await _make_user(db_session, 207, "seller3")
        channel = await _make_channel(db_session, seller, price=200.0)

        wallet_svc = WalletService(db_session)
        await wallet_svc.credit(buyer, Decimal("500"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        escrow_svc = EscrowService(db_session)
        order = await escrow_svc.create_order(buyer, channel)
        deposit_tx = await wallet_svc.credit(
            buyer, Decimal("200"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA
        )
        order = await escrow_svc.confirm_payment(order, deposit_tx)
        order = await escrow_svc.raise_dispute(order, buyer, "Seller is unresponsive")

        assert order.status == EscrowStatus.DISPUTED


# ──────────────────────────────────────────────────────────────────────────────
# Boost Service Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestBoostService:
    @pytest.mark.asyncio
    async def test_purchase_boost_success(self, db_session):
        user = await _make_user(db_session, 301, "booster")
        channel = await _make_channel(db_session, user)

        wallet_svc = WalletService(db_session)
        await wallet_svc.credit(user, Decimal("2000"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        boost_svc = BoostService(db_session)
        boost = await boost_svc.purchase_boost(user, channel, BoostType.CHANNEL_PROMOTION, 7)

        assert boost.status == BoostStatus.ACTIVE
        assert boost.duration_days == 7
        assert boost.expires_at is not None

        balance = await wallet_svc.get_balance(user, Currency.ETB)
        assert balance["balance"] == pytest.approx(1500.0, abs=0.01)  # 2000 - 500

    @pytest.mark.asyncio
    async def test_purchase_boost_insufficient_funds(self, db_session):
        user = await _make_user(db_session, 302, "poorBooster")
        channel = await _make_channel(db_session, user)

        boost_svc = BoostService(db_session)
        with pytest.raises(BoostError, match="Insufficient funds"):
            await boost_svc.purchase_boost(user, channel, BoostType.CHANNEL_PROMOTION, 7)

    @pytest.mark.asyncio
    async def test_boost_expiry(self, db_session):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select

        user = await _make_user(db_session, 303, "expireTest")
        channel = await _make_channel(db_session, user)

        wallet_svc = WalletService(db_session)
        await wallet_svc.credit(user, Decimal("2000"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        boost_svc = BoostService(db_session)
        boost = await boost_svc.purchase_boost(user, channel, BoostType.CHANNEL_PROMOTION, 7)

        # Force expiry
        boost.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db_session.flush()

        expired_ids = await boost_svc.expire_boosts()
        assert boost.id in expired_ids

        result = await db_session.execute(select(Boost).where(Boost.id == boost.id))
        updated = result.scalar_one()
        assert updated.status == BoostStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_invalid_boost_type_duration(self, db_session):
        user = await _make_user(db_session, 304, "invalidBoost")
        channel = await _make_channel(db_session, user)

        wallet_svc = WalletService(db_session)
        await wallet_svc.credit(user, Decimal("9999"), Currency.ETB, TransactionType.DEPOSIT, PaymentProvider.CHAPA)

        boost_svc = BoostService(db_session)
        with pytest.raises(BoostError, match="No pricing defined"):
            # duration 99 does not exist in pricing table
            await boost_svc.purchase_boost(user, channel, BoostType.CHANNEL_PROMOTION, 99)
