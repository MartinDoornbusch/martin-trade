"""Orchestrator: runs one full analysis/trade cycle across all configured markets."""
from __future__ import annotations

import logging
from dataclasses import replace

from .config import SHADOW_GATES, AppConfig, Secrets, gate_fingerprint
from .db import KVRow, SignalRow, session
from .decision import (
    Decision,
    DecisionEngine,
    FeeModel,
    RiskManager,
    apply_chase_guard,
    apply_regime_filter,
    apply_second_opinion,
    breakeven_offset_pct,
    correlated_positions,
)
from .exchange import BitvavoClient
from .lists import get_lists, is_paused
from .live import LiveBroker
from .llm import LLMRouter, build_router
from .notify import Notifier
from .paper import PaperBroker
from .scanner import scan, select_auto_fill
from .strategy import (
    breakeven_stop_hit,
    build_snapshot,
    chase_too_far,
    check_exit,
    drop_unclosed,
    evaluate_buy,
    time_stop_hit,
)

log = logging.getLogger(__name__)

LIVE_CONFIRM_PHRASE = "IK BEGRIJP DAT DIT ECHT GELD IS"

# Onder dit aantal candles is een snapshot niet betrouwbaar (Bollinger 20,
# MACD-slow 26 + signaal 9). Valt de afgeknipte reeks eronder, dan valt de
# entry-beoordeling terug op de volledige reeks in plaats van op ruis.
MIN_SNAPSHOT_CANDLES = 40


