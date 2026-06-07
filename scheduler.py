"""
services/scheduler.py
──────────────────────
APScheduler-based background jobs.

Jobs
────
  • expire_escrow_orders   — every 15 min
  • expire_boosts          — every hour
  • send_boost_reminders   — every hour (24 h window)
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.database import get_db_context
from services.boost_service import BoostService
from services.escrow_service import EscrowService
from utils.logger import get_logger

logger = get_logger("scheduler")

scheduler = AsyncIOScheduler(timezone="UTC")


async def _job_expire_escrow() -> None:
    async with get_db_context() as db:
        svc = EscrowService(db)
        ids = await svc.expire_stale_orders()
        if ids:
            logger.info("job_expire_escrow_done", count=len(ids))


async def _job_expire_boosts() -> None:
    async with get_db_context() as db:
        svc = BoostService(db)
        ids = await svc.expire_boosts()
        if ids:
            logger.info("job_expire_boosts_done", count=len(ids))


async def _job_boost_reminders() -> None:
    async with get_db_context() as db:
        svc = BoostService(db)
        ids = await svc.send_expiry_reminders(hours_ahead=24)
        if ids:
            logger.info("job_boost_reminders_done", count=len(ids))


def start_scheduler() -> None:
    scheduler.add_job(
        _job_expire_escrow,
        trigger=IntervalTrigger(minutes=15),
        id="expire_escrow",
        replace_existing=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _job_expire_boosts,
        trigger=IntervalTrigger(hours=1),
        id="expire_boosts",
        replace_existing=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        _job_boost_reminders,
        trigger=IntervalTrigger(hours=1),
        id="boost_reminders",
        replace_existing=True,
        misfire_grace_time=120,
    )
    scheduler.start()
    logger.info("scheduler_started")


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("scheduler_stopped")
