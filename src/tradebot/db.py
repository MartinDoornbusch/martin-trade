"""SQLite persistence via SQLAlchemy (Postgres-ready by swapping DATABASE_URL)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    market: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    amount: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee_eur: Mapped[float] = mapped_column(Float)
    pnl_eur: Mapped[float] = mapped_column(Float, default=0.0)   # realized, net of fees (sells)
    mode: Mapped[str] = mapped_column(String(6), default="paper")
    reason: Mapped[str] = mapped_column(String(500), default="")


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(20), unique=True)
    amount: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    fees_paid_eur: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    market: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(10))
    decision: Mapped[str] = mapped_column(String(10))
    score: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(1000), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class LLMCallRow(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(60))
    market: Mapped[str] = mapped_column(String(20), default="")
    verdict: Mapped[str] = mapped_column(String(10), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning: Mapped[str] = mapped_column(String(2000), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


class EquityRow(Base):
    __tablename__ = "equity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    total_eur: Mapped[float] = mapped_column(Float)
    cash_eur: Mapped[float] = mapped_column(Float)
    fees_cumulative_eur: Mapped[float] = mapped_column(Float, default=0.0)


class KVRow(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))


_engine = None
_Session: sessionmaker | None = None


def init_db(database_url: str) -> None:
    global _engine, _Session
    if database_url.startswith("sqlite:///"):
        Path(database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(database_url, future=True)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)


def session() -> Session:
    if _Session is None:
        raise RuntimeError("init_db() not called")
    return _Session()
