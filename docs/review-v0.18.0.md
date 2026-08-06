# Code- en projectreview: AI Trade Platform (v0.18.0)

Datum: 2026-08-05. Basis: `C:\Users\A109296\Trade app`, commit `1aa4adf`, werkboom schoon op drie `.disabled`-bestanden na. Alle bevindingen zijn geverifieerd tegen de code, de testrun (126 passed, 1 skipped), ruff (schoon), bandit (schoon) en een coveragerun.

## Samenvatting

De architectuur klopt en de discipline is zichtbaar: pure functies voor elke gate, fee-model overal doorgevoerd, exits mechanisch, LLM strikt in een adviesrol, PROJECTPLAN dat de werkelijkheid vrij nauwkeurig volgt. Het probleem zit niet in het ontwerp maar op drie plekken: de orchestrator (`engine.run_once`) is de enige module die de gates aan elkaar knoopt en is als enige niet getest, waardoor twee limieten binnen één cyclus omzeilbaar zijn; de live-broker heeft een bewezen niet-atomaire verkooppad en steunt volledig op een in-process poller voor stop-loss; en de fee-gate beschermt tegen een te klein koersdoel, niet tegen een te lage trefkans, wat de eigenlijke oorzaak van het verlies van 15% was.

Voor paper trading is het systeem bruikbaar. Voor live is het dat niet zonder punten 1 tot 4.

---

## 1. Blokkerend voor fase 3 (live)

### 1.1 Verkoop in `LiveBroker.sell` is niet atomair
`live.py:130-149`. Volgorde: positie lezen (sessie sluit), marktorder plaatsen, dan nieuwe sessie en `scalar_one()` op dezelfde rij. Sluit de guard-thread die positie tussendoor, dan raist `scalar_one()` nadat de echte order al is uitgevoerd. Resultaat: coins verkocht, geen `TradeRow`, P&L en fees ontbreken in de administratie. In paper eindigt hetzelfde scenario in een `ValueError` die de per-markt `try/except` opvangt, dus je ziet het probleem nu niet.

### 1.2 Geen stop-loss bij de exchange
SL en TP bestaan alleen als kolommen in SQLite en worden gehandhaafd door `check_exits_fast`, een poller die elke 60 seconden draait binnen het add-on-proces. Valt de add-on, HAOS, de Pi of het netwerk weg, dan staat er geen enkele bescherming op de markt. Voor een positie van 250 euro in een 4h-swing is dat een reëel gat. Overweeg een OCO- of stop-limit-order bij Bitvavo als primaire bescherming, met de poller als vangnet in plaats van andersom.

### 1.3 Geen lock tussen guard en analysecyclus
`main.py:67-73`. Twee APScheduler-jobs, elk `max_instances=1`, maar onderling niets. Ze delen dezelfde `TradingCycle` en dezelfde broker en draaien in dezelfde threadpool. `max_instances=1` voorkomt overlap van een job met zichzelf, niet met de andere. Dubbele verkoop of verkoop tijdens een aankoopcyclus is mogelijk. Eén `threading.Lock` om broker-mutaties lost dit op.

### 1.4 Geen reconciliatie bij opstart
De DB is de enige waarheid over open posities. Crasht het proces tussen een gevulde order en de DB-write (`live.py:116-127`), dan bestaat de positie wel op Bitvavo maar niet in de bot: geen SL, geen TP, geen exit. Een startup-check die `get_balances()` vergelijkt met `PositionRow` hoort erbij vóór live.

---

## 2. Logische fouten in de beslislogica

### 2.1 Positielimiet en cluster-cap zijn binnen één cyclus omzeilbaar (bewezen)
`engine.py:61` leest `positions` één keer, vóór de marktloop. Na een aankoop op regel 166 wordt alleen `free` bijgewerkt, `positions` niet. Gevolgen:

- **Slotlimiet.** Gereproduceerd met de echte `RiskManager` en `DecisionEngine`:

  | scenario | eff_max | daadwerkelijk geopend |
  |---|---|---|
  | bucket, portfolio 900, bucket 250 | 3 | 4 (250, 250, 250, **150**) |
  | percent, portfolio 1000, 25%, max_open 3 | 3 | 4 (4x 250) |

  De vierde positie in bucket-modus krijgt bovendien 150 euro in plaats van 250, wat de invariant "vast bedrag per positie" breekt.

- **Correlatie-cluster.** `others` op `engine.py:138` wordt gebouwd uit dezelfde stale lijst. Kandidaten die in dezelfde cyclus zijn gekocht tellen niet mee, dus je kunt in één cyclus drie of meer sterk gecorreleerde posities openen terwijl `max_correlated_positions = 2`. Dat is precies het risico dat deze gate moest afdekken, en precies het alt-cluster-gedrag uit de post-mortem.

Het risico is toegenomen door A1: met auto-fill en 10 markten in de analyse-set is "meerdere buys in één cyclus" nu het normale geval in plaats van de uitzondering.

