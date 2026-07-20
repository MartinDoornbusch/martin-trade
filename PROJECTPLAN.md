# Projectplan: AI Trade Platform

Laatste update: 2026-07-19

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
| 5 | Exits volledig mechanisch | Stop loss / take profit / trend-break zonder LLM. Voorkomt bag-holding door AI-twijfel en bespaart LLM-budget. |
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
- [x] `market_scanner` → herbouwd in v0.7.0, maar advies-only: liquiditeits/spread-filter + score over alle EUR-markten; toevoegen doet de mens, de bot handelt nooit zelf in gescande markten
- [x] Afgewezen: news_feed, sentiment, whale_tracker, DCA, house-money (zie post-mortem)

### Fase 2 — Validatie (loopt)
- [x] API-keys aangemaakt en geconfigureerd (Bitvavo read-only, Groq/Gemini/Mistral, MQTT)
- [x] Tooling: candle-paginatie (2+ jaar data), optimizer, drawdown/veto-metrics op dashboard (v0.6.0)
- [ ] 4-8 weken paper trading draaien (gestart 2026-07-05)
- [ ] Wekelijkse evaluatie: win-rate, netto P&L na fees, max drawdown, LLM-veto-rate (dashboard)
- [ ] Backtests op 2+ jaar data per markt: `python -m tradebot.backtest BTC-EUR --interval 4h --limit 4400`
- [ ] Parameter-tuning: `python -m tradebot.optimizer BTC-EUR --limit 3000` (kies op test-kolom, niet train)
- [x] Tooling LLM-veto-waarde: counterfactual-analyse per veto (voorkwam verlies vs. sneed winst weg), beide exit-modellen, richting-check op veto-redenen — dashboard-sectie + `python -m tradebot.analysis.veto` (v0.12.0)
- [ ] LLM-veto-waarde beoordelen: veto-rate + steekproef veto-redenen vs. koersverloop erna (met bovenstaande tool zodra er veto's zijn)
- [~] Eerste uitkomst v0.12.0-tool (86 veto's): netto gate negatief onder beide modellen (vaste horizon -€32, TP/SL -€272); 67% van de veto's blokkeert op "onderste Bollinger-band" wat de strategie juist als koopreden telt. Veto lijkt waardevernietigend, hypothese: omgekeerde mean-reversion-lezing in de LLM
- [ ] Shadow-mode-experiment (v0.13.0): `llm_veto_binding: false` in paper, 4 weken met-veto vs. zonder-veto vergelijken; daarna prompt fixen of veto schrappen
- [ ] Handmatige shadow-veto tracker (`docs/shadow-veto-tracker.xlsx`): koppelt elke veto aan uitkomst (TP/SL) en fictieve P&L na fees, meet precisie plus netto euro-impact per veto-reden op de nieuwe config over 15-20 afgewikkelde trades, los van de vervuilde meting van de oude config. Beslismetric is euro-impact na fees, niet precisie; 95%-marge staat op het dashboard zodat n=20 niet als hard bewijs telt
- [ ] Go/no-go criteria vastleggen (voorstel: win-rate > 45% én netto positief na fees over 100+ trades)

### Fase 3 — Live (code gebouwd in v0.11.0, activering pas na fase 2 go)
- [x] LiveBroker: maker (limit post-only) entries met fill-polling en timeout-cancel; market exits (kapitaalbescherming boven fee-optimalisatie)
- [x] Hard exposure-plafond (`live_max_capital_eur`, default €100) los van de rekeningbalans
- [x] Dubbel slot: `trading_mode=live` én letterlijke bevestigingszin "IK BEGRIJP DAT DIT ECHT GELD IS" in `live_confirm`, anders weigert de bot te starten
- [x] Kill-switch: pauzeknop in dashboard stopt alle aankopen (paper én live); exits en guard lopen altijd door
- [x] Mode-scheiding: posities/trades/stats gelabeld paper|live, historie vermengt nooit
- [ ] ACTIVERING (handmatig, pas na fase 2 go): Bitvavo API-key met trade-rechten (géén withdrawal, IP-whitelist), `trading_mode=live` + bevestigingszin invullen, klein kapitaal

### Fase 4 — Aandelen
- [ ] Brokerkeuze definitief: Alpaca (US-only, beste API, $0 commissie) vs IBKR (breder, complexere API)
- [ ] `ExchangeAdapter` implementatie voor gekozen broker
- [ ] Markturen-logica (crypto is 24/7, aandelen niet)

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
| 2026-07-20 | v0.13.2: grafiek-assen op dashboard. Zowel equity-verloop als koersgrafiek toonden alleen lijnen zonder assen. Toegevoegd: gedeelde JS-helpers `niceScale` (nette tick-waarden + auto-decimalen, werkt van BTC ~€56k tot sub-cent coins) en `xAxis` (datumlabels). Y-as met gridlines + waarde-labels en x-as met datumlabels op beide grafieken; equity-SVG verhoogd naar 120px voor labelruimte. Alleen inline frontend-JS in `web.py`, geen backend-logica geraakt | node `--check` op gewijzigde JS, niceScale range-check (4 bereiken), `ast.parse` compile-check. Pytest niet in deze omgeving gedraaid (geen backend-wijziging) |
