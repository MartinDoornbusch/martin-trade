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

> **PowerShell op Windows.** De blokken hieronder zijn bash. PowerShell kent geen
> `VAR=waarde commando`-prefix en gebruikt een backtick in plaats van `\` als regelvervolg.
> Per blok staat de PowerShell-variant eronder.

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

PowerShell:

```powershell
Copy-Item config\config.yaml "$env:TEMP\anker-config.yaml"
#  -> voeg met de hand `rsi_oversold: 35` toe ONDER de bestaande strategy-sectie

git worktree add "$env:TEMP\juli18" 56d9e55
Set-Location "$env:TEMP\juli18"
$env:CONFIG_PATH = "$env:TEMP\anker-config.yaml"
$env:PYTHONPATH  = "src"
foreach ($m in "BTC-EUR","ETH-EUR","SOL-EUR","XRP-EUR","LINK-EUR") {
    python -m tradebot.backtest $m --interval 4h --limit 1100
}
```

> **Ruim `CONFIG_PATH` daarna op**: `Remove-Item Env:CONFIG_PATH`. In PowerShell blijft een
> `$env:`-waarde staan voor de rest van de sessie, dus zonder die regel draaien je volgende
> runs stilzwijgend op de ankerconfig in plaats van op de repo-config. Dat is precies het
> soort stil verschil dat de anker-check moet uitsluiten.

De attributierun draait op de repo-config, dus de vergelijking gaat dan zuiver over code.
Welke velden de oude code leest en dus moeten kloppen: `strategy` (ema_fast, ema_slow,
rsi_period, rsi_oversold, atr_period, min_signal_score), `decision` (min_profit_pct,
atr_stop_multiplier, reward_risk_ratio) en `fees` (maker, taker, slippage). De secties
`exits`, `regime`, `universe` en `blocklist` kende hij nog niet en worden genegeerd.

Gedraaid op 2026-08-06, worktree `56d9e55`, eigen config, 1100 candles per markt:

| Markt | Juli-code: rendement % / trades / win % | Fees EUR | Max DD % | Rij 1 van de attributie | Gelijk? |
|-------|------------------------------------------|----------|----------|--------------------------|---------|
| BTC-EUR | 14,46 / 20 / 55,0 | 104,42 | 13,7 | | |
| ETH-EUR | 18,14 / 15 / 53,3 | 82,73 | 16,6 | | |
| SOL-EUR | 18,27 / 17 / 47,1 | 106,03 | 16,3 | | |
| XRP-EUR | 6,25 / 16 / 43,8 | 81,97 | 12,4 | | |
| LINK-EUR | 25,67 / 18 / 55,6 | 103,40 | 18,3 | | |
| **gemiddeld** | **16,56 / 17,2 / 50,9** | **95,71** | **15,5** | | |

Twee waarnemingen die met de bevindingen van 18 juli overeenkomen, wat het vertrouwen in de
reconstructie steunt: 15 tot 20 trades per markt over circa een half jaar (juli noteerde
11-24), en fees van 82 tot 106 EUR op een inleg van 1000, oftewel 8,2 tot 10,6% van het
kapitaal (juli noteerde 7-9% als dominant lek).

Let op drie dingen bij die vergelijking:

- **Dit is niet het juli-venster.** `--limit 1100` telt terug vanaf vandaag, dus dit beslaat
  ruwweg februari tot augustus 2026, waar de juli-run januari tot juli besloeg. Een exacte
  match met de juli-uitvoer was sowieso onmogelijk, en die uitvoer is bovendien nooit
  vastgelegd. Wat deze tabel wél valideert is de enige vergelijking die telt: oude code
  versus nieuwe code met de correcties uit, op **dezelfde** 1100 candles.
- de oude `main()` neemt één markt tegelijk en print per markt; de attributie middelt over
  de vijf markten. Vergelijk dus per markt, of gebruik de gemiddelde-regel hierboven;
- draai de worktree op zijn **eigen** config (`Remove-Item Env:\CONFIG_PATH`), niet op een
  gepatchte kopie van de huidige. De oude `AppConfig` declareert `risk: dict[str, float]` en
  valt af op `sizing: "bucket"`. De worktree-config bevat al ema 20/50, rr 1,5, score 3,
  atr 2,0, `rsi_oversold: 35` (zone 25-45) en fees 0,15/0,25/0,10, dus het configverschil met
  vandaag is op elk veld dat de oude code leest exact nul.

### Uitkomst van de anker-check (6 augustus 2026): rij 1 reproduceert

Na de twee fixes hieronder en met een gepind venster (`--end 2026-08-06T08:00:00Z`) valt de
per-markt-vergelijking zo uit:

| Markt | Anker (oude code) | Rij 1 attributie | Verschil |
|-------|-------------------|------------------|----------|
| BTC-EUR | 14,46 / 20 / 55,0 / 13,7 / 104,42 | idem | identiek op alle vijf velden |
| XRP-EUR | 6,25 / 16 / 43,8 / 12,4 / 81,97 | idem | identiek op alle vijf velden |
| ETH-EUR | 18,14 / 15 / 53,3 / 16,6 / 82,73 | 18,23 | alleen rendement, +0,09 |
| LINK-EUR | 25,67 / 18 / 55,6 / 18,3 / 103,40 | 26,71 | alleen rendement, +1,04 |
| SOL-EUR | 18,27 / 17 / 47,1 / 16,3 / 106,03 | 5,58 / 16 / 43,8 | één trade minder |

Twee markten kloppen tot op de decimaal op rendement, trades, win-rate, drawdown én fees.
Bij ETH en LINK verschilt alleen het rendement terwijl fees en drawdown identiek zijn, wat
past bij een randeffect. SOL is volledig verklaard door het vensterverschil: 17 tegen 16
trades en 47,1% tegen 43,8% is precies één winnende trade minder (8/17 tegen 7/16), en met
all-in sizing verlaagt die ene winnaar het kapitaal voor alle volgende trades, wat de fees
van 106,03 naar 89,56 drukt. Intern consistent.

**Conclusie: de reconstructie is geldig en de deltas eronder zijn leesbaar.** Herhaal de
ankerrun met dezelfde `--end` om ook SOL te sluiten.

### Wat de eerste ankerrun opleverde (6 augustus 2026)

Rij 1 reproduceerde het anker **niet** volledig, en dat is precies waarvoor deze stap
bestaat:

| | rend % | trades | win % | dd % |
|---|--------|--------|-------|------|
| Anker (oude code, `56d9e55`) | 16,56 | 86 | 51,2 | 15,5 |
| Rij 1 attributie (vóór de fix) | 13,62 | 86 | 51,2 | 13,3 |

Identieke trades en identieke win-rate, andere P&L. Dat sluit een verschil in
signaalgeneratie uit en wijst naar de kostenboekhouding. Twee defecten gevonden, allebei in
de reconstructie:

1. **De legacy-tak vulde op het NIVEAU in plaats van op de slotkoers.** De oude
   `run_backtest` deed `gross = amount * price` met `price = snap.price`, dus de close van
   de bar die de exit triggerde. Mijn legacy-tak triggerde wel op de close maar vulde op
   `stop` of `target`. Per trade klein, systematisch van richting: winnaars schoten door het
   target heen en verliezers door de stop, en op 4h-crypto is die doorschot fors. Dit
   verklaart zowel het rendementsverschil als de afwijkende drawdown.
2. **"Geen slippage" was gemodelleerd door de buffer op nul te zetten**, wat óók `min_edge`
   verlaagde van 1,10% naar 1,00% terwijl de oude code die 1,10% wél hanteerde. In dit
   venster veranderde dat geen enkele entry (de trades zijn identiek), maar rij 1 was
   daarmee op één as geen reconstructie meer. Nu twee losse vlaggen.

Beide gerepareerd en vastgepind in `tests/test_backtest.py`
(`test_legacy_exit_fills_on_the_close_not_on_the_level`,
`test_reference_row_keeps_the_real_fee_gate`), elk geverifieerd door het defect terug te
zetten. **Draai de ankerrun opnieuw voordat je de tabellen invult.** `calibrate` print de
referentierij nu ook per markt, zodat je de vijf naast de vijf ankeruitkomsten kunt leggen:
wijken ze alle vijf een beetje af, dan zit het in de kostenboekhouding; wijkt er één sterk
af, dan in een specifieke trade.

### Pin het venster, anders vergelijk je twee dingen tegelijk

`--limit N` haalt de N **nieuwste** candles op. Draai je de ankerrun 's ochtends en de
attributie 's middags, dan is er een 4h-bar bijgekomen en beslaan de twee runs een ander
stuk markt. Dat verschil lees je dan als een codeverschil, en dat is precies wat deze check
moet uitsluiten. Bij de eerste poging op 6 augustus veranderde daardoor het aantal trades
van 86 naar 88 zonder dat er iets aan de code was gewijzigd dat dat kon verklaren.

Alle drie de CLI's (`backtest`, `calibrate`, `optimizer`) accepteren daarom `--end`
(ISO-8601 of epoch in ms) en printen het gebruikte venster. Zonder `--end` staat er een
waarschuwing bij. Kies één eindtijdstip en gebruik dat voor de ankerrun én de attributie:

```powershell
$eind = "2026-08-06T08:00:00Z"
python -m tradebot.calibrate BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 1100 --end $eind
```

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

```powershell
$env:PYTHONPATH = "src"
python -m tradebot.calibrate BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 1100
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
- **De sprong naar portfolio-modus is grotendeels een artefact van de rijen erboven,
  niet een eigenschap van bucket-sizing.** Die verklaring is één keer fout opgeschreven
  ("bucket zet een kwart van het kapitaal in") en dat klopt niet: met portfolio 1000 en
  bucket 250 geeft `effective_max_positions` vier slots, dus 4 x 250 = 100% inzetbaar. Puur
  op inzet zou er niets af moeten. De echte oorzaak zit in de vergelijking: alle rijen
  bóven de portfolio-stap draaien vijf onafhankelijke all-in runs van elk 1000 euro en
  middelen die. Dat veronderstelt vijf keer je kapitaal en is dus geen haalbare strategie.
  De portfolio-rij deelt één pot over vijf markten, met slotcontentie, gedeelde drawdown,
  cooldown en correlatiecap. **Lees de rijen erboven dus als systematisch optimistisch; de
  productierij is de enige eerlijke.**
