#!/usr/bin/env python3
"""MCX India Option Chain Fair-Value & Buy-Side Mispricing Alert Bot (Upstox API Only).

Constructs live option chains for ALL optionable MCX commodity contracts using
Upstox's official BOD Instruments Master (MCX.json.gz), Option Greeks V3 API,
and Full Market Quotes V2 API (since Upstox /v2/option/chain does not support MCX).

Key Features:
- Covers all active MCX option contracts (CRUDEOIL, CRUDEOILM, NATURALGAS,
  NATGASMINI, GOLD, GOLDM, SILVER, SILVERM, COPPER, ZINC, etc.) automatically.
- Automatic expiry rollover: uses current expiry, and rolls over to next month's
  expiry when <= ROLLOVER_DTE calendar days remain (default: 5 days, configurable 4-5).
- Black-76 (options-on-futures) fair value using leave-one-out OI/spread-weighted
  peer IV + model-free Put-Call Parity cross-check.
- STRICT BUY-SIDE ONLY ("buy low, sell high"): alerts only when an option's LTP
  and executable Ask price are meaningfully below theoretical Fair Value after
  accounting for MCX statutory charges, brokerage, and bid-ask spread.
- Read-only Upstox API; sends instant alerts via Telegram (and console). No orders placed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("STATE_DIR", str(ROOT / "state")))

# Upstox official endpoints
UPSTOX_API_BASE = "https://api.upstox.com"
MCX_INSTRUMENTS_URL = os.getenv(
    "MCX_INSTRUMENTS_URL",
    "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz",
)

# Configurable strategy & risk parameters
ROLLOVER_DTE = int(os.getenv("ROLLOVER_DTE", "5"))  # Roll to next expiry if <= 4-5 days left
STRIKES_AROUND_ATM = int(os.getenv("STRIKES_AROUND_ATM", "6"))
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.065"))  # ~6.5% India 91-day T-bill proxy

# Mispricing thresholds (BUY-SIDE ONLY)
MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "5.0"))        # Recommended retail floor on MCX: 5%+
STRONG_GAP_PCT = float(os.getenv("STRONG_GAP_PCT", "10.0")) # Strong edge tier
HIGH_GAP_PCT = float(os.getenv("HIGH_GAP_PCT", "15.0"))     # High-conviction edge tier
MIN_GAP_RS = float(os.getenv("MIN_GAP_RS", "2.0"))          # Minimum rupee gap floor

# Liquidity & quote quality filters
MIN_LTP = float(os.getenv("MIN_LTP", "5.0"))
MIN_OI = int(os.getenv("MIN_OI", "50"))
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "5.0"))  # Max (ask - bid) / mid %
REQUIRE_ASK_CLEARANCE = os.getenv("REQUIRE_ASK_CLEARANCE", "true").lower() == "true"

# MCX round-trip statutory + brokerage cost parameters (Upstox)
BROKERAGE_PER_ORDER = float(os.getenv("BROKERAGE_PER_ORDER", "20.0"))
MCX_CTT_SELL_RATE = 0.0005         # 0.05% on sell-side option premium
MCX_EXCHANGE_TXN_RATE = 0.000418   # 0.0418% on option premium turnover (both legs)
SEBI_TURNOVER_RATE = 0.000001      # Rs 10 per crore
STAMP_DUTY_BUY_RATE = 0.00003      # 0.003% on buy-side premium
GST_RATE = 0.18                    # 18% on brokerage + exchange txn + SEBI fees

# Optional symbol filter: "ALL" or comma-separated e.g. "CRUDEOIL,CRUDEOILM,NATURALGAS,GOLDM"
MCX_SYMBOLS_RAW = os.getenv("MCX_SYMBOLS", "ALL").strip().upper()
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "60"))


def now_ist() -> datetime:
    return datetime.now(IST)


def safe_num(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def parse_expiry_date(raw_expiry: Any) -> Optional[date]:
    """Parse Upstox expiry field (epoch ms int/float or ISO YYYY-MM-DD string) to IST date."""
    if raw_expiry is None:
        return None
    if isinstance(raw_expiry, (int, float)):
        ts = float(raw_expiry)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=IST).date()
    s = str(raw_expiry).strip()
    if not s:
        return None
    if s.isdigit():
        ts = float(s)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=IST).date()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pricing Engine: Black-76 (Options on Commodity Futures) + Put-Call Parity
# ---------------------------------------------------------------------------

def black76_price(
    futures_price: float,
    strike: float,
    time_years: float,
    volatility: float,
    side: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Black-76 theoretical price for a European option on a commodity futures contract.

    Args:
        futures_price: Underlying MCX futures price (F).
        strike: Option strike price (K).
        time_years: Time to option expiry in years (T = DTE / 365).
        volatility: Annualized volatility as a decimal (e.g., 0.35 for 35%).
        side: 'CE' (call) or 'PE' (put).
        rate: Risk-free interest rate as a decimal.
    """
    side = side.upper()
    if min(futures_price, strike, time_years, volatility) <= 0:
        intrinsic = futures_price - strike if side == "CE" else strike - futures_price
        return max(0.0, intrinsic)

    sqrt_t = math.sqrt(time_years)
    sigma_sqrt_t = volatility * sqrt_t
    d1 = (math.log(futures_price / strike) + 0.5 * volatility * volatility * time_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    df = math.exp(-rate * time_years)

    if side == "CE":
        return max(0.0, df * (futures_price * norm_cdf(d1) - strike * norm_cdf(d2)))
    return max(0.0, df * (strike * norm_cdf(-d2) - futures_price * norm_cdf(-d1)))


def black76_greeks(
    futures_price: float,
    strike: float,
    time_years: float,
    volatility: float,
    side: str,
    rate: float = RISK_FREE_RATE,
) -> Dict[str, float]:
    """Analytical Black-76 Greeks (Delta, Gamma, Theta/day, Vega per 1% IV, Rho)."""
    side = side.upper()
    if min(futures_price, strike, time_years, volatility) <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrt_t = math.sqrt(time_years)
    sigma_sqrt_t = volatility * sqrt_t
    d1 = (math.log(futures_price / strike) + 0.5 * volatility * volatility * time_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    df = math.exp(-rate * time_years)
    pdf_d1 = norm_pdf(d1)

    if side == "CE":
        delta = df * norm_cdf(d1)
        price = df * (futures_price * norm_cdf(d1) - strike * norm_cdf(d2))
    else:
        delta = -df * norm_cdf(-d1)
        price = df * (strike * norm_cdf(-d2) - futures_price * norm_cdf(-d1))

    gamma = (df * pdf_d1) / (futures_price * sigma_sqrt_t)
    vega_raw = futures_price * df * pdf_d1 * sqrt_t
    vega_per_pct = vega_raw / 100.0
    theta_annual = -(futures_price * df * pdf_d1 * volatility) / (2.0 * sqrt_t) + rate * price
    theta_daily = theta_annual / 365.0
    rho_per_pct = (-time_years * price) / 100.0

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta_daily,
        "vega": vega_per_pct,
        "rho": rho_per_pct,
    }


def black76_implied_vol(
    market_price: float,
    futures_price: float,
    strike: float,
    time_years: float,
    side: str,
    rate: float = RISK_FREE_RATE,
) -> Optional[float]:
    """Solve Black-76 implied volatility (decimal) via robust bisection when provider IV is missing."""
    if min(market_price, futures_price, strike, time_years) <= 0:
        return None
    df = math.exp(-rate * time_years)
    intrinsic = max(0.0, df * (futures_price - strike if side == "CE" else strike - futures_price))
    if market_price <= intrinsic + 1e-6:
        return None

    low, high = 0.01, 4.0
    for _ in range(60):
        mid = 0.5 * (low + high)
        val = black76_price(futures_price, strike, time_years, mid, side, rate)
        if abs(val - market_price) < 1e-5:
            return mid
        if val < market_price:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def put_call_parity_fair(
    opposite_price: float,
    futures_price: float,
    strike: float,
    time_years: float,
    target_side: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Model-free Put-Call Parity on futures options: C - P = exp(-rT) * (F - K).

    Returns theoretical price of target_side given the opposite side's price at the same strike.
    """
    if opposite_price <= 0 or futures_price <= 0 or strike <= 0:
        return 0.0
    df = math.exp(-rate * max(time_years, 0.0))
    if target_side.upper() == "PE":
        # P = C - e^(-rT)*(F - K)
        return max(0.0, opposite_price - df * (futures_price - strike))
    # C = P + e^(-rT)*(F - K)
    return max(0.0, opposite_price + df * (futures_price - strike))


def estimate_mcx_round_trip_cost_per_unit(
    buy_price: float,
    target_sell_price: float,
    lot_size: int = 1,
) -> Tuple[float, float]:
    """Estimate round-trip MCX option charges (Rs per unit and % of buy price).

    Includes Upstox brokerage (Rs 20 buy + Rs 20 sell), MCX CTT (0.05% on sell premium),
    MCX exchange txn fee (0.0418% both sides), SEBI fee (Rs 10/Cr), stamp duty (0.003% buy),
    and 18% GST on brokerage + exchange + SEBI charges.
    """
    qty = max(1, int(lot_size))
    buy_turnover = buy_price * qty
    sell_turnover = max(target_sell_price, buy_price) * qty
    total_turnover = buy_turnover + sell_turnover

    brokerage = 2.0 * BROKERAGE_PER_ORDER
    exchange_txn = total_turnover * MCX_EXCHANGE_TXN_RATE
    sebi_fee = total_turnover * SEBI_TURNOVER_RATE
    gst = GST_RATE * (brokerage + exchange_txn + sebi_fee)
    ctt = sell_turnover * MCX_CTT_SELL_RATE
    stamp_duty = buy_turnover * STAMP_DUTY_BUY_RATE

    total_cost_rs = brokerage + exchange_txn + sebi_fee + gst + ctt + stamp_duty
    cost_per_unit = total_cost_rs / qty
    cost_pct = (cost_per_unit / buy_price * 100.0) if buy_price > 0 else 0.0
    return cost_per_unit, cost_pct


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class OptionContract:
    symbol: str
    trading_symbol: str
    instrument_key: str
    underlying_key: str
    side: str          # CE or PE
    strike: float
    expiry: date
    lot_size: int
    tick_size: float
    futures_price: float = 0.0
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    iv: float = 0.0    # percentage, e.g., 32.5 for 32.5%
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


@dataclass
class BuyAlert:
    alert_id: str
    timestamp: str
    symbol: str
    trading_symbol: str
    instrument_key: str
    side: str
    strike: float
    expiry: str
    dte_days: float
    rolled_over: bool
    futures_price: float
    ltp: float
    bid: float
    ask: float
    spread_pct: float
    oi: float
    volume: float
    lot_size: int
    market_iv_pct: float
    peer_iv_pct: float
    fair_black76: float
    fair_parity: Optional[float]
    actual_fair_value: float
    gap_rs_ltp: float
    gap_pct_ltp: float
    gap_rs_ask: float
    gap_pct_ask: float
    cost_per_unit_rs: float
    cost_pct: float
    net_edge_rs: float
    net_edge_pct: float
    lot_potential_rs: float
    tier: str
    delta: float
    gamma: float
    theta: float
    vega: float


# ---------------------------------------------------------------------------
# Expiry Selection & Rollover Logic (4–5 DTE Rule)
# ---------------------------------------------------------------------------

def select_target_expiry(
    available_expiries: List[date],
    today: date,
    rollover_dte: int = ROLLOVER_DTE,
) -> Tuple[Optional[date], bool, int]:
    """Select active MCX option expiry with automatic 4-5 DTE rollover to next month.

    Returns:
        (selected_expiry, rolled_over_flag, dte_calendar_days)
    """
    future_expiries = sorted({e for e in available_expiries if e >= today})
    if not future_expiries:
        return None, False, 0

    current_exp = future_expiries[0]
    current_dte = (current_exp - today).days

    if current_dte <= rollover_dte and len(future_expiries) >= 2:
        next_exp = future_expiries[1]
        return next_exp, True, (next_exp - today).days

    return current_exp, False, current_dte


def match_underlying_future(
    futures_list: List[Dict[str, Any]],
    option_expiry: date,
) -> Optional[Dict[str, Any]]:
    """Match the MCX futures contract whose expiry is on or immediately after option_expiry."""
    candidates = []
    for fut in futures_list:
        f_exp = parse_expiry_date(fut.get("expiry"))
        if f_exp is not None and f_exp >= option_expiry:
            candidates.append((f_exp, fut))
    if not candidates:
        # Fallback: latest available future for this symbol
        dated = [(parse_expiry_date(f.get("expiry")), f) for f in futures_list]
        dated = [(d, f) for d, f in dated if d is not None]
        if not dated:
            return None
        dated.sort(key=lambda x: x[0])
        return dated[-1][1]
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Valuation & Buy-Side Signal Engine
# ---------------------------------------------------------------------------

def is_liquid(c: OptionContract) -> bool:
    if c.ltp < MIN_LTP:
        return False
    if c.oi < MIN_OI or c.volume < MIN_VOLUME:
        return False
    if c.bid > 0 and c.ask >= c.bid:
        mid = 0.5 * (c.bid + c.ask)
        spread_pct = ((c.ask - c.bid) / max(mid, 1e-6)) * 100.0
        if spread_pct > MAX_SPREAD_PCT:
            return False
    elif REQUIRE_ASK_CLEARANCE:
        return False
    return True


def compute_peer_iv(
    target: OptionContract,
    chain: List[OptionContract],
) -> Optional[float]:
    """Leave-one-out OI & spread-weighted peer IV (returns decimal volatility, e.g., 0.35).

    Never uses target's own IV to price target, avoiding circular valuation.
    Prefers bracketing same-side peers; falls back to nearest liquid peers across the
    same strike / opposite side if a wing is thin.
    """
    same_side_peers = [
        p for p in chain
        if p.instrument_key != target.instrument_key
        and p.side == target.side
        and 1.0 < p.iv < 250.0
        and p.oi >= max(10, MIN_OI // 2)
    ]

    below = sorted([p for p in same_side_peers if p.strike < target.strike], key=lambda p: -p.strike)[:2]
    above = sorted([p for p in same_side_peers if p.strike > target.strike], key=lambda p: p.strike)[:2]

    def weighted_strike_iv(group: List[OptionContract]) -> Tuple[float, float]:
        weights = []
        for p in group:
            spread = max((p.ask - p.bid) if (p.ask > p.bid > 0) else p.ltp * 0.02, 0.05)
            w = math.sqrt(max(p.oi, 1.0)) / spread
            weights.append(w)
        total_w = sum(weights)
        avg_k = sum(w * p.strike for w, p in zip(weights, group)) / total_w
        avg_iv = sum(w * p.iv for w, p in zip(weights, group)) / total_w
        return avg_k, avg_iv

    if below and above:
        kl, vl = weighted_strike_iv(below)
        kh, vh = weighted_strike_iv(above)
        if kh > kl:
            interp_iv_pct = vl + (vh - vl) * (target.strike - kl) / (kh - kl)
            return max(0.01, interp_iv_pct / 100.0)

    # Fallback: opposite side at exact same strike (put-call IV equivalence)
    opp_same_strike = [
        p for p in chain
        if p.strike == target.strike and p.side != target.side and 1.0 < p.iv < 250.0
    ]
    if opp_same_strike:
        return opp_same_strike[0].iv / 100.0

    # Last fallback: weighted average of nearest 2 same-side liquid peers
    nearest = sorted(same_side_peers, key=lambda p: abs(p.strike - target.strike))[:2]
    if nearest:
        _, avg_iv = weighted_strike_iv(nearest)
        return max(0.01, avg_iv / 100.0)

    return None


def evaluate_chain_buy_opportunities(
    contracts: List[OptionContract],
    now: datetime,
    rolled_over: bool = False,
    rate: float = RISK_FREE_RATE,
    min_gap_pct: float = MIN_GAP_PCT,
    min_gap_rs: float = MIN_GAP_RS,
) -> List[BuyAlert]:
    """Evaluate an MCX commodity's option chain and return BUY-SIDE ONLY mispricing alerts.

    Combines:
    1. Black-76 theoretical fair value using leave-one-out OI/spread-weighted peer IV.
    2. Model-free Put-Call Parity fair value when the opposite option at the same strike is liquid.
    3. Conservative fair value selection (min of Black-76 and Parity when both exist, or Black-76)
       to prevent false alerts from single-strike distortion.
    4. Executable Ask-price & MCX statutory/brokerage cost clearance check.
    """
    if not contracts:
        return []

    by_strike_side: Dict[Tuple[float, str], OptionContract] = {
        (c.strike, c.side): c for c in contracts
    }

    alerts: List[BuyAlert] = []
    for c in contracts:
        if not is_liquid(c) or c.futures_price <= 0:
            continue

        # MCX option expiry close (approx 23:30 IST on expiry day)
        expiry_dt = datetime.combine(c.expiry, dt_time(23, 30), tzinfo=IST)
        secs_left = (expiry_dt - now).total_seconds()
        if secs_left < 3600:
            continue
        time_years = secs_left / (365.0 * 86400.0)
        dte_days = secs_left / 86400.0

        # Ensure IV exists (compute via Black-76 solver if Upstox returned 0)
        if c.iv <= 0:
            solved = black76_implied_vol(c.ltp, c.futures_price, c.strike, time_years, c.side, rate)
            if solved:
                c.iv = solved * 100.0

        peer_vol = compute_peer_iv(c, contracts)
        if peer_vol is None:
            continue

        fair_b76 = black76_price(c.futures_price, c.strike, time_years, peer_vol, c.side, rate)

        # Put-call parity cross-check from opposite contract at same strike
        opp_side = "PE" if c.side == "CE" else "CE"
        opp = by_strike_side.get((c.strike, opp_side))
        fair_parity: Optional[float] = None
        if opp and opp.ltp >= MIN_LTP and opp.oi >= max(10, MIN_OI // 2):
            opp_ref_price = 0.5 * (opp.bid + opp.ask) if (opp.bid > 0 and opp.ask >= opp.bid) else opp.ltp
            fair_parity = put_call_parity_fair(
                opp_ref_price, c.futures_price, c.strike, time_years, c.side, rate
            )

        # Conservative fair value: if parity is available and both show underpricing,
        # use the conservative (lower) fair value so we don't chase phantom parity/skew gaps.
        if fair_parity is not None and fair_parity > 0:
            actual_fair = min(fair_b76, fair_parity)
        else:
            actual_fair = fair_b76

        if actual_fair <= MIN_LTP:
            continue

        # STRICT BUY-SIDE ONLY: Actual Fair Value must be strictly above LTP and Ask
        gap_rs_ltp = actual_fair - c.ltp
        if gap_rs_ltp <= 0:
            continue  # Overpriced or fair — ignore (BUY side only)

        exec_buy_price = c.ask if (c.ask > 0) else c.ltp
        gap_rs_ask = actual_fair - exec_buy_price
        if REQUIRE_ASK_CLEARANCE and gap_rs_ask <= 0:
            continue

        gap_pct_ltp = (gap_rs_ltp / actual_fair) * 100.0
        gap_pct_ask = (gap_rs_ask / actual_fair) * 100.0

        spread_pct = (
            ((c.ask - c.bid) / max(0.5 * (c.ask + c.bid), 1e-6)) * 100.0
            if (c.bid > 0 and c.ask >= c.bid)
            else 0.0
        )

        cost_per_unit_rs, cost_pct = estimate_mcx_round_trip_cost_per_unit(
            exec_buy_price, actual_fair, c.lot_size
        )
        net_edge_rs = gap_rs_ask - cost_per_unit_rs
        net_edge_pct = (net_edge_rs / exec_buy_price * 100.0) if exec_buy_price > 0 else 0.0

        # Effective threshold check: must clear minimum % gap, minimum Rs gap, and positive net edge
        if gap_pct_ltp < min_gap_pct:
            continue
        if REQUIRE_ASK_CLEARANCE and gap_pct_ask < min_gap_pct:
            continue
        if gap_rs_ask < min_gap_rs or net_edge_rs <= 0:
            continue

        # Analytical Black-76 Greeks fallback if Upstox Greeks missing
        calc_g = black76_greeks(c.futures_price, c.strike, time_years, peer_vol, c.side, rate)
        delta = c.delta if c.delta != 0 else calc_g["delta"]
        gamma = c.gamma if c.gamma != 0 else calc_g["gamma"]
        theta = c.theta if c.theta != 0 else calc_g["theta"]
        vega = c.vega if c.vega != 0 else calc_g["vega"]

        if gap_pct_ask >= HIGH_GAP_PCT:
            tier = "HIGH_CONVICTION"
        elif gap_pct_ask >= STRONG_GAP_PCT:
            tier = "STRONG"
        else:
            tier = "STANDARD"

        alert_id = hashlib.sha256(
            f"{now.date().isoformat()}:{c.instrument_key}:{c.expiry.isoformat()}:BUY".encode()
        ).hexdigest()[:12]

        alerts.append(
            BuyAlert(
                alert_id=alert_id,
                timestamp=now.isoformat(),
                symbol=c.symbol,
                trading_symbol=c.trading_symbol,
                instrument_key=c.instrument_key,
                side=c.side,
                strike=c.strike,
                expiry=c.expiry.isoformat(),
                dte_days=round(dte_days, 2),
                rolled_over=rolled_over,
                futures_price=round(c.futures_price, 2),
                ltp=round(c.ltp, 2),
                bid=round(c.bid, 2),
                ask=round(c.ask, 2),
                spread_pct=round(spread_pct, 2),
                oi=c.oi,
                volume=c.volume,
                lot_size=c.lot_size,
                market_iv_pct=round(c.iv, 2),
                peer_iv_pct=round(peer_vol * 100.0, 2),
                fair_black76=round(fair_b76, 2),
                fair_parity=round(fair_parity, 2) if fair_parity is not None else None,
                actual_fair_value=round(actual_fair, 2),
                gap_rs_ltp=round(gap_rs_ltp, 2),
                gap_pct_ltp=round(gap_pct_ltp, 2),
                gap_rs_ask=round(gap_rs_ask, 2),
                gap_pct_ask=round(gap_pct_ask, 2),
                cost_per_unit_rs=round(cost_per_unit_rs, 2),
                cost_pct=round(cost_pct, 2),
                net_edge_rs=round(net_edge_rs, 2),
                net_edge_pct=round(net_edge_pct, 2),
                lot_potential_rs=round(net_edge_rs * max(1, c.lot_size), 2),
                tier=tier,
                delta=round(delta, 4),
                gamma=round(gamma, 6),
                theta=round(theta, 2),
                vega=round(vega, 2),
            )
        )

    alerts.sort(key=lambda a: (-a.net_edge_pct, -a.gap_rs_ask))
    return alerts


# ---------------------------------------------------------------------------
# Upstox API Client (MCX Only — Instruments Master + Greeks V3 + Quotes V2)
# ---------------------------------------------------------------------------

class UpstoxMCXClient:
    """Read-only Upstox API client tailored for MCX Commodity Futures & Options."""

    def __init__(self, access_token: Optional[str] = None, cache_dir: Path = STATE_DIR):
        token = (access_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")).strip()
        if not token:
            raise RuntimeError("Missing UPSTOX_ACCESS_TOKEN environment variable")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{UPSTOX_API_BASE}{path}"
        for attempt in range(4):
            try:
                resp = self.session.get(url, params=params, timeout=25)
            except requests.RequestException:
                if attempt == 3:
                    raise RuntimeError("Upstox network error") from None
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = safe_num(resp.headers.get("Retry-After"), 1.0)
                time.sleep(min(30.0, max(float(2 ** attempt), retry_after)))
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"Upstox HTTP {resp.status_code} on {path}; check token/permissions")

            payload = resp.json()
            if payload.get("status") != "success":
                raise RuntimeError(f"Upstox API unsuccessful on {path}")
            return payload.get("data") or {}

        raise RuntimeError(f"Upstox retry limit exceeded on {path}")

    def load_mcx_instruments(self, today: Optional[date] = None) -> List[Dict[str, Any]]:
        """Download and cache daily Upstox MCX BOD instruments master JSON."""
        today = today or now_ist().date()
        cache_file = self.cache_dir / f"mcx_instruments_{today.isoformat()}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        resp = requests.get(MCX_INSTRUMENTS_URL, timeout=45)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to download Upstox MCX instruments ({resp.status_code})")

        raw_bytes = gzip.decompress(resp.content) if resp.content[:2] == b"\x1f\x8b" else resp.content
        items = json.loads(raw_bytes.decode("utf-8"))
        mcx_fo = [x for x in items if x.get("segment") == "MCX_FO"]
        cache_file.write_text(json.dumps(mcx_fo), encoding="utf-8")
        return mcx_fo

    def get_full_quotes(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch Upstox V2 Full Market Quotes (up to 500 keys per call).

        Returns mapping keyed by instrument_token (e.g. 'MCX_FO|435356').
        """
        result: Dict[str, Dict[str, Any]] = {}
        unique_keys = list(dict.fromkeys(instrument_keys))
        for i in range(0, len(unique_keys), 500):
            chunk = unique_keys[i : i + 500]
            data = self._get("/v2/market-quote/quotes", {"instrument_key": ",".join(chunk)})
            for _, quote_obj in data.items():
                tok = quote_obj.get("instrument_token")
                if tok:
                    result[tok] = quote_obj
        return result

    def get_option_greeks(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch Upstox V3 Option Greeks (max 50 keys per call).

        Returns mapping keyed by instrument_token.
        """
        result: Dict[str, Dict[str, Any]] = {}
        unique_keys = list(dict.fromkeys(instrument_keys))
        for i in range(0, len(unique_keys), 50):
            chunk = unique_keys[i : i + 50]
            data = self._get("/v3/market-quote/option-greek", {"instrument_key": ",".join(chunk)})
            for _, g_obj in data.items():
                tok = g_obj.get("instrument_token")
                if tok:
                    result[tok] = g_obj
        return result


# ---------------------------------------------------------------------------
# Universe Builder & Scanner
# ---------------------------------------------------------------------------

def group_mcx_universe(
    mcx_instruments: List[Dict[str, Any]],
    symbol_filter: str = MCX_SYMBOLS_RAW,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Group all MCX_FO instruments by underlying symbol into futures and options.

    Only retains symbols that have BOTH active futures and active CE/PE options.
    """
    allowed = None if symbol_filter in ("", "ALL") else {s.strip() for s in symbol_filter.split(",") if s.strip()}
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for item in mcx_instruments:
        if item.get("segment") != "MCX_FO":
            continue
        itype = str(item.get("instrument_type", "")).upper()
        if itype not in ("FUT", "CE", "PE"):
            continue
        sym = str(item.get("underlying_symbol") or item.get("asset_symbol") or item.get("name") or "").upper().strip()
        if not sym:
            continue
        if allowed is not None and sym not in allowed:
            continue
        bucket = grouped.setdefault(sym, {"FUT": [], "OPT": []})
        if itype == "FUT":
            bucket["FUT"].append(item)
        else:
            bucket["OPT"].append(item)

    return {sym: data for sym, data in grouped.items() if data["FUT"] and data["OPT"]}


def build_mcx_option_chains(
    client: UpstoxMCXClient,
    now: datetime,
    rollover_dte: int = ROLLOVER_DTE,
    strikes_around_atm: int = STRIKES_AROUND_ATM,
) -> Dict[str, Tuple[List[OptionContract], bool]]:
    """Fetch live option chains for ALL optionable MCX contracts via Upstox API."""
    today = now.date()
    instruments = client.load_mcx_instruments(today)
    universe = group_mcx_universe(instruments)
    if not universe:
        return {}

    # 1. Select target expiry (with 4-5 DTE rollover) & matching underlying future for each commodity
    selected_meta: Dict[str, Dict[str, Any]] = {}
    fut_keys: List[str] = []

    for sym, data in sorted(universe.items()):
        opt_expiries = [parse_expiry_date(o.get("expiry")) for o in data["OPT"]]
        opt_expiries_clean = [e for e in opt_expiries if e is not None]
        target_exp, rolled, _ = select_target_expiry(opt_expiries_clean, today, rollover_dte)
        if target_exp is None:
            continue
        fut = match_underlying_future(data["FUT"], target_exp)
        if fut is None:
            continue

        opts_for_exp = [o for o in data["OPT"] if parse_expiry_date(o.get("expiry")) == target_exp]
        if not opts_for_exp:
            continue

        selected_meta[sym] = {
            "expiry": target_exp,
            "rolled_over": rolled,
            "future": fut,
            "options": opts_for_exp,
        }
        fut_keys.append(fut["instrument_key"])

    if not fut_keys:
        return {}

    # 2. Fetch live underlying futures quotes for all commodities in one batched call
    fut_quotes = client.get_full_quotes(fut_keys)

    # 3. Filter ATM +/- N strikes per commodity to stay fast & well within Upstox rate limits
    option_meta_to_fetch: List[Tuple[str, Dict[str, Any], float, date, bool]] = []
    for sym, meta in selected_meta.items():
        fkey = meta["future"]["instrument_key"]
        fq = fut_quotes.get(fkey) or {}
        f_ltp = safe_num(fq.get("last_price"))
        if f_ltp <= 0:
            ohlc = fq.get("ohlc") or {}
            f_ltp = safe_num(ohlc.get("close"))
        if f_ltp <= 0:
            continue

        strikes = sorted({safe_num(o.get("strike_price")) for o in meta["options"] if safe_num(o.get("strike_price")) > 0})
        if not strikes:
            continue
        atm_idx = min(range(len(strikes)), key=lambda idx: abs(strikes[idx] - f_ltp))
        lo = max(0, atm_idx - strikes_around_atm)
        hi = min(len(strikes), atm_idx + strikes_around_atm + 1)
        band_strikes = set(strikes[lo:hi])

        for o in meta["options"]:
            if safe_num(o.get("strike_price")) in band_strikes:
                option_meta_to_fetch.append((sym, o, f_ltp, meta["expiry"], meta["rolled_over"]))

    if not option_meta_to_fetch:
        return {}

    opt_keys = [o["instrument_key"] for _, o, _, _, _ in option_meta_to_fetch]

    # 4. Batch fetch Option Greeks V3 (max 50/call) and Full Quotes V2 (max 500/call) for bid/ask depth
    greeks_map = client.get_option_greeks(opt_keys)
    quotes_map = client.get_full_quotes(opt_keys)

    chains: Dict[str, Tuple[List[OptionContract], bool]] = {}
    for sym, o, f_ltp, exp_date, rolled in option_meta_to_fetch:
        okey = o["instrument_key"]
        g = greeks_map.get(okey) or {}
        q = quotes_map.get(okey) or {}

        depth = q.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []
        best_bid = safe_num(buys[0].get("price")) if buys else 0.0
        best_ask = safe_num(sells[0].get("price")) if sells else 0.0

        ltp = safe_num(q.get("last_price")) or safe_num(g.get("last_price"))
        oi = safe_num(q.get("oi")) or safe_num(g.get("oi"))
        vol = safe_num(q.get("volume")) or safe_num(g.get("volume"))

        # Upstox V3 option-greek returns IV as decimal (e.g. 0.3359) or percentage; normalize to %
        raw_iv = safe_num(g.get("iv"))
        iv_pct = raw_iv * 100.0 if 0 < raw_iv <= 3.5 else raw_iv

        contract = OptionContract(
            symbol=sym,
            trading_symbol=str(o.get("trading_symbol") or okey),
            instrument_key=okey,
            underlying_key=str(o.get("underlying_key") or ""),
            side=str(o.get("instrument_type", "")).upper(),
            strike=safe_num(o.get("strike_price")),
            expiry=exp_date,
            lot_size=int(safe_num(o.get("lot_size"), 1)),
            tick_size=safe_num(o.get("tick_size"), 0.05),
            futures_price=f_ltp,
            ltp=ltp,
            bid=best_bid,
            ask=best_ask,
            oi=oi,
            volume=vol,
            iv=iv_pct,
            delta=safe_num(g.get("delta")),
            gamma=safe_num(g.get("gamma")),
            theta=safe_num(g.get("theta")),
            vega=safe_num(g.get("vega")),
        )
        if sym not in chains:
            chains[sym] = ([], rolled)
        chains[sym][0].append(contract)

    return chains


# ---------------------------------------------------------------------------
# Alert Formatting, State Persistence & Telegram Delivery
# ---------------------------------------------------------------------------

def format_alert_message(a: BuyAlert) -> str:
    roll_note = " [NEXT-MONTH ROLLOVER <=5 DTE]" if a.rolled_over else ""
    parity_line = f" | Parity Fair: ₹{a.fair_parity:.2f}" if a.fair_parity is not None else ""
    return (
        f"🚨 MCX BUY-SIDE GAP ALERT [{a.tier}]\n"
        f"{a.symbol} {a.strike:g} {a.side} | Exp: {a.expiry} ({a.dte_days:.1f} DTE){roll_note}\n"
        f"Futures: ₹{a.futures_price:.2f} | LTP: ₹{a.ltp:.2f} | Ask: ₹{a.ask:.2f} (Spread {a.spread_pct:.2f}%)\n"
        f"Actual Fair Value: ₹{a.actual_fair_value:.2f} (B76: ₹{a.fair_black76:.2f}{parity_line})\n"
        f"Gap vs LTP: +₹{a.gap_rs_ltp:.2f} ({a.gap_pct_ltp:.2f}%) | vs Ask: +₹{a.gap_rs_ask:.2f} ({a.gap_pct_ask:.2f}%)\n"
        f"Est. MCX Cost: ₹{a.cost_per_unit_rs:.2f}/u ({a.cost_pct:.2f}%) → Net Edge: +₹{a.net_edge_rs:.2f}/u (+₹{a.lot_potential_rs:,.0f}/lot)\n"
        f"IV: {a.market_iv_pct:.1f}% (Peer {a.peer_iv_pct:.1f}%) | OI: {a.oi:g} | Vol: {a.volume:g}\n"
        f"Greeks: Δ {a.delta:+.3f} | Γ {a.gamma:.5f} | Θ {a.theta:+.2f}/d | Vega {a.vega:.2f}\n"
        f"Action: BUY-SIDE WATCH ONLY (Buy Low → Target Fair Value ₹{a.actual_fair_value:.2f})\n"
        f"ID: {a.alert_id}"
    )


class MCXAlertBot:
    def __init__(self, client: Optional[UpstoxMCXClient], state_dir: Path = STATE_DIR):
        self.client = client
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "mcx_state.json"
        self.state = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.exists()
            else {"version": 1, "sent_alerts": {}, "outbox": [], "history": []}
        )

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _should_alert(self, alert: BuyAlert, now: datetime) -> bool:
        prev_iso = self.state["sent_alerts"].get(alert.alert_id)
        if not prev_iso:
            return True
        try:
            prev_dt = datetime.fromisoformat(prev_iso)
            return (now - prev_dt).total_seconds() >= ALERT_COOLDOWN_MINUTES * 60
        except ValueError:
            return True

    def enqueue(self, alert: BuyAlert, now: datetime) -> bool:
        if not self._should_alert(alert, now):
            return False
        msg = format_alert_message(alert)
        self.state["sent_alerts"][alert.alert_id] = now.isoformat()
        self.state["history"].append(asdict(alert))
        self.state["outbox"].append({"id": alert.alert_id, "text": msg, "sent": False})
        self.save()
        return True

    def flush_telegram(self) -> int:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        sent_count = 0
        for item in self.state["outbox"]:
            if item.get("sent"):
                continue
            print("\n" + item["text"] + "\n", flush=True)
            if token and chat_id:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": item["text"], "disable_web_page_preview": True},
                    timeout=20,
                )
                if not resp.ok or not resp.json().get("ok"):
                    raise RuntimeError(f"Telegram delivery failed (HTTP {resp.status_code})")
            item["sent"] = True
            sent_count += 1
            self.save()
        return sent_count

    def scan_once(self, now: Optional[datetime] = None) -> List[BuyAlert]:
        if self.client is None:
            raise RuntimeError("UpstoxMCXClient is required for live scans")
        now = now or now_ist()
        chains = build_mcx_option_chains(self.client, now)
        all_alerts: List[BuyAlert] = []
        for sym, (contracts, rolled) in sorted(chains.items()):
            sym_alerts = evaluate_chain_buy_opportunities(contracts, now, rolled_over=rolled)
            for a in sym_alerts:
                if self.enqueue(a, now):
                    all_alerts.append(a)
        self.flush_telegram()
        return all_alerts


def run_demo_simulation() -> List[BuyAlert]:
    """Offline demonstration using the user's Crude Oil 7500 example + rollover test."""
    sim_now = datetime(2026, 11, 25, 18, 30, tzinfo=IST)
    # Example: Near expiry is Nov 28 (3 DTE <= 5 -> rolls over to Dec 17 next month!)
    selected_exp, rolled, dte = select_target_expiry(
        [date(2026, 11, 28), date(2026, 12, 17)], sim_now.date(), rollover_dte=5
    )
    assert selected_exp is not None
    # Build realistic Crude Oil 7500 chain where peer IV ~ 72% yields ~550 fair on 7500PE,
    # while 7500PE is trading cheap at LTP=500 / Ask=502 (Gap ~ Rs 48-50 -> BUY ALERT!)
    # and 7500CE is slightly overpriced at 565 (ignored because BUY-SIDE ONLY).
    t_years = (datetime.combine(selected_exp, dt_time(23, 30), tzinfo=IST) - sim_now).total_seconds() / (365 * 86400)
    f_price = 7500.0
    peer_sigma = 0.75  # gives ~551 fair on ATM 7500 with ~22 DTE

    def mk(strike: float, side: str, ltp: float, bid: float, ask: float, iv_pct: float, oi: int = 2500) -> OptionContract:
        return OptionContract(
            symbol="CRUDEOIL",
            trading_symbol=f"CRUDEOIL {int(strike)} {side} {selected_exp.strftime('%d %b %y').upper()}",
            instrument_key=f"MCX_FO|DEMO_{int(strike)}_{side}",
            underlying_key="MCX_FO|DEMO_FUT",
            side=side,
            strike=strike,
            expiry=selected_exp,
            lot_size=100,
            tick_size=0.1,
            futures_price=f_price,
            ltp=ltp,
            bid=bid,
            ask=ask,
            oi=oi,
            volume=1200,
            iv=iv_pct,
        )

    p7400_pe = black76_price(f_price, 7400, t_years, peer_sigma, "PE")
    p7600_pe = black76_price(f_price, 7600, t_years, peer_sigma, "PE")
    p7400_ce = black76_price(f_price, 7400, t_years, peer_sigma, "CE")
    p7600_ce = black76_price(f_price, 7600, t_years, peer_sigma, "CE")
    fair_7500 = black76_price(f_price, 7500, t_years, peer_sigma, "PE")

    demo_chain = [
        mk(7400, "PE", round(p7400_pe, 2), round(p7400_pe - 1, 2), round(p7400_pe + 1, 2), peer_sigma * 100),
        mk(7600, "PE", round(p7600_pe, 2), round(p7600_pe - 1, 2), round(p7600_pe + 1, 2), peer_sigma * 100),
        mk(7400, "CE", round(p7400_ce, 2), round(p7400_ce - 1, 2), round(p7400_ce + 1, 2), peer_sigma * 100),
        mk(7600, "CE", round(p7600_ce, 2), round(p7600_ce - 1, 2), round(p7600_ce + 1, 2), peer_sigma * 100),
        # 7500 CE trading at fair value (~550) so Put-Call Parity also confirms 7500 PE should be ~550
        mk(7500, "CE", round(fair_7500 + 2, 2), round(fair_7500 + 1, 2), round(fair_7500 + 3, 2), peer_sigma * 100),
        # 7500 PE underpriced by ~Rs 50 (LTP 500, Ask 502 vs Fair ~551) -> triggers BUY alert!
        mk(7500, "PE", 500.0, 498.0, 502.0, 68.0),
    ]

    alerts = evaluate_chain_buy_opportunities(demo_chain, sim_now, rolled_over=rolled)
    print(f"=== MCX DEMO SIMULATION (Selected Expiry: {selected_exp}, Rolled Over: {rolled}, DTE: {dte}d) ===")
    for a in alerts:
        print("\n" + format_alert_message(a))
    return alerts


def is_mcx_market_open(t: datetime) -> bool:
    """MCX non-agri commodities trade Mon-Fri 09:00 to 23:30 IST (23:55 during US winter)."""
    if t.weekday() >= 5:
        return False
    return dt_time(9, 0) <= t.time() <= dt_time(23, 55)


def main() -> None:
    parser = argparse.ArgumentParser(description="MCX Option Chain Fair-Value & Buy-Side Alert Bot (Upstox API)")
    parser.add_argument("--mode", choices=["run", "once", "test", "demo"], default="demo")
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("POLL_SECONDS", "30")))
    args = parser.parse_args()

    if args.mode == "demo":
        run_demo_simulation()
        return

    client = UpstoxMCXClient()
    bot = MCXAlertBot(client)

    if args.mode == "test":
        instruments = client.load_mcx_instruments()
        universe = group_mcx_universe(instruments)
        summary = (
            f"MCX Fair-Value Bot connection test OK.\n"
            f"Discovered {len(universe)} optionable MCX commodities: {', '.join(sorted(universe.keys()))}.\n"
            f"Rollover DTE: <= {ROLLOVER_DTE} days | Min Gap: {MIN_GAP_PCT}% (BUY-SIDE ONLY)."
        )
        print(summary)
        bot.state["outbox"].append({"id": f"test-{now_ist().isoformat()}", "text": summary, "sent": False})
        bot.flush_telegram()
        return

    if args.mode == "once":
        bot.scan_once()
        return

    started = now_ist()
    max_end = started + timedelta(hours=5, minutes=20)  # Safe for GitHub Actions 6h cap
    while now_ist() < max_end:
        t = now_ist()
        if is_mcx_market_open(t):
            try:
                bot.scan_once(t)
            except Exception as exc:
                print(f"[{t:%H:%M:%S} IST] Scan error: {exc}", flush=True)
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    main()
