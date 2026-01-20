#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
main.py

Команды:
- monitor : короткий монитор bid/ask + Binance mark/funding
- hedge   : один расчёт хеджа (коротко, WIN/LOSE разложение)
- live    : monitor + hedge каждый тик
- live --graph : live + график Ask vs NeedAsk (без лагов: сеть в фоне, график в main thread)

Новая фича:
- --both : считать hedge для ОБОИХ исходов (например 80k и 100k) в одном тике
           и печатать need ask для каждого.

График:
- если --both --graph: рисует ask/need для каждого исхода (4 линии)
- красные точки: моменты когда ask <= need (feasible) для соответствующего исхода
"""

import argparse
import asyncio
import json
import math
import re
import ssl
import threading
import queue
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp
import certifi

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
BINANCE_FAPI = "https://fapi.binance.com"


# -----------------------------
# Helpers
# -----------------------------
def make_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def now_str() -> str:
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


def blank(n: int = 1):
    print("\n" * max(0, n), end="")


def extract_event_slug(url: str) -> str:
    m = re.search(r"/event/([^/?#]+)", url)
    if not m:
        raise ValueError(f"Не смог вытащить slug из URL: {url}")
    return m.group(1)


def _maybe_json(x):
    if isinstance(x, str):
        s = x.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                return json.loads(s)
            except Exception:
                return x
    return x


def best_bid_ask(book: dict) -> Tuple[Optional[float], Optional[float]]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = None
    best_ask = None
    if bids:
        best_bid = max(float(b["price"]) for b in bids if "price" in b)
    if asks:
        best_ask = min(float(a["price"]) for a in asks if "price" in a)
    return best_bid, best_ask


# -----------------------------
# Data models
# -----------------------------
@dataclass
class OutcomeQuote:
    outcome: str
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]


@dataclass
class HedgeResult:
    side_model: str
    qty_btc_model: float
    pnl_win_total: float
    pnl_lose_total: float
    fut_per1_win: float
    fut_per1_lose: float
    ok: bool


@dataclass
class GraphPoint:
    ts: datetime
    # series: [(outcome_name, ask, need)]
    series: List[Tuple[str, Optional[float], Optional[float]]]


# -----------------------------
# Polymarket (Gamma & CLOB)
# -----------------------------
async def gamma_get_event_by_slug(session: aiohttp.ClientSession, slug: str) -> dict:
    async with session.get(f"{POLY_GAMMA}/events/slug/{slug}", timeout=15) as r:
        r.raise_for_status()
        return await r.json()


async def gamma_get_market_by_slug(session: aiohttp.ClientSession, slug: str) -> dict:
    params = {"slug": slug, "limit": 1}
    async with session.get(f"{POLY_GAMMA}/markets", params=params, timeout=15) as r:
        r.raise_for_status()
        data = await r.json()
    if not data:
        raise RuntimeError(f"Gamma не вернул market по slug={slug}.")
    return data[0]


def market_outcome_token_map(market: dict) -> List[Tuple[str, str]]:
    outcomes = _maybe_json(market.get("outcomes"))
    token_ids = (
        _maybe_json(market.get("clobTokenIds"))
        or _maybe_json(market.get("clob_token_ids"))
        or _maybe_json(market.get("clobTokenIDs"))
    )
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        raise RuntimeError(f"Не нашёл outcomes/token_ids в market. keys={list(market.keys())}")
    if len(outcomes) != len(token_ids):
        raise RuntimeError(f"outcomes и token_ids разной длины: {len(outcomes)} vs {len(token_ids)}")
    return list(zip([str(o) for o in outcomes], [str(t) for t in token_ids]))


async def clob_get_books(session: aiohttp.ClientSession, token_ids: List[str]) -> Dict[str, dict]:
    payload = [{"token_id": tid} for tid in token_ids]
    async with session.post(f"{POLY_CLOB}/books", json=payload, timeout=15) as r:
        r.raise_for_status()
        books = await r.json()
    by_token = {}
    for b in books:
        asset_id = str(b.get("asset_id") or b.get("token_id") or "")
        if asset_id:
            by_token[asset_id] = b
    return by_token


async def polymarket_fee_rate_bps(session: aiohttp.ClientSession, token_id: str) -> int:
    params = {"token_id": token_id}
    async with session.get(f"{POLY_CLOB}/fee-rate", params=params, timeout=15) as r:
        r.raise_for_status()
        data = await r.json()
    return int(data.get("fee_rate_bps", 0))


# -----------------------------
# Polymarket fee approximation (simple)
# -----------------------------
_POLY_FEE_CURVE = [
    (0.10, 0.0020),
    (0.20, 0.0064),
    (0.30, 0.0110),
    (0.40, 0.0144),
    (0.50, 0.0156),
    (0.60, 0.0144),
    (0.70, 0.0110),
    (0.80, 0.0064),
    (0.90, 0.0020),
]


def _interp_fee_rate(price: float) -> float:
    p = max(0.01, min(0.99, float(price)))
    pts = _POLY_FEE_CURVE
    if p <= pts[0][0]:
        return pts[0][1]
    if p >= pts[-1][0]:
        return pts[-1][1]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if x1 <= p <= x2:
            t = (p - x1) / (x2 - x1)
            return y1 + t * (y2 - y1)
    return 0.0


def polymarket_trade_fee(trade_value_usdc: float, price: float, fee_rate_bps: int) -> float:
    if fee_rate_bps <= 0:
        return 0.0
    eff = _interp_fee_rate(price)
    return trade_value_usdc * eff


def polymarket_pnl(stake_usdc: float, fill_price: float, fee_rate_bps: int) -> dict:
    p = float(fill_price)
    shares = stake_usdc / p
    fee = polymarket_trade_fee(stake_usdc, p, fee_rate_bps)
    pnl_win = shares - stake_usdc - fee
    pnl_lose = -stake_usdc - fee
    return {"shares": shares, "fee": fee, "pnl_win": pnl_win, "pnl_lose": pnl_lose}


# -----------------------------
# Binance futures
# -----------------------------
async def binance_premium_index(session: aiohttp.ClientSession, symbol: str) -> dict:
    params = {"symbol": symbol}
    async with session.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params=params, timeout=15) as r:
        r.raise_for_status()
        data = await r.json()
    return {
        "markPrice": float(data["markPrice"]),
        "lastFundingRate": float(data.get("lastFundingRate", 0.0)),
    }


def futures_pnl_per_1btc(
    side: str,
    entry: float,
    exit_price: float,
    open_fee_rate: float,
    close_fee_rate: float,
    funding_rate_per_8h: float,
    expected_hours: float,
) -> float:
    s = 1.0 if side.lower() == "long" else -1.0
    periods = max(0, math.ceil(max(0.0, expected_hours) / 8.0))
    pnl_price = s * (exit_price - entry)
    fee_open = entry * open_fee_rate
    fee_close = exit_price * close_fee_rate
    funding_payment = s * funding_rate_per_8h * entry * periods
    return pnl_price - fee_open - fee_close - funding_payment


# -----------------------------
# Parse barrier levels
# -----------------------------
def parse_levels_from_text(text: str) -> List[float]:
    t = (text or "").lower().replace(",", "")
    levels: List[float] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*k", t):
        levels.append(float(m.group(1)) * 1000.0)
    for m in re.finditer(r"\b(\d{4,7})\b", t):
        v = float(m.group(1))
        if v >= 1000:
            levels.append(v)
    uniq = sorted(set(int(x) for x in levels))
    return [float(x) for x in uniq]


def extract_level_from_outcome(outcome: str) -> Optional[float]:
    lv = parse_levels_from_text(outcome)
    return lv[0] if lv else None


# -----------------------------
# Hedge math
# -----------------------------
def solve_hedge_qty_for_breakeven_win(
    poly_pnl_win: float,
    poly_pnl_lose: float,
    fut_per1_win: float,
    fut_per1_lose: float,
    desired_plus_on_lose: float,
    side_model: str,
) -> HedgeResult:
    if abs(fut_per1_win) < 1e-12:
        return HedgeResult(side_model, float("nan"), float("nan"), float("nan"), fut_per1_win, fut_per1_lose, False)

    qty = -poly_pnl_win / fut_per1_win
    pnl_win_total = poly_pnl_win + qty * fut_per1_win
    pnl_lose_total = poly_pnl_lose + qty * fut_per1_lose
    ok = pnl_lose_total >= desired_plus_on_lose
    return HedgeResult(side_model, qty, pnl_win_total, pnl_lose_total, fut_per1_win, fut_per1_lose, ok)


def required_poly_ask_for_target(
    stake_usdc: float,
    fee_rate_bps: int,
    desired_plus: float,
    fut_per1_win: float,
    fut_per1_lose: float,
) -> Optional[float]:
    def feasible(p: float) -> bool:
        poly = polymarket_pnl(stake_usdc, p, fee_rate_bps)
        if abs(fut_per1_win) < 1e-12:
            return False
        qty = -poly["pnl_win"] / fut_per1_win
        pnl_lose_total = poly["pnl_lose"] + qty * fut_per1_lose
        return pnl_lose_total >= desired_plus

    lo, hi = 0.01, 0.99
    if not feasible(lo):
        return None
    if feasible(hi):
        return hi

    best = lo
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if feasible(mid):
            best = mid
            lo = mid
        else:
            hi = mid
    return best


def pretty_position(side_model: str, qty_btc_model: float) -> Tuple[str, float]:
    if math.isnan(qty_btc_model):
        return ("UNKNOWN", float("nan"))
    side_model = side_model.lower().strip()
    if qty_btc_model == 0:
        return ("FLAT", 0.0)
    if qty_btc_model > 0:
        return ("LONG" if side_model == "long" else "SHORT", abs(qty_btc_model))
    return ("SHORT" if side_model == "long" else "LONG", abs(qty_btc_model))


# -----------------------------
# Load event/markets
# -----------------------------
async def load_event_markets(session: aiohttp.ClientSession, event_url: str) -> Tuple[str, List[dict]]:
    slug = extract_event_slug(event_url)
    try:
        event = await gamma_get_event_by_slug(session, slug)
        title = event.get("title") or slug
        markets = event.get("markets") or []
        if markets:
            return title, markets
    except Exception:
        pass

    market = await gamma_get_market_by_slug(session, slug)
    title = market.get("question") or slug
    return title, [market]


def pick_outcome_token(pairs: List[Tuple[str, str]], buy_outcome: str) -> Optional[Tuple[str, str]]:
    needle = (buy_outcome or "").strip().lower()
    for outcome, tid in pairs:
        if needle and needle in outcome.lower():
            return outcome, tid
    if len(pairs) == 2 and needle in ("yes", "no"):
        return pairs[0] if needle == "yes" else pairs[1]
    return None


# -----------------------------
# Output helpers
# -----------------------------
def print_monitor_lines(quotes: List[OutcomeQuote]):
    for q in quotes:
        bid_s = f"{q.best_bid:.4f}" if q.best_bid is not None else "—"
        ask_s = f"{q.best_ask:.4f}" if q.best_ask is not None else "—"
        print(f"  {q.outcome:<8} bid {bid_s:<7} ask {ask_s:<7}")

    asks = [q.best_ask for q in quotes if q.best_ask is not None]
    if len(asks) == len(quotes):
        s = sum(asks)
        if s < 1.0:
            print(f"  ✅ INTERNAL-ARB: Σasks={s:.4f} (edge={1.0 - s:.4f})")


# -----------------------------
# Core: compute hedge for one chosen outcome inside one market
# -----------------------------
async def compute_hedge_for_outcome(
    session: aiohttp.ClientSession,
    pairs: List[Tuple[str, str]],
    outcome_name: str,
    token_id: str,
    entry: float,
    fund: float,
    stake_usdc: float,
    target_plus: float,
    expected_hours: float,
    open_fee_rate: float,
    close_fee_rate: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns: (ask, need_ask)
    Also prints short hedge block.
    """
    books = await clob_get_books(session, [token_id])
    _bid, ask = best_bid_ask(books.get(token_id, {}))
    if ask is None:
        print(f"  HEDGE({outcome_name}): no ask")
        return None, None

    fee_bps = await polymarket_fee_rate_bps(session, token_id)
    poly = polymarket_pnl(stake_usdc, ask, fee_bps)

    # barriers from outcomes text like "80k"/"100k"
    outcome_levels: Dict[str, float] = {}
    for o, _tid in pairs:
        lv = extract_level_from_outcome(o)
        if lv is not None:
            outcome_levels[o] = lv

    win_exit = outcome_levels.get(outcome_name)
    lose_exit = None
    if win_exit is not None:
        others = [lv for o, lv in outcome_levels.items() if o != outcome_name]
        if others:
            lose_exit = others[0]

    if win_exit is None or lose_exit is None:
        print(f"  HEDGE({outcome_name}): can't parse barriers from outcomes")
        return float(ask), None

    # choose best of (model LONG or model SHORT)
    candidates: List[HedgeResult] = []
    for side_model in ("long", "short"):
        fut_win = futures_pnl_per_1btc(side_model, entry, win_exit, open_fee_rate, close_fee_rate, fund, expected_hours)
        fut_lose = futures_pnl_per_1btc(side_model, entry, lose_exit, open_fee_rate, close_fee_rate, fund, expected_hours)
        candidates.append(
            solve_hedge_qty_for_breakeven_win(
                poly_pnl_win=poly["pnl_win"],
                poly_pnl_lose=poly["pnl_lose"],
                fut_per1_win=fut_win,
                fut_per1_lose=fut_lose,
                desired_plus_on_lose=target_plus,
                side_model=side_model,
            )
        )

    candidates.sort(key=lambda x: (x.ok, x.pnl_lose_total), reverse=True)
    best = candidates[0]

    eff_side, eff_qty_btc = pretty_position(best.side_model, best.qty_btc_model)
    notional_usd = eff_qty_btc * entry

    qty_model = best.qty_btc_model
    bin_win = qty_model * best.fut_per1_win
    bin_lose = qty_model * best.fut_per1_lose

    need_ask = None
    if not best.ok:
        need_ask = required_poly_ask_for_target(stake_usdc, fee_bps, target_plus, best.fut_per1_win, best.fut_per1_lose)

    print(f"  HEDGE({outcome_name}): ask={ask:.4f} shares≈{poly['shares']:.2f}")
    print(f"    Binance: {eff_side} ${notional_usd:.2f} notional ({eff_qty_btc:.6f} BTC)")
    print(f"    WIN : Poly {poly['pnl_win']:+.2f}, Binance {bin_win:+.2f} => Total {(poly['pnl_win']+bin_win):+.2f}")
    print(f"    LOSE: Poly {poly['pnl_lose']:+.2f}, Binance {bin_lose:+.2f} => Total {(poly['pnl_lose']+bin_lose):+.2f}")

    if not best.ok:
        if need_ask is None:
            print("    ❌ not feasible (assumptions)")
        else:
            print(f"    ❌ need ask <= {need_ask:.4f} to be feasible")

    return float(ask), (None if need_ask is None else float(need_ask))


