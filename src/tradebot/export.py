"""Lichte meetexport: de fase 2-data los van de image-back-up.

De HA-back-up maakt dagelijks een volledig image en bewaart er drie. Voor een
DIENST is dat prima, maar fase 2 is een accumulerende meting van vier tot acht
weken. Ontdek je over twee weken dat de analyzercijfers al een tijd niet kloppen,
dan is er binnen drie dagen retentie geen goede toestand meer om naar terug te
vallen.

Deze export is geen vervanging van die back-up maar een aanvulling met een heel
ander herstelpad: een paar honderd kilobyte gzip in plaats van een versleuteld
image van ruim een gigabyte, en terugzetten duurt seconden. Hij is meteen het
bewijsartefact waar deze reviewronde over ging: elke export draagt de config én de
per-gate fingerprints waaronder de data is ontstaan, zodat een conclusie later
navolgbaar blijft in plaats van alleen de conclusie te bewaren.

Usage:
    python -m tradebot.export                      # schrijf een export weg
    python -m tradebot.export --list               # toon wat er ligt
    python -m tradebot.export --import <bestand>   # terugzetten in een LEGE DB
"""
from __future__ import annotations

import gzip
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from . import __version__
from .config import SHADOW_GATES, gate_fingerprint
from .db import (
    EquityRow,
    KVRow,
    LLMCallRow,
    PositionRow,
    SignalRow,
    TradeRow,
    session,
)

log = logging.getLogger(__name__)

# Tweede bestemming naast de werkvoorraad. `/data` is niet vanaf het netwerk te
# benaderen en overleeft geen verwijdering-en-herinstallatie van de add-on: om er
# een bestand uit te halen heb je óf een draaiende Pi (dan had je de export niet
# nodig) óf precies het image dat de export moest vermijden. `/share` staat ook in
# de back-up, is via Samba te pakken zonder de add-on aan te raken, en blijft staan
# bij herinstallatie.
DEFAULT_SHARE_DIR = "/share/tradebot-export"

TABELLEN = {
    "trades": TradeRow,
    "signals": SignalRow,
    "positions": PositionRow,
    "equity": EquityRow,
    "llm_calls": LLMCallRow,
    "kv": KVRow,
}

# Config-secties die mee moeten om een meting later te kunnen duiden.
CONFIG_SECTIES = ("strategy", "decision", "fees", "risk", "exits", "regime",
                  "universe", "schedule", "markets", "watchlist", "blocklist")


def _waarde(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _rijen(model) -> list[dict]:
    with session() as s:
        objecten = s.execute(select(model)).scalars().all()
    kolommen = [c.name for c in model.__table__.columns]
    return [{k: _waarde(getattr(o, k)) for k in kolommen} for o in objecten]


def build_export(cfg, mode: str | None = None) -> dict:
    """Bouw de exportinhoud. Puur op de DB en de config, geen bestandssysteem."""
    data = {naam: _rijen(model) for naam, model in TABELLEN.items()}
    return {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
            "mode": mode,
            "rows": {naam: len(rijen) for naam, rijen in data.items()},
            # Zonder deze twee is de export data zonder context: je weet dan wel
            # wat er gebeurde, maar niet onder welke configuratie.
            "config": {sectie: _config_sectie(cfg, sectie) for sectie in CONFIG_SECTIES},
            "gate_hash": {gate: gate_fingerprint(cfg, gate) for gate in SHADOW_GATES},
        },
        "data": data,
    }


def _config_sectie(cfg, sectie: str):
    waarde = getattr(cfg, sectie, None)
    if isinstance(waarde, dict):
        return dict(waarde)
    if isinstance(waarde, list):
        return list(waarde)
    return waarde


def schrijf_export(cfg, doelmap: Path, mode: str | None = None) -> Path:
    doelmap.mkdir(parents=True, exist_ok=True)
    inhoud = build_export(cfg, mode)
    stempel = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pad = doelmap / f"tradebot-export-{stempel}.json.gz"
    with gzip.open(pad, "wt", encoding="utf-8") as fh:
        json.dump(inhoud, fh, ensure_ascii=False, default=str)
    return pad


