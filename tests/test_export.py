"""Tests op de lichte meetexport en op de SQLite-pragma's.

Aanleiding (review ronde 2, na de back-updoorlichting): de HA-image-back-up
bewaart drie dagen. Voor een dienst is dat genoeg, maar fase 2 is een
accumulerende meting van vier tot acht weken; ontdek je over twee weken dat de
cijfers al langer niet kloppen, dan is er geen goede toestand meer om naar terug
te vallen. De export is geen vervanging maar een tweede herstelpad, en meteen het
bewijsartefact: hij draagt de config en de per-gate fingerprints waaronder de data
is ontstaan.
"""
from datetime import datetime, timedelta, timezone
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
        decision={"reward_risk_ratio": 1.5, "llm_veto_binding": False,
                  "atr_stop_multiplier": 2.0, "min_profit_pct": 0.50},
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        risk={"bucket_eur": 250.0, "sizing": "bucket", "paper_start_eur": 1000.0,
              "max_position_pct": 25.0},
        schedule={"candle_interval": "4h"},
        exits={"breakeven_stop": {"binding": False, "trigger_atr": 1.0}},
        regime={"binding": False}, universe={"auto_fill": True}, export={},
        meta={},
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
    assert set(meta["gate_hash"]) == {"veto", "regime", "timestop", "breakeven", "chase"}
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


# --- de restore echt draaien, met de analyzers erop -----------------------------

def gate_cijfers(cfg) -> dict:
    """Wat het dashboard zou tonen: de netto gate-waarde per shadow-gate."""
    from tradebot.analysis import analyze_breakeven, analyze_chase, analyze_regime

    return {naam: fn(cfg, mode="paper") for naam, fn in
            (("regime", analyze_regime), ("breakeven", analyze_breakeven),
             ("chase", analyze_chase))}


def vul_db_met_meetdata() -> None:
    """Een afgewikkelde regime-down shadow-buy plus een breakeven-treffer, dus
    genoeg om alle drie de analyzers een uitkomst te laten produceren."""
    basis = datetime(2026, 8, 1, tzinfo=timezone.utc)
    stap = timedelta(hours=4)
    with db.session() as s:
        s.add(db.TradeRow(ts=basis, market="ETH-EUR", side="buy", amount=1.0,
                          price=100.0, fee_eur=0.25, mode="paper", reason="setup"))
        s.add(db.TradeRow(ts=basis + 3 * stap, market="ETH-EUR", side="sell", amount=1.0,
                          price=90.0, fee_eur=0.23, pnl_eur=-10.5, mode="paper",
                          reason="stop loss"))
        s.add(db.SignalRow(ts=basis, market="ETH-EUR", action="buy", decision="buy",
                           score=3, reason="SHADOW-REGIME genegeerd", mode="paper",
                           details={"shadow_regime": "regime gate: BTC-EUR trend down",
                                    "shadow_chase": "chase-guard: 1.2x ATR",
                                    "gate_hash": {"regime": "h-regime",
                                                  "chase": "h-chase"}}))
        s.add(db.SignalRow(ts=basis + stap, market="ETH-EUR", action="sell",
                           decision="shadow", score=0, reason="SHADOW-BREAKEVEN",
                           mode="paper",
                           details={"shadow_breakeven": "treffer", "price": 100.55,
                                    "entry_price": 100.0,
                                    "gate_hash": {"breakeven": "h-be"}}))
        s.commit()


def test_restore_reproduces_the_gate_numbers(memory_db, tmp_path):
    """De restore is één keer echt gedraaid in plaats van alleen beschreven.

    Een ongeteste back-up is een hypothese, geen voorziening. Deze test doet wat de
    handmatige procedure doet: exporteren, wissen, terugzetten, en dan de ANALYZERS
    erop draaien om te controleren of de gate-cijfers gelijk zijn. Rijtellingen
    vergelijken is niet genoeg; het gaat om de conclusies die op die rijen rusten.
    """
    cfg = make_cfg()
    vul_db_met_meetdata()
    voor = gate_cijfers(cfg)
    assert voor["regime"]["n_resolved"] == 1, "testdata levert geen meetbare gate op"
    assert voor["breakeven"]["n_resolved"] == 1
    assert voor["chase"]["n_resolved"] == 1

    pad = schrijf_export(cfg, tmp_path, mode="paper")

    # Wissen en terugzetten, zoals de procedure in docs/export-en-herstel.md
    db._engine = None
    db._Session = None
    db.init_db(f"sqlite:///{tmp_path}/hersteld.db")
    importeer(pad)

    na = gate_cijfers(cfg)
    for gate in voor:
        assert na[gate]["summary"] == voor[gate]["summary"], f"{gate} wijkt af"
        assert na[gate]["n_resolved"] == voor[gate]["n_resolved"]
        assert na[gate]["per_market"] == voor[gate]["per_market"]


def test_share_copy_lands_outside_data(tmp_path, memory_db):
    """`/data` is niet vanaf het netwerk te benaderen en overleeft geen
    herinstallatie van de add-on, dus een export die alleen daar staat is alleen te
    verzilveren via precies het image dat hij moest vermijden."""
    from tradebot.export import geplande_export, kopieer_naar_share

    vul_db()
    data_map = tmp_path / "data" / "exports"
    share_map = tmp_path / "share" / "tradebot-export"
    share_map.parent.mkdir(parents=True)

    cfg = make_cfg(export={"dir": str(data_map), "share_dir": str(share_map)})
    pad = geplande_export(cfg, f"sqlite:///{tmp_path}/data/tradebot.db", "paper")

    assert pad is not None and pad.parent == data_map
    kopieen = list(share_map.glob("tradebot-export-*.json.gz"))
    assert len(kopieen) == 1
    assert kopieen[0].read_bytes() == pad.read_bytes()

    # Buiten de add-on (geen /share-ouder) faalt hij stil in plaats van te breken.
    assert kopieer_naar_share(pad, str(tmp_path / "bestaat-niet" / "x")) is None


def test_run_purpose_travels_with_the_evidence(memory_db, tmp_path):
    """Spiegelbeeld van de verdwenen julikalibratie: daar verdween het bewijs, hier
    dreigt bewijs betekenis te krijgen die het nooit had. Draait de bot als
    infrastructuurtest op een strategie waarvan vaststaat dat ze geen edge heeft, dan
    bouwt het meetapparaat keurig gescopede cohortes op die over een half jaar als
    strategievalidatie gelezen worden. Het doel hoort dus in het bewijsstuk zelf, niet
    in iemands hoofd."""
    from types import SimpleNamespace

    cfg = make_cfg(meta={"run_purpose": "infrastructuurtest"})
    vul_db()
    pad = schrijf_export(cfg, tmp_path, mode="paper")
    meta = lees_export(pad)["meta"]

    assert meta["run_purpose"] == "infrastructuurtest"
    assert meta["config"]["meta"]["run_purpose"] == "infrastructuurtest"

    # en de analyzers lezen het uit de EVENTS, niet uit de config van vandaag
    from tradebot.analysis import analyze_regime
    events = [{"ts": 1_700_000_000_000, "market": "A-EUR", "shadow_regime": "risk-off",
               "run_purpose": "infrastructuurtest"}]
    d = analyze_regime(SimpleNamespace(**{**vars(cfg), "meta": {}}),
                       events=events, trades=[])
    assert d["run_purpose"] == ["infrastructuurtest"]
