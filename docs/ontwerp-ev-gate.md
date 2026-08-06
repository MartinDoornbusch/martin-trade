# Ontwerpvoorstel: EV-gate op gerealiseerde trefkans

Status: **ter beoordeling, niet gebouwd** (code-review-punt 4.1, v0.19.0)

## 1. Het probleem met de huidige fee-gate

`DecisionEngine.evaluate_buy` toetst:

```
verwachte move = 3 x ATR / prijs           (2 x ATR stop, RR 1,5)
vereist        = 0,50% fees + 0,10% slippage + 0,50% winstdrempel = 1,10%
```

De gate bindt dus pas als `ATR / prijs < 0,367%`. Op 4h-crypto ligt ATR doorgaans
tussen 1% en 3% van de prijs, dus de gate is in de praktijk vrijwel altijd waar.
Hij toetst bovendien iets anders dan hij suggereert: hij vraagt of het TP-niveau
ver genoeg weg ligt, niet of de trade in verwachting geld oplevert.

### Wat break-even werkelijk vraagt

Met stop 2 x ATR, target 3 x ATR, kosten `c` (round-trip fee + spread) en
`α = ATR / prijs` geldt per trade:

```
EV = p · 3α − (1 − p) · 2α − c = 5pα − 2α − c
p* = (2α + c) / (5α)
```

Met `c = 0,60%`:

| ATR/prijs | break-even trefkans | EV bij 45% | EV bij 50% |
|---|---|---|---|
| 0,50% | 64,0% | −0,48% | −0,35% |
| 1,00% | 52,0% | −0,35% | −0,10% |
| 1,50% | 48,0% | −0,22% | +0,15% |
| 2,00% | 46,0% | −0,10% | +0,40% |
| 3,00% | 44,0% | +0,15% | +0,90% |

De 40% uit de review is de asymptoot zonder kosten. Realistisch ligt de lat op
44-52%. Twee consequenties die los van dit ontwerp al gelden:

1. Het fase 2 go/no-go-criterium "win-rate > 45%" is **niet voldoende**. Bij ATR
   van 1% is 45% trefkans nog steeds verliesgevend (−0,35% per trade). Het
   tweede deel van het criterium, "netto positief na fees over 100+ trades", is
   de enige harde toets; de win-rate-drempel is hooguit een indicator.
2. De oude bot op 27% zat niet marginaal fout maar structureel: bij ATR 2% is de
   EV daar −1,25% per trade, precies het gat dat de −15% verklaart.

## 2. Voorstel op hoofdlijnen

Meet de gerealiseerde EV per trade uit de eigen round-trip-historie en gebruik
die als extra gate. Niet als vervanging van de fee-gate: die twee doen ander werk
(zie §6).

### 2.1 Schatter

Niet de trefkans schatten en dan EV afleiden, maar **de EV direct schatten** als
het gemiddelde gerealiseerde nettorendement per afgewikkelde trade:

```
r_i   = pnl_eur_i / kostenbasis_i          (netto, fees zitten al in pnl_eur)
EV    = gemiddelde(r_i)
```

De trefkans blijft wel meelopen als diagnostiek (waarom is de EV wat hij is),
maar niet als grondslag voor de gate. Reden: de uitkomstverdeling is niet
binomiaal meer. Met de time-stop (v0.18.0) en de breakeven-stop (v0.19.0) zijn er
vier exit-typen, en een `p`-model dat alleen TP en SL kent, meet de verkeerde
grootheid. Het gemiddelde gerealiseerde rendement heeft dat probleem niet.

### 2.2 Groepering en venster

| Keuze | Voorstel | Reden |
|---|---|---|
| Groepen | hiërarchie: markt → regime-bucket (BTC-proxy up/down) → globaal | de meest specifieke groep die genoeg data heeft wint, anders val je een niveau terug |
| Venster | laatste 50 afgewikkelde round-trips per groep, maximaal 180 dagen oud | crypto-regimes verschuiven; een edge van twee jaar geleden is geen bewijs voor vandaag |
| Config-scoping | alleen trades met de huidige `config_fingerprint` | bestaat al voor de veto-meting (`llm_calls.config_hash`); zonder dit meet je de edge van een strategie die je niet meer draait |
| Onzekerheid | bootstrap-percentiel-interval over `r_i` (numpy is al een dependency) | de verdeling is bimodaal en scheef, een t-interval past er slecht op |

Implementatienoot: `TradeRow` heeft nog geen `config_hash`. Toevoegen kan met de
bestaande mini-migratiepatroon in `db.init_db` (`ALTER TABLE ... ADD COLUMN`),
net als bij `llm_calls.config_hash` in v0.14.0.

### 2.3 Bindingsregels

Zelfde ontwerp als de regime-gate en het LLM-veto, want dat is de vaste regel in
dit project:

