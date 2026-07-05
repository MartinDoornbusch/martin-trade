"""Entrypoint: scheduler (analysis cycles + equity snapshots) + web dashboard."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_config, get_secrets
from .db import EquityRow, init_db, session
from .engine import TradingCycle
from .web import app

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def snapshot_equity(cycle: TradingCycle) -> None:
    with session() as s:
        s.add(EquityRow(total_eur=cycle.broker.portfolio_value_eur(),
                        cash_eur=cycle.broker.cash_eur(),
                        fees_cumulative_eur=cycle.broker.fees_cumulative_eur()))
        s.commit()


def create_app():
    cfg = get_config()
    secrets = get_secrets()
    init_db(secrets.database_url)
    cycle = TradingCycle(cfg, secrets)
    scheduler = BackgroundScheduler(timezone="UTC")
    minutes = int(cfg.schedule["analysis_interval_minutes"])
    scheduler.add_job(cycle.run_once, "interval", minutes=minutes, id="analysis",
                      max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))  # eerste run direct bij start
    scheduler.add_job(snapshot_equity, "interval", hours=6, args=[cycle], id="equity")

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        log.info("Scheduler started: analysis every %s min, mode=%s",
                 minutes, secrets.trading_mode)
        cycle.notify.send("✅ Trade platform gestart (paper mode)")
        yield
        scheduler.shutdown(wait=False)

    app.router.lifespan_context = lifespan
    return app


def run() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)  # nosec B104 - container port


if __name__ == "__main__":
    run()
