# Hermeting v0.20.0 — attributie en kalibratie

Status: **klaar om te draaien, cijfers nog in te vullen.** De bouwomgeving heeft geen
netwerktoegang tot `api.bitvavo.com`, dus de runs gebeuren op de Pi of een laptop.

## 0. Eerst de referentierij valideren — anders is alles eronder betekenisloos

Het v0.18.0-model wordt gereconstrueerd door correcties uit te zetten, niet door de oude
code te draaien. Dat is de goedkope aanpak, maar hij is alleen geldig als rij 1 de uitvoer
van 18 juli 2026 daadwerkelijk reproduceert op dezelfde data.

**Bevinding bij het opstellen van dit document: die uitvoer is nergens vastgelegd.** Er is
geen `docs/backtest-bevindingen-en-beslissing.md`. Wat er van de kalibratie van 18 juli
over is:

- commit `56d9e55` (18 juli 2026, 21:51) met de boodschap "fase2 kalibratie: ema20/50,
  rr1.5, LLM shadow-mode" en een config-diff (ema 12/26 → 20/50, rr 2,0 → 1,5,
  `llm_veto_binding` true → false). Geen cijfers, geen commando, geen `--limit`;
- één overgebleven getal in `docs/ontwerp-ev-gate.md`: "de backtest van 2026-07-18 gaf
  11-24 trades per markt per half jaar".

De les daaruit staat los van deze hermeting: **leg de uitvoer van een kalibratie vast, niet
alleen de conclusie.** Voor deze ronde is dat opgelost door dit document als vaste vorm te
gebruiken.

Omdat er niets is om tegenaan te leggen, is de enige echte anker-check de oude code zelf
draaien.

**Wijs beide runs naar hetzelfde configbestand.** De worktree brengt zijn eigen
`config/config.yaml` van 18 juli mee, en de optimizer overschrijft alleen ema, score, atr en
rr uit de grid, niet `min_profit_pct`, de fees of de exit-parameters. Zonder dat te pinnen
lees je een configverschil als een codeverschil.

Dat vraagt één ingreep, want de oude code leest een sleutel die niet meer bestaat: regel 71
van de toenmalige `strategy.py` doet `cfg["rsi_oversold"]`, en die is in v0.19.0 vervangen
door `rsi_buy_zone_min/max`. Wijs je de oude code zonder meer naar de huidige config, dan
crasht hij op een `KeyError`. Voeg die ene sleutel toe aan een kopie:

```bash
# Ankerconfig = de huidige config plus de ene sleutel die de oude code nodig heeft.
# rsi_oversold 35 geeft in de oude regel (rsi > 25 and rsi < rsi_oversold + 10)
# exact de zone 25-45 die nu expliciet in de config staat: gedrag identiek.
cp config/config.yaml /tmp/anker-config.yaml
#  -> voeg met de hand `rsi_oversold: 35` toe ONDER de bestaande strategy-sectie

# Oude code in een aparte worktree, zonder de huidige checkout aan te raken
git worktree add /tmp/juli18 56d9e55
cd /tmp/juli18
for M in BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR; do
    CONFIG_PATH=/tmp/anker-config.yaml PYTHONPATH=src python -m tradebot.backtest \
        "$M" --interval 4h --limit 1100
done
```

De attributierun draait op de repo-config, dus de vergelijking gaat dan zuiver over code.
Welke velden de oude code leest en dus moeten kloppen: `strategy` (ema_fast, ema_slow,
rsi_period, rsi_oversold, atr_period, min_signal_score), `decision` (min_profit_pct,
atr_stop_multiplier, reward_risk_ratio) en `fees` (maker, taker, slippage). De secties
`exits`, `regime`, `universe` en `blocklist` kende hij nog niet en worden genegeerd.

| Markt | Juli-code: rendement % / trades / win % | Rij 1 van de attributie | Gelijk? |
|-------|------------------------------------------|--------------------------|---------|
| BTC-EUR | | | |
| ETH-EUR | | | |
| SOL-EUR | | | |
| XRP-EUR | | | |
| LINK-EUR | | | |

Let op twee dingen bij die vergelijking:

