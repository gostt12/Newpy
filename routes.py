"""
api/routes.py
──────────────
Mini App REST API endpoints.

Authentication
──────────────
Every endpoint requires a valid Telegram initData token in the
`X-Telegram-Init-Data` header.  The dependency `get_current_user`
verifies it and returns the authenticated User.

Endpoints
─────────
  GET  /wallet/balance              — current balance(s)
  GET  /transactions                — paginated history
  GET  /transactions/{tx_id}        — single transaction detail

  POST /escrow/orders               — create escrow order
  GET  /escrow/orders/{order_id}    — order detail
  POST /escrow/orders/{order_id}/initiate-transfer
  POST /escrow/orders/{order_id}/confirm-receipt
  POST /escrow/orders/{order_id}/dispute

  POST /boosts                      — purchase a boost
  GET  /boosts                      — list user boosts
  GET  /boosts/pricing              — available boost packages

  POST /withdrawal/request          — initiate withdrawal (queued for manual review in prod)
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from models.orm import (
    Boost,
    BoostType,
    Channel,
    Currency,
    EscrowOrder,
    EscrowStatus,
    Transaction,
    TransactionType,
    User,
    Wallet,
)
from services import BoostError, BoostService, EscrowError, EscrowService, WalletService
from services.boost_service import BOOST_PRICING
from utils.logger import get_logger
from utils.security import verify_telegram_init_data

router = APIRouter(tags=["mini-app"])
logger = get_logger("routes")
security = HTTPBearer()


# ──────────────────────────────────────────────────────────────────────────────
# Auth dependency
# ──────────────────────────────────────────────────────────────────────────────

async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Validates the Telegram initData Bearer token and returns the authenticated user.
    Creates the user record on first login.
    """
    try:
        data = verify_telegram_init_data(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    import json
    tg_user: dict = json.loads(data.get("user", "{}"))
    telegram_id = int(tg_user.get("id", 0))
    if not telegram_id:
        raise HTTPException(status_code=400, detail="Missing user in initData")

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=tg_user.get("username"),
            first_name=tg_user.get("first_name", ""),
            last_name=tg_user.get("last_name"),
        )
        db.add(user)
        await db.flush()
        logger.info("user_created", telegram_id=telegram_id)

    return user


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class BalanceOut(BaseModel):
    currency: str
    balance: float
    locked: float
    available: float


class TransactionOut(BaseModel):
    id: str
    type: str
    status: str
    currency: str
    amount: float
    fee: float
    net_amount: float
    provider: str
    description: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedTransactions(BaseModel):
    items: list[TransactionOut]
    total: int
    page: int
    page_size: int


class EscrowOrderOut(BaseModel):
    id: str
    channel_id: str
    buyer_id: str
    seller_id: str
    status: str
    currency: str
    amount: float
    platform_fee: float
    seller_receives: float
    expires_at: datetime | None
    created_at: datetime


class CreateEscrowOrderIn(BaseModel):
    channel_id: str


class DisputeIn(BaseModel):
    reason: str = Field(min_length=10, max_length=1000)


class BoostPurchaseIn(BaseModel):
    channel_id: str
    boost_type: BoostType
    duration_days: int = Field(ge=7, le=30)


class BoostOut(BaseModel):
    id: str
    channel_id: str | None
    type: str
    status: str
    currency: str
    amount_paid: float
    duration_days: int
    starts_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class WithdrawalRequestIn(BaseModel):
    amount: float = Field(gt=0)
    currency: Currency
    provider: str = Field(pattern="^(chapa|stripe|paypal)$")
    account_details: str = Field(min_length=5, max_length=500, description="Bank / wallet details")


# ──────────────────────────────────────────────────────────────────────────────
# Wallet
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/wallet/balance", response_model=list[BalanceOut])
async def get_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    wallet_svc = WalletService(db)
    balances = []
    for currency in Currency:
        b = await wallet_svc.get_balance(current_user, currency)
        balances.append(BalanceOut(**b))
    return balances