# -----------------------------
# One tick for one event URL
# -----------------------------
async def compute_tick(
    session: aiohttp.ClientSession,
    url: str,
    buy_outcome: str,
    both: bool,
    stake_usdc: float,
    target_plus: float,
    expected_hours: float,
    binance_symbol: str,
    open_fee_rate: float,
    close_fee_rate: float,
) -> GraphPoint:
    prem = await binance_premium_index(session, binance_symbol)
    entry = prem["markPrice"]
    fund = prem["lastFundingRate"]

    title, markets = await load_event_markets(session, url)

    print(f"\n[{now_str()}] Binance {binance_symbol} mark={entry:.2f} funding8h={fund:+.6%}")
    print(f"EVENT: {title}")

    # monitor all markets, choose first market that contains needed outcome (or any for both)
    chosen_market = None
    chosen_pairs = None

    for m in markets:
        pairs = market_outcome_token_map(m)
        token_ids = [tid for _, tid in pairs]
        books = await clob_get_books(session, token_ids)
        quotes = []
        for outcome, tid in pairs:
            bid, ask = best_bid_ask(books.get(tid, {}))
            quotes.append(OutcomeQuote(outcome, tid, bid, ask))
        print_monitor_lines(quotes)

        if chosen_market is None:
            if both:
                # pick first market with at least 2 outcomes (typical)
                if len(pairs) >= 2:
                    chosen_market = m
                    chosen_pairs = pairs
            else:
                picked = pick_outcome_token(pairs, buy_outcome)
                if picked:
                    chosen_market = m
                    chosen_pairs = pairs

    if chosen_market is None or chosen_pairs is None:
        print("  HEDGE: can't choose market for hedge (no suitable outcomes)")
        return GraphPoint(datetime.now(), [])

    # hedge calc
    series: List[Tuple[str, Optional[float], Optional[float]]] = []

    if both:
        # compute for all outcomes in that market (usually 2)
        for outcome_name, token_id in chosen_pairs:
            a, n = await compute_hedge_for_outcome(
                session=session,
                pairs=chosen_pairs,
                outcome_name=outcome_name,
                token_id=token_id,
                entry=entry,
                fund=fund,
                stake_usdc=stake_usdc,
                target_plus=target_plus,
                expected_hours=expected_hours,
                open_fee_rate=open_fee_rate,
                close_fee_rate=close_fee_rate,
            )
            series.append((outcome_name, a, n))
    else:
        picked = pick_outcome_token(chosen_pairs, buy_outcome)
        if not picked:
            print(f"  HEDGE: outcome '{buy_outcome}' not found")
            return GraphPoint(datetime.now(), [])
        outcome_name, token_id = picked
        a, n = await compute_hedge_for_outcome(
            session=session,
            pairs=chosen_pairs,
            outcome_name=outcome_name,
            token_id=token_id,
            entry=entry,
            fund=fund,
            stake_usdc=stake_usdc,
            target_plus=target_plus,
            expected_hours=expected_hours,
            open_fee_rate=open_fee_rate,
            close_fee_rate=close_fee_rate,
        )
        series.append((outcome_name, a, n))

    return GraphPoint(datetime.now(), series)