def lees_export(pad: Path) -> dict:
    with gzip.open(pad, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def opruimen(doelmap: Path, bewaar: int) -> list[Path]:
    """Houd de nieuwste `bewaar` exports over; geeft terug wat er weg is."""
    bestanden = sorted(doelmap.glob("tradebot-export-*.json.gz"))
    weg = bestanden[:-bewaar] if bewaar > 0 and len(bestanden) > bewaar else []
    for pad in weg:
        pad.unlink()
    return weg


def importeer(pad: Path, *, force: bool = False) -> dict[str, int]:
    """Zet een export terug in een LEGE database.

    Weigert standaard op een gevulde tabel: importeren bovenop bestaande data
    geeft dubbele rijen en botst op de unieke markt-constraint van `positions`.
    """
    inhoud = lees_export(pad)
    aantallen: dict[str, int] = {}
    with session() as s:
        if not force:
            bezet = [naam for naam, model in TABELLEN.items()
                     if s.execute(select(model).limit(1)).first() is not None]
            if bezet:
                raise ValueError(
                    f"database is niet leeg ({', '.join(bezet)}); gebruik force=True "
                    "als je bewust bovenop bestaande data wilt importeren")
        for naam, model in TABELLEN.items():
            kolommen = {c.name: c for c in model.__table__.columns}
            for rij in inhoud["data"].get(naam, []):
                velden = {}
                for k, v in rij.items():
                    if k not in kolommen:
                        continue          # kolom bestaat niet meer in dit schema
                    kolom = kolommen[k]
                    if isinstance(v, str) and str(kolom.type).startswith("DATETIME"):
                        v = datetime.fromisoformat(v)
                    velden[k] = v
                s.add(model(**velden))
            aantallen[naam] = len(inhoud["data"].get(naam, []))
        s.commit()
    return aantallen


def export_map(cfg, database_url: str) -> Path:
    """Doelmap: uit config, anders naast de database (in de add-on dus /data/exports)."""
    export_cfg = getattr(cfg, "export", {}) or {}
    ingesteld = str(export_cfg.get("dir", "") or "").strip()
    if ingesteld:
        return Path(ingesteld)
    if database_url.startswith("sqlite:///"):
        return Path(database_url.replace("sqlite:///", "")).parent / "exports"
    return Path("data/exports")


def kopieer_naar_share(pad: Path, share_dir: str | None = None) -> Path | None:
    """Zet de nieuwste export ook buiten `/data` neer.

    Faalt stil als de map niet bestaat (buiten de add-on, bijvoorbeeld op een
    ontwikkelmachine): dat is geen fout maar een andere omgeving.
    """
    doel = Path(share_dir or DEFAULT_SHARE_DIR)
    if not doel.parent.exists():
        return None
    doel.mkdir(parents=True, exist_ok=True)
    kopie = doel / pad.name
    shutil.copy2(pad, kopie)
    return kopie


def wal_checkpoint() -> None:
    """Schrijf de WAL terug in het hoofdbestand na een export.

    Sinds v0.20.0 draait SQLite in WAL-modus, dus `tradebot.db` is op zichzelf niet
    meer de volledige database: er staan `-wal` en `-shm` naast en gecommitte
    transacties kunnen nog in de WAL zitten. Een PASSIVE checkpoint blokkeert nooit
    en verkleint het venster waarin iemand die onder druk snel "even het
    db-bestand" veiligstelt, een onvolledige kopie meeneemt. Het VERVANGT die regel
    niet: kopieer altijd alle drie de bestanden, of gebruik deze export.
    """
    from .db import _engine
    if _engine is None or _engine.dialect.name != "sqlite":
        return
    with _engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")


def geplande_export(cfg, database_url: str, mode: str) -> Path | None:
    """Scheduler-taak: schrijf een export, kopieer naar /share, ruim oude op.

    Mag nooit de bot breken.
    """
    export_cfg = getattr(cfg, "export", {}) or {}
    if not bool(export_cfg.get("enabled", True)):
        return None
    try:
        doelmap = export_map(cfg, database_url)
        pad = schrijf_export(cfg, doelmap, mode)
        kopie = kopieer_naar_share(pad, export_cfg.get("share_dir"))
        verwijderd = opruimen(doelmap, int(export_cfg.get("keep", 30)))
        if kopie is not None:
            opruimen(kopie.parent, int(export_cfg.get("share_keep", 7)))
        wal_checkpoint()
        log.info("meetexport geschreven: %s (%d oude opgeruimd, share=%s)",
                 pad.name, len(verwijderd), kopie or "n.v.t.")
        return pad
    except Exception:  # noqa: BLE001 - een mislukte export mag de bot niet stoppen
        log.exception("meetexport mislukt")
        return None


def main() -> None:  # pragma: no cover - CLI-gemak, niet in de testsuite
    import argparse

    from .config import get_config, get_secrets
    from .db import init_db

    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--import", dest="importeren", metavar="BESTAND")
    parser.add_argument("--force", action="store_true",
                        help="importeren bovenop bestaande data toestaan")
    args = parser.parse_args()

    cfg, secrets = get_config(), get_secrets()
    init_db(secrets.database_url)
    doelmap = export_map(cfg, secrets.database_url)

    if args.importeren:
        aantallen = importeer(Path(args.importeren), force=args.force)
        print(json.dumps(aantallen, indent=2))
        return
    if args.list:
        for pad in sorted(doelmap.glob("tradebot-export-*.json.gz")):
            meta = lees_export(pad)["meta"]
            print(f"{pad.name}  v{meta['version']}  mode={meta['mode']}  "
                  f"{sum(meta['rows'].values())} rijen  {pad.stat().st_size // 1024} kB")
        return
    pad = schrijf_export(cfg, doelmap, secrets.trading_mode)
    print(f"{pad}  ({pad.stat().st_size // 1024} kB)")


if __name__ == "__main__":  # pragma: no cover
    main()
