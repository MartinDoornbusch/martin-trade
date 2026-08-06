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
draaien:

```bash
# Oude code in een aparte worktree, zonder je huidige checkout aan te raken
git worktree add /tmp/juli18 56d9e55
cd /tmp/juli18
for M in BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR; do
    PYTHONPATH=src python -m tradebot.backtest "$M" --interval 4h --limit 1100
done
```

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
- de config in `/tmp/juli18` is die van 18 juli. Dat is precies de bedoeling, maar
  controleer wel dat `ema_fast/ema_slow` daar 20/50 is en `reward_risk_ratio` 1,5.

**Wijkt rij 1 af, stop dan.** Dan is er onderweg nog iets anders veranderd en is elke delta
eronder betekenisloos.

## 1. Waarom niet gewoon de grid opnieuw draaien

De backtester van 18 juli was op **zeven** punten anders dan de huidige:

| # | Punt | Wat er anders was |
|---|------|-------------------|
| r1/1.2 | trend-break-exit | `check_exit` had een derde regel (EMA-cross-down + RSI > 70), geschrapt in v0.19.0 |
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
- Verwachting bij r1/1.2: delta ongeveer nul. De trend-break-exit eiste twee vrijwel
  disjuncte condities en heeft in productie nooit gevuurd. Is de delta hier wél groot, dan
  is de conclusie uit ronde 1 ("hij vuurde nooit") op een te kleine steekproef getrokken.

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