* `ev_gate.enabled` / `ev_gate.binding`, default `binding: false` (shadow);
* onder `n_min` afgewikkelde trades bindt de gate niet (fail-open). Een gate die
  op ruis bindt is slechter dan geen gate;
* bindend maken pas bij een positieve netto gate-waarde, gemeten met dezelfde
  methode als `analyze_regime`.

### 2.4 De absorberende toestand, en waarom hij zichzelf oplost

Een EV-gate per markt heeft een gevaarlijke eigenschap: blokkeer je een markt na
een verliesreeks, dan komen er geen nieuwe trades meer, dus kan de schatting nooit
herstellen. De markt zit permanent op slot op basis van een steekproef van 20.

Het tijdvenster lost dat vanzelf op: zonder nieuwe trades veroudert de steekproef,
valt onder `n_min` en gaat de gate fail-open. De markt krijgt dan automatisch een
nieuwe kans. Dat gedrag moet expliciet in de tests staan, anders sloopt iemand het
later per ongeluk door het venster te verwijderen.

## 3. Het bezwaar dat het ontwerp bijna omver duwt: statistische haalbaarheid

Dit is de reden dat ik het voorstel **niet** zou bouwen zoals de review het
formuleert.

De spreiding van `r_i` is groot omdat de uitkomsten bimodaal zijn (ongeveer
+3α−c of −2α−c). Benodigde steekproef om een EV-verschil aan te tonen
(tweezijdig 5%, power 80%):

| ATR/prijs | σ per trade | n voor 0,5%-punt | n voor 1%-punt | kleinst detecteerbaar bij n=20 |
|---|---|---|---|---|
| 1% | 2,49% | 194 | 49 | 1,56% |
| 2% | 4,97% | 776 | 194 | 3,11% |
| 3% | 7,46% | 1.746 | 437 | 4,67% |

Leg daarnaast het handelsvolume: de backtest van 2026-07-18 gaf 11-24 trades per
markt per half jaar. Met 3-4 slots levert dat grofweg 40-80 afgewikkelde trades
per jaar over de hele portefeuille.

Daaruit volgt hard:

* **per markt gaten is onhaalbaar.** 50 trades in één markt kost jaren.
* **op 20 trades bindt de gate op ruis.** Bij ATR 2% kan hij alleen een EV
  detecteren die slechter is dan ongeveer −3% per trade. Dat is geen selectie,
  dat is een ramp-detector.
* de drempel van 20 trades die voor het LLM-veto en de regime-gate geldt, is voor
  díe gates verdedigbaar (je meet daar een verschil tussen twee groepen op
  dezelfde markt), maar niet voor een absolute EV-schatting.

## 4. Aangepast voorstel

Drie stappen, in deze volgorde.

**Stap 1 — bouw het als meter, niet als gate.**
`analysis/ev.py` met een pure `estimate_ev(trades, window, min_n)` en een
dashboardkaart naast de regime- en veto-kaart: EV per groep, trefkans, aantal
afgewikkelde trades, bootstrap-interval, en de break-even-trefkans uit §1 bij de
actuele ATR. Dat laatste is nu al het meest bruikbare getal: het maakt zichtbaar
of de strategie überhaupt in de buurt van break-even opereert. Geen gate, geen
shadow-logging, geen extra pad in de engine.

**Stap 2 — vul de steekproef met backtest-data.**
`run_backtest` levert per markt over 2 jaar 4h-candles honderden round-trips met
exact dezelfde strategie- en fee-logica. Dat is de enige bron die binnen afzienbare
tijd genoeg n heeft. Gebruik die als prior en de live-trades als update
(shrinkage naar de backtest-schatting, gewicht `n_live / (n_live + k)`). Expliciet
benoemen dat dit overfit-risico draagt: de backtest kent de periode al, dus
gebruik de test-helft van de train/test-split, niet de train-helft.

**Stap 3 — als hij ooit bindt, dan als circuit breaker, niet als selectiefilter.**
Bindend op het gepoolde niveau (alle markten samen, eventueel per regime-bucket),
met een ruime drempel: blokkeer nieuwe entries pas als de bovengrens van het
bootstrap-interval onder nul ligt, dus alleen bij duidelijk bewijs van
verliesgevendheid. Dat is een andere functie dan "kies de beste markt" en het is
de enige functie waarvoor de data toereikend is. Merk op dat `daily_loss_cap_pct`
al een grove circuit breaker is; de EV-gate zou de trage variant daarvan zijn.

## 5. Verhouding tot de lopende regime- versus LLM-veto-vergelijking

Drie punten die het ontwerp raken.

1. **Niet nu bouwen.** Er staan al twee gates in shadow op een steekproef van
   ongeveer 11-20 trades. Een derde shadow-gate erbij maakt de toewijzing van
   waarde onmogelijk: dezelfde geblokkeerde koop telt dan bij meerdere gates mee
   en de "netto gate"-getallen zijn niet meer optelbaar. Eerst de regime/LLM-
   verdict afmaken.
