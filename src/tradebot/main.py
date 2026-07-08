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
from .mqtt import MqttPublisher
from .web import app

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def publish_mqtt(cycle: TradingCycle, publisher: MqttPublisher) -> None:
    if not publisher.enabled:
        return
    from sqlalchemy import select

    from .db import SignalRow, TradeRow
    with session() as s:
        sells = s.execute(select(TradeRow).where(TradeRow.side == "sell")).scalars().all()
        last_sig = s.execute(select(SignalRow).order_by(SignalRow.ts.desc()).limit(1)
                             ).scalar_one_or_none()
    wins = [t for t in sells if t.pnl_eur > 0]
    publisher.publish_status({
        "total_eur": round(cycle.broker.portfolio_value_eur(), 2),
        "cash_eur": round(cycle.broker.cash_eur(), 2),
        "open_positions": len(cycle.broker.open_positions()),
        "closed_trades": len(sells),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "net_pnl_eur": round(sum(t.pnl_eur for t in sells), 2),
        "total_fees_eur": round(cycle.broker.fees_cumulative_eur(), 2),
        "last_decision": f"{last_sig.market}: {last_sig.decision}" if last_sig else "geen",
    })


def analysis_job(cycle: TradingCycle, publisher: MqttPublisher) -> None:
    cycle.run_once()
    publish_mqtt(cycle, publisher)


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
    publisher = MqttPublisher(secrets.mqtt_host, secrets.mqtt_port,
                              secrets.mqtt_user, secrets.mqtt_password)
    scheduler = BackgroundScheduler(timezone="UTC")
    minutes = int(cfg.schedule["analysis_interval_minutes"])
    scheduler.add_job(analysis_job, "interval", minutes=minutes, id="analysis",
                      args=[cycle, publisher], max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))  # eerste run direct bij start
    scheduler.add_job(snapshot_equity, "interval", hours=6, args=[cycle], id="equity")
    guard_s = int(cfg.schedule.get("guard_interval_seconds", 60))
    scheduler.add_job(cycle.check_exits_fast, "interval", seconds=guard_s, id="guard",
                      max_instances=1, coalesce=True)

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        log.info("Scheduler started: analysis every %s min, guard every %ss, mode=%s",
                 minutes, guard_s, secrets.trading_mode)
        cycle.notify.send("✅ Trade platform gestart (paper mode)")
        yield
        scheduler.shutdown(wait=False)

    app.router.lifespan_context = lifespan
    return app


def run() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)  # nosec B104 - container port


if __name__ == "__main__":
    run()
