# Ontwerpvoorstel: één bron van waarheid voor spread en slippage

Status: **ter beoordeling, niet gebouwd** (code-review-punt 4.2, v0.19.0)

## 1. De discrepantie

Twee plekken rekenen de vereiste edge uit, met verschillende kosten:

```python
# scanner.py, per markt, met de werkelijke spread uit de ticker
required = fee_model.round_trip_pct() + c["spread_pct"] + min_profit

# decision.py, met een vaste buffer
min_edge = self.fees.min_edge_pct(min_profit_pct)
#         = round_trip_pct() + slippage_buffer_pct (0,10) + min_profit
```

Auto-fill-kandidaten worden dus geselecteerd op de ene lat en afgerekend op de
andere.

## 2. Welke kant is strenger

Round-trip 0,50%, winstdrempel 0,50%, buffer 0,10%:

| Spread | Scanner eist | Decision eist | Verschil | Strenger |
|---|---|---|---|---|
| 0,00% | 1,00% | 1,10% | −0,10% | decision |
| 0,02% | 1,02% | 1,10% | −0,08% | decision |
| 0,05% | 1,05% | 1,10% | −0,05% | decision |
| **0,10%** | 1,10% | 1,10% | 0,00% | gelijk |
| 0,15% | 1,15% | 1,10% | +0,05% | scanner |
| 0,20% | 1,20% | 1,10% | +0,10% | scanner |
| 0,40% | 1,40% | 1,10% | +0,30% | scanner |
| 0,60% | 1,60% | 1,10% | +0,50% | scanner |

Het omslagpunt ligt exact op de buffer: 0,10%. De scannerfilter laat spreads toe
tot `MAX_SPREAD_PCT = 0,60%`, dus in de bovenste helft van dat bereik is de
scanner tot een half procentpunt strenger dan de gate die de trade daadwerkelijk
toestaat.

### Wat dat concreet betekent

* **Dunne markten (spread > 0,10%): de scanner is te streng en de engine te ruim.**
  De scanner filtert kandidaten weg die de engine wél zou kopen. Dat kost je alleen
  kansen, geen geld. Maar de keerzijde is er ook: een kandidaat mét spread 0,40%
  die de scanner passeert, wordt in de engine op 1,10% afgerekend terwijl zijn
  werkelijke kosten 0,90% zijn. De marge boven kosten is dan 0,20% in plaats van
  de bedoelde 0,50%.
* **Krappe markten (spread < 0,10%): de engine is te streng.** Op BTC-EUR en
  ETH-EUR, waar de spread eerder 0,02% is, betaal je een buffer die vijf keer de
  werkelijke kost is. Dat weert trades die netto winstgevend zouden zijn.
* **Het echte lek zit bij gepinde markten.** De engine kent de spread helemaal
  niet. Een handmatig toegevoegde of gepinde markt met een brede spread passeert
  de fee-gate op 1,10% zonder dat iemand ooit naar zijn spread heeft gekeken. De
  strengere meting geldt alléén voor auto-fill-kandidaten, terwijl de fees voor
  alles gelijk zijn. Dat is de asymmetrie die opgeruimd moet worden.

## 3. Welke van de twee is inhoudelijk correct

De scanner. In paper (en bij taker-exits live) kruis je de spread bij zowel entry
als exit; de round-trip-spreadkost is dus ongeveer één volle spread. De vaste
0,10% is een benadering daarvan uit fase 1, toen alleen BTC en ETH in beeld waren
en 0,10% ruim genoeg was.

Wel houdt de buffer een functie die de gemeten spread niet heeft: de gemeten
spread is een momentopname en verbreedt juist wanneer je moet uitstappen. De
buffer hoort dus te blijven, maar als **ondergrens**, niet als alternatief.

## 4. Voorstel

Eén pure functie, gebruikt door scanner, decision engine en backtest:

```python
# decision.py
def required_edge_pct(fees: FeeModel, min_profit_pct: float,
                      spread_pct: float | None = None) -> float:
    """Vereiste bruto-move om na kosten de winstdrempel te halen.

    De werkelijke spread van de markt telt als kost; de slippage-buffer is de
    ondergrens, want een gemeten spread is een momentopname en verbreedt precies
    wanneer je moet uitstappen. Zonder meting valt hij terug op de buffer.
    """
    spread = fees.slippage_buffer_pct if spread_pct is None else spread_pct
    return fees.round_trip_pct() + max(spread, fees.slippage_buffer_pct) + min_profit_pct
```

