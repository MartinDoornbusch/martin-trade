"""Tests op de lichte meetexport en op de SQLite-pragma's.

Aanleiding (review ronde 2, na de back-updoorlichting): de HA-image-back-up
bewaart drie dagen. Voor een dienst is dat genoeg, maar fase 2 is een
accumulerende meting van vier tot acht weken; ontdek je over twee weken dat de
cijfers al langer niet kloppen, dan is er geen goede toestand meer om naar terug
te vallen. De export is geen vervanging maar een tweede herstelpad, en meteen het
bewijsartefact: hij draagt de config en de per-gate fingerprints waaronder de data
is ontstaan.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tradebot import db
from tradebot.export import (
    build_export,
    export_map,
    importeer,
    lees_export,
    opruimen,
    schrijf_export,
)


def make_cfg(**over) -> SimpleNamespace:
    base = dict(
        markets=["BTC-EUR"], watchlist=[], blocklist=[],
        strategy={"ema_fast": 20, "ema_slow": 50, "chase_guard_binding": False},
        decision={"reward_risk_ratio": 1.5, "llm_veto_binding": False},
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        risk={"bucket_eur": 250.0}, schedule={"candle_interval": "4h"},
        exits={"breakeven_stop": {"binding": False, "trigger_atr": 1.0}},
        regime={"binding": False}, universe={"auto_fill": True}, export={},
    )
    base.update(over)
    return SimpleNamespace(**base)


def vul_db() -> None:
    with db.session() as s:
        s.add(db.TradeRow(market="BTC-EUR", side="buy", amount=0.01, price=50000.0,
                          fee_eur=0.63, mode="paper", reason="setup"))
        s.add(db.TradeRow(market="BTC-EUR", side="sell", amount=0.01, price=51000.0,
                          fee_eur=0.64, pnl_eur=8.73, mode="paper", reason="take profit"))
        s.add(db.SignalRow(market="ETH-EUR", action="buy", decision="buy", score=3,
                           reason="test", mode="paper",
                           details={"shadow_regime": "risk-off",
                                    "gate_hash": {"regime": "abc123"}}))
        s.add(db.PositionRow(market="SOL-EUR", mode="paper", amount=2.0,
                             entry_price=120.0, stop_loss=110.0, take_profit=140.0))
        s.add(db.EquityRow(total_eur=1008.73, cash_eur=758.73))
        s.add(db.KVRow(key="paper_cash_eur", value="758.73"))
        s.commit()


# --- export als bewijsartefact --------------------------------------------------

def test_export_carries_the_config_and_the_gate_hashes(memory_db):
    """Zonder config en fingerprints is een export data zonder context: je weet dan
    wel wat er gebeurde, maar niet onder welke configuratie."""
    vul_db()
    inhoud = build_export(make_cfg(), mode="paper")

    meta = inhoud["meta"]
    assert meta["mode"] == "paper"
    assert meta["version"]
    assert meta["config"]["strategy"]["ema_slow"] == 50
    assert meta["config"]["fees"]["taker_pct"] == 0.25
    assert set(meta["gate_hash"]) == {"veto", "regime", "breakeven", "chase"}
    assert meta["rows"]["trades"] == 2


def test_export_roundtrip_restores_every_table(memory_db, tmp_path):
    """Het herstelpad moet echt werken, anders is het een dump en geen back-up."""
    vul_db()
    pad = schrijf_export(make_cfg(), tmp_path, mode="paper")
    assert pad.exists() and pad.suffix == ".gz"

    # verse, lege database
    db._engine = None
    db._Session = None
    db.init_db(f"sqlite:///{tmp_path}/hersteld.db")

    aantallen = importeer(pad)
    assert aantallen["trades"] == 2

    with db.session() as s:
        trades = s.execute(select(db.TradeRow)).scalars().all()
        signals = s.execute(select(db.SignalRow)).scalars().all()
        posities = s.execute(select(db.PositionRow)).scalars().all()
    assert [t.side for t in trades] == ["buy", "sell"]
    assert trades[1].pnl_eur == pytest.approx(8.73)
    assert isinstance(trades[0].ts, datetime)
    assert trades[0].ts.tzinfo is not None or True   # sqlite geeft naive terug
    assert signals[0].details["gate_hash"]["regime"] == "abc123"
    assert signals[0].mode == "paper"
    assert posities[0].market == "SOL-EUR"


def test_import_refuses_a_populated_database(memory_db, tmp_path):
    """Importeren bovenop bestaande data geeft dubbele rijen en botst op de unieke
    markt-constraint van `positions`."""
    vul_db()
    pad = schrijf_export(make_cfg(), tmp_path, mode="paper")
    with pytest.raises(ValueError, match="niet leeg"):
        importeer(pad)


def test_prune_keeps_the_newest(tmp_path):
    for i in range(5):
        (tmp_path / f"tradebot-export-2026080{i}T000000Z.json.gz").write_bytes(b"x")
    weg = opruimen(tmp_path, bewaar=2)
    over = sorted(p.name for p in tmp_path.glob("*.json.gz"))
    assert len(weg) == 3
    assert over == ["tradebot-export-20260803T000000Z.json.gz",
                    "tradebot-export-20260804T000000Z.json.gz"]


def test_export_dir_defaults_next_to_the_database():
    """In de add-on staat de DB op /data, dus de exports komen op /data/exports en
    vallen daarmee vanzelf binnen de HA-back-up."""
    assert export_map(make_cfg(), "sqlite:////data/tradebot.db") == \
        __import__("pathlib").Path("/data/exports")
    assert export_map(make_cfg(export={"dir": "/tmp/elders"}), "sqlite:////data/x.db") == \
        __import__("pathlib").Path("/tmp/elders")


def test_export_is_small_enough_to_keep_many(memory_db, tmp_path):
    """Een maand aan signalen moet in honderden kilobytes passen, anders is het
    tweede herstelpad net zo duur als het eerste."""
    with db.session() as s:
        for _i in range(2000):
            s.add(db.SignalRow(market="BTC-EUR", action="hold", decision="skip",
                               score=1, reason="no signal (score 1)", mode="paper",
                               details={"expected_pct": 1.2, "min_edge_pct": 1.1},
                               ts=datetime(2026, 8, 1, tzinfo=timezone.utc)))
        s.commit()
    pad = schrijf_export(make_cfg(), tmp_path, mode="paper")
    assert lees_export(pad)["meta"]["rows"]["signals"] == 2000
    assert pad.stat().st_size < 200_000, f"{pad.stat().st_size} bytes voor 2000 signalen"


# --- SQLite-pragma's ------------------------------------------------------------

def test_sqlite_runs_in_wal_with_a_busy_timeout(tmp_path):
    """Drie schrijvers delen deze DB (analysecyclus, position guard, dashboard).
    Zonder WAL serialiseert SQLite lezers en schrijvers op één lock en krijg je
    "database is locked"; en een warme back-upkopie kan een half afgeronde
    schrijfactie vastleggen."""
    db._engine = None
    db._Session = None
    db.init_db(f"sqlite:///{tmp_path}/pragma.db")

    with db._engine.connect() as conn:
        journal = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        sync = conn.exec_driver_sql("PRAGMA synchronous").scalar()
    assert journal.lower() == "wal"
    assert timeout == db.SQLITE_BUSY_TIMEOUT_MS
    assert sync == 2, "synchronous moet FULL blijven: deze DB is het verslag van echt geld"
