# AI Trade Platform add-on

Fee-bewust AI swing-tradingplatform (Bitvavo, paper trading). Volledige documentatie: zie de repo-README en PROJECTPLAN.md.

## Installatie

### Optie A — Local add-on (handmatig, direct werkend)

1. Installeer de "Samba share" of "SSH & Web Terminal" add-on in Home Assistant.
2. Draai eenmalig `./sync.sh` in deze map (kopieert `src/`, `config/`, `requirements.txt` hierheen).
3. Kopieer deze hele map naar `/addons/tradebot` op de Pi.
4. Instellingen → Add-ons → Add-on Store → menu (⋮) → "Check for updates" → "AI Trade Platform" verschijnt onder Local add-ons.
5. Installeren, daarna bij Configuratie de API-keys invullen, starten.

### Optie B — Add-on repository (automatische updates via GitHub)

1. Push deze repo naar GitHub; CI bouwt het add-on image naar GHCR.
2. Zet in `config.yaml` de `image:` regel aan met jouw GitHub-gebruikersnaam.
3. Add-on Store → menu (⋮) → Repositories → voeg de GitHub-URL toe.
4. Zet "Auto-update" aan op de add-on: elke versie-bump in `config.yaml` op main wordt dan automatisch uitgerold.

## Configuratie

| Optie | Uitleg |
|---|---|
| `trading_mode` | `paper` (default). `live` is vergrendeld tot fase 2 is afgerond. |
| `markets` | Startwaarde; sinds v0.8.0 beheer je de lijsten in het dashboard zelf (sectie "Instellingen — markten"). Zodra je daar iets wijzigt, geldt de GUI-lijst en niet meer deze optie. |
| `watchlist` | Zie `markets`: startwaarde, GUI-beheer heeft voorrang. |
| `blocklist` | Comma-gescheiden markten die de bot nooit verhandelt (scanner, auto-fill, koopgate en handmatig toevoegen). Open posities worden nog wel netjes gesloten. Leeg = de banlijst uit `config/config.yaml`. |
| `analysis_interval_minutes` | Hoe vaak de bot analyseert (15-1440, default 60). Vaker = meer API/LLM-verbruik, niet meer rendement. |
| `candle_interval` | Candle-granulariteit voor de analyse (1h t/m 1d, default 4h). Korter = meer signalen én meer fee-druk. |
| `sizing` | `bucket` (default): vast bedrag per positie, aantal slots schaalt met kapitaal. `percent`: vast % van portfolio (legacy). |
| `bucket_eur` | Bucket-modus: bedrag per positie én kapitaal per extra slot (10-1000, default 250). |
| `max_position_pct` | Percent-modus: max % van portfolio per positie (5-50, default 25). |
| `max_open_positions` | Plafond op gelijktijdige posities (1-10, default 10). In bucket-modus de bovengrens; het werkelijke aantal slots volgt uit portfolio / `bucket_eur`. |
| `cooldown_hours` | Wachttijd per markt na een trade (1-72, default 12). Verlagen vergroot het flip-flop/fee-risico. |
| `auto_fill` | Vrije slots aanvullen met scanner-kandidaten die alle gates halen (default aan). Uit = alleen gepinde markten en open posities. |
| `regime_enabled` | Markt-brede regime-gate op de BTC-proxy aan/uit (default aan). |
| `regime_binding` | `false` (default) = shadow: de gate wordt gemeten maar blokkeert niets. Pas op `true` zetten bij een positieve netto gate over ≥ 20 afgewikkelde trades. |
| `llm_veto_binding` | Idem voor het LLM-veto. Staat bewust op `false`: de meting tot nu toe is netto negatief. |
| `live_confirm` | Fase 3. Leeg = live onmogelijk. Alleen met de exacte zin "IK BEGRIJP DAT DIT ECHT GELD IS" start de bot in live mode. Niet invullen vóór de fase 2 go/no-go. |
| `live_max_capital_eur` | Fase 3. Hard plafond op live-inleg (10-1000, default 100), los van je rekeningbalans. |
| `bitvavo_api_key/secret` | Voor paper volstaat een key zonder trade-rechten. |
| `groq_api_key` | Primaire LLM (gratis, console.groq.com). Minimaal één LLM-key vereist. |
| `gemini_api_key`, `mistral_api_key` | Optionele fallbacks. |
| `telegram_bot_token/chat_id` | Optioneel, trade-notificaties. |
| `dashboard_token` | Leeg laten bij gebruik via ingress (HA regelt auth). Zetten als je poort 8000 opent. |
| `mqtt_host` | Optioneel. Zet op `core-mosquitto` (met de Mosquitto broker add-on) en de bot verschijnt als apparaat "AI Trade Platform" met 8 sensoren in HA. Leeg = uit. |
| `mqtt_user/password` | HA-gebruiker voor de broker (of aparte broker-login). |

Strategie-parameters (EMA-periodes, RSI-koopzone, signaalscore, ATR/reward-risk, fee-gate-drempels, exit-parameters zoals de time-stop en de breakeven-stop) zijn bewust géén opties: die wijzig je op basis van optimizer/backtest-bewijs via een commit, niet op gevoel via de UI. De vuistregel: een knop hoort hier alleen als omzetten een bedrijfsvoeringsbeslissing is die geen deploy mag vergen én de gemeten strategie niet verandert.

Let op: elke ingevulde optie wordt als env-var gezet en overschrijft `config/config.yaml`. De defaults hierboven zijn daarom gelijk aan die yaml; een test in CI (`tests/test_addon_config.py`) laat de build falen zodra ze uit elkaar lopen.

Dashboard: via het zijbalk-icoon (ingress). De database staat op `/data` en overleeft updates.