# -----------------------------
# Modes (no graph)
# -----------------------------
async def run_monitor(urls: List[str], interval: int, binance_symbol: str):
    ssl_ctx = make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            prem = await binance_premium_index(session, binance_symbol)
            print(f"[{now_str()}] Binance {binance_symbol} mark={prem['markPrice']:.2f} funding8h={prem['lastFundingRate']:+.6%}")
            for url in urls:
                title, markets = await load_event_markets(session, url)
                print(f"EVENT: {title}")
                for m in markets:
                    pairs = market_outcome_token_map(m)
                    token_ids = [tid for _, tid in pairs]
                    books = await clob_get_books(session, token_ids)
                    quotes = []
                    for outcome, tid in pairs:
                        bid, ask = best_bid_ask(books.get(tid, {}))
                        quotes.append(OutcomeQuote(outcome, tid, bid, ask))
                    print_monitor_lines(quotes)
            blank(1)
            await asyncio.sleep(interval)


async def run_hedge(
    urls: List[str],
    buy_outcome: str,
    both: bool,
    stake_usdc: float,
    target_plus: float,
    expected_hours: float,
    binance_symbol: str,
    open_fee_rate: float,
    close_fee_rate: float,
):
    ssl_ctx = make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        for url in urls:
            await compute_tick(
                session=session,
                url=url,
                buy_outcome=buy_outcome,
                both=both,
                stake_usdc=stake_usdc,
                target_plus=target_plus,
                expected_hours=expected_hours,
                binance_symbol=binance_symbol,
                open_fee_rate=open_fee_rate,
                close_fee_rate=close_fee_rate,
            )