class TradingCycle:
    def __init__(self, cfg: AppConfig, secrets: Secrets):
        self.cfg = cfg
        self.secrets = secrets
        self.feed = BitvavoClient(secrets.bitvavo_api_key, secrets.bitvavo_api_secret,
                                  cfg.fees["maker_pct"], cfg.fees["taker_pct"])
        self.fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                                  cfg.fees["slippage_buffer_pct"])
        if secrets.trading_mode == "live":
            # Dubbel slot: mode=live is niet genoeg, de bevestigingszin moet exact kloppen.
            if secrets.live_confirm.strip() != LIVE_CONFIRM_PHRASE:
                raise RuntimeError(
                    "Live mode geweigerd: zet de add-on optie live_confirm exact op "
                    f"'{LIVE_CONFIRM_PHRASE}'. Zie PROJECTPLAN fase 2 go/no-go criteria "
                    "voordat je dit doet.")
            self.broker = LiveBroker(self.feed, self.fee_model,
                                     secrets.live_max_capital_eur)
            log.warning("LIVE MODE ACTIEF — exposure-plafond %.2f EUR",
                        secrets.live_max_capital_eur)
        else:
            self.broker = PaperBroker(self.feed, self.fee_model, cfg.risk["paper_start_eur"])
        self.decider = DecisionEngine(self.fee_model, RiskManager(cfg.risk), cfg.decision)
        self.llm: LLMRouter = build_router(cfg.llm_providers, secrets,
                                           int(cfg.llm.get("timeout_seconds", 20)))
        self.notify = Notifier(secrets.telegram_bot_token, secrets.telegram_chat_id)

    def run_once(self) -> list[Decision]:
        decisions: list[Decision] = []
        interval = self.cfg.schedule["candle_interval"]
        limit = int(self.cfg.schedule["candle_limit"])
        # In-cycle state. `positions`, `free` en `daily_pnl` worden na elke uitgevoerde
        # order ververst (zie _refresh_after_trade): binnen één cyclus moeten de
        # slotlimiet en de correlatie-cluster-cap de posities meetellen die in
        # diezelfde cyclus zijn geopend. `portfolio` blijft bewust cyclus-vast:
        # het herberekenen kost een prijs-API-call per open positie (rate limit), en
        # binnen één cyclus verschuift de portefeuillewaarde alleen met de betaalde
        # fees en de zojuist gerealiseerde P&L. Dat is te klein om het aantal
        # bucket-slots te veranderen, behalve pal op een bucketgrens; daar wint
        # stabiliteit (alle markten in één cyclus worden aan dezelfde limiet getoetst)
        # van precisie. De volgende cyclus (max 1 uur later) leest de waarde vers.
        positions = self.broker.open_positions()
        portfolio = self.broker.portfolio_value_eur()
        free = self.broker.cash_eur()
        daily_pnl = self.broker.daily_pnl_eur()

        blocklist = {m.upper() for m in (getattr(self.cfg, "blocklist", []) or [])}
        pinned_markets = get_lists(self.cfg)["markets"]
        open_markets = [p.market for p in positions]
        auto_markets = self._auto_fill_markets(pinned_markets, open_markets, positions,
                                               portfolio, blocklist)
        self._store_auto_fill(auto_markets)
        # Analyse-set: gepind + open posities (zodat exits altijd draaien) + auto-fill,
        # ontdubbeld met behoud van volgorde (gepind eerst).
        analysis_markets = list(dict.fromkeys(pinned_markets + open_markets + auto_markets))
        candles_map = {}
        for market in analysis_markets:
            try:
                candles_map[market] = self.feed.get_candles(market, interval, limit)
            except Exception:  # noqa: BLE001
                log.exception("candles ophalen mislukt voor %s", market)

        regime_cfg = self.cfg.regime or {}
        regime_enabled = bool(regime_cfg.get("enabled", False))
        proxy_market = str(regime_cfg.get("proxy_market", "BTC-EUR"))
        regime_binding = bool(regime_cfg.get("binding", False))
        regime_ok = True
        if regime_enabled:
            regime_ok = self._proxy_regime_ok(proxy_market, candles_map, interval, limit)

        for market, candles in candles_map.items():
            try:
                snap = build_snapshot(market, candles, self.cfg.strategy)

                # 1) Mechanical exits first — no AI involved.
                pos = next((p for p in positions if p.market == market), None)
                if pos:
                    should_exit, why = check_exit(pos.entry_price, pos.stop_loss,
                                                  pos.take_profit, snap)
                    if not should_exit:
                        should_exit, why = self._extra_exits(pos, candles, snap)
                    if should_exit:
                        self.broker.sell(market, why)
                        positions, free, daily_pnl = self._refresh_after_trade()
                        self._log_signal(market, "sell", "executed", 0, why, {})
                        self.notify.send(f"🔴 SELL {market} @ {snap.price:.2f}: {why}")
                        decisions.append(Decision(market, "sell", why))
                        continue

                # 2) Candidate generation (deterministic) op afgesloten candles.
                entry_snap = self._entry_snapshot(market, candles, snap)
                candidate = evaluate_buy(entry_snap, self.cfg.strategy)
                candidate.snapshot = self._anchor_price(entry_snap, snap)
                decision = self.decider.evaluate_buy(candidate, positions,
                                                     self.broker.last_trade_at(market),
                                                     portfolio, free, daily_pnl)

                # 2b) Banlijst: nooit kopen in een geweerde markt (exits hierboven
                # lopen wel door, zodat een reeds open positie netjes gesloten wordt).
                if decision.action == "buy" and market in blocklist:
                    decision = Decision(market, "skip", "blocklist: markt geweerd")

                # 3a) Kill-switch: gebruiker heeft kopen gepauzeerd (exits lopen door).
                if decision.action == "buy" and is_paused():
                    decision = Decision(market, "skip",
                                        "kill-switch: kopen gepauzeerd door gebruiker")

                # 3) Correlatie-gate met cluster-cap: max K posities in één
                # correlatie-cluster (K-1 gecorreleerde open posities toegestaan,
                # de K-de wordt geweigerd). Voorkomt zowel schijndiversificatie als
                # het doodblokkeren van een gecorreleerd universum.
                if decision.action == "buy" and positions:
                    max_corr = float(self.cfg.risk.get("max_correlation", 0.85))
                    lookback = int(self.cfg.risk.get("correlation_lookback", 60))
                    max_cluster = int(self.cfg.risk.get("max_correlated_positions", 2))
                    others = {p.market: [c.close for c in candles_map[p.market]]
                              for p in positions if p.market in candles_map}
                    correlated = correlated_positions([c.close for c in candles], others,
                                                      max_corr, lookback)
                    if len(correlated) >= max_cluster:
                        names = ", ".join(f"{m} {c:.2f}" for m, c in correlated)
                        decision = Decision(
                            market, "skip",
                            f"correlatie-gate: clustermax {max_cluster} bereikt, "
                            f"{len(correlated)} gecorreleerde posities (>{max_corr}): {names}")

                # 3c) Regime-gate (gecodeerd, markt-breed): geen nieuwe entries
                # als de proxy-markt (BTC) risk-off staat. Shadow tenzij binding.
                if decision.action == "buy" and regime_enabled:
                    decision = apply_regime_filter(decision, regime_ok, proxy_market,
                                                   regime_binding)

                # 3d) Chase-guard (gecodeerd): het signaal komt van de afgesloten
                # candle, de fill van de live prijs. Is de koers daartussen te ver
                # doorgelopen, dan klopt het risico nog wel (stop en target hangen
                # aan de fill) maar de edge niet: een deel van de verwachte
                # beweging is al gemaakt. Shadow tenzij binding.
                if decision.action == "buy" and entry_snap is not snap:
                    hit, why = chase_too_far(
                        entry_snap.price, snap.price, entry_snap.atr,
                        float(self.cfg.strategy.get("max_chase_atr", 0.0) or 0.0))
                    decision = apply_chase_guard(
                        decision, hit, why,
                        bool(self.cfg.strategy.get("chase_guard_binding", False)))

                # 4) LLM second opinion only for BUYs that passed every gate.
                # De LLM-laag logt het oordeel altijd (llm_calls), ook in shadow-mode.
                # llm_veto_binding=false laat het veto los: gelogd maar niet-bindend.
                if decision.action == "buy" and self.cfg.decision.get("use_llm_second_opinion"):
                    verdict = self.llm.second_opinion(candidate)
                    min_conf = float(self.cfg.decision["llm_min_confidence"])
                    binding = bool(self.cfg.decision.get("llm_veto_binding", True))
                    decision = apply_second_opinion(decision, verdict, min_conf, binding)

                # 5) Execute.
                if decision.action == "buy":
                    self.broker.buy(market, decision.amount_quote_eur,
                                    decision.stop_loss, decision.take_profit, decision.reason)
                    positions, free, daily_pnl = self._refresh_after_trade()
                    self.notify.send(
                        f"🟢 BUY {market} voor {decision.amount_quote_eur:.2f} EUR @ "
                        f"{snap.price:.2f}\nSL {decision.stop_loss:.2f} / "
                        f"TP {decision.take_profit:.2f}\n{decision.reason}")

                self._log_signal(market, candidate.action, decision.action,
                                 candidate.score, decision.reason, decision.details)
                decisions.append(decision)
            except Exception:  # noqa: BLE001 - one market must not kill the cycle
                log.exception("cycle failed for %s", market)
        return decisions

    def _entry_snapshot(self, market: str, candles: list, live_snap):
        """Snapshot waarop een ENTRY beoordeeld wordt: standaard zonder de lopende candle.

        De zwaarst wegende koopreden (`macd_hist > 0 and macd_hist_prev <= 0`) is een
        flip-conditie die binnen één bar komt en gaat. Beoordeel je hem op de lopende
        bar, dan koopt de bot bij de eerste uurrun waarin hij toevallig waar is en
        selecteert hij systematisch de vluchtigste realisatie van het signaal.

        Bewust hier en niet in `build_snapshot`: die functie wordt ook gebruikt door
        de scanner, het dashboard, de regime-proxy en de veto-analyse. Afknippen op
        die plek zou al die paden stil meeveranderen. De exit-route (`check_exit`,
        `_extra_exits`) en de position guard blijven de live prijs gebruiken, want
        daar is actualiteit juist gewenst.

        `strategy.signal_on_closed_candles: false` zet het oude gedrag terug, zodat
        oud en nieuw naast elkaar meetbaar blijven. De schakelaar staat onder
        `strategy` en niet onder `schedule`, zodat `config_fingerprint` verandert
        als hij omgaat en de veto-meting de twee signaalregimes vanzelf scheidt.
        """
        if not bool(self.cfg.strategy.get("signal_on_closed_candles", True)):
            return live_snap
        closed = drop_unclosed(candles)
        if len(closed) == len(candles) or len(closed) < MIN_SNAPSHOT_CANDLES:
            return live_snap
        return build_snapshot(market, closed, self.cfg.strategy)

    @staticmethod
    def _anchor_price(entry_snap, live_snap):
        """Score en indicatoren uit de afgesloten bar, prijs uit de live bar.

        De SL/TP-niveaus en de fee-gate rekenen vanaf de prijs die de bot
        daadwerkelijk betaalt; die op een tot 4 uur oude close ankeren zou de
        stop-afstand systematisch verkeerd zetten. ATR blijft uit de afgesloten
        reeks: dat is een indicator, geen fillprijs.
        """
        if entry_snap is live_snap:
            return entry_snap
        return replace(entry_snap, price=live_snap.price)

    def _extra_exits(self, pos, candles: list, snap) -> tuple[bool, str]:
        """Tijd- en trendgebonden exits die candles nodig hebben (dus alleen in de
        analysecyclus, niet in de position guard): time-stop en breakeven-stop.

        De breakeven-stop volgt de shadow-semantiek van de andere gates
        (`regime.binding`, `decision.llm_veto_binding`): met `binding: false` wordt
        een treffer wel gelogd (SignalRow met decision "shadow" en
        `details["shadow_breakeven"]`) maar niet uitgevoerd, zodat de waarde van de
        gate gemeten kan worden zonder trades te kosten. Bindend maken pas bij een
        positieve netto gate over >= 20 afgewikkelde trades.

        Bewuste keuze over ATR: `snap` is hier de LIVE snapshot, dus de
        breakeven-stop bewapent op de live ATR, terwijl sizing en fee-gate de ATR
        uit de afgesloten reeks gebruiken (`_entry_snapshot`). Dat is geen
        bijproduct maar de asymmetrie van v0.20.0 doorgetrokken: de piek waartegen
        de trigger wordt afgezet, wordt óók inclusief de lopende bar gemeten (zie
        `strategy.breakeven_stop_hit`), dus piek en drempel komen uit dezelfde
        data. Entries hebben juist een stabiele ATR nodig, want die bepaalt de
        stop-afstand van een positie die er nog jaren uit kan zien.

        Ook live blijft `current_price` in beide exits: alleen het TELLEN van
        candles in de time-stop loopt via `drop_unclosed`, niet de prijs.
        """
        exits_cfg = getattr(self.cfg, "exits", {}) or {}
        n_ts = int(exits_cfg.get("time_stop_candles", 0) or 0)
        if n_ts > 0:
            hit, why = time_stop_hit(
                candles, pos.opened_at, pos.entry_price, snap.price,
                self.fee_model.round_trip_pct(), n_ts,
                float(exits_cfg.get("time_stop_min_net_pct", 0.0)))
            if hit:
                return True, why

        be_cfg = exits_cfg.get("breakeven_stop", {}) or {}
        if not bool(be_cfg.get("enabled", False)):
            return False, ""
        hit, why = breakeven_stop_hit(
            candles, pos.opened_at, pos.entry_price, snap.price, snap.atr,
            float(be_cfg.get("trigger_atr", 1.0)),
            breakeven_offset_pct(be_cfg, self.fee_model,
                                 getattr(self.broker, "entry_is_maker", False)))
        if not hit:
            return False, ""
        if bool(be_cfg.get("binding", False)):
            return True, why
        # Prijzen mee in het event: de netto gate is (hypothetische exit) min
        # (werkelijke exit), en de hypothetische exitprijs is precies de koers op
        # dit moment. Zonder deze twee velden is die som achteraf niet te maken.
        self._log_signal(pos.market, "sell", "shadow", 0,
                         f"SHADOW-BREAKEVEN genegeerd: {why}",
                         {"shadow_breakeven": why, "price": snap.price,
                          "entry_price": pos.entry_price})
        return False, ""

    def _refresh_after_trade(self) -> tuple[list, float, float]:
        """Lees de in-cycle state opnieuw na een uitgevoerde order.

        Alleen de goedkope, betrouwbare grootheden: open posities en dagwinst komen
        uit de eigen database, vrije cash uit de broker. Zonder deze verversing
        beoordeelt de rest van de cyclus elke volgende markt op de toestand van vóór
        de order: dan gaan er meer posities open dan `effective_max_positions`
        toestaat en telt de correlatie-cluster-cap de zojuist geopende posities niet
        mee. `portfolio` wordt bewust niet herberekend (zie run_once).
        """
        return (self.broker.open_positions(), self.broker.cash_eur(),
                self.broker.daily_pnl_eur())

    def _auto_fill_markets(self, pinned: list[str], open_markets: list[str],
                           positions: list, portfolio_eur: float,
                           blocklist: set[str] | None = None) -> list[str]:
        """Vul de vrije slots met de beste scanner-kandidaten die alle gates halen.

        Gepinde markten en open posities houden voorrang; auto-fill voegt alleen
        kandidaten toe zolang er slots vrij zijn. Nooit dwingend: de kandidaten
        moeten zelfstandig door score, fee-gate en liquiditeit komen (dat doet de
        scanner), en daarna alsnog door alle engine-gates.
        """
        uni = getattr(self.cfg, "universe", {}) or {}
        if not bool(uni.get("auto_fill", False)):
            return []
        eff_max = self.decider.risk.effective_max_positions(portfolio_eur)
        free_slots = max(0, eff_max - len(positions))
        if free_slots == 0:
            return []
        max_auto = int(uni.get("max_auto", eff_max))
        buffer = int(uni.get("auto_fill_buffer", 2))
        want = min(max_auto, free_slots + buffer)
        try:
            results, _ = scan(self.feed, self.cfg, top_n=40)
        except Exception:  # noqa: BLE001 - scanfout mag de cyclus niet breken
            log.warning("auto-fill: scan mislukt, geen extra kandidaten deze cyclus")
            return []
        exclude = set(pinned) | set(open_markets) | (blocklist or set())
        return select_auto_fill(results, exclude, want)

    @staticmethod
    def _store_auto_fill(markets: list[str]) -> None:
        """Bewaar de auto-fill-set van deze cyclus zodat het dashboard hem kan tonen."""
        try:
            with session() as s:
                row = s.get(KVRow, "last_auto_fill")
                value = ",".join(markets)
                if row is None:
                    s.add(KVRow(key="last_auto_fill", value=value))
                else:
                    row.value = value
                s.commit()
        except Exception:  # noqa: BLE001 - puur informatief, mag nooit de cyclus breken
            log.debug("auto-fill-set opslaan mislukt")

    def _proxy_regime_ok(self, proxy_market: str, candles_map: dict,
                         interval: str, limit: int) -> bool:
        """Markt-breed regime: proxy (BTC) EMA-snel >= EMA-traag = risk-on.
        Fail-open: bij een datafout niet blokkeren, want een regime-storing mag
        entries niet stilleggen op ruis."""
        try:
            candles = candles_map.get(proxy_market)
            if candles is None:
                candles = self.feed.get_candles(proxy_market, interval, limit)
            snap = build_snapshot(proxy_market, candles, self.cfg.strategy)
            return snap.ema_fast >= snap.ema_slow
        except Exception:  # noqa: BLE001 - regime-datafout mag de cyclus niet blokkeren
            log.warning("regime-proxy %s ophalen/bouwen mislukt; fail-open", proxy_market)
            return True

    def check_exits_fast(self) -> int:
        """Position guard: alleen prijs vs SL/TP van open posities (elke minuut).
        Geen indicatoren, geen AI — puur risicobeheersing tussen analysecycli in.
        Time-stop en breakeven-stop blijven bij de uurcyclus (die hebben candles
        nodig); de guard dekt precies dezelfde niveaus als `strategy.check_exit`."""
        closed = 0
        for pos in self.broker.open_positions():
            try:
                price = self.feed.get_price(pos.market)
            except Exception:  # noqa: BLE001 - prijsfout mag de guard niet stoppen
                log.warning("guard: prijs ophalen mislukt voor %s", pos.market)
                continue
            if price <= pos.stop_loss:
                why = f"guard: stop loss geraakt ({price:.4f} <= {pos.stop_loss:.4f})"
            elif price >= pos.take_profit:
                why = f"guard: take profit geraakt ({price:.4f} >= {pos.take_profit:.4f})"
            else:
                continue
            self.broker.sell(pos.market, why)
            self._log_signal(pos.market, "sell", "executed", 0, why, {})
            self.notify.send(f"🔴 SELL {pos.market} @ {price:.4f}: {why}")
            closed += 1
        return closed

    def _log_signal(self, market: str, action: str, decision: str, score: int,
                    reason: str, details: dict) -> None:
        """Schrijf een SignalRow, gelabeld met de mode en met de config-hash van
        elke shadow-gate die in dit besluit heeft gevuurd.

        Instance-method sinds v0.20.0: een staticmethod kent de mode en de config
        niet, en zonder die twee is de meting achteraf niet te scheiden. De mode
        staat als kolom (één waarde per rij), de config-hashes staan in
        `details["gate_hash"]` en bewust NIET als kolom: één besluit kan meerdere
        shadow-gates tegelijk dragen (regime én chase annoteren dezelfde buy), en
        één kolom kan die niet los van elkaar scopen.
        """
        details = dict(details or {})
        hashes = {gate: gate_fingerprint(self.cfg, gate)
                  for gate, spec in SHADOW_GATES.items()
                  if f"shadow_{gate}" in details}
        if hashes:
            details["gate_hash"] = hashes
        with session() as s:
            s.add(SignalRow(market=market, action=action, decision=decision,
                            score=score, reason=reason[:1000], details=details,
                            mode=self.broker.mode))
            s.commit()