Fix is klein: `positions.append(...)` na een geslaagde buy, en de guard-clausule vóór de buy opnieuw evalueren.

### 2.2 De trend-break-exit is de facto dode code
`strategy.py:95`: `if snap.ema_fast < snap.ema_slow and snap.rsi > 70`. Neergaande EMA-kruising én overbought RSI komen zelden of nooit samen voor. Coverage bevestigt het: regel 96 is nooit uitgevoerd in de hele testsuite. Feitelijk zijn de exits dus SL, TP en de nieuwe time-stop. De omschrijving "stop/target/trend-break" in code en PROJECTPLAN klopt niet met het gedrag.

### 2.3 `rsi_overbought` is een knop die niets doet
Staat in `config.yaml`, wordt alleen gelezen in `web.py:131` voor de adviestabel. `strategy.check_exit` hardcodeert 70. Een configuratieparameter die de strategie niet raakt is een uitnodiging voor een verkeerde conclusie bij tuning.

### 2.4 De fee-gate is geen verwachtingswaarde-gate
`decision.py:134-139`. `expected_move_pct = 3 x ATR / prijs` (2x ATR stop, RR 1,5), vereiste `min_edge = 2 x 0,25 + 0,10 + 0,50 = 1,10%`. De gate bindt dus pas als ATR onder circa 0,37% van de prijs zakt. Op 4h-crypto is dat vrijwel nooit. De "kernbescherming" uit de docstring is in de praktijk bijna altijd open.

Belangrijker: hij toetst of het koersdoel ver genoeg weg ligt, niet of de trade positieve verwachtingswaarde heeft. Bij SL 2x ATR en TP 3x ATR is het break-even-trefpercentage 40%, plus fees. De oude bot zat op 27%. Nergens in de code wordt de gerealiseerde trefkans teruggekoppeld naar deze drempel. Dat is het gat tussen "we hebben een fee-gate" en "we lopen niet leeg op fees".

Concreet voorstel: een rolling EV-gate over de laatste N afgewikkelde trades per markt of per regime, die koopt zolang `p_gerealiseerd x TP - (1-p) x SL - fees > 0` en anders de markt tijdelijk uitzet. Dat is dezelfde meetmachinerie die `analysis/veto.py` en `analysis/regime.py` al hebben.

### 2.5 Scanner en engine gebruiken verschillende fee-drempels
`scanner.py:91` rekent met de werkelijke spread per markt, `decision.py:51` met een vaste `slippage_buffer_pct` van 0,10. Auto-fill-kandidaten passeren dus een andere lat dan waar de engine ze daarna op afrekent. Voor markten met een spread boven 0,10% is de scanner strenger, daaronder soepeler. Eén bron voor de vereiste edge is beter.

### 2.6 Er staat op dit moment geen enkele kwalitatieve gate aan
`llm_veto_binding: false` en `regime.binding: false`. Bewuste keuze, maar het effect is dat de LLM elke cyclus per kandidaat wordt aangeroepen, budget en latency kost, en het resultaat wordt weggegooid. Als de meting op het regime-filter binnenkort de winnaar aanwijst, zet dan tegelijk `use_llm_second_opinion` uit in plaats van de laag alleen niet-bindend te laten.

---

## 3. Volledigheid en testdekking

### 3.1 De testsuite dekt de gates, niet de bedrading
126 tests groen, ruff en bandit schoon. Coverage per module:

| module | coverage | opmerking |
|---|---|---|
| `decision.py` | 99% | gates los uitstekend gedekt |
| `strategy.py` | 96% | behalve de trend-break-regel |
| `engine.py` | **23%** | `run_once` (58-179) en `check_exits_fast` (243-260) volledig ongedekt |
| `web.py` | 31% | |
| `scanner.py` | 52% | `scan()` zelf ongedekt |
| `main.py` | 0% | scheduler-opzet |

Slechts één testbestand importeert `engine` (`test_interlock.py`), en dat test alleen de constructor. Erger: `test_guard.py` bevat een `GuardHarness` met het commentaar "minimale nabootsing van `TradingCycle.check_exits_fast`". Er wordt dus een kopie van de logica getest, niet de logica zelf. Dat is schijnzekerheid precies op de plek waar de bugs uit 1.3 en 2.1 zitten.

Prioriteit: één integratietest die `run_once` draait met een fake feed en fake broker, en assert op het aantal geopende posities en op de cluster-cap. Die test zou 2.1 vandaag rood maken.

### 3.2 Add-on-schema overschrijft de config die je hebt afgestemd
`tradebot-addon/config.yaml` heeft `max_open_positions: int(1,5)` met default 3, en `entrypoint.py` zet elke ingevulde optie als env-var, die in `config.py:128-136` de yaml overschrijft. Je `config.yaml` zegt `max_open_positions: 10`, maar de draaiende add-on gebruikt vermoedelijk 3, en het schema staat 10 niet eens toe. Hetzelfde geldt voor `markets` (default `BTC-EUR,ETH-EUR`) en `max_position_pct`.