async def run_live(
    urls: List[str],
    interval: int,
    binance_symbol: str,
    buy_outcome: str,
    both: bool,
    stake_usdc: float,
    target_plus: float,
    expected_hours: float,
    open_fee_rate: float,
    close_fee_rate: float,
):
    ssl_ctx = make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            for url in urls:
                await compute_tick(
                    session=session,
                    url=url,
                    buy_outcome=buy_outcome,
                    both=both,
                    stake_usdc=stake_usdc,
                    target_plus=target_plus,
                    expected_hours=expected_hours,
                    binance_symbol=binance_symbol,
                    open_fee_rate=open_fee_rate,
                    close_fee_rate=close_fee_rate,
                )
            blank(1)
            await asyncio.sleep(interval)


# -----------------------------
# Graph mode (NO LAG): network in background thread
# -----------------------------
def run_live_with_graph(
    urls: List[str],
    interval: int,
    binance_symbol: str,
    buy_outcome: str,
    both: bool,
    stake_usdc: float,
    target_plus: float,
    expected_hours: float,
    open_fee_rate: float,
    close_fee_rate: float,
    max_points: int,
    graph_refresh_ms: int,
):
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from collections import deque
    from matplotlib.animation import FuncAnimation

    q: "queue.Queue[GraphPoint]" = queue.Queue()
    stop_flag = threading.Event()

    def worker():
        async def bg():
            ssl_ctx = make_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                while not stop_flag.is_set():
                    for url in urls:
                        try:
                            gp = await compute_tick(
                                session=session,
                                url=url,
                                buy_outcome=buy_outcome,
                                both=both,
                                stake_usdc=stake_usdc,
                                target_plus=target_plus,
                                expected_hours=expected_hours,
                                binance_symbol=binance_symbol,
                                open_fee_rate=open_fee_rate,
                                close_fee_rate=close_fee_rate,
                            )
                            q.put(gp)
                        except Exception as e:
                            print(f"[bg] error: {e}")

                    # sleep in bg loop (responsive stop)
                    for _ in range(max(1, interval * 10)):
                        if stop_flag.is_set():
                            break
                        await asyncio.sleep(0.1)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bg())
        finally:
            loop.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    # buffers per outcome
    ts = deque(maxlen=max_points)
    asks: Dict[str, deque] = {}
    needs: Dict[str, deque] = {}

    fig, ax = plt.subplots(figsize=(13, 5))
    ax.set_title(f"Ask vs NeedAsk ({'BOTH' if both else buy_outcome})")
    ax.set_xlabel("time")
    ax.set_ylabel("price")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    lines_ask: Dict[str, any] = {}
    lines_need: Dict[str, any] = {}
    scatters: Dict[str, any] = {}

    def ensure_series(outcome: str):
        if outcome not in asks:
            asks[outcome] = deque(maxlen=max_points)
            needs[outcome] = deque(maxlen=max_points)

            (la,) = ax.plot([], [], marker="o", markersize=3, linewidth=1.5, label=f"ask {outcome}")
            (ln,) = ax.plot([], [], linestyle="--", marker="o", markersize=3, linewidth=1.2, label=f"need {outcome}")
            sc = ax.scatter([], [], s=35, alpha=0.9, label=f"feasible {outcome}")

            lines_ask[outcome] = la
            lines_need[outcome] = ln
            scatters[outcome] = sc

            ax.legend(loc="upper right")

    def drain_queue() -> bool:
        got = False
        while True:
            try:
                p = q.get_nowait()
            except queue.Empty:
                break

            if not p.series:
                continue

            ts.append(p.ts)

            # for each outcome in this point, update buffers
            for outcome, a, n in p.series:
                ensure_series(outcome)

                asks[outcome].append(float("nan") if a is None else float(a))

                # need: if None, keep last need if exists else nan
                if n is None:
                    needs[outcome].append(needs[outcome][-1] if len(needs[outcome]) else float("nan"))
                else:
                    needs[outcome].append(float(n))

            # for outcomes not present this tick (rare), we still must keep aligned length:
            # easiest: do nothing; lines will have shorter arrays, still OK.

            got = True
        return got

    def animate(_frame):
        updated = drain_queue()
        if not updated:
            # return artists (doesn't matter blit=False)
            return []

        xs = list(ts)

        artists = []

        # update each outcome line with its own length (might be shorter than xs)
        for outcome in list(asks.keys()):
            ya = list(asks[outcome])
            yn = list(needs[outcome])

            # align x with y length
            x_use = xs[-len(ya):] if len(ya) <= len(xs) else xs

            lines_ask[outcome].set_data(x_use, ya)
            lines_need[outcome].set_data(x_use, yn)

            # feasible dots: ask <= need
            cx, cy = [], []
            for x, a, n in zip(x_use, ya, yn):
                if (not math.isnan(a)) and (not math.isnan(n)) and (a <= n):
                    cx.append(x)
                    cy.append(a)

            if cx:
                scatters[outcome].set_offsets(np.column_stack([cx, cy]))
            else:
                scatters[outcome].set_offsets(np.empty((0, 2)))

            artists.extend([lines_ask[outcome], lines_need[outcome], scatters[outcome]])

        ax.relim()
        ax.autoscale_view()

        ymin, ymax = ax.get_ylim()
        ax.set_ylim(max(0.0, ymin - 0.01), min(1.0, ymax + 0.01))
        fig.autofmt_xdate()

        return artists

    def on_close(_evt):
        stop_flag.set()

    fig.canvas.mpl_connect("close_event", on_close)

    _ani = FuncAnimation(
        fig,
        animate,
        interval=graph_refresh_ms,
        blit=False,
        cache_frame_data=False,
    )

    plt.show()

    stop_flag.set()
    t.join(timeout=2.0)


