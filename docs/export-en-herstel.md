# Meetexport en herstel

De HA-back-up maakt dagelijks een volledig versleuteld image, bewaart er drie, en gaat naar
de NAS naast lokaal. Dat dekt een uitval van de Pi. Wat het niet dekt is het scenario dat
fase 2 kenmerkt: over twee weken ontdekken dat de analyzercijfers al langer niet kloppen.
Binnen drie dagen retentie is er dan geen goede toestand meer om naar terug te vallen, en
het herstelpad loopt hoe dan ook via een image-restore.

De meetexport is daarom geen vervanging maar een tweede pad met heel andere kosten:
tientallen kilobytes gzip in plaats van een image van gigabytes, en terugzetten in seconden.

## Wat er in zit, en waarom dat ertoe doet

Elke export bevat de tabellen `trades`, `signals`, `positions`, `equity`, `llm_calls` en
`kv`, plus metadata: tijdstip, versie, mode, rijtellingen, **de volledige config** en **de
vier per-gate fingerprints**.

Die laatste twee maken het verschil tussen data en bewijs. Het patroon dat deze
reviewrondes telkens blootlegden is "conclusie bewaard, bewijs weggegooid": de kalibratie
van 18 juli liet een configwijziging na zonder de cijfers waarop die rustte. Een export
zonder config is precies dezelfde fout in het klein — je weet dan wél wat er gebeurde, maar
niet onder welke configuratie.

## Waar hij terechtkomt

| Bestemming | Rol | Waarom |
|-----------|-----|--------|
| `/data/exports` (`keep: 30`) | archief | staat in het image, dus lange historie |
| `/share/tradebot-export` (`share_keep: 7`) | grijpvoorraad | via Samba te pakken zónder de add-on aan te raken, en overleeft verwijderen-en-herinstalleren van de add-on |

Alleen `/data` zou het doel ondergraven: om daar een bestand uit te halen heb je óf een
draaiende Pi (en dan had je de export niet nodig) óf precies het image dat je wilde
vermijden. `/share` staat ook in de back-up, dus je verliest die dekking niet.

## WAL: `tradebot.db` is niet meer de database

Sinds v0.20.0 draait SQLite in WAL-modus. Er staan nu `tradebot.db-wal` en
`tradebot.db-shm` naast het hoofdbestand, en **gecommitte transacties kunnen in de WAL
zitten op het moment dat je kopieert**.

> **Kopieer nooit alleen `tradebot.db`.** Neem alle drie de bestanden mee, of gebruik deze
> export. Dat je onder druk snel "even het db-bestand" veiligstelt is precies het moment
> waarop dit misgaat.

Het HA-image pakt de hele map, dus dat gaat goed. De export leest via SQLAlchemy en ziet een
consistent beeld. Na elke export draait bovendien een `PRAGMA wal_checkpoint(PASSIVE)`, die
nooit blokkeert en het venster verkleint waarin het hoofdbestand achterloopt. Dat verkleint
het risico, het heft het niet op: de regel hierboven blijft gelden.

## Herstelprocedure

Let op: `--import` weigert op een gevulde database, want importeren bovenop bestaande data
geeft dubbele rijen en botst op de unieke markt-constraint van `positions`. Er hoort dus een
wisstap vóór, en die staat hier expliciet omdat een procedure die alleen in iemands hoofd
zit geen procedure is.

```bash
# 1. Pak de nieuwste export (via Samba uit /share, of uit /data/exports op de Pi)
ls -t /share/tradebot-export/

# 2. Zet hem eerst terug in een SCRATCH-database, nooit meteen over de echte heen
export SCRATCH=/tmp/tradebot-restore.db
DATABASE_URL="sqlite:///$SCRATCH" PYTHONPATH=src \
    python -m tradebot.export --import /share/tradebot-export/tradebot-export-<stempel>.json.gz

# 3. Draai de analyzers op de teruggezette data
DATABASE_URL="sqlite:///$SCRATCH" PYTHONPATH=src python -m tradebot.analysis.regime
DATABASE_URL="sqlite:///$SCRATCH" PYTHONPATH=src python -m tradebot.analysis.breakeven
DATABASE_URL="sqlite:///$SCRATCH" PYTHONPATH=src python -m tradebot.analysis.chase

# 4. Vergelijk met wat het dashboard op de Pi toont (dezelfde drie secties)

# 5. Pas als 4 klopt: de echte DB vervangen. Stop de add-on eerst, en neem
#    alle drie de WAL-bestanden mee in de oude-toestand-kopie.
#    ha addons stop local_tradebot
#    cp /data/tradebot.db /data/tradebot.db-wal /data/tradebot.db-shm /share/oude-toestand/
#    rm /data/tradebot.db /data/tradebot.db-wal /data/tradebot.db-shm
#    ... --import op de lege DB, daarna de add-on starten
```

PowerShell (stap 2 en 3, als je de scratch-restore op Windows doet):

```powershell
$env:DATABASE_URL = "sqlite:///$env:TEMP\tradebot-restore.db"
$env:PYTHONPATH   = "src"
python -m tradebot.export --import <pad-naar-export>.json.gz
python -m tradebot.analysis.regime
python -m tradebot.analysis.breakeven
python -m tradebot.analysis.chase
Remove-Item Env:DATABASE_URL      # anders draait de rest van je sessie op de scratch-DB
```

### Uitkomst van de gedraaide restore

Een ongeteste back-up is een hypothese, geen voorziening. Deze tabel invullen ná stap 4,
zodat de claim "dit is herstelbaar" bewijs heeft in plaats van alleen een beschrijving.

| Datum | Export | Rijen (trades / signals) | Netto gate regime | Netto gate breakeven | Netto gate chase | Gelijk aan dashboard? |
|-------|--------|--------------------------|-------------------|----------------------|------------------|------------------------|
| | | | | | | |

De geautomatiseerde tegenhanger draait in CI:
`tests/test_export.py::test_restore_reproduces_the_gate_numbers` exporteert, wist, zet terug
en vergelijkt de uitkomst van alle drie de analyzers, niet alleen de rijtellingen.
Geverifieerd dat die test omvalt zodra de restore de `details`-kolom laat vallen. Dat vervangt
de handmatige run niet — CI draait op synthetische data en niet op de echte Pi — maar het
vangt regressies tussen twee handmatige controles door.

## Wat dit niet oplost

De export beschermt de meetdata. Hij beschermt niet tegen het verlies van de
**encryptiesleutel** van de HA-back-up. Een versleutelde back-up op de NAS is waardeloos als
de sleutel alleen stond op het apparaat dat je kwijt bent, en dat is de enige fout in deze
opzet die je pas ontdekt op het moment dat je hem nodig hebt. Zie het openstaande punt in
PROJECTPLAN.md.