- de oude `main()` neemt één markt tegelijk en print per markt; de attributie middelt over
  de vijf markten. Vergelijk dus per markt, of middel de vijf oude uitkomsten;
- controleer dat `CONFIG_PATH` daadwerkelijk is opgepikt. Draait hij op de worktree-config,
  dan zie je dezelfde ema en rr (18 juli had die al), maar wel andere fees- of
  exit-parameters als die sindsdien zijn gewijzigd.

**Wijkt rij 1 af, stop dan.** Dan is er onderweg nog iets anders veranderd en is elke delta
eronder betekenisloos.

## 1. Waarom niet gewoon de grid opnieuw draaien

De backtester van 18 juli was op **zeven** punten anders dan de huidige:

| # | Punt | Wat er anders was |
|---|------|-------------------|
| r1/1.2 | trend-break-exit | `check_exit` had een derde regel (EMA-cross-down + RSI > 70, hardcoded), geschrapt in v0.19.0 |
| 2.1 | time-stop | ontbrak volledig in de backtest |
| 2.1 | breakeven-stop | ontbrak volledig in de backtest |
| 2.2 | exits | slotkoers in plaats van intrabar high/low |
| 2.3 | slippage | zat in `min_edge` maar niet op de fill |
| 2.4 | sizing | all-in per positie in plaats van buckets en slots |
| 3.2 | warmup | vast op 60, te kort voor ema_slow 50 |

De trend-break-exit stond niet in de oorspronkelijke lijst van zes. Hij is gevonden door
`git diff 56d9e55 ead5554 -- src/tradebot/strategy.py`: `backtest.py` zelf is tussen die
twee commits byte-identiek, maar de exitregels die hij aanroept zijn dat niet. Precies het
soort verschil dat de anker-check moet vangen.

Zet je die zeven tegelijk aan en de winnaar verschuift, dan weet je alleen *dát* hij
verschoof. De vraag "zijn ema20/50, rr 1,5 en score 3 nog steeds de winnaars" is dan niet
beantwoordbaar, alleen "ze zijn het niet meer".

**Blok 1 (entries op afgesloten candles) staat bewust niet in die lijst.** De backtester had
dat probleem nooit: historische candles zijn per definitie afgesloten. Precies daarom
maakte de live bot méér trades dan de backtest voorspelde — de fout zat in de engine, niet
in het model. Blok 1 is dus niet met een backtest te attribueren; het effect verschijnt in
de paper-run.

## 2. Attributie op de productievariant

```bash
PYTHONPATH=src python -m tradebot.calibrate \
    BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 1100
```

`--limit 1100` (circa 6 maanden 4h) is hier het juiste venster: vergelijkbaarheid met juli
telt zwaarder dan statistische ruimte, want dit is een verschilmeting op dezelfde data.

| Stap | Punt | Rendement % | Delta | Trades | Win % | DD % |
|------|------|------------|-------|--------|-------|------|
| v0.18.0-model (zoals 18 juli) | referentie | | — | | | |
| − trend-break-exit | r1/1.2 | | | | | |
| + intrabar exits | 2.2 | | | | | |
| + slippage op beide benen | 2.3 | | | | | |
| + time-stop | 2.1 | | | | | |
| + geschaalde warmup | 3.2 | | | | | |
| **+ bucket-sizing en slots** ← productie | 2.4 | | | | | |
| + breakeven-stop BINDEND | 2.1 | | | | | |

Bij het lezen:

- **De onderste rij is niet de productiebot.** De breakeven-stop staat daar bindend, wat
  in een backtest onvermijdelijk is (een shadow-gate doet per definitie niets), terwijl hij
  in productie op shadow staat. De chase-guard zit helemaal niet in de backtester. De rij
  die het huidige gedrag beschrijft is die met `← productie`; de regel eronder leest als
  "wat de bot zou doen als je de breakeven-stop bindend maakt", wat meteen nuttige input is
  voor die go/no-go.
- De correcties zijn **niet additief**. Een andere stapelvolgorde geeft andere
  tussenstappen bij hetzelfde eindresultaat. Lees elke delta als "wat deze correctie
  toevoegde gegeven de voorgaande".
