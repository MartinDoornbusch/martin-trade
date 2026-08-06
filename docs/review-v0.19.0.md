# Review ronde 2: AI Trade Platform (v0.19.0)

Datum: 2026-08-05. Basis: commit `ead5554`, tien commits na de eerste review (`1aa4adf`). Twee delen: verificatie van de doorgevoerde fixes, en de modules die de eerste ronde niet raakte (backtest, optimizer, veto-analyse).

## Samenvatting

Alle tien punten uit ronde 1 zijn correct geland; de fixes zijn geverifieerd in de code, niet alleen in de commit messages. Testdekking op `engine.py` ging van 23% naar 81%, `strategy.py` naar 100%, 162 tests groen, ruff en bandit schoon.

De belangrijkste bevinding van deze ronde zit elders en is zwaarder dan wat ronde 1 opleverde: de bot genereert koopsignalen op een candle die nog niet gesloten is, terwijl de backtester uitsluitend op gesloten candles rekent. Empirisch bevestigd tegen de Bitvavo-API. Daarbovenop modelleert de backtester sinds v0.18.0 een strategie die niet meer bestaat. Samen betekent dat: de fase 2-kalibratie (ema 20/50, rr 1,5, drempel 3) rust op een model dat op vier punten afwijkt van de draaiende bot.

---

## Deel 1 — verificatie van de v0.19.0-fixes

| punt | status | bewijs |
|---|---|---|
| 1.1 stale positielijst | opgelost | `_refresh_after_trade()` herleest posities, cash en dagwinst na elke order; `portfolio` bewust cyclus-vast met onderbouwing in de code |
| 1.2 trend-break | geschrapt, breakeven-stop erin | `check_exit` is nu puur SL/TP; `breakeven_stop_hit` bewapent op piek >= 1x ATR en vuurt op entry +0,55% |
| 1.3 rsi_overbought | opgelost | `rsi_buy_zone_min/max` expliciet in config, `rsi_oversold + 10` weg, `rsi_overbought` blijft voor de adviestabel |
| 1.4 mode-scheiding | opgelost | mode-filter op `paper.last_trade_at`, `paper.daily_pnl_eur`, `main.publish_mqtt`, `web` regels 89/252/281; `broker.mode` als klasse-attribuut |
| db.py unique | terecht ongemoeid | geen migratierisico genomen |
| 2.1 integratietests | opgelost | `test_engine_cycle.py`, 11 tests op de echte `run_once` |
| 2.2 GuardHarness | opgelost | vervangen door 7 tests tegen de echte `check_exits_fast` |
| 2.3 regressiedekking | opgelost | tests benoemen expliciet welke bug ze afdekken |
| 3.1 / 3.2 add-on-schema | opgelost | `max_open_positions: int(1,10)`, plus `sizing`, `bucket_eur`, `auto_fill`, `regime_*`, `llm_veto_binding`, `blocklist` ontsloten |
| 3.3 versiedrift | opgelost | `test_addon_config.py` bewaakt versie, defaults, schema-grenzen en de scheiding operationeel/strategie |

CI-resultaat: 162 passed, 1 skipped. Coverage totaal 66% naar 72%.

`test_addon_config.py` is de sterkste toevoeging: het bewaakt niet alleen de versie maar ook dat elke add-on-default gelijk is aan `config/config.yaml`, dat elke optie een schema-entry en een env-var heeft, en dat strategieparameters buiten de add-on blijven. Dat sluit de hele klasse van drift af die ronde 1 aan het licht bracht.

Twee restpunten in het nieuwe werk staan in deel 3.

---

## Deel 2 — nieuwe bevindingen

### 2.1 Signalen worden berekend op een candle die nog loopt

Geverifieerd tegen de live API op 2026-08-05 17:35 UTC:

```
GET /v2/BTC-EUR/candles?interval=4h&limit=2
nieuwste candle open : 2026-08-05 16:00 UTC
sluit pas om         : 2026-08-05 20:00 UTC
```

Bitvavo levert de lopende candle mee als nieuwste element. `build_snapshot` gebruikt `closes[-1]`, `hist[-1]` en `r[-1]`, dus elke analysecyclus rekent op een bar die nog 2,5 uur te gaan heeft. De analyse draait elk uur, de candle duurt vier uur: dezelfde bar wordt drie tot vier keer beoordeeld terwijl hij nog beweegt.

Waarom dat hier meer kwaad doet dan gemiddeld:

