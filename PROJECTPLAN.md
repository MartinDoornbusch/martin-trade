# Projectplan: AI Trade Platform

Laatste update: 2026-07-05

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
- [ ] `optimizer` parameter-tuning → kandidaat fase 2, let op overfitting
- [x] `correlation` → herbouwd in v0.5.0 als risk-gate + onderdeel instap-advies
- [x] Afgewezen: market_scanner, news_feed, sentiment, whale_tracker, DCA, house-money (zie post-mortem)

### Fase 2 — Validatie (volgende stap, handmatig af te vinken)
- [ ] API-keys aanmaken (Bitvavo read-only + Groq/Gemini) en `.env` vullen
- [ ] 4-8 weken paper trading draaien
- [ ] Wekelijkse evaluatie: win-rate, netto P&L na fees, max drawdown (dashboard)
- [ ] Backtests op 2+ jaar data per markt
- [ ] Go/no-go criteria vastleggen (voorstel: win-rate > 45% én netto positief na fees over 100+ trades)

### Fase 3 — Live (pas na fase 2)
- [ ] Bitvavo API-key met trade-rechten (géén withdrawal-rechten, IP-whitelist aan)
- [ ] `TRADING_MODE=live` met klein kapitaal (max 10% van portfolio)
- [ ] Maker (limit post-only) orders i.p.v. market orders
- [ ] Kill-switch en daily loss cap monitoring

### Fase 4 — Aandelen
- [ ] Brokerkeuze definitief: Alpaca (US-only, beste API, $0 commissie) vs IBKR (breder, complexere API)
- [ ] `ExchangeAdapter` implementatie voor gekozen broker
- [ ] Markturen-logica (crypto is 24/7, aandelen niet)

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
