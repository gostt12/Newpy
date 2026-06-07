"""
models/orm.py
─────────────
SQLAlchemy ORM models for the entire Bot Manager platform.

Tables
──────
  users           – Telegram user accounts
  wallets         – Per-user multi-currency wallet
  transactions    – All financial movements (deposit, withdrawal, escrow, boost)
  escrow_orders   – Channel buy/sell lifecycle
  channels        – Telegram channels listed on the marketplace
  boosts          – Channel / ad boost purchases
  notifications   – Outbound notification log
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.database import Base


# ──────────────────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────────────────

class Currency(str, enum.Enum):
    ETB = "ETB"
    USD = "USD"


class TransactionType(str, enum.Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    ESCROW_HOLD = "escrow_hold"
    ESCROW_RELEASE = "escrow_release"
    ESCROW_REFUND = "escrow_refund"
    BOOST_PURCHASE = "boost_purchase"
    AD_PURCHASE = "ad_purchase"
    SALE_CREDIT = "sale_credit"


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REVERSED = "reversed"


class PaymentProvider(str, enum.Enum):
    CHAPA = "chapa"
    STRIPE = "stripe"
    PAYPAL = "paypal"
    INTERNAL = "internal"


class EscrowStatus(str, enum.Enum):
    INITIATED = "initiated"
    PAYMENT_PENDING = "payment_pending"
    FUNDS_HELD = "funds_held"
    TRANSFER_IN_PROGRESS = "transfer_in_progress"
    TRANSFER_VERIFIED = "transfer_verified"
    COMPLETED = "completed"
    DISPUTED = "disputed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class BoostStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class BoostType(str, enum.Enum):
    CHANNEL_PROMOTION = "channel_promotion"
    AD_BANNER = "ad_banner"
    FEATURED_LISTING = "featured_listing"


class NotificationStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_new_uuid
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    wallets: Mapped[list["Wallet"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    channels: Mapped[list["Channel"]] = relationship(back_populates="owner", foreign_keys="Channel.owner_id")
    boosts: Mapped[list["Boost"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notifications: Mapped[list["Notification"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} username={self.username}>"


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("user_id", "currency", name="uq_wallet_user_currency"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    currency: Mapped[Currency] = mapped_column(Enum(Currency), nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0, nullable=False)
    locked_balance: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="wallets")

    @property
    def available_balance(self) -> float:
        return float(self.balance) - float(self.locked_balance)

    def __repr__(self) -> str:
        return f"<Wallet user={self.user_id} {self.currency}={self.balance}>"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    wallet_id: Mapped[str] = mapped_column(ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)

    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType), nullable=False, index=True)
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus), default=TransactionStatus.PENDING, nullable=False, index=True
    )
    currency: Mapped[Currency] = mapped_column(Enum(Currency), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0, nullable=False)
    net_amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    provider: Mapped[PaymentProvider] = mapped_column(Enum(PaymentProvider), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(256), index=True)
    provider_payload: Mapped[str | None] = mapped_column(Text)   # raw webhook / response JSON

    reference_type: Mapped[str | None] = mapped_column(String(64))  # "escrow" | "boost" | None
    reference_id: Mapped[str | None] = mapped_column(String(64), index=True)

    description: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="transactions")

    def __repr__(self) -> str:
        return f"<Transaction {self.type} {self.amount} {self.currency} [{self.status}]>"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    telegram_channel_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    description: Mapped[str | None] = mapped_column(Text)

    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    pending_buyer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    subscriber_count: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[Currency] = mapped_column(Enum(Currency), nullable=False)
    is_listed: Mapped[bool] = mapped_column(Boolean, default=True)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    owner: Mapped["User"] = relationship(back_populates="channels", foreign_keys=[owner_id])
    escrow_orders: Mapped[list["EscrowOrder"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    boosts: Mapped[list["Boost"]] = relationship(back_populates="channel", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Channel '{self.title}' owner={self.owner_id}>"


class EscrowOrder(Base):
    __tablename__ = "escrow_orders"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    status: Mapped[EscrowStatus] = mapped_column(
        Enum(EscrowStatus), default=EscrowStatus.INITIATED, nullable=False, index=True
    )
    currency: Mapped[Currency] = mapped_column(Enum(Currency), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    platform_fee: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0)
    seller_receives: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    payment_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    release_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)

    transfer_admin_rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_ownership_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    buyer_confirmed_receipt: Mapped[bool] = mapped_column(Boolean, default=False)

    dispute_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    channel: Mapped["Channel"] = relationship(back_populates="escrow_orders")
    buyer: Mapped["User"] = relationship(foreign_keys=[buyer_id])
    seller: Mapped["User"] = relationship(foreign_keys=[seller_id])

    def __repr__(self) -> str:
        return f"<EscrowOrder channel={self.channel_id} [{self.status}] {self.amount} {self.currency}>"


class Boost(Base):
    __tablename__ = "boosts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"), nullable=True, index=True)

    type: Mapped[BoostType] = mapped_column(Enum(BoostType), nullable=False)
    status: Mapped[BoostStatus] = mapped_column(
        Enum(BoostStatus), default=BoostStatus.PENDING, nullable=False, index=True
    )
    currency: Mapped[Currency] = mapped_column(Enum(Currency), nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    payment_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)   # flexible extra config

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="boosts")
    channel: Mapped["Channel | None"] = relationship(back_populates="boosts")

    def __repr__(self) -> str:
        return f"<Boost {self.type} [{self.status}] expires={self.expires_at}>"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus), default=NotificationStatus.QUEUED, nullable=False
    )
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    reference_type: Mapped[str | None] = mapped_column(String(64))
    reference_id: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification tg={self.telegram_id} [{self.status}]>"