- De zwaarst wegende koopreden is `macd_hist > 0 and macd_hist_prev <= 0`, goed voor 2 van de 3 benodigde punten. Precies dat soort flip-conditie verschijnt en verdwijnt binnen een bar.
- De bot koopt bij de eerste uurrun waarin de conditie waar is. Hij selecteert dus systematisch de meest voorbijgaande realisatie van het signaal, niet de bevestigde.
- Het effect is eenzijdig: het verhoogt de handelsfrequentie ten opzichte van de backtest. Meer trades bij hetzelfde signaal is precies de mechaniek die de vorige bot 15% kostte.
- De 12-uurs cooldown per markt dempt het, maar heft het niet op.

Fix is asymmetrisch en klein: entries beoordelen op afgesloten candles (`candles[:-1]` in de signaalroute), exits op de live prijs houden, want daar wil je de actualiteit juist wel. Neem dat mee als aparte config-schakelaar zodat je de oude en nieuwe variant naast elkaar kunt meten.

Zijeffect van dezelfde oorzaak: `time_stop_hit` telt candles met `ts > opened_ms` en telt de lopende bar mee, dus de time-stop vuurt ongeveer een candle te vroeg. De breakeven-stop telt de lopende bar juist terecht mee bij het bepalen van de piek.

### 2.2 De backtester modelleert een strategie die niet meer bestaat

`run_backtest` roept alleen `check_exit` aan. Sinds v0.18.0 heeft de engine een time-stop, sinds v0.19.0 een breakeven-stop. Geen van beide zit in de backtest. De backtestcijfers van 18 juli beschrijven dus een bot van vóór v0.18.0.

Vier afwijkingen tussen backtest en engine, op volgorde van impact:

1. **Exits.** Geen time-stop, geen breakeven-stop. De time-stop verandert de uitkomst materieel: hij begrenst de houdduur en geeft kapitaal terug.
2. **Exit op slotkoers in plaats van intrabar.** `check_exit` vergelijkt `snap.price`, de close, met stop en target. Een candle waarvan de low de stop doorboorde maar die erboven sloot, houdt in de backtest de positie open; live stopt de guard binnen de minuut uit. Bij 2x ATR-stops op 4h-crypto overheerst dat effect en tilt het de win-rate kunstmatig op. Opvallend: `analysis/veto.py` doet het in `_tp_sl` wel goed, intrabar met high en low en stop-eerst bij gelijke candle. De juiste logica staat dus al in de codebase, alleen niet in de backtester.
3. **Geen slippage in de fill.** `slippage_buffer_pct` zit in `min_edge` maar wordt nooit op de fillprijs toegepast. Bij 0,10% per been is dat 0,20% per round trip bovenop 0,50% fees.
4. **All-in sizing.** `spend = cash`, één markt, één positie. De bot doet buckets van 250 euro over maximaal 10 slots met correlatiecap en dagverliescap. Rendement en drawdown uit de backtest zijn daarom niet vergelijkbaar met live.

Consequentie voor je beslissingen: "drempel 3 is optimaal" en "score 2 is overtrading" zijn signaalniveau-conclusies en blijven waarschijnlijk overeind. "BTC eruit levert 9 punten op" is een portefeuilleconclusie die uit losse enkelvoudige runs is getrokken, en die zou ik niet doorvoeren zonder portfolio-simulatie. Dat staat ook al zo in je PROJECTPLAN.

### 2.3 De optimizer selecteert op train en toont test alleen voor de trainwinnaars

`main()` sorteert alle 81 varianten op `net_return_pct` van de trainingsperiode en draait daarna alleen voor de top vijf een testrun. De afsluitende regel zegt "kies op test-prestatie, niet op train", maar het instrument laat je testcijfers zien van varianten die al op train zijn voorgeselecteerd. Staat de echte testwinnaar zesde op train, dan zie je hem nooit.

Los het op door alle varianten op beide perioden te draaien en de tabel op test te sorteren, met de trainkolom ernaast als overfit-indicator. De kosten zijn 81 extra runs, wat op 3000 candles goed te doen is.

Twee kleinere punten in hetzelfde bestand:

- De grid dekt ema, score, ATR-multiplier en RR. Niet: RSI-zonegrenzen, `min_profit_pct`, `time_stop_candles`, breakeven-parameters. De nieuwe exitparameters zijn dus niet te optimaliseren.
- Ranken gebeurt op rendement, niet risicogecorrigeerd. Drawdown wordt geprint maar telt niet mee. Met all-in compounding krijgt een variant met veel trades een hefboom die live niet bestaat.

### 2.4 `warmup = 60` is te kort voor de gekozen EMA

