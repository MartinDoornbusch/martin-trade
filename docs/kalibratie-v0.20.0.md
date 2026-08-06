# Hermeting v0.20.0 — attributie en kalibratie

Status: **klaar om te draaien, cijfers nog in te vullen.** De sandbox waarin v0.20.0 is
gebouwd heeft geen netwerktoegang tot `api.bitvavo.com`, dus de twee commando's hieronder
draai jij op de Pi of je laptop; de tabellen worden daarna op de echte uitvoer ingevuld.

## Waarom niet gewoon de grid opnieuw draaien

De kalibratie van 18 juli 2026 gebruikte een backtester die op zes punten anders was:

| # | Punt | Wat er anders was |
|---|------|-------------------|
| 2.1 | time-stop | ontbrak volledig in de backtest |
| 2.1 | breakeven-stop | ontbrak volledig in de backtest |
| 2.2 | exits | slotkoers in plaats van intrabar high/low |
| 2.3 | slippage | zat in `min_edge` maar niet op de fill |
| 2.4 | sizing | all-in per positie in plaats van buckets en slots |
| 3.2 | warmup | vast op 60, te kort voor ema_slow 50 |

Zet je die zes tegelijk aan en de winnaar verschuift, dan weet je alleen *dát* hij
verschoof. De vraag "zijn ema20/50, rr 1,5 en score 3 nog steeds de winnaars" is dan niet
beantwoordbaar, alleen "ze zijn het niet meer". Daarom eerst attributie op één variant,
daarna pas de volledige grid.

**Blok 1 (entries op afgesloten candles) staat bewust niet in die lijst.** De backtester
had dat probleem nooit: historische candles zijn per definitie afgesloten. Precies daarom
maakte de live bot méér trades dan de backtest voorspelde — de fout zat in de engine, niet
in het model. Blok 1 is dus niet met een backtest te attribueren en verandert deze cijfers
niet; het effect ervan zie je pas terug in de paper-run.

## Stap 1 — attributie op de productievariant

```bash
PYTHONPATH=src python -m tradebot.calibrate \
    BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 1100
```

Zes extra enkelvoudige runs, cumulatief gestapeld op ema20/50, score>=3, atr\*2,0, rr 1,5.

| Stap | Punt | Rendement % | Delta | Trades | Win % | DD % |
|------|------|------------|-------|--------|-------|------|
| v0.18.0-model (zoals 18 juli) | referentie | | — | | | |
| + intrabar exits | 2.2 | | | | | |
| + slippage op beide benen | 2.3 | | | | | |
| + time-stop | 2.1 | | | | | |
| + breakeven-stop (bindend) | 2.1 | | | | | |
| + geschaalde warmup | 3.2 | | | | | |
| + bucket-sizing en slots | 2.4 | | | | | |

Bij het lezen:

- De correcties zijn **niet additief**. Een andere stapelvolgorde geeft andere
  tussenstappen bij hetzelfde eindresultaat. Lees elke delta als "wat deze correctie
  toevoegde gegeven de voorgaande".
- Verwachting vooraf bij 2.2: de win-rate zakt. Het oude model hield een positie open als
  een candle met zijn low door de stop ging maar erboven sloot, terwijl de position guard
  live binnen de minuut uitstopt. Dat tilde de win-rate kunstmatig op. Zakt hij niet, dan
  is dat een signaal dat er iets anders speelt.
- Verwachting bij 2.4: rendement én drawdown dalen allebei, want er staat nog maar een
  fractie van het kapitaal in de markt. Dit is de enige modus die met de live-run te
  vergelijken is.

## Stap 2 — volledige grid, één keer, met alles aan

```bash
PYTHONPATH=src python -m tradebot.optimizer \
    BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 1100 --portfolio
```

Pass 1 (81 kernvarianten) en pass 2 (81 exit-varianten op de winnaar), elk op train én
test, gesorteerd op risicogecorrigeerd testrendement.

### Vergelijking met 18 juli 2026

| Parameter | Winnaar 18 juli | Winnaar v0.20.0 | Verschuift? | Toelichting |
|-----------|-----------------|-----------------|-------------|-------------|
| EMA-paar | 20/50 | | | |
| reward\_risk\_ratio | 1,5 | | | |
| min\_signal\_score | 3 | | | |
| atr\_stop\_multiplier | 2,0 | | | |
| RSI-koopzone | 25-45 (was niet in de grid) | | | nieuw in pass 2 |
| min\_profit\_pct | 0,50 (was niet in de grid) | | | nieuw in pass 2 |
| time\_stop\_candles | 12 (was niet in de grid) | | | nieuw in pass 2 |
| breakeven trigger | 1,0x ATR (was niet in de grid) | | | nieuw in pass 2 |

### Besluit

Config **alleen** wijzigen als de nieuwe meting daar aanleiding toe geeft. Drie
voorwaarden voordat een parameter verandert:

1. de nieuwe waarde wint op de **test**-kolom, niet op train;
2. de variant haalt minstens 20 afgewikkelde trades (daaronder is max drawdown één
   ongelukkige reeks in plaats van een meting);
3. de gap tussen train en test is niet groter dan die van de huidige waarde.

Wijzig je iets in `strategy`, `decision`, `fees` of `universe`, houd er dan rekening mee
dat dat de **kern** van de meet-fingerprint is: alle vier de shadow-gates beginnen dan
opnieuw te tellen richting hun drempel van 20 afgewikkelde trades. Noteer die wijziging in
het register in PROJECTPLAN.md, net als een gate-flip.

| Datum | Wijziging | Reden | Cohorte-reset? |
|-------|-----------|-------|----------------|
| | | | |
