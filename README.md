# AI Trade Platform

Fee-bewust, AI-ondersteund swing-tradingplatform voor Bitvavo (crypto nu, aandelen later via broker-abstractie). Zie `PROJECTPLAN.md` voor roadmap en architectuurbeslissingen.

## Kernontwerp

- **Deterministische strategie** (EMA/RSI/MACD/ATR/Bollinger) genereert kandidaat-signalen; geen AI voor zaken die code beter kan.
- **Fee-gate**: kopen alleen als verwachte beweging ≥ round-trip fees + slippage + winstdrempel (default ≥ 1,10%).
- **LLM second opinion** (Groq → Gemini → Mistral, gratis tiers met dagbudget) mag alleen vetoën, nooit initiëren. Exits zijn 100% mechanisch.
- **Paper trading** met echte marktdata en echte fee-percentages. Live modus zit bewust achter een slot tot fase 2 is afgerond.

## Lokaal draaien

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                  # vul minimaal 1 LLM-key in
PYTHONPATH=src python -m tradebot.main
```

Dashboard: http://localhost:8000/?token=<DASHBOARD_TOKEN>

### Tests en checks

```bash
PYTHONPATH=src pytest -q
ruff check src tests
bandit -r src -ll
```

### Backtest

```bash
PYTHONPATH=src python -m tradebot.backtest BTC-EUR --interval 4h --limit 1000
```

## Deployment op Raspberry Pi (aanbevolen)

Vereisten: Pi 4/5 met 64-bit Raspberry Pi OS, Docker + Compose plugin.

```bash
# 1. Docker installeren (eenmalig)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # daarna opnieuw inloggen

# 2. Repo klonen en configureren
git clone <jouw-repo-url> tradebot && cd tradebot
cp .env.example .env && nano .env    # keys invullen, DASHBOARD_TOKEN zetten

# 3. Starten
docker compose up -d --build
docker compose logs -f
```

### Updates via CI/CD (DevSecOps-flow)

1. Wijziging in branch → PR → CI draait ruff, pytest, bandit, pip-audit.
2. Merge naar `main` → CI bouwt multi-arch image en pusht naar GHCR.
3. Op de Pi: `docker compose pull && docker compose up -d` (of automatisch met [Watchtower](https://containrrr.dev/watchtower/)).

### Security-checklist

- [ ] Bitvavo API-key: **alleen view-rechten** in paper-fase; nooit withdrawal-rechten; IP-whitelist op het Pi-adres.
- [ ] `.env` staat in `.gitignore`; secrets nooit committen. In GitHub Actions via repository secrets.
- [ ] Dashboard-poort bindt op 127.0.0.1; externe toegang alleen via reverse proxy met TLS (bijv. Caddy) of Tailscale/WireGuard (aanbevolen: Tailscale, dan geen open poorten).
- [ ] `DASHBOARD_TOKEN` gezet.
- [ ] Container draait als non-root user; `restart: unless-stopped`.
- [ ] Pi: `sudo apt install unattended-upgrades` voor automatische security-patches.

## Structuur

```
src/tradebot/
  config.py      .env + config.yaml
  exchange.py    ExchangeAdapter ABC + BitvavoClient (HMAC, rate limits, operatorId)
  indicators.py  EMA, RSI, MACD, ATR, Bollinger (puur, testbaar)
  strategy.py    kandidaat-signalen + mechanische exits
  decision.py    FeeModel, RiskManager, DecisionEngine (fee-gate)
  llm.py         Groq/Gemini/Mistral router, second opinion met veto
  paper.py       paper broker met echte fees
  engine.py      orchestratie van één analysecyclus
  web.py         FastAPI dashboard + JSON API
  backtest.py    backtester met zelfde strategie + fee-model
  main.py        scheduler + webserver
```