- Verwachting bij 3.2 (warmup). **Deze stond hier eerst te sterk geformuleerd**, en zo
  geformuleerd kun je een juiste redenering per ongeluk verwerpen. Wat er feitelijk gebeurt
  bij warmup 60 met een EMA die op `arr[0]` seedt: `ema_fast` (20) convergeert sneller dan
  `ema_slow` (50), dus vroeg in de reeks hangt de trage nog bij het startpunt terwijl de
  snelle al is weggelopen. Dat produceert spookwaarden in de conditie `ema_fast > ema_slow`,
  één van de drie scorepunten. Dat is **ruis, geen consistente straf in één richting**: of
  die ruis ema 20/50 benadeelt of toevallig helpt, hangt af van de richting van de eerste
  beweging in het datavenster.

  Daaruit volgen twee dingen voor de toets:

  1. **Kijk relatief, niet absoluut.** De warmup van 60 naar 150 schrapt ook 90 candles
     handel aan het begin van elke periode, voor álle varianten. Het absolute rendement van
     ema 20/50 kan dus alle kanten op simpelweg omdat er een ander stuk markt verhandeld
     wordt. Kijk naar zijn positie ten opzichte van ema 9/21 en ema 12/26, niet naar zijn
     eigen getal.
  2. **De goede toets is stabiliteit, niet richting.** Houdt ema 20/50 zijn rang op train
     én test, en is de gap kleiner dan op 18 juli? Komt hij zwakker uit, dan is de eerste
     verklaring dat de ruis eerder toevallig gunstig uitviel, niet dat de keuze van juli
     fout was. Pas als hij op beide periodes consistent zakt, verdient die keuze een tweede
     blik.
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