# -----------------------------
# CLI
# -----------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_mon = sub.add_parser("monitor", help="Short monitor")
    p_mon.add_argument("--url", action="append", required=True)
    p_mon.add_argument("--interval", type=int, default=10)
    p_mon.add_argument("--binance", default="BTCUSDT")

    p_hedge = sub.add_parser("hedge", help="Hedge once")
    p_hedge.add_argument("--url", action="append", required=True)
    p_hedge.add_argument("--outcome", required=True)
    p_hedge.add_argument("--stake", type=float, required=True)
    p_hedge.add_argument("--target_plus", type=float, default=0.0)
    p_hedge.add_argument("--hours", type=float, default=24.0)
    p_hedge.add_argument("--binance", default="BTCUSDT")
    p_hedge.add_argument("--open_fee", type=float, default=0.00045)
    p_hedge.add_argument("--close_fee", type=float, default=0.00018)
    p_hedge.add_argument("--both", action="store_true", help="Compute hedge for BOTH outcomes in the market")

    p_live = sub.add_parser("live", help="Live: monitor + hedge")
    p_live.add_argument("--url", action="append", required=True)
    p_live.add_argument("--interval", type=int, default=10)
    p_live.add_argument("--binance", default="BTCUSDT")
    p_live.add_argument("--outcome", required=True)
    p_live.add_argument("--stake", type=float, required=True)
    p_live.add_argument("--target_plus", type=float, default=0.0)
    p_live.add_argument("--hours", type=float, default=24.0)
    p_live.add_argument("--open_fee", type=float, default=0.00045)
    p_live.add_argument("--close_fee", type=float, default=0.00018)

    p_live.add_argument("--both", action="store_true", help="Compute hedge for BOTH outcomes in the market")
    p_live.add_argument("--graph", action="store_true", help="Show Ask vs NeedAsk graph (no lag)")
    p_live.add_argument("--graph_points", type=int, default=200, help="Max points on graph")
    p_live.add_argument("--graph_refresh_ms", type=int, default=500, help="Graph refresh interval in ms")

    return p