# ──────────────────────────────────────────────────────────────────────────────
# Transactions
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/transactions", response_model=PaginatedTransactions)
async def list_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tx_type: TransactionType | None = Query(default=None),
    currency: Currency | None = Query(default=None),
):
    q = select(Transaction).where(Transaction.user_id == current_user.id)
    if tx_type:
        q = q.where(Transaction.type == tx_type)
    if currency:
        q = q.where(Transaction.currency == currency)
    q = q.order_by(desc(Transaction.created_at))

    # Count
    count_q = select(Transaction.id).where(Transaction.user_id == current_user.id)
    count_result = await db.execute(count_q)
    total = len(count_result.all())

    # Page
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    items = result.scalars().all()

    return PaginatedTransactions(
        items=[TransactionOut.model_validate(t) for t in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/transactions/{tx_id}", response_model=TransactionOut)
async def get_transaction(
    tx_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == tx_id, Transaction.user_id == current_user.id
        )
    )
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return TransactionOut.model_validate(tx)


# ──────────────────────────────────────────────────────────────────────────────
# Escrow
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/escrow/orders", response_model=EscrowOrderOut, status_code=201)
async def create_escrow_order(
    body: CreateEscrowOrderIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    channel_result = await db.execute(select(Channel).where(Channel.id == body.channel_id))
    channel = channel_result.scalar_one_or_none()
    if not channel or not channel.is_listed:
        raise HTTPException(status_code=404, detail="Channel not found or not listed")

    escrow_svc = EscrowService(db)
    try:
        order = await escrow_svc.create_order(current_user, channel)
    except EscrowError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _escrow_out(order)


@router.get("/escrow/orders/{order_id}", response_model=EscrowOrderOut)
async def get_escrow_order(
    order_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    order = await _fetch_order(order_id, current_user, db)
    return _escrow_out(order)


@router.post("/escrow/orders/{order_id}/initiate-transfer", response_model=EscrowOrderOut)
async def initiate_transfer(
    order_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    order = await _fetch_order(order_id, current_user, db)
    if order.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can initiate transfer")
    escrow_svc = EscrowService(db)
    try:
        order = await escrow_svc.initiate_transfer(order)
    except EscrowError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _escrow_out(order)


@router.post("/escrow/orders/{order_id}/confirm-receipt", response_model=EscrowOrderOut)
async def confirm_receipt(
    order_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    order = await _fetch_order(order_id, current_user, db)
    if order.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can confirm receipt")
    escrow_svc = EscrowService(db)
    try:
        order = await escrow_svc.confirm_transfer_and_release(order)
    except EscrowError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _escrow_out(order)


@router.post("/escrow/orders/{order_id}/dispute", response_model=EscrowOrderOut)
async def raise_dispute(
    order_id: str,
    body: DisputeIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    order = await _fetch_order(order_id, current_user, db)
    escrow_svc = EscrowService(db)
    try:
        order = await escrow_svc.raise_dispute(order, current_user, body.reason)
    except EscrowError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _escrow_out(order)


# ──────────────────────────────────────────────────────────────────────────────
# Boosts
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/boosts/pricing")
async def boost_pricing():
    """Return all available boost packages and prices."""
    packages = []
    for (boost_type, days), (price, currency) in BOOST_PRICING.items():
        packages.append({
            "boost_type": boost_type,
            "duration_days": days,
            "price": price,
            "currency": currency.value,
        })
    return {"packages": packages}


@router.post("/boosts", response_model=BoostOut, status_code=201)
async def purchase_boost(
    body: BoostPurchaseIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    channel_result = await db.execute(select(Channel).where(Channel.id == body.channel_id))
    channel = channel_result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the channel owner can purchase a boost")

    boost_svc = BoostService(db)
    try:
        boost = await boost_svc.purchase_boost(
            current_user, channel, body.boost_type, body.duration_days
        )
    except BoostError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _boost_out(boost)


@router.get("/boosts", response_model=list[BoostOut])
async def list_boosts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Boost)
        .where(Boost.user_id == current_user.id)
        .order_by(desc(Boost.created_at))
    )
    return [_boost_out(b) for b in result.scalars().all()]


# ──────────────────────────────────────────────────────────────────────────────
# Withdrawals (queued for manual / automated processing)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/withdrawal/request", status_code=202)
async def request_withdrawal(
    body: WithdrawalRequestIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Queue a withdrawal request.  Funds are immediately debited from the wallet
    and a PENDING withdrawal transaction is created.  A background worker (or
    admin review) processes the actual payout.
    """
    from decimal import Decimal
    from models.orm import PaymentProvider, TransactionType
    from services.notifier import NotifierService, Templates

    wallet_svc = WalletService(db)
    notifier = NotifierService(db)

    try:
        tx = await wallet_svc.debit(
            current_user,
            Decimal(str(body.amount)),
            body.currency,
            TransactionType.WITHDRAWAL,
            PaymentProvider(body.provider),
            description=f"Withdrawal to {body.provider}: {body.account_details[:60]}",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await notifier.send(
        current_user,
        Templates.withdrawal_success(body.amount, body.currency.value),
        reference_type="withdrawal",
        reference_id=tx.id,
    )
    return {"status": "queued", "tx_id": tx.id}


# ──────────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_order(order_id: str, user: User, db: AsyncSession) -> EscrowOrder:
    result = await db.execute(
        select(EscrowOrder).where(
            EscrowOrder.id == order_id,
            (EscrowOrder.buyer_id == user.id) | (EscrowOrder.seller_id == user.id),
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Escrow order not found")
    return order


def _escrow_out(order: EscrowOrder) -> EscrowOrderOut:
    return EscrowOrderOut(
        id=order.id,
        channel_id=order.channel_id,
        buyer_id=order.buyer_id,
        seller_id=order.seller_id,
        status=order.status.value,
        currency=order.currency.value,
        amount=float(order.amount),
        platform_fee=float(order.platform_fee),
        seller_receives=float(order.seller_receives),
        expires_at=order.expires_at,
        created_at=order.created_at,
    )


def _boost_out(boost: Boost) -> BoostOut:
    return BoostOut(
        id=boost.id,
        channel_id=boost.channel_id,
        type=boost.type.value,
        status=boost.status.value,
        currency=boost.currency.value,
        amount_paid=float(boost.amount_paid),
        duration_days=boost.duration_days,
        starts_at=boost.starts_at,
        expires_at=boost.expires_at,
        created_at=boost.created_at,
    )
