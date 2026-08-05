# Projectplan: AI Trade Platform

Laatste update: 2026-08-05 (v0.20.0, tweede reviewronde)

## Doel

Geautomatiseerd analyse- en tradingplatform voor crypto (Bitvavo, later aandelen) dat LLM's alleen inzet waar ze waarde toevoegen. Harde eis: fee-bewust beslissen. Eerdere poging verloor ~15% door fees bij 27% correcte keuzes; dit platform handelt alleen als de verwachte winst de round-trip fees plus marge overstijgt.

## Kernprincipes

1. **Deterministisch waar mogelijk, AI waar zinvol.** Indicatoren, fee-berekening, risk management en exits (stop loss / take profit) zijn pure code. De LLM geeft alleen een second opinion op kandidaat-koopsignalen en kan vetoën, nooit zelf trades initiëren.
2. **Fee-gate vóór alles.** Een trade gaat alleen door als: verwachte beweging (ATR-gebaseerd doel) > round-trip fees + slippage-buffer + minimale winstdrempel.
3. **Paper trading eerst.** Volledige pipeline met echte marktdata en echte fee-percentages, gesimuleerde orders. Live is een config-switch (`TRADING_MODE=live`) die pas omgaat na bewezen win-rate.
4. **Gratis LLM-tiers.** Groq (primair, ruimste limieten) → Gemini → Mistral als fallback-keten met dagbudget per provider.
5. **DevSecOps.** Elke wijziging via Git, CI draait lint + tests + security scans (bandit, pip-audit), deploy via Docker.

## Architectuurbeslissingen (ADR-samenvatting)

| # | Beslissing | Rationale |
|---|-----------|-----------|
| 1 | Python 3.11, FastAPI + APScheduler | Eén proces: scheduler voor analyse-cycli, web-dashboard erbij. Licht genoeg voor een Pi. |
| 2 | SQLite via SQLAlchemy | Geen aparte DB-server nodig op de Pi; SQLAlchemy maakt Postgres-migratie later triviaal. |
| 3 | Hosting: Raspberry Pi + Docker Compose | Swing-bot is een long-running proces; serverless (Vercel/Cloudflare) past niet (timeouts, cold starts). Zelfde image draait later op elke VPS. |
| 4 | Exchange-abstractie (`ExchangeAdapter`) | Bitvavo nu; Alpaca (US-aandelen, $0 commissie, beste API) of IBKR later inplugbaar. |
| 5 | Exits volledig mechanisch | Stop loss / take profit (ook per minuut door de guard), time-stop en breakeven-stop, zonder LLM. Voorkomt bag-holding door AI-twijfel en bespaart LLM-budget. De trend-break-exit is in v0.19.0 geschrapt (zie hieronder). |
| 6 | Market orders in paper-modus, taker fee gerekend | Conservatief: als het met taker fees rendeert, rendeert het live met maker (limit) orders beter. |

## Fee-model (Bitvavo, basis-tier)

- Maker 0,15% / Taker 0,25% (worden live opgehaald via `GET /account`, config als fallback)
- Round-trip (koop+verkoop, taker): 0,50%
- Decision gate default: verwachte edge ≥ round-trip + 0,10% slippage + 0,50% minimale winst = **≥ 1,10%**

## Roadmap

### Fase 1 — Fundament (deze iteratie)
- [x] Onderzoek: Bitvavo API v2, gratis LLM-tiers, aandelenbrokers, hosting
- [x] Projectplan en architectuur
- [x] Bitvavo REST client (HMAC-auth, rate-limit bewaking, operatorId)
- [x] Marktdata + technische indicatoren (EMA, RSI, MACD, ATR, Bollinger)
- [x] Deterministische signaalgeneratie (swing-strategie)
- [x] Fee-aware decision engine + risk management
- [x] Paper trading engine met echte fees
- [x] LLM-laag: Groq/Gemini/Mistral met fallback en dagbudget
- [x] SQLite persistence (trades, posities, signalen, LLM-calls, equity)
- [x] Web-dashboard (posities, P&L na fees, signalen, LLM-verdicts)
- [x] Telegram-notificaties
- [x] Backtester (zelfde strategie + fee-model op historische candles)
- [x] Unit tests (indicatoren, fee-gate, risk, paper fills, LLM-router)
- [x] Docker (ARM64/AMD64), docker-compose, CI-pipeline (ruff, pytest, bandit, pip-audit)
- [x] Deployment-handleiding Raspberry Pi
- [x] Home Assistant add-on (Pi 5 draait HAOS): manifest, ingress-dashboard, options→env entrypoint, CI-job voor add-on image (GHCR), add-on-repository structuur voor auto-updates

- [x] v0.3.0: MQTT/HA-discovery integratie (bot-status als HA-sensoren; concept hergebruikt uit oude bot)

### Hergebruik-analyse oude app (Claude-project)
- [x] `mqtt_publisher` → herbouwd in v0.3.0 (alleen status, geen commando-kanaal)
- [ ] `live_trader` order/fill-afhandeling → referentie voor fase 3
- [x] `optimizer` parameter-tuning → herbouwd in v0.6.0 mét train/test-split tegen overfitting
- [x] `correlation` → herbouwd in v0.5.0 als risk-gate + onderdeel instap-advies
- [x] `market_scanner` → herbouwd in v0.7.0: liquiditeits/spread-filter + score over alle EUR-markten. **Herzien in v0.19.0:** de regel "advies-only, de bot handelt nooit zelf in gescande markten" gold tot v0.16.0 en is achterhaald door auto-fill (v0.17.0). Sindsdien handelt de bot wél in gescande markten, maar uitsluitend in kandidaten die zelfstandig door score, fee-gate en liquiditeit komen en daarna nog door alle engine-gates (risk, correlatie-cluster, blocklist, kill-switch, regime). De scanner verlaagt geen enkele drempel; gepinde `markets` blijven handmatig beheerd en de watchlist blijft advies-only
- [x] Afgewezen: news_feed, sentiment, whale_tracker, DCA, house-money (zie post-mortem)