`run_backtest` hanteert 60 candles warmup. `indicators.ema` seedt op `arr[0]` en convergeert pas na ruwweg twee tot drie keer de periode. Voor ema_slow 50 is dat 100 tot 150 candles. De grid vergelijkt ema 9/21, 12/26 en 20/50 bij dezelfde warmup, dus de traagste variant wordt met de minst geconvergeerde indicator beoordeeld. De bias werkt tegen ema 20/50, de variant die je uiteindelijk hebt gekozen, dus je keuze staat er waarschijnlijk sterker voor dan de cijfers lieten zien. De vergelijking zelf is niet eerlijk. Zet warmup op `3 x max(ema_slow, 26) + macd_signal`, of seed de EMA met een SMA over de eerste periode.

### 2.5 De breakeven-stop logt shadow-events die niemand meet

`_extra_exits` schrijft bij een niet-bindende treffer een `SignalRow` met `details["shadow_breakeven"]`. Er is geen analysemodule voor: `analysis/__init__.py` exporteert alleen `analyze_regime` en `analyze_vetos`, er is geen dashboardkaart en geen endpoint. Je eigen go/no-go-regel, bindend pas bij positieve netto gate over minstens 20 afgewikkelde trades, is voor deze gate dus niet uit te rekenen.

Daar komt bij dat de treffer elke cyclus opnieuw wordt gelogd zolang de koers onder het niveau blijft. Een positie die vier cycli onder de drempel hangt levert vier events op. Een analysemodule moet dus dedupliceren op de eerste treffer per positie, anders telt hij hetzelfde signaal meerdere keren.

De meting zelf is trouwens eenvoudiger dan bij de andere twee gates: je kent de prijs op signaalmoment en de werkelijke exitprijs, dus de netto gate is (hypothetische exit) min (werkelijke exit), zonder counterfactual-reconstructie.

### 2.6 Kleinere punten

| punt | plaats | impact |
|---|---|---|
| `offset_pct: 0.55` staat los van het fee-model | `config.yaml` | bij een andere Bitvavo-tier klopt de breakeven-drempel niet meer; afleiden van `fee_model.round_trip_pct()` plus marge |
| counterfactual-entry gebruikt de definitieve close van de lopende candle | `veto.py._entry_index` | milde look-ahead in de counterfactual-kolommen; de echte shadow-uitkomst heeft dit niet, en die weegt bij jou al zwaarder |
| `_load_roundtrips_from_db` filtert hardcoded op `mode == "paper"` | `veto.py:437` | in live mode rapporteert de veto- en regime-analyse stil op paper-historie; gemist in de mode-pass van 1.4 |
| `SignalRow` heeft geen mode-kolom | `db.py` | shadow-metingen zijn niet per mode te scheiden |
| `scan()` en `web.py` blijven op 52% en 31% dekking | | `scan()` zit nu in het koudepad van auto-fill, dus elke cyclus in productie |

---

## Deel 3 — volgorde

1. Entries op afgesloten candles (2.1). Enige bevinding die vandaag, in paper, je trades verandert, en die precies raakt aan je historische faalwijze.
2. Backtester gelijktrekken met de engine (2.2): time-stop, breakeven-stop, intrabar exits met high en low, slippage op de fill. Hergebruik `_tp_sl` uit `veto.py`, die logica klopt al.
3. Optimizer op test laten sorteren en warmup schalen met ema_slow (2.3, 2.4). Draai daarna de kalibratie opnieuw; pas dan weet je of ema 20/50 en rr 1,5 nog de winnaars zijn.
4. Analysemodule voor de breakeven-stop met deduplicatie (2.5), anders staat er een gate te loggen die je nooit bindend kunt maken.
5. Mode-filter op `veto.py:437` en de kleine punten uit 2.6.

De vier live-blockers uit ronde 1 (niet-atomaire sell, ontbrekende exchange-side stop-loss, ontbrekende lock tussen guard en analysecyclus, ontbrekende reconciliatie bij opstart) staan nog open en waren bewust buiten scope van v0.19.0. Die blijven de poort naar fase 3.

## Wat er goed staat

De v0.19.0-ronde is netjes uitgevoerd: elk reviewpunt een eigen commit, elke fix een regressietest die de bug benoemt, achterhaalde claims in PROJECTPLAN en docstrings gecorrigeerd in plaats van laten staan, en `test_addon_config.py` als structurele borging tegen een hele klasse fouten. De breakeven-stop is bovendien ingebouwd volgens je eigen shadow-regel in plaats van meteen bindend. Dat is de juiste reflex; hij mist alleen nog de meter.