Gevolgen per aanroeper:

| Aanroeper | Nu | Straks |
|---|---|---|
| `scanner.scan` | round_trip + spread + marge | `required_edge_pct(..., spread_pct=c["spread_pct"])`, dus met de buffer als vloer |
| `DecisionEngine.evaluate_buy` | round_trip + 0,10 + marge | zelfde functie, met de spread van de markt als die bekend is |
| `backtest.run_backtest` | `min_edge_pct` | zelfde functie zonder spread (historische spread is niet beschikbaar), expliciet gedocumenteerd als optimistisch |
| `web.advice` | `min_edge_pct` | zelfde functie met spread, zodat het dashboard toont waarop de bot echt afrekent |

### Hoe komt de spread in de engine

Niet met een boek-call per markt per cyclus. `feed.get_ticker_24h()` levert bid en
ask voor alle markten in één call en wordt in de auto-fill-cyclus toch al gedaan.
Voorstel: die tickers één keer per cyclus ophalen, er een `dict[market, spread_pct]`
van maken en die aan `DecisionEngine.evaluate_buy` meegeven. Kosten: nul extra
API-calls als auto-fill aan staat, één call als hij uit staat.

Bij een ontbrekende of onbruikbare spread valt de berekening terug op de buffer.
Dat is fail-open, en dat is hier verdedigbaar omdat het gelijk is aan het huidige
gedrag; fail-closed zou bij één API-hik alle handel stilleggen. Wel loggen.

## 5. Wat dit zichtbaar gaat maken

Op BTC/ETH daalt de vereiste edge van 1,10% naar 1,00% (spread onder de buffer,
dus de vloer bindt) en verandert er feitelijk niets. Op dunne markten stijgt de
eis, en daar zit het punt: de getrapte liquiditeit uit fase A3 (nu v0.20.0)
gebruikt een `1,5x edge-eis` voor de tussenzone. Die vermenigvuldiger en deze
spread-correctie doen deels hetzelfde werk. Als beide landen, moet expliciet
vastliggen of ze stapelen of dat de spread-correctie de multiplier vervangt. Mijn
voorstel: de spread-correctie vervangt hem, want die is gemeten in plaats van
geschat, en stapelen maakt dunne markten onbereikbaar zonder onderbouwing.

## 6. Derde inconsistentie, nu vastleggen voor fase 3

Het kostenmodel klopt niet meer zodra live aan gaat. `LiveBroker` doet maker
entries (0,15%, post-only, kruist de spread niet) en taker exits (0,25%):

| Spread | Kosten paper (taker/taker) | Kosten live (maker in, taker uit) |
|---|---|---|
| 0,05% | 0,55% | 0,43% |
| 0,20% | 0,70% | 0,50% |
| 0,40% | 0,90% | 0,60% |

`round_trip_pct(use_taker=True)` is de default en wordt overal gebruikt, ook in
live. Dat is conservatief (je eist meer edge dan nodig) en dus niet gevaarlijk,
maar het is wel een systematische vertekening: live wordt daardoor strenger
gemeten dan het is, precies in de fase waarin je live tegen paper wil vergelijken.
Voorstel: `required_edge_pct` een `entry_is_maker`-vlag geven die de broker zet,
en de spread dan halveren (alleen de exit kruist). Niet nu bouwen, wel meenemen in
de fase 3-activering, waar het naast de vier bestaande live-blockers hoort.

## 7. Openstaande beslissingen voor Martin

1. Akkoord met de buffer als **vloer** in plaats van als alternatief? Dat maakt de
   engine op BTC/ETH iets ruimer (1,00% i.p.v. 1,10%) en op dunne markten
   strenger.
2. Spread-map per cyclus uit `get_ticker_24h`, of liever helemaal geen spread in
   de engine en in plaats daarvan de scanner naar de vaste buffer terugbrengen?
   Dat laatste is minder werk en consistent, maar dan modelleer je de kosten van
   dunne markten bewust verkeerd.
3. Vervangt de spread-correctie de `1,5x edge-eis` van de getrapte liquiditeit
   (fase A3 / v0.20.0), of stapelen ze?