### Fase 2 — Validatie (loopt)
- [x] API-keys aangemaakt en geconfigureerd (Bitvavo read-only, Groq/Gemini/Mistral, MQTT)
- [x] Tooling: candle-paginatie (2+ jaar data), optimizer, drawdown/veto-metrics op dashboard (v0.6.0)
- [ ] 4-8 weken paper trading draaien (gestart 2026-07-05)
- [ ] Wekelijkse evaluatie: win-rate, netto P&L na fees, max drawdown, LLM-veto-rate (dashboard)
- [ ] Backtests op 2+ jaar data per markt: `python -m tradebot.backtest BTC-EUR --interval 4h --limit 4400`
- [ ] Parameter-tuning: `python -m tradebot.optimizer BTC-EUR --limit 3000` (kies op test-kolom, niet train)
- [x] Tooling LLM-veto-waarde: counterfactual-analyse per veto (voorkwam verlies vs. sneed winst weg), beide exit-modellen, richting-check op veto-redenen — dashboard-sectie + `python -m tradebot.analysis.veto` (v0.12.0)
- [x] Veto-checker in de app uitgebreid (v0.14.0): config-scoping (config-hash per veto, schone meting op nieuwe config los van de oude), echte shadow-trade-uitkomst naast de counterfactual, precisie plus 95%-Wilson-marge, uitsplitsing per veto-reden (mean-reversion apart). Dashboard-sectie + CLI (`--all` voor de vervuilde totaalmeting). Vervangt de handmatige xlsx-tracker als primaire meting
- [ ] LLM-veto-waarde beoordelen: veto-rate + veto-redenen vs. echte uitkomst op de nieuwe config (met de uitgebreide tool, doel 15-20 afgewikkelde trades)
- [~] Eerste uitkomst v0.12.0-tool (86 veto's): netto gate negatief onder beide modellen (vaste horizon -€32, TP/SL -€272); 67% van de veto's blokkeert op "onderste Bollinger-band" wat de strategie juist als koopreden telt. Veto lijkt waardevernietigend, hypothese: omgekeerde mean-reversion-lezing in de LLM
- [ ] Shadow-mode-experiment (v0.13.0): `llm_veto_binding: false` in paper, 4 weken met-veto vs. zonder-veto vergelijken; daarna prompt fixen of veto schrappen
- [~] Tussenstand v0.14.0-tool: echte shadow-uitkomst netto negatief (-€13,23), vaste horizon +€15,57 maar dat is de zwakste horizon-definitie; 9/11 veto's blokkeren op "onderste Bollinger-band" (strategie telt dat als koopreden). Conclusie: LLM-veto op de TA-as is redundant én tegenstrijdig. Besluit: LLM-veto blijft shadow ("uit tenzij bewezen"), niet bindend maken tot een variant op de echte-uitkomst-maat ≥20 afgewikkelde trades positief scoort
- [x] Gecodeerd regime-filter i.p.v. LLM op de TA-as (v0.15.0): BTC-proxy-trend als markt-brede risk-on/off-gate, deterministisch, gratis. Shadow default (`regime.binding: false`), eigen meting `analyze_regime` + dashboardkaart. Rationale: een LLM heeft geen edge op numerieke TA; "koop niet in een zwakke markt" is een coded regel. Go/no-go per gate identiek: bindend pas bij positieve netto gate op ≥20 afgewikkelde trades
- [ ] Regime- vs. LLM-veto vergelijken op de echte shadow-uitkomst (beide niet-bindend); de gate die netto waarde toevoegt wordt bindend, de andere gaat eruit
- [x] Handmatige shadow-veto tracker (`docs/shadow-veto-tracker.xlsx`): koppelt elke veto aan uitkomst (TP/SL) en fictieve P&L na fees. Diende als ontwerp; nu geautomatiseerd in de app (v0.14.0). Blijft bruikbaar voor handmatige sanity-checks
- [ ] Go/no-go criteria vastleggen (voorstel: win-rate > 45% én netto positief na fees over 100+ trades)

### Fase 3 — Live (code gebouwd in v0.11.0, activering pas na fase 2 go)
- [x] LiveBroker: maker (limit post-only) entries met fill-polling en timeout-cancel; market exits (kapitaalbescherming boven fee-optimalisatie)
- [x] Hard exposure-plafond (`live_max_capital_eur`, default €100) los van de rekeningbalans
- [x] Dubbel slot: `trading_mode=live` én letterlijke bevestigingszin "IK BEGRIJP DAT DIT ECHT GELD IS" in `live_confirm`, anders weigert de bot te starten
- [x] Kill-switch: pauzeknop in dashboard stopt alle aankopen (paper én live); exits en guard lopen altijd door
- [x] Mode-scheiding: posities/trades/stats gelabeld paper|live. De claim "historie vermengt nooit" klopte tot v0.19.0 **niet volledig**: `paper.daily_pnl_eur`, `paper.last_trade_at`, `main.publish_mqtt` en drie web-queries (advies, grafiek, portfolio) lazen zonder mode-filter. In v0.19.0 gerepareerd; de claim geldt nu wel. `PositionRow.market` blijft bewust `unique=True` (laatste vangnet tegen een dubbele insert; twee modi draaien nooit tegelijk in één proces, dus een samengestelde uniciteit op (market, mode) levert operationeel niets op en zou een sqlite-tabelrebuild op de meet-DB vergen)
- [ ] Aandachtspunt bij fase 3-activering: door die unique-constraint blokkeert een achtergebleven open **paper**-positie een live-positie in dezelfde markt op dezelfde DB. Oplossen door open paper-posities te sluiten of te archiveren vóór de omschakeling, niet door de constraint te verslappen
- [ ] ACTIVERING (handmatig, pas na fase 2 go): Bitvavo API-key met trade-rechten (géén withdrawal, IP-whitelist), `trading_mode=live` + bevestigingszin invullen, klein kapitaal

### Fase 4 — Aandelen
- [ ] Brokerkeuze definitief: Alpaca (US-only, beste API, $0 commissie) vs IBKR (breder, complexere API)
- [ ] `ExchangeAdapter` implementatie voor gekozen broker
- [ ] Markturen-logica (crypto is 24/7, aandelen niet)

## Deployment-verbetering: kapitaal aan het werk (gepland, nog niet geimplementeerd)

Doel: voorkomen dat slots leeg blijven terwijl er budget is, zonder de fee-discipline te breken. Uitgangspunt: een leeg slot door gebrek aan een kwaliteitssignaal blijft leeg (dat is correct gedrag, geen bug). We verruimen alleen het kandidaten-universum en de gate-logica; we forceren nooit deployment. De handmatige watchlist blijft advies-only en is dus niet de groeimotor, de scanner is dat.

Beslissingen (2026-08-04, met Martin):
- Universum: gepinde kern (`markets`) plus auto-fill uit de scanner tot de slots vol zijn, uitsluitend kandidaten die alle gates halen.
- Correlatie: cluster-cap (max N posities per correlatie-cluster) in plaats van een hard blok na de eerste.
- Liquiditeit: getrapt, dunne coins toegestaan met een strengere edge-eis en kleinere inzet.
- Sizing: vaste bucket nu; positiegrootte laten groeien is bewust geparkeerd naar fase 2.

### Fase A1 (v0.17.0) — universum + correlatie [GEBOUWD]
- [x] Auto-fill kandidaten: elke cyclus de resterende vrije slots aanvullen met scanner-top-hits die score, fee-gate, liquiditeit en niet-reeds-open/gepind passeren. Config `universe.auto_fill` (bool), `universe.max_auto`, `universe.auto_fill_buffer`. Deterministisch, geen AI. Gepinde `markets` en open posities houden voorrang; de analyse-set is gepind + open + auto-fill (open posities altijd meegenomen zodat hun trend-break-exit blijft draaien). Pure helper `scanner.select_auto_fill`, engine `_auto_fill_markets`. Env `TRADEBOT_AUTO_FILL`
- [x] Cluster-cap correlatie: `risk.max_correlated_positions` (K, default 2). Kandidaat geweigerd zodra het aantal open posities met correlatie > `max_correlation` gelijk of groter is dan K. Pure helper `decision.correlated_positions`, vervangt het harde "blok bij de eerste" in de engine
- [x] Dashboard: auto-fill-set van deze cyclus getoond in de Instellingen-sectie (KV `last_auto_fill`); slot-bezetting al zichtbaar (v0.16.0)
- [x] Tests (test_universe.py, 6 nieuw), CI-identieke run, versiebump, docs

### Fase A2 (v0.18.0) — curatie: time-stop, banlijst, quiet-vlag [GEBOUWD]
Bewust vóór de getrapte liquiditeit gezet (die is A3 geworden): de time-stop is het vangnet dat je wil hebben vóórdat de bot actief in dunne coins handelt, want dun = het makkelijkst blijven hangen. Kernonderscheid: een coin die niets doet in de lijst kost vrijwel niets (auto-fill vult de slots toch), maar een open positie die eindeloos zijwaarts drift bezet wel een slot plus kapitaal.
- [x] Time-stop op open posities: pure `strategy.time_stop_hit`, gewired in de engine-exit-stap. Sluit een positie die na `exits.time_stop_candles` (12) geen TP/SL raakte en per saldo op/onder `exits.time_stop_min_net_pct` (0 = break-even, incl. round-trip fees) staat. Alleen prijs/tijd, geen AI, geen nieuws. Reden gelogd als "time-stop". Winnaars boven de drempel blijven staan.
- [x] Banlijst: top-level `blocklist`. Geweerd uit de scanner (dus ook auto-fill), de engine-buy-gate (open posities exiten nog wel) en handmatig toevoegen (`lists.modify` weigert). Memecoins vallen hieronder: geen auto-classificatie, wel per specifieke coin te bannen.
- [x] Quiet-vlag (adviserend): `/api/lists` verrijkt met gepinde coins die in `curation.quiet_days` (30) 0 koopsignalen hadden; dashboard toont "overweeg naar watchlist". Puur advies, de bot verplaatst niets zelf.
- [x] Tests (test_curation.py + banlijst-test in test_lists.py), CI-identieke run, versiebump, docs.

### Fase A3 (v0.20.0) — getrapte liquiditeit + segment-meting [GEBOUWD, apart bewaard, nog niet gecommit]
Code al af (was eerst v0.18.0, doorgeschoven na de swap). Ligt buiten de repo-tree bewaard (`outputs/liq_v019_backup/`, naam nog van de oude nummering); komt terug als **v0.20.0** zodra de time-stop op paper draait, met de tussenzone dan meteen actief (het vangnet staat er). Nummer opgeschoven omdat v0.19.0 is opgegaan aan de code-review-opvolging hieronder.
- [x] Getrapte liquiditeit: `LiquidityPolicy` (vloer 250k, tussenzone tot 100k met 1,5x edge-eis en 0,5x inzet, daaronder uitgesloten); scanner + `DecisionEngine.evaluate_buy` passen de tier toe; tier + volume in SignalRow.details.
- [x] Segment-meting: `analysis.liquidity.analyze_liquidity_segments` cohort normaal vs dun uit echte round-trip-P&L, dashboard-sectie + `/api/liquidity-segments`, "dun"-tag in de scanner.
- [x] Code staat sinds v0.20.0 op branch `feature/a3-liquiditeit` onder de echte bestandsnamen, dus in git, diffbaar en met CI erop. Op main liggen de drie `.disabled`-bestanden nog in de working tree; ze zijn daar met drie expliciete paden genegeerd (geen `*.disabled`-glob, die zou toekomstige bestanden stilzwijgend slikken). Aanleiding: ongetrackt bewaren maakte van "nog niet gecommit" feitelijk "kan niet gecommit worden" — één `git clean -fdx`, een verse clone of een workspace-reset en het werk was zonder waarschuwing weg
- [ ] Mergen naar main als v0.21.0 (na paper-validatie van v0.18.0/v0.19.0).

### Code-review-opvolging (v0.19.0)

Externe review op v0.18.0. Vier bevindingen raakten de draaiende paper-run, twee de testdekking, drie de configuratie naar de Pi, twee waren analysevragen.

**Blok 1 — bugfixes**
- [x] 1.1 Stale positielijst in `engine.run_once`: `positions` werd één keer vóór de marktloop gelezen en na een buy alleen `free` bijgewerkt. Daardoor kon de slotlimiet overschreden worden (bij eff_max 3 een vierde positie, in bucket-modus met een restbedrag) en telde de correlatie-cluster-cap posities uit dezelfde cyclus niet mee. Nu wordt de in-cycle state na elke uitgevoerde order ververst (`_refresh_after_trade`: posities, cash, dagwinst). `portfolio` blijft bewust cyclus-vast (herberekenen kost een prijs-API-call per positie; binnen één cyclus schuift de waarde alleen met fees en zojuist gerealiseerde P&L)
- [x] 1.2 Trend-break-exit **geschrapt**, niet herschreven. De regel eiste `ema_fast < ema_slow` én `rsi > 70`, twee vrijwel disjuncte condities; coverage bevestigde dat hij nooit gevuurd heeft. Vervangen door een **breakeven-stop** (`strategy.breakeven_stop_hit`): bewapent zodra de piek sinds entry ≥ `trigger_atr` × ATR boven de entry lag, vuurt als de koers terugzakt tot ≤ entry + `offset_pct` (0,55%, dekt de round-trip taker van 0,50%). Zelfde bedoeling, maar op prijs en gerealiseerde winst in plaats van op indicatorcondities. Default `binding: false` (shadow), conform de vaste regel: elke nieuwe gate eerst meten, bindend pas bij een positieve netto gate over ≥ 20 afgewikkelde trades
- [x] 1.3 RSI-parameters. `rsi_overbought` blijft: die is niet dood, het dashboard-advies (`web.advice`) leest hem. Wat dood was, is de hardcoded 70 in `check_exit`, en die verdween met 1.2. Aanpalend opgelost: `evaluate_buy` rekende met `rsi_oversold + 10` en een hardcoded ondergrens 25, dus config zei 35 terwijl de effectieve bovengrens 45 was. Nu expliciet `rsi_buy_zone_min: 25` / `rsi_buy_zone_max: 45`; gedrag ongewijzigd
- [x] 1.4 Mode-filters toegevoegd in `paper.daily_pnl_eur`, `paper.last_trade_at`, `main.publish_mqtt` en web-queries voor advies, grafiek en portfolio. `PositionRow.market unique=True` bewust ongewijzigd (zie fase 3 hierboven)

**Blok 2 — testdekking op de bedrading**
- [x] 2.1 Integratietests tegen de echte `TradingCycle.run_once` met fake feed en echte `PaperBroker` op een in-memory DB
- [x] 2.2 `GuardHarness` uit `test_guard.py` vervangen door tests tegen de echte `check_exits_fast`
- [x] 2.3 Test-naar-bug-mapping vastgelegd in `tests/test_engine_cycle.py`

**Blok 3 — config-drift naar de Pi**
- [x] 3.1 Add-on-schema gelijkgetrokken met `config/config.yaml`. `max_open_positions` stond op `int(1,5)` met default 3 terwijl de yaml 10 zei; omdat entrypoint.py elke ingevulde optie als env-var zet, draaide de Pi feitelijk op 3 slots. Grens nu `int(1,10)` met default 10, gelijk aan `lists.MAX_MARKETS` (meer open posities dan verhandelbare markten heeft geen betekenis, en alt-sprawl was een verliesoorzaak van de oude bot)
- [x] 3.2 Operationele knoppen toegevoegd als add-on-optie: `sizing`, `bucket_eur`, `auto_fill`, `regime_enabled`, `regime_binding`, `llm_veto_binding` (stond al als env, was niet als optie ontsloten) en `blocklist` (nieuwe env `TRADEBOT_BLOCKLIST`). Bewust NIET toegevoegd: `exits.time_stop_candles` en `time_stop_min_net_pct`. De scheidslijn: een knop hoort in de add-on als omzetten een bedrijfsvoeringsbeslissing is die geen deploy mag vergen én de gemeten strategie niet verandert (universum, positiegrootte, gate-schakelaars, banlijst). Exit-parameters bepalen de uitkomstverdeling van elke trade en horen dus bij de strategie, net als EMA-periodes en de scoredrempel: die gaan via optimizer/backtest en een commit
- [x] 3.3 Versienummers gekoppeld. `pyproject.toml` stond op 0.14.1 tegen 0.18.0 in de add-on, vier minors drift. Beide nu 0.19.0. Structureel dichtgezet met `tests/test_addon_config.py`, dat in CI faalt zodra de versies, de option-defaults, de schemagrenzen of de ENV_MAP uit de pas lopen. Releaseprocedure: versie bumpen in pyproject.toml én tradebot-addon/config.yaml in dezelfde commit; de test is het vangnet, niet de procedure

**Blok 4 — analyse (geen code)**
- [x] 4.1 `docs/ontwerp-ev-gate.md` geschreven. Kern: de break-even trefkans is niet 40% maar 44-52% bij realistische ATR (1-3% van de prijs), waardoor het go/no-go-criterium "win-rate > 45%" op zichzelf niet voldoende is. Belangrijker bezwaar: een EV-gate op live gerealiseerde trades is statistisch niet haalbaar op dit handelsvolume (σ ≈ 5% per trade bij ATR 2%, dus ~776 trades nodig om een EV-verschil van 0,5%-punt aan te tonen, tegenover 40-80 afgewikkelde trades per jaar). Voorstel daarom: eerst bouwen als **meter** met dashboardkaart, steekproef vullen uit de backtest-testhelft als prior, en pas bindend maken als gepoolde circuit breaker, nooit als selectiefilter per markt. Expliciet advies: niet bouwen vóór de regime- versus LLM-veto-vergelijking rond is, want een derde shadow-gate maakt de netto-gate-getallen niet meer optelbaar
- [ ] 4.1 besluit van Martin op de drie openstaande vragen in de notitie (meter of gate, backtest als prior, go/no-go-criterium herzien)
- [x] 4.2 `docs/ontwerp-spread-bron-van-waarheid.md` geschreven. Het omslagpunt ligt exact op de buffer: onder 0,10% spread is de decision engine strenger, erboven de scanner (tot +0,50%-punt bij de scanner-limiet van 0,60%). Het echte lek is dat de engine de spread helemaal niet kent, dus gepinde markten met een brede spread passeren de fee-gate op 1,10% terwijl hun werkelijke kosten hoger liggen. Voorstel: één pure `required_edge_pct(fees, min_profit_pct, spread_pct=None)` met de slippage-buffer als **vloer** in plaats van als alternatief, spread-map per cyclus uit `get_ticker_24h` (nul extra API-calls zolang auto-fill aan staat). Signaleert daarnaast dat het kostenmodel bij fase 3 niet meer klopt: `LiveBroker` doet maker-entries die de spread niet kruisen, dus live wordt met `round_trip_pct(use_taker=True)` systematisch te streng gemeten
- [ ] 4.2 besluit van Martin op de drie openstaande vragen in de notitie (buffer als vloer, spread in de engine, verhouding tot de 1,5x edge-eis van fase A3)

### Code-review-opvolging ronde 2 (v0.20.0)

Tweede externe review op v0.19.0. Zes blokken; blok 1 raakt de draaiende paper-run direct.

**Blok 1 — signalen op afgesloten candles**
- [x] 1.1 Bitvavo levert de lopende, nog niet gesloten candle mee als nieuwste element (geverifieerd 2026-08-05 17:35 UTC: nieuwste 4h-candle opende 16:00 UTC). `build_snapshot` las `closes[-1]`/`hist[-1]`/`r[-1]`, dus elke uurcyclus beoordeelde een bar die nog beweegt, drie tot vier keer per bar. De zwaarst wegende koopreden (`macd_hist > 0 and macd_hist_prev <= 0`, 2 van de 3 punten) is precies zo'n flip-conditie: de bot kocht bij de eerste uurrun waarin hij toevallig waar was en selecteerde dus systematisch de vluchtigste realisatie van het signaal. Effect eenzijdig: meer trades dan de backtest voorspelt bij hetzelfde signaal. Opgelost met de pure `strategy.drop_unclosed` (interval uit candle-spacing, `now_ms` injecteerbaar) plus een aparte entry-snapshot in de engine (`_entry_snapshot`). **Asymmetrisch**: entries op afgesloten candles, exits en position guard op de live prijs. **Prijsanker**: score en ATR uit de afgesloten reeks, SL/TP en fee-gate vanaf de live prijs (`_anchor_price`), want daar wordt de order gevuld. Schakelaar `strategy.signal_on_closed_candles` (default true) houdt oud en nieuw naast elkaar meetbaar; hij staat bewust onder `strategy` en niet onder `schedule`, zodat `config_fingerprint` verandert als hij omgaat en de veto-meting de twee signaalregimes vanzelf scheidt
- [x] 1.1 Bewust NIET in `build_snapshot` afgeknipt: die functie voedt ook de scanner, het dashboard, de regime-proxy en de veto-analyse; daar afknippen zou al die paden stil meeveranderen. De regime-proxy blijft eveneens op de volledige reeks: EMA-snel vs EMA-traag is een traag lopende vergelijking en geen flip-conditie binnen een bar, en omzetten zou de lopende regime-meting midden in het experiment veranderen
- [x] 1.2 `time_stop_hit` telde candles met `ts > opened_ms` inclusief de lopende bar, waardoor de time-stop ongeveer een candle te vroeg vuurde (en binnen dezelfde bar meerdere uurcycli lang). Telt nu via `drop_unclosed` alleen afgesloten candles. `breakeven_stop_hit` telt de lopende bar bewust wél mee: die MEET een piek in plaats van candles te TELLEN, en een high die zojuist gezet is, is een even echte piek. Verschil vastgelegd in beide docstrings
- [x] 1.3 Chase-guard (nieuw, niet uit de review maar een bijwerking van 1.1). Doordat het signaal van de afgesloten candle komt en de fill van de live prijs, ontstaat entry-drift: is de koers sinds die close doorgelopen, dan klopt het risico nog (stop en target hangen aan de fill, dus 2x ATR blijft 2x ATR) maar de edge niet, want een deel van de verwachte beweging is al gemaakt. Vóór v0.20.0 viel dat samen omdat signaal en prijs uit dezelfde bar kwamen. Pure `strategy.chase_too_far` + `decision.apply_chase_guard`, config `strategy.max_chase_atr` (0,5) en `strategy.chase_guard_binding` (false). **Shadow by default**, conform de vaste regel; treffer belandt als `details.shadow_chase` in de SignalRow, zodat hij met dezelfde meetmethode als regime en breakeven te beoordelen is
- [x] 1.x Twee ATR's zijn nu een bewuste keuze, vastgelegd in de docstring van `_extra_exits`: sizing en fee-gate gebruiken de ATR uit de afgesloten reeks (een entry heeft een stabiele stop-afstand nodig), de breakeven-stop bewapent op de live ATR omdat de piek waartegen hij afzet óók inclusief de lopende bar gemeten wordt. `current_price` blijft in beide exits live; alleen het TELLEN van candles loopt via `drop_unclosed`
- [x] 1.4 Meet-scoping per gate. `config_fingerprint` dekte alleen `strategy`, `decision` en `fees`, terwijl de gate-parameters van de breakeven-stop (`exits.breakeven_stop.trigger_atr`) en van de regime-gate erbuiten lagen: aan zo'n drempel draaien liet de hertunede gate stilzwijgend met de oude meting mengen. Uitbreiden naar één globale hash over alles is echter óók fout: bij vier shadow-gates zou tunen van één gate de meetklok van alle vier resetten, waardoor de drempel van 20 afgewikkelde trades per gate praktisch onbereikbaar wordt in precies de fase waarin je afstelt. Daarom een gedeelde KERN (`strategy`, `decision`, `fees`, `universe`) plus per gate zijn eigen secties (`GATE_SECTIONS`, `gate_fingerprint`). `risk` blijft erbuiten: dat schaalt alleen de euro-bedragen, niet de procentuele uitkomst. Geaccepteerde onnauwkeurigheid, expliciet vastgelegd: de uitkomst van een entry-gate is een volledige round-trip, dus exit-parameters beïnvloeden hem wel degelijk; `exits` zit toch niet in hun scope, want anders is de kern feitelijk weer globaal

**Blok 2 — backtester gelijktrekken met de engine**
De backtester modelleerde sinds v0.18.0 een strategie die niet meer bestond. Vier afwijkingen, alle vier gerepareerd; de module draait nu op dezelfde functies en dezelfde config-objecten als `engine.TradingCycle`.
- [x] 2.1 Exits ontbraken: `run_backtest` riep alleen `check_exit` aan, dus geen time-stop (v0.18.0) en geen breakeven-stop (v0.19.0). Beide toegevoegd met dezelfde parameters uit `exits`. De breakeven-stop volgt ook `binding`: in shadow verkoopt de engine niet, dus de backtester ook niet, anders modelleert hij een strengere strategie dan er draait
- [x] 2.2 Exit op slotkoers vervangen door intrabar. De regel staat nu als gedeelde pure functie `strategy.intrabar_exit` (stop wint bij een candle die stop én target raakt); `analysis/veto.py._tp_sl` is erop overgezet, zodat backtester en veto-analyse één bron van waarheid delen in plaats van twee kopieën
- [x] 2.3 Slippage op de fill: `slippage_buffer_pct` zat wel in `min_edge` maar werd nooit op de fillprijs toegepast. Nu op beide benen, elk de helft van de buffer. Onderbouwing: de buffer is in `min_edge_pct` een round-trip-post (hij staat naast `round_trip_pct` als één term) en de scanner telt de werkelijke spread op dezelfde manier één keer per round-trip; economisch klopt dat ook, want kopen op de laat en verkopen op de bied kost samen precies één spread. De volle buffer per been zou de backtester strenger maken dan de fee-gate die de entry toelaat
- [x] 2.4 All-in sizing vervangen door twee expliciete modi, met de modus in de output (`"mode"`). `single` = één markt all-in, bewust behouden voor signaalonderzoek en niet met live vergelijkbaar. `portfolio` = meerdere markten met gedeelde cash, bucket-sizing, slotlimiet, cooldown, dagverliescap (alle via dezelfde `RiskManager`) en de correlatie-cluster-cap via `decision.correlated_positions`. Alleen die tweede modus geeft rendement en drawdown die met de live-run te vergelijken zijn
- [x] 2.x Performance: `strategy.build_snapshots` rekent alle indicatoren in één pass door in plaats van per bar de hele historie opnieuw (O(n) i.p.v. O(n²)), wat de uitgebreide optimizer-grid van blok 3 haalbaar maakt. Mag alleen omdat elke gebruikte indicator prefix-stabiel is; een test pint de gelijkheid met `build_snapshot` vast zodat de backtester niet stilletjes andere waarden kan zien dan de engine

### Fase B (in fase 2, na go/no-go) — positiegrootte laten meegroeien
- [ ] Optionele sizing-modus `bucket_pct` (bucket als vast % van portfolio) of mijlpaal-gebaseerd, zodat de inzet meeschaalt met kapitaal. Pas activeren als de edge in fase 2 bewezen is; nu zou het compounding op een nog onbewezen strategie zetten. De config-hook (`sizing`-veld) ligt er al (v0.16.0), dus dit is een uitbreiding, geen herbouw.

### Openstaand aandachtspunt: spread versus marktimpact (fase B)

De backtester vouwt spread en slippage samen in één post (`slippage_buffer_pct`, half per been). Dat klopt op de huidige omvang: spread kost één keer per round-trip, en bij EUR250 in markten boven EUR250k dagvolume is marktimpact verwaarloosbaar. Marktimpact is echter wél per been. Zodra de positiegrootte materieel groeit (fase B, sizing laten meeschalen) moet die samenvouwing opnieuw tegen het licht.

### Fase C (in fase 4) — echte diversificatie
- [ ] Crypto-alts zijn onderling 0,7-0,9 gecorreleerd, dus spreiding over veel crypto-slots is deels illusie. Echte diversificatie komt met aandelen (fase 4). De cluster-cap is tot die tijd een pragmatisch compromis, geen echte risicospreiding.

Guardrail (alle fases): geen enkele stap vult slots door de fee-gate of de correlatie-gate los te laten bij onderbezetting. Auto-fill voegt uitsluitend kandidaten toe die zelfstandig door alle gates komen.

## Bekende beperking: update-knop bij rode CI

HA leest de add-on versie uit git (main), maar het image bestaat pas na een groene CI-run. Bij een rode run biedt HA dus tijdelijk een update aan die faalt met "unknown error" — dit is de quality gate die uitrol van kapotte code blokkeert, niet een defect. Herstel: fix pushen, groene run afwachten, opnieuw updaten. Structurele oplossing (CI promoot pas na image-push naar een `stable`-branch waar HA naar wijst) is bewust uitgesteld tot na fase 2: wisselen van repository-URL betekent herinstallatie van de add-on en verlies van de paper-historie in /data.

## Post-mortem oude bot (Claude-project repo, -15% kapitaal)

Analyse van de vorige bot (272 commits, live gedraaid). Fees werden geboekt in P&L maar nergens als beslisdrempel gebruikt. Oorzaken van het verlies en de tegenmaatregel in dit platform:

| # | Oude bot | Nieuw platform |
|---|----------|----------------|
| 1 | LLM was beslisser: tactical AI-chain gaf elk uur BUY/HOLD/SELL per markt | LLM alleen veto op deterministisch kandidaat-signaal |
| 2 | Geen fee-gate op entry; fee pas zichtbaar bij P&L | Harde gate: verwachte move ≥ round-trip fees + slippage + winstdrempel (1,10%) |
| 3 | DCA-bijkopen bij -5% onder inkoop, in lagen | Geen DCA; één positie per markt met ATR-stop |
| 4 | MAX_TRADE_EUR=25: winst per trade verwaarloosbaar t.o.v. ruis | Positie 25% van portfolio, minimum €10 |
| 5 | Tientallen alt-markten, spread nergens gemodelleerd | Alleen BTC/ETH + 0,10% slippage-buffer |
| 6 | Zes koop-triggers (AI, DCA, house-money, hodl-accu, scanner, sentiment), nul validatielagen | Eén koop-pad, vier gates (score, risk, fee, LLM-veto); exits 100% mechanisch |

Les: het aantal manieren om een positie te openen moet kleiner zijn dan het aantal manieren om er een tegen te houden.

## Wijzigingslog

| Datum | Wijziging | Getest |
|-------|-----------|--------|
| 2026-08-05 | v0.20.0 meet-scoping per gate: `config_fingerprint` kreeg een kern (`strategy`, `decision`, `fees`, `universe`) plus `GATE_SECTIONS`/`gate_fingerprint` per shadow-gate, zodat tunen van één gate de meetklok van de andere drie niet reset (bij vier gates en een drempel van 20 trades maakte één globale hash die drempel onbereikbaar). Slippage-docstring aangevuld: op het SL-pad verschuift de kost naar de risicokolom in plaats van te verdwijnen, met een test die dat pad vastpint; `build_snapshots`-gelijkheid ook op een slice getest, want EMA seedt op arr[0] | 201 tests (6 nieuw), ruff, bandit exit 0 |
| 2026-08-05 | v0.20.0 blok 1.3 (opvolging op 1.1): chase-guard tegen entry-drift. Signaal komt van de afgesloten candle, fill van de live prijs; is de koers daartussen meer dan `strategy.max_chase_atr` x ATR doorgelopen, dan klopt het risico nog maar de edge niet. Pure `strategy.chase_too_far` + `decision.apply_chase_guard` in dezelfde shadow-vorm als regime- en LLM-gate (default niet-bindend, treffer als `details.shadow_chase`). Twee-ATR-keuze expliciet vastgelegd in de docstring van `_extra_exits` | 195 tests (10 nieuw), ruff, bandit exit 0 |
| 2026-08-05 | v0.20.0 blok 2 (review ronde 2): backtester gelijkgetrokken met de engine. Time-stop en breakeven-stop toegevoegd (breakeven volgt `binding`, dus in shadow verkoopt hij niet); exits intrabar via de nieuwe gedeelde `strategy.intrabar_exit`, waar ook `veto._tp_sl` op is overgezet; slippage op beide benen (half per been = één buffer per round-trip, consistent met `min_edge_pct` en de scanner); all-in sizing vervangen door modus `single` (signaalonderzoek) naast modus `portfolio` (gedeelde cash, buckets, slots, cooldown, dagverliescap, correlatiecap), met de modus expliciet in de output. `strategy.build_snapshots` maakt de run O(n) i.p.v. O(n²) | 185 tests (12 nieuw), ruff, bandit exit 0 |
| 2026-08-05 | v0.20.0 blok 1.2 (review ronde 2): time-stop telt alleen afgesloten candles (`drop_unclosed` met injecteerbare klok) in plaats van ook de lopende bar, die hem ongeveer een candle te vroeg liet vuren; breakeven-stop telt de lopende bar bewust wél mee (piek meten is iets anders dan candles tellen), verschil vastgelegd in beide docstrings | 173 tests (4 nieuw), ruff, bandit exit 0 |
| 2026-08-05 | v0.20.0 blok 1.1 (review ronde 2): entries worden beoordeeld op de laatst afgesloten candle in plaats van op de lopende bar. Pure `strategy.drop_unclosed` (interval uit candle-spacing, klok injecteerbaar), engine `_entry_snapshot` + `_anchor_price` (score/ATR uit de afgesloten reeks, prijs/SL/TP vanaf de live prijs). Exit-route, position guard, scanner, dashboard en regime-proxy expliciet ongemoeid. Schakelaar `strategy.signal_on_closed_candles` (default true) onder `strategy`, zodat `config_fingerprint` de oude en nieuwe signaalregimes in de veto-meting scheidt | 169 tests (7 nieuw), ruff, bandit exit 0 |
| 2026-08-05 | v0.19.0 blok 1 (code-review, bugfixes): in-cycle state wordt na elke order ververst, zodat slotlimiet en correlatie-cluster-cap posities uit dezelfde cyclus meetellen (`portfolio` bewust cyclus-vast, `daily_pnl` wel ververst); trend-break-exit geschrapt (vuurde nooit: twee disjuncte condities) en vervangen door een breakeven-stop in shadow (`exits.breakeven_stop`, trigger 1x ATR, offset 0,55%); RSI-koopzone expliciet in config (`rsi_buy_zone_min/max`) in plaats van `rsi_oversold + 10` met een hardcoded 25; mode-filter toegevoegd op zes queries die paper- en live-historie vermengden | 137 tests (11 nieuw), ruff, bandit exit 0 |
| 2026-08-05 | v0.19.0 blok 2 (code-review, testdekking): integratietests op de echte `TradingCycle.run_once` met fake feed en echte PaperBroker op een in-memory DB (slotlimiet, cluster-cap, exits vóór entries, blocklist, kill-switch, time-stop, breakeven shadow/bindend, marktfout); `GuardHarness` uit test_guard.py vervangen door tests tegen de echte `check_exits_fast`. Elke bug uit blok 1 heeft een benoemde test, geverifieerd door de bugs terug te zetten | 152 tests (15 nieuw), coverage engine.py 23% -> 81%, strategy.py 100%, ruff, bandit exit 0 |
| 2026-08-05 | v0.19.0 blok 4 (code-review, analyse zonder codewijziging): twee ontwerpnotities in `docs/`. EV-gate op gerealiseerde trefkans: break-even ligt op 44-52% in plaats van 40%, en de gate is op dit handelsvolume statistisch niet haalbaar als selectiefilter, dus voorstel meter-eerst met backtest-prior en eventueel een gepoolde circuit breaker. Spread als één bron van waarheid: omslagpunt op 0,10%, engine kent de spread niet (lek bij gepinde markten), voorstel `required_edge_pct` met de buffer als vloer plus een spread-map per cyclus; signaleert ook dat het kostenmodel voor live-maker-entries te streng is | n.v.t. (analyse, geen codewijziging) |
| 2026-08-05 | v0.19.0 blok 3 (code-review, config-drift): add-on-schema en `config/config.yaml` gelijkgetrokken (`max_open_positions` van `int(1,5)`/default 3 naar `int(1,10)`/default 10, gelijk aan `lists.MAX_MARKETS`); operationele knoppen ontsloten als add-on-optie (`sizing`, `bucket_eur`, `auto_fill`, `regime_enabled`, `regime_binding`, `llm_veto_binding`, `blocklist` met nieuwe env `TRADEBOT_BLOCKLIST`); exit-parameters bewust in de yaml gehouden; versies gekoppeld (pyproject 0.14.1 -> 0.19.0, add-on 0.18.0 -> 0.19.0) met een CI-test die drift in versie, defaults, schemagrenzen en ENV_MAP laat falen | 162 tests (10 nieuw), ruff, bandit exit 0 |
| 2026-07-05 | Initiële bouw fase 1 compleet | pytest suite, backtest dry-run |
| 2026-07-05 | HA add-on verpakking (HAOS op Pi 5), dashboard ingress-compatibel, CI bouwt add-on image | 30 tests, ruff, YAML-validatie, compile-check |
| 2026-07-05 | CI-fixes (lowercase GHCR-tags addon-job), fastapi 0.139 / starlette >= 1.3.1 (8 CVE's opgelost) | 30 tests, pip-audit schoon |
| 2026-07-05 | v0.2.0: dashboard toont paper portfolio (cash, posities, ongerealiseerde P&L) en echte Bitvavo-balans (read-only); eerste analysecyclus direct bij start | 30 tests, ruff, compile-check |
| 2026-07-05 | Post-mortem oude bot + hergebruik-analyse vastgelegd | n.v.t. (documentatie) |
| 2026-07-05 | v0.3.0: MQTT-publisher met HA discovery (8 sensoren: portfolio, cash, posities, trades, win-rate, P&L, fees, laatste besluit) | 35 tests (5 nieuw), ruff |
| 2026-07-05 | v0.4.0: balans-fix (available + inOrder — available-only toonde alleen niet-in-order kruimels), markttabel met koersen/indicatoren, GUI-opfrissing (aandeel-%, dust-aggregatie, tabular nums, nl-NL formatting) | 35 tests, ruff |
| 2026-07-05 | v0.5.0: correlatie-gate (blokkeert 2e positie bij return-correlatie > 0,85), instap-adviestabel op dashboard (score, fee-gate, correlatie, advies) met watchlist SOL/XRP/LINK (analyse-only) | 39 tests (4 nieuw), ruff |
| 2026-07-05 | v0.6.0: candle-paginatie voor lange backtests (>1440), optimizer CLI met 70/30 train/test-split, dashboard: max drawdown, LLM-veto-rate, equity-grafiek | 43 tests (4 nieuw), ruff |
| 2026-07-05 | v0.6.1: markets en watchlist instelbaar via HA add-on opties (comma-separated, override op config.yaml) | 45 tests (2 nieuw), ruff |
| 2026-07-08 | v0.6.2: fix Supervisor-update-fout (provenance/SBOM-attestations uit in Docker-builds, "unknown/unknown" manifest brak de pull); schedule- en risk-instellingen als HA-opties met schema-grenzen; strategie-parameters bewust niet | 47 tests (2 nieuw), ruff |
| 2026-07-08 | v0.7.0: marktscanner over alle Bitvavo EUR-markten (volume ≥ €250k, spread ≤ 0,6%, score + fee-gate incl. werkelijke spread per markt), dashboard-sectie met 30-min cache. Advies-only by design | 52 tests (5 nieuw), ruff |
| 2026-07-08 | v0.8.0: markten beheren vanuit de GUI (instellingen-sectie met chips, add/remove/promote-knoppen in scanner, DB-override boven HA-opties, direct actief zonder herstart). Vangrails: max 5 trading / 15 watchlist, min 1 trading, marktvalidatie tegen Bitvavo. Scanner toont trechter-statistieken (gescand → liquide → geanalyseerd → getoond) | 58 tests (6 nieuw), ruff |
| 2026-07-08 | v0.9.0: grafiek per markt (koers + EMA-snel/traag + SL/TP/entry-lijnen bij open positie, selector over markets+watchlist), uitklapbare begrippenuitleg (NL) en tooltips op kolomkoppen | 61 tests (3 nieuw), ruff |
| 2026-07-08 | v0.10.0: position guard — SL/TP-bewaking van open posities elke 60s (alleen prijscheck, geen indicatoren/AI). Dicht het gat dat exits alleen bij de uurcyclus werden gecheckt. Hype-detectie/news/sentiment opnieuw beoordeeld en afgewezen (post-mortem); regime-filter genoteerd als fase 3-kandidaat na backtest-bewijs | 64 tests (3 nieuw), ruff |
| 2026-07-08 | v0.11.0: fase 3-fundament — LiveBroker (maker-entries, market-exits, exposure-cap), interlock met bevestigingszin, kill-switch in GUI, mode-scheiding paper/live incl. sqlite-migratie | 74 tests (10 nieuw), ruff |
| 2026-07-08 | v0.11.1: fix CI-fail v0.10.0/v0.11.0 — test-import faalde onder kaal `pytest` (CI) maar niet onder `python -m pytest` (lokaal); tests/__init__.py toegevoegd, lokale verificatie voortaan met exact het CI-commando | 74 tests via `pytest` (CI-identiek), ruff, bandit |
| 2026-07-16 | v0.12.0: veto-analyse — counterfactual per gevetoode buy (voorkwam verlies vs. sneed winst weg) met beide exit-modellen (vaste horizon + ATR-TP/SL, hergebruik van strategie- en fee-logica), richting-check die veto's op "onderste Bollinger-band" flagt (strategie scoort datzelfde signaal juist als koopreden). Nieuwe module `tradebot.analysis.veto`, dashboard-sectie met 30-min cache, CLI `python -m tradebot.analysis.veto` | 88 tests (14 nieuw + 1 live-marker), ruff |
| 2026-07-16 | v0.13.0: LLM-veto shadow-mode. Schakelaar `decision.llm_veto_binding` (plus env `TRADEBOT_LLM_VETO_BINDING` voor HA-optie zonder commit): false betekent dat het veto gelogd wordt maar de koop niet blokkeert, zodat de gate-waarde gemeten wordt zonder trades te kosten. Veto-logica uit de engine getild naar de testbare `apply_second_opinion()`. Aanleiding: v0.12.0-analyse toonde de bindende veto als netto waardevernietigend | 94 tests (6 nieuw), ruff |
| 2026-07-19 | docs: handmatige shadow-veto tracker (`docs/shadow-veto-tracker.xlsx`) toegevoegd. Koppelt elke veto aan uitkomst (TP/SL) en fictieve P&L na fees, meet precisie plus netto euro-impact per veto-reden op de nieuwe config, met 95%-marge zodat n=20 niet als hard bewijs telt. Aanleiding: shadow-veto op ETH bleek een momentum-instap om te keren met mean-reversion-argumenten (Bollinger, 24h-change), dezelfde omgekeerde lezing als in v0.12.0 | n.v.t. (analyse-artefact) |
| 2026-07-20 | v0.14.0: veto-checker in de app. Config-hash per veto (`llm_calls.config_hash`, sqlite-migratie) scheidt configs, zodat precisie schoon op de nieuwe config gemeten wordt los van de vervuilde oude. Echte shadow-trade-uitkomst (round-trip-matching veto->buy->sell binnen 2 candles) naast de bestaande candle-counterfactual. Precisie met 95%-Wilson-marge, uitsplitsing per veto-reden (mean-reversion apart), voortgang naar 20 afgewikkelde trades. Dashboard-sectie uitgebreid, CLI `--all` voor de totaalmeting | 101 tests (7 nieuw), ruff, bandit, pip-audit schoon |
| 2026-07-20 | v0.14.1: fix live-test. `analyze_vetos` koppelde het laden van trades uit de DB aan `candles_by_market` i.p.v. aan of de vetos geinjecteerd waren; de live-test (geinjecteerde vetos, live candles, geen init_db) viel daardoor om met "init_db() not called". Nu haalt de functie trades alleen uit de DB bij een echte run. Regressietest toegevoegd | 102 tests (1 nieuw), ruff, bandit, pip-audit schoon |
| 2026-08-04 | v0.18.0: Fase A2 curatie (na swap met de liquiditeit-fase). Time-stop op stilstaande open posities (pure `strategy.time_stop_hit`, engine-exit-stap): sluit een positie die na `exits.time_stop_candles` (12) geen TP/SL raakte en per saldo op/onder break-even staat (incl. round-trip fees); maakt slot + kapitaal vrij, winnaars blijven staan. Banlijst (`blocklist`): geweerd uit scanner, auto-fill, engine-buy-gate en handmatig toevoegen; open posities exiten nog wel; memecoins per specifieke coin te bannen (geen auto-classificatie). Quiet-vlag (adviserend): `/api/lists` markeert gepinde coins met 0 koopsignalen in `curation.quiet_days` (30) als "overweeg naar watchlist", bot verplaatst niets zelf. Rationale voor de volgorde: time-stop als vangnet vóór actieve dunne-coin-handel (A3/v0.19.0) | 126 tests (6 nieuw), ruff, bandit exit 0 |
| 2026-08-04 | v0.17.0: Fase A1 deployment: auto-fill + cluster-cap correlatie. Auto-fill vult vrije slots met scanner-hits die alle gates halen (pure `scanner.select_auto_fill`, engine `_auto_fill_markets`, config `universe.*`, env TRADEBOT_AUTO_FILL); analyse-set = gepind + open posities + auto-fill (open posities altijd, zodat trend-break-exits blijven draaien ook als een markt uit de scan valt). Cluster-cap: `risk.max_correlated_positions` (K=2) vervangt het harde correlatie-blok (pure `decision.correlated_positions`). Dashboard toont de auto-fill-set (KV last_auto_fill). Guardrail: auto-fill dwingt niets, kandidaten moeten zelfstandig door score, fee-gate, liquiditeit en alle engine-gates | 120 tests (6 nieuw), ruff, bandit exit 0 |
| 2026-08-04 | v0.16.0: kapitaal-schalende positie-slots (bucket-sizing). Nieuwe risk-modus `sizing: bucket` met `bucket_eur` (250): elke positie een vast bedrag, aantal slots = floor(portfolio / bucket), begrensd door `max_open_positions` (nu plafond 10). Zo komt er per EUR250 groei een slot bij (> EUR1250 = 5, > EUR1500 = 6). Legacy `percent`-modus blijft default in code (bestaande tests ongewijzigd), config.yaml staat op bucket. RiskManager: `effective_max_positions()` + capital-aware `can_open`; `params_from_config` (veto/regime-meting) gebruikt bucket-bedrag. Env-overrides TRADEBOT_SIZING/POSITION_BUCKET_EUR. Dashboard: posities-sectie toont "n/max slots". Rationale: groei voegt posities toe (meer spreiding) i.p.v. bestaande posities te vergroten, en schaalt vanzelf terug bij drawdown | 114 tests (4 nieuw), ruff, bandit exit 0 |
| 2026-08-04 | v0.15.2: max_open_positions op 4 gezet (i.p.v. 5), max_position_pct terug naar 25%. 4 x 25% = 100% inzetbaar, exact vullend, met ~EUR262 per positie (betere fee-ratio dan 20%). Martins keuze na afweging 4 vs 5 | 110 tests, ruff, bandit exit 0 |
| 2026-08-04 | v0.15.1: max_open_positions 3->5, met max_position_pct 25->20 als noodzakelijke koppeling (5 x 20% = 100% inzetbaar; bij 25% zou de cash na ~4 posities op zijn en de 5e slot onder het EUR10-minimum vallen). Inzet per positie zakt van ~EUR262 naar ~EUR210, fee-ratio marginaal slechter. Beide zijn HA-optie-overridebaar (TRADEBOT_MAX_OPEN_POSITIONS/MAX_POSITION_PCT). Correlatie-gate (0,85) blijft de facto begrenzer bij sterk gecorreleerde alts | 110 tests, ruff, bandit exit 0 |
| 2026-08-04 | v0.15.0: markt-brede regime-gate (gecodeerd, geen AI) + verbreding naar 10 trading-markten. Aanleiding: de v0.14-analyse toonde de LLM-veto op de echte shadow-uitkomst netto negatief (-€13,23) en richting-technisch tegenstrijdig (mean-reversion tegen een momentum-entry). De LLM heeft geen edge op numerieke TA; een gecodeerd regime-filter vangt "koop niet in een zwakke markt" deterministisch. Nieuwe `regime`-config (enabled/proxy_market/binding, env-overrides TRADEBOT_REGIME_*), `apply_regime_filter` naast `apply_second_opinion` (shadow-semantiek: niet-bindend logt maar blokkeert niet), engine berekent BTC-proxy-regime 1x/cyclus (EMA-snel vs traag, fail-open), gate na correlatie en voor de LLM. Meting: `analysis.regime.analyze_regime` leest de echte round-trip-P&L van regime-down shadow-buys (geen counterfactual nodig want ze executen), symmetrische same-cycle-match, netto gate met 95%-Wilson. Dashboard: aparte "Regime-gate"-sectie + `/api/regime-analysis`. MAX_MARKETS 5->10 (max_open_positions=3 en 25% inzet bewust ongemoeid: bredere kandidatenpool, geen extra gelijktijdige exposure). LLM-veto blijft shadow tot bewijs | 110 tests (8 nieuw), ruff, bandit exit 0. pip-audit in CI (sandbox-netwerk geblokkeerd) |
| 2026-08-04 | v0.14.2: fix koersweergave sub-cent coins. Prijzen werden overal met vaste 2 decimalen getoond, waardoor micro-price markten (PUMP-EUR) als € 0,00 verschenen in markttabel, posities, trades en grafieklijnen (terwijl P&L wel klopte). Nieuwe frontend-formatter `fmtp` schaalt decimalen mee met de magnitude (>=1 twee decimalen, daaronder tot 8 significante). Alleen inline JS in `web.py`, geen backend/analyse geraakt | node-check formatter (9 waarden 55k→sub-cent→null), ruff, bandit, pip-audit schoon |
| 2026-07-20 | v0.13.2: grafiek-assen op dashboard. Zowel equity-verloop als koersgrafiek toonden alleen lijnen zonder assen. Toegevoegd: gedeelde JS-helpers `niceScale` (nette tick-waarden + auto-decimalen, werkt van BTC ~€56k tot sub-cent coins) en `xAxis` (datumlabels). Y-as met gridlines + waarde-labels en x-as met datumlabels op beide grafieken; equity-SVG verhoogd naar 120px voor labelruimte. Alleen inline frontend-JS in `web.py`, geen backend-logica geraakt | node `--check` op gewijzigde JS, niceScale range-check (4 bereiken), `ast.parse` compile-check. Pytest niet in deze omgeving gedraaid (geen backend-wijziging) |