- Verwachting bij 2.2: de win-rate zakt. Het oude model hield een positie open als een
  candle met zijn low door de stop ging maar erboven sloot, terwijl de position guard live
  binnen de minuut uitstopt. Zakt hij niet, dan speelt er iets anders.
- **De trend-break-exit zit in rij 1 zelf en de eerste delta is het weghalen ervan**, niet
  andersom. Dat is geen cosmetische volgorde. `check_exit` toetste SL en TP eerst, dus de
  trend-break kwam alleen aan bod als geen van beide raakte. In het oude model met
  close-exits werden intrabar-stops gemist, dus overleefden er méér posities tot aan die
  derde regel. Zou je de trend-break pas aanzetten nadat de intrabar-correctie erin zit, dan
  meet je zijn effect in een wereld die nooit heeft bestaan, en systematisch te laag.
- Verwachting bij r1/1.2: delta ongeveer nul. **Let op: dat is een aanname, geen meting.**
  Het bewijs uit ronde 1 was tweeledig, coverage liet zien dat de TESTSUITE die regel nooit
  raakte en de twee condities zijn bijna disjunct, en geen van beide is een productiemeting.
  Er is nooit geteld hoe vaak hij live vuurde. Deze stap is de eerste keer dat het echt
  gemeten wordt. Is de delta groot, dan was de conclusie van ronde 1 fout.

## 3. Volledige grid, één keer, met alles aan

```bash
PYTHONPATH=src python -m tradebot.optimizer \
    BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 3000 --portfolio
```

**Bewust een ander venster dan stap 2.** Met `--limit 1100` en een 70/30-split blijven er
330 testcandles over, waar de geschaalde warmup van 150 nog eens vanaf gaat: 180 bruikbare
candles, oftewel 30 dagen. Op dat venster haalt vrijwel geen enkele variant de drempel van
20 afgewikkelde trades, en dan lever je een tabel op waarin alles gemarkeerd staat als
`n<20, dd onbetrouwbaar`. Met `--limit 3000` wordt de testhelft 900 candles, na warmup 750,
oftewel circa 125 dagen. Twee vensters voor twee doelen: stap 2 meet een verschil op
dezelfde data als juli, stap 3 kiest parameters en heeft statistische ruimte nodig.

De run doet pass 1 (81 kernvarianten) en pass 2 (81 exit-varianten op de winnaar), elk op
train én test, gesorteerd op risicogecorrigeerd testrendement.

### Vergelijking met 18 juli 2026

| Parameter | Winnaar 18 juli | Winnaar v0.20.0 | Verschuift? | Toelichting |
|-----------|-----------------|-----------------|-------------|-------------|
| EMA-paar | 20/50 | | | |
| reward\_risk\_ratio | 1,5 | | | |
| min\_signal\_score | 3 | | | |
| atr\_stop\_multiplier | 2,0 | | | |
| RSI-koopzone | 25-45 (niet in de grid van juli) | | | nieuw in pass 2 |
| min\_profit\_pct | 0,50 (niet in de grid van juli) | | | nieuw in pass 2 |
| time\_stop\_candles | 12 (niet in de grid van juli) | | | nieuw in pass 2 |
| breakeven trigger | 1,0x ATR (niet in de grid van juli) | | | nieuw in pass 2 |

### Besluit

Config **alleen** wijzigen als de nieuwe meting daar aanleiding toe geeft. Drie voorwaarden
voordat een parameter verandert:

1. de nieuwe waarde wint op de **test**-kolom, niet op train;
2. de variant haalt minstens 20 afgewikkelde trades (daaronder is max drawdown één
   ongelukkige reeks in plaats van een meting);
3. de gap tussen train en test is niet groter dan die van de huidige waarde.

Wijzig je iets in `strategy`, `decision`, `fees` of `universe`, dan raak je de **kern** van
de meet-fingerprint: alle vier de shadow-gates beginnen dan opnieuw te tellen richting hun
drempel van 20 afgewikkelde trades. Noteer die wijziging in het register in PROJECTPLAN.md,
net als een gate-flip.

| Datum | Wijziging | Reden | Cohorte-reset? |
|-------|-----------|-------|----------------|
| | | | |