Ontbrekend als add-on-optie: `sizing`, `bucket_eur`, `universe.auto_fill`, `regime.*`, `exits.*`, `blocklist`. Die komen dus alleen uit de yaml in het image.

Controleer wat er nu daadwerkelijk in HA staat en trek schema en yaml gelijk, anders meet je fase 2 op een configuratie die je niet denkt te draaien.

### 3.3 Mode-scheiding is niet volledig
PROJECTPLAN regel 87 claimt "historie vermengt nooit". Zonder mode-filter:

- `paper.py:76` `daily_pnl_eur` telt alle sells, ook live. De dagverlies-cap kan dus door de verkeerde modus getriggerd worden.
- `paper.py:66` `last_trade_at` idem, raakt de cooldown.
- `main.py:29` `publish_mqtt` telt alle sells voor de HA-sensoren.
- `web.py:89, 252, 281` selecteren `PositionRow` zonder modus; `/api/portfolio` koppelt alle posities aan de paper-cash.

`db.py` maakt `PositionRow.market` bovendien globaal uniek, ongeacht modus.

### 3.4 Vervallen guardrail niet expliciet herzien
PROJECTPLAN regel 61 en de docstring van `scanner.py` stellen allebei dat de bot nooit zelf handelt in een gescande markt; dat was een post-mortem-les. Auto-fill (v0.17.0) draait dat om. De omkering is verdedigbaar, maar staat nergens als bewuste herziening van die les. Zet dat expliciet in het plan met de reden en de compenserende gates, anders sluipt de oude fout terug via de documentatie.

### 3.5 Kleinere punten

| punt | plaats | impact |
|---|---|---|
| `pyproject.toml` op 0.14.1, add-on op 0.18.0 | versiebeheer | verwarring, geen functioneel effect |
| A3 (getrapte liquiditeit) staat als `.disabled` buiten git | 3 bestanden | werk buiten versiebeheer kun je kwijtraken; zet het op een branch |
| `dashboard_token` default leeg = geen auth | `web.py:42-46` | onder ingress acceptabel, met poort 8000 open niet |
| SQLite zonder WAL en `busy_timeout` | `db.py:99` | drie gelijktijdige schrijvers (analyse, guard, web), "database is locked" onder last |
| LLM-dagbudget in geheugen, reset bij herstart; mislukte calls tellen niet mee | `llm.py:59-66, 92` | budgetbewaking is zwakker dan bedoeld |
| `place_market_order(... amount_quote)` krijgt bij sell een base-amount | `exchange.py:174-184` | misleidende naam, correcte aanroep |
| Telegram met `parse_mode: Markdown` op vrije tekst | `notify.py:26` | reden met speciale tekens geeft 400, melding verdwijnt |
| `scan(top_n=40)` elke cyclus voor auto-fill | `engine.py:202` | circa 41 extra API-calls per uur bovenop de analyse-set |

---

## 4. Wat als eerste

1. `positions.append()` na een buy in `run_once`, plus een integratietest op `run_once` die slotlimiet en cluster-cap afdekt. Dit is de enige bevinding die vandaag, in paper, geld raakt.
2. Add-on-schema en `config.yaml` gelijktrekken en controleren waarmee de Pi nu echt draait. Anders is de fase 2-meting niet interpreteerbaar.
3. Trend-break-exit repareren of schrappen, en `rsi_overbought` daadwerkelijk gebruiken. Nu is er een exit-regel die niet bestaat.
4. Mode-filters toevoegen op de vier genoemde plekken.
5. EV-gate ontwerpen op basis van gerealiseerde trefkans. Dit is het antwoord op de vraag die de fee-gate niet beantwoordt en zou de logische opvolger moeten zijn van de regime- versus LLM-vergelijking.
6. Pas daarna fase 3: eerst 1.1 tot 1.4 (atomaire sell, exchange-side stop, lock, reconciliatie).

## 5. Wat goed is en zo moet blijven

- Elke gate als pure functie met eigen tests, en elke shadow-gate met een eigen meetmodule die de echte round-trip-P&L leest in plaats van een aanname. Dat is zeldzaam volwassen voor een privéproject.
- De go/no-go-drempel (`TARGET_RESOLVED = 20`, netto gate positief) staat in code, niet alleen in een document.
- Herhaald afgewezen: news, sentiment, whale-tracking, DCA. De post-mortem wordt daadwerkelijk gehandhaafd.
- Dubbel slot op live plus kill-switch, en `LiveBroker` die nooit per ongeluk actief wordt.
- Het PROJECTPLAN als levend document met expliciete "gebouwd maar niet gecommit"-status. Dat maakte deze review een stuk sneller.