async def async_main():
    args = build_parser().parse_args()

    if args.cmd == "monitor":
        await run_monitor(args.url, args.interval, args.binance)

    elif args.cmd == "hedge":
        await run_hedge(
            urls=args.url,
            buy_outcome=args.outcome,
            both=args.both,
            stake_usdc=args.stake,
            target_plus=args.target_plus,
            expected_hours=args.hours,
            binance_symbol=args.binance,
            open_fee_rate=args.open_fee,
            close_fee_rate=args.close_fee,
        )

    elif args.cmd == "live":
        if args.graph:
            run_live_with_graph(
                urls=args.url,
                interval=args.interval,
                binance_symbol=args.binance,
                buy_outcome=args.outcome,
                both=args.both,
                stake_usdc=args.stake,
                target_plus=args.target_plus,
                expected_hours=args.hours,
                open_fee_rate=args.open_fee,
                close_fee_rate=args.close_fee,
                max_points=args.graph_points,
                graph_refresh_ms=args.graph_refresh_ms,
            )
        else:
            await run_live(
                urls=args.url,
                interval=args.interval,
                binance_symbol=args.binance,
                buy_outcome=args.outcome,
                both=args.both,
                stake_usdc=args.stake,
                target_plus=args.target_plus,
                expected_hours=args.hours,
                open_fee_rate=args.open_fee,
                close_fee_rate=args.close_fee,
            )

    else:
        raise RuntimeError("Unknown cmd")


if __name__ == "__main__":
    asyncio.run(async_main())