2. **De EV-gate per regime-bucket overlapt met de regime-gate.** Als blijkt dat de
   EV in regime-down negatief is, blokkeert de EV-gate precies de trades die de
   regime-gate ook blokkeert, maar op gemeten bewijs in plaats van op een aanname.
   In die zin is dit de opvolger van de winnaar van die vergelijking, niet een
   derde speler ernaast.
3. **Meet overlap expliciet.** Zodra er twee of meer gates in shadow staan, hoort
   het dashboard te rapporteren hoeveel geblokkeerde koopkandidaten door meer dan
   één gate geraakt worden. Zonder dat getal is elke "netto gate €"-optelling
   misleidend.

## 6. Verhouding tot de bestaande fee-gate

De fee-gate blijft, hij doet ander werk:

| | fee-gate | EV-gate |
|---|---|---|
| Vraag | kan deze trade zijn kosten überhaupt terugverdienen? | heeft dit soort trade dat feitelijk gedaan? |
| Bron | huidige ATR en prijs | historie van afgewikkelde trades |
| Beschikbaar | altijd, ook bij nul historie | pas na tientallen trades |
| Bindt bij | lage volatiliteit | aangetoonde verliesgevendheid |

Ze zijn allebei een noodzakelijke voorwaarde. Volgorde: fee-gate eerst (gratis,
geen data nodig), EV-gate daarna, beide vóór de LLM.

Wel hoort de fee-gate zelf herzien te worden, maar om een andere reden dan 4.1:
hij rekent met een vaste slippage-buffer in plaats van de werkelijke spread. Zie
`docs/ontwerp-spread-bron-van-waarheid.md` (review-punt 4.2). Een zinvoller
strengheidsknop is bovendien een multiplicatieve eis (`verwachte move ≥ k × kosten`)
in plaats van de huidige optelling met een vaste marge; dan schaalt de eis mee met
de kosten in plaats van er los van te staan.

## 7. Openstaande beslissingen voor Martin

1. Akkoord met "meter eerst, gate later"? Zo niet, welke n_min accepteer je,
   wetende dat 20 trades alleen een EV slechter dan ongeveer −3% per trade kan
   aantonen?
2. Backtest als prior gebruiken, of alleen live-data en accepteren dat de gate
   pas over jaren bindt?
3. Moet het go/no-go-criterium "win-rate > 45%" vervangen worden door een
   ATR-bewuste variant, of laten we het staan als indicator naast "netto positief
   na fees"?

---

## Besluit van Martin (2026-08-06): goedgekeurd als ontwerp, niet bouwen

Niet omdat het ontwerp slecht is, maar omdat de kalibratie het heeft ingehaald.

- **Stap 2 (backtest als prior).** Die prior is inmiddels gemeten in plaats van geschat, en
  hij is negatief. De shrinkage-machinerie bouwen zou met veel moeite ontdekken wat er al
  staat: de EV per trade is niet positief, en de alfa van de beste van 324 varianten is
  +1,17 en -1,82 punt.
- **Stap 3 (bindend bij bovengrens onder nul).** Die voorwaarde is op strategieniveau al
  vervuld, vóór de eerste live trade. Een circuit breaker die bij inschakeling meteen alles
  blokkeert is geen gate maar een conclusie.
- **Stap 1 (de meter).** Blijft verdedigbaar, maar zijn waarde is verschoven: hij moest
  zichtbaar maken of je in de buurt van break-even opereert, en dat weet je nu offline. Wat
  overblijft is monitoring van de paper-run tegen het backtest-verdict.

Daarbovenop zou het de vijfde gate zijn op een signaal met negatieve alfa.

### Wat er wél uit is gehaald

De formule uit §1 is het bruikbaarste onderdeel van dit document en heeft geen historie
nodig: `p*` volgt uit ATR, de niveaus en de kosten. `decision.breakeven_win_rate` vervangt
sinds v0.20.0 de optellende fee-gate (plafond `decision.max_breakeven_win_rate`, 0,50) en
`p*` staat bij elke kandidaat op het dashboard.

Daarmee verandert de vraag van "ligt het doel 1,1% weg", waarop het antwoord in twee jaar
backtest 100% van de tijd ja was, in "geloven we dat we op deze setup 48% halen". Dat is de
eerlijke versie van kernprincipe 2, en het is ongeveer tien regels code.

### Correctie die uit dit document volgde

De 40% break-even die in dit project herhaaldelijk als lat is gebruikt, is de KOSTENLOZE
asymptoot. Met `c = 0,60%` ligt de werkelijke lat op 44 tot 52%. Elke eerdere vergelijking
("27,2% tegen een break-even van 40%") onderschatte het gat dus. Het go/no-go-criterium in
PROJECTPLAN is daarop aangepast.
