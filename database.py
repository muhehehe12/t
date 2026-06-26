from __future__ import annotations

import asyncio
import enum
import logging
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, String, func
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings

logger = logging.getLogger(__name__)


class JobStatus(str, enum.Enum):
    SCRAPED = "SCRAPED"
    GENERATING = "GENERATING"
    DEPLOYED = "DEPLOYED"
    OUTREACH_SENT = "OUTREACH_SENT"
    REPLIED = "REPLIED"
    PAID = "PAID"
    FAILED = "FAILED"


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    seller_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )

    # B2B fields — pentru UK Companies House pipeline.
    # Pe pipeline-ul B2B nu trimitem WhatsApp ci email, deci
    # phone_number poate fi un placeholder (ex: "+44000" + crn).
    # Email-ul e canalul real de contact pe lead-uri B2B UK.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True,
    )
    company_website: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
    )
    is_b2b: Mapped[bool] = mapped_column(default=False, nullable=False)

    # ── Lead scoring & CRM (toate optionale, default neutre) ──
    # Scor 0-15: doar scor >=7 ajunge automat la deployment
    lead_score: Mapped[int] = mapped_column(default=0, nullable=False)
    score_reasons: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )

    # CRM tracking — toate momentele cheie cu clientul
    contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    replied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    interested: Mapped[bool] = mapped_column(default=False, nullable=False)
    sold: Mapped[bool] = mapped_column(default=False, nullable=False)
    sale_amount: Mapped[float | None] = mapped_column(nullable=True)
    sale_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Follow-up scheduling — next time to contact
    next_followup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    followup_count: Mapped[int] = mapped_column(default=0, nullable=False)
    niche: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    language: Mapped[str] = mapped_column(String(50), nullable=False, default="Czech")
    vercel_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    github_repo: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Tip pachet vandut: "premium", "landing", "standard" (default
    # pentru job-uri vechi, inainte de productizare pe niveluri)
    package_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="standard",
    )
    package_price: Mapped[float | None] = mapped_column(nullable=True)
    package_currency: Mapped[str | None] = mapped_column(
        String(8), nullable=True,
    )

    # Add-on chatbot AI pe site (upsell separat de abonamentul generic)
    chatbot_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=32),
        nullable=False,
        default=JobStatus.SCRAPED,
        index=True,
    )

    # ── Abonament (upsell pe clienti existenti, tracking manual) ──
    is_subscriber: Mapped[bool] = mapped_column(default=False, nullable=False)
    subscription_price: Mapped[float | None] = mapped_column(nullable=True)
    subscription_currency: Mapped[str | None] = mapped_column(
        String(8), nullable=True,
    )
    subscription_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_billing_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_payment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    subscription_notes: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Job id={self.id} name={self.business_name!r} "
            f"status={self.status.value} url={self.vercel_url!r}>"
        )


def _engine_kwargs() -> dict:
    """
    pool_size/max_overflow sunt valide doar pentru QueuePool
    (PostgreSQL, MySQL). SQLite (aiosqlite) foloseste NullPool
    si respinge acesti parametri cu TypeError.
    """
    kwargs: dict = {"pool_pre_ping": True, "echo": False}
    if not settings.database_url.startswith("sqlite"):
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return kwargs


engine = create_async_engine(settings.database_url, **_engine_kwargs())

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Migrare usoara pentru baze de date existente create
        # inainte de adaugarea coloanelor seller_name/description.
        # create_all() NU adauga coloane noi pe tabele existente,
        # de-aceea le adaugam manual daca nu exista deja.
        if settings.database_url.startswith("sqlite"):
            from sqlalchemy import text
            for col_sql in [
                "ALTER TABLE jobs ADD COLUMN seller_name VARCHAR(255)",
                "ALTER TABLE jobs ADD COLUMN description VARCHAR(2000)",
                "ALTER TABLE jobs ADD COLUMN is_subscriber BOOLEAN DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN subscription_price FLOAT",
                "ALTER TABLE jobs ADD COLUMN subscription_currency VARCHAR(8)",
                "ALTER TABLE jobs ADD COLUMN subscription_started_at DATETIME",
                "ALTER TABLE jobs ADD COLUMN next_billing_date DATETIME",
                "ALTER TABLE jobs ADD COLUMN last_payment_at DATETIME",
                "ALTER TABLE jobs ADD COLUMN subscription_notes VARCHAR(500)",
                "ALTER TABLE jobs ADD COLUMN package_type VARCHAR(20) DEFAULT 'standard'",
                "ALTER TABLE jobs ADD COLUMN package_price FLOAT",
                "ALTER TABLE jobs ADD COLUMN package_currency VARCHAR(8)",
                "ALTER TABLE jobs ADD COLUMN chatbot_enabled BOOLEAN DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN email VARCHAR(255)",
                "ALTER TABLE jobs ADD COLUMN company_number VARCHAR(20)",
                "ALTER TABLE jobs ADD COLUMN company_website VARCHAR(512)",
                "ALTER TABLE jobs ADD COLUMN is_b2b BOOLEAN DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN lead_score INTEGER DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN score_reasons VARCHAR(500)",
                "ALTER TABLE jobs ADD COLUMN contacted_at DATETIME",
                "ALTER TABLE jobs ADD COLUMN replied_at DATETIME",
                "ALTER TABLE jobs ADD COLUMN interested BOOLEAN DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN sold BOOLEAN DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN sale_amount FLOAT",
                "ALTER TABLE jobs ADD COLUMN sale_currency VARCHAR(8)",
                "ALTER TABLE jobs ADD COLUMN next_followup_at DATETIME",
                "ALTER TABLE jobs ADD COLUMN followup_count INTEGER DEFAULT 0",
            ]:
                try:
                    await conn.execute(text(col_sql))
                    logger.info("Migrare: %s", col_sql)
                except Exception:
                    pass  # coloana exista deja — normal la rulari ulterioare

    logger.info("Database tables ensured.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(init_db())