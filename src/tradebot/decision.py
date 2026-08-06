"""Fee-aware decision engine and risk management.

Hard lesson from the previous attempt: 27% correct calls + fees = -15% capital.
Therefore every BUY must pass the fee gate BEFORE any LLM is consulted:

    expected_move_pct >= round_trip_fee + slippage_buffer + min_profit

The LLM can only veto a candidate that already passed all mechanical gates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .correlation import correlation_from_closes
from .strategy import Candidate

log = logging.getLogger(__name__)


def correlated_positions(own_closes: list[float],
                         others: dict[str, list[float]],
                         max_corr: float, lookback: int) -> list[tuple[str, float]]:
    """Welke open posities bewegen sterk mee met een kandidaat.

    Puur en testbaar: returnt (markt, correlatie) voor elke reeks met
    return-correlatie > `max_corr`. De engine gebruikt de lengte hiervan voor de
    cluster-cap: bij >= K gecorreleerde posities wordt de kandidaat geweigerd, in
    plaats van al bij de eerste (dat blokkeerde een gecorreleerd universum dood).
    """
    out: list[tuple[str, float]] = []
    for market, closes in others.items():
        corr = correlation_from_closes(own_closes, closes, lookback)
        if corr is not None and corr > max_corr:
            out.append((market, round(corr, 2)))
    return out


@dataclass
class FeeModel:
    """Kosten van een round-trip, inclusief hoe de broker hem werkelijk maakt.

    `entry_is_maker` hoort hier en niet bij elke aanroeper: het is één eigenschap
    van de draaiende opzet en de brokers dragen hem zelf (`PaperBroker` false,
    `LiveBroker` true). Zo volgen de fee-gate, de scanner, de time-stop en de
    breakeven-offset automatisch dezelfde aanname, in plaats van dat de een de
    brokermodus volgt en de ander de paper-aanname vastbakt.
    """
    maker_pct: float
    taker_pct: float
    slippage_buffer_pct: float
    entry_is_maker: bool = False

    def round_trip_pct(self, entry_is_maker: bool | None = None) -> float:
        """Werkelijke round-trip: entry-fee plus exit-fee.

        Paper vult beide benen als taker (0,50% bij het basistarief). `LiveBroker`
        doet een maker-entry (limit postOnly) en een market-exit, dus live is het
        maker + taker (0,40%). Tot v0.20.0 stond hier `2 * taker` met een
        `use_taker`-vlag die nergens werd gebruikt en die de werkelijke combinatie
        maker+taker niet eens kon uitdrukken.
        """
        maker_entry = self.entry_is_maker if entry_is_maker is None else entry_is_maker
        return (self.maker_pct if maker_entry else self.taker_pct) + self.taker_pct

    def min_edge_pct(self, min_profit_pct: float,
                     entry_is_maker: bool | None = None) -> float:
        """Vereiste beweging: round-trip + slippage-buffer + minimale winst.

        Volgt de brokermodus via `round_trip_pct`. In paper verandert er niets
        (beide benen taker, dus 1,10% blijft 1,10%); in live wordt het 1,00%, wat
        klopt omdat de entry daar maker is. Bewust NIET meegenomen: de tweede
        divergentie tussen scanner en engine (echte spread per markt tegen de vaste
        buffer van 0,10). Die verandert wél welke kandidaten door de gate komen, dus
        de populatie, dus onder de per-gate fingerprint de meetcohorte van alle vier
        de gates. Zie `docs/ontwerp-spread-bron-van-waarheid.md`: besloten, maar
        uitgevoerd op hetzelfde moment dat de eerste gate bindend wordt, zodat die
        reset één keer betaald wordt in plaats van twee.
        """
        return (self.round_trip_pct(entry_is_maker) + self.slippage_buffer_pct
                + min_profit_pct)


def breakeven_offset_pct(be_cfg: dict, fee_model: FeeModel,
                         entry_is_maker: bool | None = None) -> float:
    """Drempel waarop de breakeven-stop vuurt, afgeleid uit het fee-model.

    Stond als losse `offset_pct: 0.55` in config, los van het fee-model: bij een
    andere Bitvavo-tier klopt die drempel niet meer en eindigt een "breakeven"-exit
    stilletjes op een verlies na kosten. Nu is het de werkelijke round-trip plus
    een configureerbare marge (`offset_margin_pct`).

    De round-trip volgt de BROKERMODUS. Paper doet beide benen taker (0,50%), live
    doet een maker-entry en een taker-exit (0,40%). Zou de offset de paper-aanname
    vastbakken, dan vuurt de gate live 0,15 procentpunt te laat en verandert haar
    gedrag stilzwijgend op het moment van omschakelen naar fase 3, wat het slechtst
    denkbare moment is voor een verrassing.

    `offset_pct` blijft als expliciete override werken, zodat een bestaande config
    op de Pi niet stil van gedrag verandert.
    """
    expliciet = be_cfg.get("offset_pct")
    if expliciet is not None:
        return float(expliciet)
    marge = float(be_cfg.get("offset_margin_pct", 0.05) or 0.0)
    return round(fee_model.round_trip_pct(entry_is_maker) + marge, 4)


@dataclass
class Position:
    market: str
    amount: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    fees_paid_eur: float = 0.0


@dataclass
class Decision:
    market: str
    action: str                     # "buy" | "sell" | "skip"
    reason: str
    amount_quote_eur: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    details: dict = field(default_factory=dict)


class RiskManager:
    """Positielimieten en -grootte. Twee sizing-modi:

    * "percent" (standaard, legacy): elke positie is `max_position_pct`% van het
      portfolio; het aantal slots is vast (`max_open_positions`).
    * "bucket": elke positie is een vast bedrag (`bucket_eur`) en het aantal
      slots schaalt mee met het kapitaal (1 extra slot per bucket), begrensd door
      `max_open_positions`. Zo komt er boven elke `bucket_eur` aan groei een
      positie bij (bijv. > €1250 = 5 slots, > €1500 = 6) zonder bestaande
      posities te verkleinen. `max_open_positions` is in deze modus het plafond.
    """

    def __init__(self, cfg: dict):
        self.sizing = str(cfg.get("sizing", "percent")).lower()
        self.bucket_eur = float(cfg.get("bucket_eur", 0.0) or 0.0)
        self.max_position_pct = float(cfg["max_position_pct"])
        self.max_open_positions = int(cfg["max_open_positions"])
        self.cooldown = timedelta(hours=float(cfg["cooldown_hours_after_trade"]))
        self.daily_loss_cap_pct = float(cfg["daily_loss_cap_pct"])

    def _bucket_mode(self) -> bool:
        return self.sizing == "bucket" and self.bucket_eur > 0

    def effective_max_positions(self, portfolio_eur: float) -> int:
        """Aantal toegestane open posities gegeven het huidige kapitaal. In
        bucket-modus: floor(portfolio / bucket), begrensd op [1, max_open_positions].
        In percent-modus: het vaste `max_open_positions`."""
        if not self._bucket_mode():
            return self.max_open_positions
        by_capital = int(portfolio_eur // self.bucket_eur)
        return max(1, min(self.max_open_positions, by_capital))

    def can_open(self, market: str, open_positions: list[Position],
                 last_trade_at: datetime | None, portfolio_eur: float,
                 daily_pnl_eur: float) -> tuple[bool, str]:
        if any(p.market == market for p in open_positions):
            return False, "position already open in this market"
        eff_max = self.effective_max_positions(portfolio_eur)
        if len(open_positions) >= eff_max:
            return False, f"max open positions ({eff_max}) reached"
        if last_trade_at and datetime.now(timezone.utc) - last_trade_at < self.cooldown:
            return False, f"cooldown active until {(last_trade_at + self.cooldown).isoformat()}"
        if portfolio_eur > 0 and daily_pnl_eur < -portfolio_eur * self.daily_loss_cap_pct / 100:
            return False, f"daily loss cap ({self.daily_loss_cap_pct}%) reached"
        return True, "ok"

    def position_size_eur(self, portfolio_eur: float, free_eur: float) -> float:
        if self._bucket_mode():
            return min(self.bucket_eur, free_eur)
        return min(portfolio_eur * self.max_position_pct / 100, free_eur)


class DecisionEngine:
    def __init__(self, fee_model: FeeModel, risk: RiskManager, decision_cfg: dict):
        self.fees = fee_model
        self.risk = risk
        self.cfg = decision_cfg

    def expected_move_pct(self, candidate: Candidate) -> float:
        """ATR-based expected favourable move to the take-profit level."""
        snap = candidate.snapshot
        stop_dist = snap.atr * float(self.cfg["atr_stop_multiplier"])
        target_dist = stop_dist * float(self.cfg["reward_risk_ratio"])
        return target_dist / snap.price * 100

    def levels(self, candidate: Candidate) -> tuple[float, float]:
        snap = candidate.snapshot
        stop_dist = snap.atr * float(self.cfg["atr_stop_multiplier"])
        stop = snap.price - stop_dist
        target = snap.price + stop_dist * float(self.cfg["reward_risk_ratio"])
        return stop, target

    def evaluate_buy(self, candidate: Candidate, open_positions: list[Position],
                     last_trade_at: datetime | None, portfolio_eur: float,
                     free_eur: float, daily_pnl_eur: float) -> Decision:
        market = candidate.market
        if candidate.action != "buy":
            return Decision(market, "skip", f"no signal (score {candidate.score})")

        # Gate 1: risk limits
        ok, why = self.risk.can_open(market, open_positions, last_trade_at,
                                     portfolio_eur, daily_pnl_eur)
        if not ok:
            return Decision(market, "skip", f"risk gate: {why}")

        # Gate 2: fee gate — the core protection against fee bleed
        expected = self.expected_move_pct(candidate)
        min_edge = self.fees.min_edge_pct(float(self.cfg["min_profit_pct"]))
        if expected < min_edge:
            return Decision(market, "skip",
                            f"fee gate: expected move {expected:.2f}% < required {min_edge:.2f}%",
                            details={"expected_pct": expected, "min_edge_pct": min_edge})

        size = self.risk.position_size_eur(portfolio_eur, free_eur)
        if size < 10:  # Bitvavo minimum order ~5 EUR; below 10 fees dominate
            return Decision(market, "skip", f"position size too small ({size:.2f} EUR)")

        stop, target = self.levels(candidate)
        return Decision(market, "buy",
                        "; ".join(candidate.reasons),
                        amount_quote_eur=round(size, 2),
                        stop_loss=stop, take_profit=target,
                        details={"expected_pct": expected, "min_edge_pct": min_edge,
                                 "score": candidate.score})


def apply_regime_filter(decision: Decision, regime_ok: bool, proxy_market: str,
                        binding: bool = True) -> Decision:
    """Pas de markt-brede regime-gate toe op een buy-besluit.

    Volledig gecodeerd, geen AI: als de proxy-markt (BTC) in down-trend staat,
    is het regime risk-off en worden nieuwe entries geweerd. Spiegelt de
    shadow-semantiek van `apply_second_opinion`:

    binding=True  : regime-down blokkeert de koop (skip).
    binding=False : de koop blijft staan, geannoteerd met de regime-reden en
                    `details["shadow_regime"]`, zodat de gate-waarde gemeten
                    wordt zonder trades te kosten.

    `regime_ok=True` (uptrend of proxy niet beschikbaar) laat het besluit ongemoeid.
    """
    if decision.action != "buy" or regime_ok:
        return decision
    reason = f"regime gate: {proxy_market} trend down (risk-off)"
    if binding:
        return Decision(decision.market, "skip", reason)
    return Decision(
        decision.market, "buy",
        f"{decision.reason} | SHADOW-REGIME genegeerd: {reason}",
        amount_quote_eur=decision.amount_quote_eur,
        stop_loss=decision.stop_loss, take_profit=decision.take_profit,
        details={**decision.details, "shadow_regime": reason})


def apply_chase_guard(decision: Decision, hit: bool, reason: str,
                      binding: bool = False) -> Decision:
    """Pas de chase-guard toe op een buy-besluit.

    Spiegelt de shadow-semantiek van `apply_regime_filter` en
    `apply_second_opinion`:

    binding=True  : te ver doorgelopen koers blokkeert de koop (skip).
    binding=False : de koop blijft staan, geannoteerd met de reden en
                    `details["shadow_chase"]`, zodat de gate-waarde gemeten wordt
                    zonder trades te kosten.

    Default niet-bindend, conform de vaste projectregel: elke nieuwe gate eerst
    meten, bindend pas bij een positieve netto gate over >= 20 afgewikkelde trades.
    """
    if decision.action != "buy" or not hit:
        return decision
    if binding:
        return Decision(decision.market, "skip", reason)
    return Decision(
        decision.market, "buy",
        f"{decision.reason} | SHADOW-CHASE genegeerd: {reason}",
        amount_quote_eur=decision.amount_quote_eur,
        stop_loss=decision.stop_loss, take_profit=decision.take_profit,
        details={**decision.details, "shadow_chase": reason})


def apply_second_opinion(decision: Decision, verdict, min_conf: float,
                         binding: bool = True) -> Decision:
    """Pas het LLM-tweede-oordeel toe op een buy-besluit.

    binding=True (normaal): een veto (LLM oneens of confidence < drempel) blokkeert
    de koop en wordt een skip.

    binding=False (shadow-mode): het veto wordt nog steeds door de LLM-laag gelogd,
    maar is niet bindend. De koop blijft staan, geannoteerd met de veto-reden, zodat
    de waarde van de gate gemeten kan worden zonder dat hij trades kost. LLM
    onbereikbaar telt in shadow-mode niet als veto (de koop gaat door).

    `verdict` is duck-typed (velden agree, confidence, reasoning, provider) of None.
    """
    if verdict is None:
        if binding:
            return Decision(decision.market, "skip", "LLM unavailable; conservative skip")
        return decision
    vetoed = (not verdict.agree) or (verdict.confidence < min_conf)
    if not vetoed:
        return decision
    veto_reason = (f"LLM veto ({verdict.provider}, conf {verdict.confidence:.2f}): "
                   f"{verdict.reasoning}")
    if binding:
        return Decision(decision.market, "skip", veto_reason)
    return Decision(
        decision.market, "buy",
        f"{decision.reason} | SHADOW-VETO genegeerd: {veto_reason}",
        amount_quote_eur=decision.amount_quote_eur,
        stop_loss=decision.stop_loss, take_profit=decision.take_profit,
        details={**decision.details, "shadow_veto": veto_reason})
