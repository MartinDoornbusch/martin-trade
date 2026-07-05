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
| `markets` | Comma-separated, bijv. `BTC-EUR,ETH-EUR`. Hierin handelt de bot (paper). Houd deze lijst kort; veel alt-markten was een verliesoorzaak van de oude bot. |
| `watchlist` | Comma-separated. Alleen analyse/instap-advies op het dashboard, de bot handelt hier nooit. Vrij uit te breiden. |
| `bitvavo_api_key/secret` | Voor paper volstaat een key zonder trade-rechten. |
| `groq_api_key` | Primaire LLM (gratis, console.groq.com). Minimaal één LLM-key vereist. |
| `gemini_api_key`, `mistral_api_key` | Optionele fallbacks. |
| `telegram_bot_token/chat_id` | Optioneel, trade-notificaties. |
| `dashboard_token` | Leeg laten bij gebruik via ingress (HA regelt auth). Zetten als je poort 8000 opent. |
| `mqtt_host` | Optioneel. Zet op `core-mosquitto` (met de Mosquitto broker add-on) en de bot verschijnt als apparaat "AI Trade Platform" met 8 sensoren in HA. Leeg = uit. |
| `mqtt_user/password` | HA-gebruiker voor de broker (of aparte broker-login). |

Dashboard: via het zijbalk-icoon (ingress). De database staat op `/data` en overleeft updates.