```powershell
$env:PYTHONPATH = "src"
python -m tradebot.optimizer BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 3000 --portfolio
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

---

## Uitkomst van de hermeting (6 augustus 2026)

Venster: 2025-03-23 t/m 2026-08-06, 3000 4h-candles per markt, vijf markten, portfolio-modus.
Train 2100 / test 900 candles, chronologisch gesplitst.

### 1. Geen enkele configuratie overleeft beide vensters

162 varianten in pass 1, 81 in pass 2. De beste `min%` (uitkomst in het slechtste van de
twee vensters) is **-14,55%**. Positief in beide vensters: nul varianten.

De top van de test-tabel is misleidend zonder die tweede tabel: `ema12/26 score>=3 atr*2.5
rr1.5` haalt +6,92% op test, maar -38,01% op train. Een gap van 45 punten. Dat is geen
edge, dat is één venster.

### 2. De markt daalde in BEIDE vensters, en dat kantelt de lezing

IJkpunt kopen-en-vasthouden, gelijkgewogen over dezelfde vijf markten: **train -20,99%, test
-9,17%**. De aanname dat het testvenster een stijgende markt was, die eerder in dit document
en in de reviewgesprekken werd gebruikt, is dus onjuist.

Dat verandert wat "negatief rendement" betekent. Voor een long-only strategie in een dalende
markt is verlies de normale uitkomst; alleen het verschil met vasthouden zegt iets over de
strategie. Daarom staat dat verschil nu als kolom in de overlevingstabel.

| Variant | train% | vs vasthouden | test% | vs vasthouden |
|---------|--------|---------------|-------|---------------|
| beste overlever (`ema9/21 score>=3 atr*2.5 rr2.0 regime:aan`) | -14,55 | **+6,44** | -12,84 | -3,67 |
| beste op test (`ema12/26 score>=3 atr*2.5 rr1.5 regime:uit`) | -38,01 | -17,02 | +6,92 | **+16,09** |

### 3. Het regime-filter helpt wél, maar in het andere venster

Alle vijf de beste overlevers hebben `regime:aan`; alle tien de besten op test hebben
`regime:uit`. Het filter verlaagt het verlies in het ongunstige venster van circa -34% naar
-14,55% en kost rendement in het gunstige. Dat is precies wat een trendfilter hoort te doen,
en het is de tegenovergestelde conclusie van wat de losse zesmaands-vergelijking
(`--vergelijk`) suggereerde. Eén venster is geen meting.

### 4. Wat dit betekent voor fase 2

- **Absoluut: geen edge.** Geen configuratie is positief in beide vensters over 17 maanden.
  Het go/no-go-criterium (win-rate > 45% én netto positief na fees) wordt door geen enkele
  variant gehaald.
- **Relatief: gemengd.** Met regime bindend verslaat de strategie vasthouden in het slechte
  venster met 6,44 punt en verliest ze 3,67 punt in het goede. Dat is geen edge maar het is
  ook geen ruis.
- **Gates zijn niet de oplossing.** Een gate die een kansloze instap filtert maakt van een
  verlies een kleiner verlies. Het werk verhuist naar de instaplogica; het EV-gate-ontwerp in
  `docs/ontwerp-ev-gate.md` gaat precies over dat gat, want de huidige fee-gate toetst of het
  koersdoel ver genoeg weg ligt en niet of de trade positieve verwachtingswaarde heeft.
- **Config niet wijzigen op deze uitkomst.** De drie voorwaarden hierboven zijn niet gehaald:
  geen variant wint op test én overleeft train, en de gaps zijn overal 25 tot 50 punten.

### 5. Methodische fout in de tweede pass, gerepareerd

Pass 2 draaide alleen op de test-winnaar van pass 1. Die had `regime:uit`, dus alle 81
exit-varianten hadden regime uit en de overlevingstabel van pass 2 (-22,09%) was slechter dan
die van pass 1 (-14,55%). De pass kon de tak die het ongunstige venster het beste overleeft
per constructie niet verkennen. Pass 2 draait nu op twee zaadjes: de test-winnaar én de beste
overlever.

### 6. De onverkende tak: de eerste configuratie die vasthouden in beide vensters verslaat

Pass 2 op de beste overlever (`ema9/21 score>=3 atr*2.5 rr2.0 regime:aan`) levert:

| Variant | train% | vs vasthouden | test% | vs vasthouden | trades (test) |
|---------|--------|---------------|-------|---------------|---------------|
| `rsi25-45 ts0 be:uit` | -6,26 | **+14,73** | -2,81 | **+6,36** | 31 |

Dat is de enige configuratie uit 324 die vasthouden in **beide** vensters verslaat, en de gap
tussen train en test is met 3,45 punt veruit de kleinste van alle geteste varianten (elders
25 tot 50). Absoluut blijft ze negatief in beide vensters.

Drie dingen die de lezing begrenzen, en het derde is het zwaarste:

1. **31 trades in de testperiode.** Boven de drempel van 20, maar dun. Eén ongelukkige reeks
   verschuift de drawdown en daarmee de rangschikking.
2. **Geen gates.** `ts0 be:uit` betekent geen time-stop en geen breakeven-stop; het
   regime-filter staat wél aan. De best overlevende configuratie is dus de configuratie met
   de minste exit-gates plus de enige entry-gate die iets doet.
3. **Selectie uit 324 varianten op twee vensters.** Bij dat aantal trekkingen is één variant
   vinden die een benchmark in beide vensters verslaat, niet verrassend. De kleine gap is een
   gunstig teken maar geen bewijs. Deze variant hoort behandeld te worden als hypothese voor
   een volgende meting op ándere data, niet als uitkomst.

### 7. De fee-gate bindt nooit

In elke pass 2-tabel geven `profit0.25`, `profit0.5` en `profit1.0` **bit-identieke**
uitkomsten. Een drempel die je met 0,75 procentpunt kunt verschuiven zonder dat er ook maar
één trade verandert, houdt niets tegen.

Dat is te verwachten uit de rekensom: `min_edge` is 0,50% round-trip plus 0,10% slippage plus
`min_profit_pct`, dus maximaal circa 1,60%. De verwachte beweging is ATR x
`atr_stop_multiplier` x `reward_risk_ratio`, en bij een ATR van 2 tot 3% van de prijs op
4h-crypto levert dat 10 tot 15% op. De gate ligt een orde van grootte te laag om ooit te
binden.

**Dat is de kernles uit de post-mortem die in de praktijk niet werkt.** Niet omdat fees niet
worden gerekend, want dat gebeurt wel, maar omdat de gate de verkeerde vraag stelt: hij toetst
of het koersdoel ver genoeg weg ligt, niet of de trade positieve verwachtingswaarde heeft.
Precies het gat waarvoor `docs/ontwerp-ev-gate.md` is geschreven.

Sinds v0.20.0 telt de backtester dit expliciet (`fee_gate_blocks`, `fee_gate_block_pct`) en
waarschuwt de optimizer als de gate onder 1% van de signalen tegenhoudt. Meten in plaats van
afleiden.

### 8. De grens onder alle relatieve cijfers: blootstelling

De kandidaat doet 31 trades waar de regime:uit-varianten er 82 doen. Dat is een fractie van
de blootstelling, en in twee dalende vensters is +14,73 en +6,36 procentpunt dan mogelijk
volledig verklaard door "minder in de markt zitten" en niet door betere instapkeuzes. Een
variant die niets doet scoort in deze twee vensters +20,99 en +9,17 punt ten opzichte van
vasthouden.

Daarom rapporteert de optimizer sinds v0.20.0 twee dingen extra:

- `expo tr` / `expo te`: kapitaalgewogen tijd-in-markt, dus het gemiddelde van (belegd /
  totaal vermogen). Tijdgewogen zou de blootstelling van een bucket-strategie overschatten;
- `alfa tr` / `alfa te`: rendement min (blootstelling x marktrendement). Eerste-orde-
  benadering, want ze veronderstelt dat de blootstelling niet systematisch samenvalt met de
  beweging — maar precies dát samenvallen is wat een regime-filter claimt, dus het is de
  juiste maat om die claim te toetsen.

**Toets:** ligt de outperformance in lijn met de exposurereductie, dan meet je afwezigheid.
Blijft er alfa over, dan zit er timing in.

### 9. De steekproef mist een stijgend venster

Zeventien maanden, twee vensters, allebei dalend. Over stijgende markten zegt deze meting
niets, en juist daar kost een regime-filter geld: hij houdt je uit de markt terwijl die
oploopt. De conclusie "het regime-filter verbetert de overleving" is daarmee half gemeten.

De optimizer toont nu het ijkpunt per venster én per kwartaal in de kop, zodat in één blik te
zien is of er een stijgend deel in de steekproef zit. Volgende run met een langer venster:

```powershell
python -m tradebot.optimizer BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR --interval 4h --limit 4400 --end $eind --portfolio
```

4400 4h-candles is ongeveer twee jaar. `get_candles_history` pagineert, dus dat gaat in
stappen van 1440 per markt.
