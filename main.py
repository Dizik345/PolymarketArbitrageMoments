# main.py
import argparse
import asyncio
import json
import math
import re
import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import signal
import aiohttp
import certifi

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
BINANCE_FAPI = "https://fapi.binance.com"


# -----------------------------
# Utils
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
# Dataclasses
# -----------------------------
@dataclass
class OutcomeQuote:
    outcome: str
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]


@dataclass
class GraphPoint:
    ts: datetime
    # list of (outcome, ask, need_ask)
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
    # side: "long" or "short"
    s = 1.0 if side.lower() == "long" else -1.0
    periods = max(0, math.ceil(max(0.0, expected_hours) / 8.0))
    pnl_price = s * (exit_price - entry)
    fee_open = entry * open_fee_rate
    fee_close = exit_price * close_fee_rate
    funding_payment = s * funding_rate_per_8h * entry * periods
    return pnl_price - fee_open - fee_close - funding_payment


def pretty_position(side_model: str, qty_btc_model: float) -> Tuple[str, float]:
    # qty_btc_model is in "model-side" space; if negative => opposite side.
    if math.isnan(qty_btc_model):
        return ("UNKNOWN", float("nan"))
    if abs(qty_btc_model) < 1e-18:
        return ("FLAT", 0.0)
    if qty_btc_model > 0:
        return ("LONG" if side_model.lower() == "long" else "SHORT", abs(qty_btc_model))
    return ("SHORT" if side_model.lower() == "long" else "LONG", abs(qty_btc_model))


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


# -----------------------------
# Printing
# -----------------------------
def print_monitor_lines(quotes: List[OutcomeQuote]):
    for q in quotes:
        bid_s = f"{q.best_bid:.4f}" if q.best_bid is not None else "—"
        ask_s = f"{q.best_ask:.4f}" if q.best_ask is not None else "—"
        print(f"  {q.outcome:<6}  bid {bid_s:<7} ask {ask_s:<7}")

    # internal arb only if Σasks < 1
    asks = [q.best_ask for q in quotes if q.best_ask is not None]
    if len(asks) == len(quotes):
        s = sum(asks)
        if s < 1.0:
            print(f"  ✅ INTERNAL-ARB: Σasks={s:.4f} (edge={1.0 - s:.4f})")


# -----------------------------
# Hedge math
# -----------------------------
def solve_qty_for_win_zero(poly_pnl_win: float, fut_per1_win: float) -> Optional[float]:
    if abs(fut_per1_win) < 1e-12:
        return None
    return -poly_pnl_win / fut_per1_win


def required_poly_ask_for_target(
    stake_usdc: float,
    fee_rate_bps: int,
    desired_plus_on_lose: float,
    fut_per1_win: float,
    fut_per1_lose: float,
) -> Optional[float]:
    def ok(p: float) -> bool:
        poly = polymarket_pnl(stake_usdc, p, fee_rate_bps)
        qty = solve_qty_for_win_zero(poly["pnl_win"], fut_per1_win)
        if qty is None:
            return False
        lose_total = poly["pnl_lose"] + qty * fut_per1_lose
        return lose_total >= desired_plus_on_lose

    lo, hi = 0.01, 0.99
    if not ok(lo):
        return None
    if ok(hi):
        return hi

    best = lo
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if ok(mid):
            best = mid
            lo = mid
        else:
            hi = mid
    return best


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
    Prints compact hedge block.
    Returns (ask, need_ask).
    """
    books = await clob_get_books(session, [token_id])
    _bid, ask = best_bid_ask(books.get(token_id, {}))
    if ask is None:
        print(f"  HEDGE({outcome_name}): no ask")
        return None, None

    fee_bps = await polymarket_fee_rate_bps(session, token_id)
    poly = polymarket_pnl(stake_usdc, ask, fee_bps)

    # parse barriers from outcomes like "80k"/"100k"
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

    # try both model sides; pick one that maximizes LOSE total
    best = None  # (side_model, qty_model, fut_win, fut_lose, lose_total, win_total)
    for side_model in ("long", "short"):
        fut_win = futures_pnl_per_1btc(
            side_model, entry, win_exit, open_fee_rate, close_fee_rate, fund, expected_hours
        )
        fut_lose = futures_pnl_per_1btc(
            side_model, entry, lose_exit, open_fee_rate, close_fee_rate, fund, expected_hours
        )

        qty_model = solve_qty_for_win_zero(poly["pnl_win"], fut_win)
        if qty_model is None:
            continue

        win_total = poly["pnl_win"] + qty_model * fut_win
        lose_total = poly["pnl_lose"] + qty_model * fut_lose

        cand = (side_model, qty_model, fut_win, fut_lose, lose_total, win_total)
        if best is None or cand[4] > best[4]:
            best = cand

    if best is None:
        print(f"  HEDGE({outcome_name}): no hedge candidate")
        return float(ask), None

    side_model, qty_model, fut_win, fut_lose, lose_total, _win_total = best
    eff_side, eff_qty_btc = pretty_position(side_model, qty_model)
    notional_usd = eff_qty_btc * entry

    bin_win = qty_model * fut_win
    bin_lose = qty_model * fut_lose

    ok = lose_total >= target_plus

    need_ask = None
    if not ok:
        need_ask = required_poly_ask_for_target(stake_usdc, fee_bps, target_plus, fut_win, fut_lose)

    print(f"  HEDGE({outcome_name}): ask={ask:.4f} shares≈{poly['shares']:.2f}")
    print(f"    Binance: {eff_side} ${notional_usd:.2f} notional ({eff_qty_btc:.6f} BTC)")
    print(f"    WIN :  Poly {poly['pnl_win']:+.2f}, Binance {bin_win:+.2f} => Total {(poly['pnl_win']+bin_win):+.2f}")
    print(f"    LOSE:  Poly {poly['pnl_lose']:+.2f}, Binance {bin_lose:+.2f} => Total {(poly['pnl_lose']+bin_lose):+.2f}")
    if not ok:
        if need_ask is None:
            print("    ❌ not feasible (assumptions)")
        else:
            print(f"    ❌ need ask <= {need_ask:.4f} to be feasible")
    else:
        print("  ✅✅✅ КНОПКА БАБЛО ✅✅✅")

    return float(ask), (None if need_ask is None else float(need_ask))


# -----------------------------
# One tick for one URL
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

    chosen_pairs: Optional[List[Tuple[str, str]]] = None

    # monitor all markets
    for m in markets:
        pairs = market_outcome_token_map(m)
        token_ids = [tid for _, tid in pairs]
        books = await clob_get_books(session, token_ids)

        quotes = []
        for outcome, tid in pairs:
            bid, ask = best_bid_ask(books.get(tid, {}))
            quotes.append(OutcomeQuote(outcome, tid, bid, ask))

        print_monitor_lines(quotes)

        # choose first "normal" market (2+ outcomes)
        if chosen_pairs is None and len(pairs) >= 2:
            chosen_pairs = pairs

    if chosen_pairs is None:
        print("  HEDGE: can't choose market for hedge (no suitable outcomes)")
        return GraphPoint(datetime.now(), [])

    series: List[Tuple[str, Optional[float], Optional[float]]] = []

    if both:
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
        # pick by substring
        needle = (buy_outcome or "").strip().lower()
        picked = None
        for o, tid in chosen_pairs:
            if needle in o.lower():
                picked = (o, tid)
                break
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
# Modes
# -----------------------------
async def run_monitor(urls: List[str], interval: int, binance_symbol: str):
    ssl_ctx = make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            prem = await binance_premium_index(session, binance_symbol)
            print(
                f"[{now_str()}] Binance {binance_symbol} mark={prem['markPrice']:.2f} funding8h={prem['lastFundingRate']:+.6%}"
            )
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
# Graph mode: collect then plot ONCE on stop (no live updates, no lag)
# -----------------------------
def plot_points_once(
    points: List[GraphPoint],
    save_png: Optional[str],
    both: bool,
    buy_outcome: str,
):
    import math as _math
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    if not points:
        print("[PLOT] No points collected.")
        return

    # outcomes в порядке появления
    outcomes: List[str] = []
    seen = set()
    for p in points:
        for outcome, _a, _n in p.series:
            if outcome not in seen:
                seen.add(outcome)
                outcomes.append(outcome)

    ts = [p.ts for p in points]

    fig, ax = plt.subplots(figsize=(13, 5))
    ax.set_title(f"Ask vs NeedAsk ({'BOTH' if both else buy_outcome})")
    ax.set_xlabel("time")
    ax.set_ylabel("price")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    for outcome in outcomes:
        ask_y: List[float] = []
        need_y: List[float] = []
        last_need = float("nan")

        for p in points:
            found = None
            for o, a, n in p.series:
                if o == outcome:
                    found = (a, n)
                    break

            if found is None:
                ask_y.append(float("nan"))
                need_y.append(last_need)
                continue

            a, n = found
            ask_y.append(float("nan") if a is None else float(a))

            if n is None:
                need_y.append(last_need)
            else:
                last_need = float(n)
                need_y.append(last_need)

        ax.plot(ts, ask_y, marker="o", markersize=3, linewidth=1.5, label=f"ask {outcome}")
        ax.plot(ts, need_y, linestyle="--", marker="o", markersize=3, linewidth=1.2, label=f"need {outcome}")

        # feasible: ask <= need
        fx, fy = [], []
        for x, a, n in zip(ts, ask_y, need_y):
            if (not _math.isnan(a)) and (not _math.isnan(n)) and (a <= n):
                fx.append(x)
                fy.append(a)
        if fx:
            ax.scatter(fx, fy, s=35, alpha=0.9, label=f"feasible {outcome}")

    ax.legend(loc="upper right")
    fig.autofmt_xdate()

    ymin, ymax = ax.get_ylim()
    ax.set_ylim(max(0.0, ymin - 0.01), min(1.0, ymax + 0.01))

    plt.tight_layout()

    if save_png:
        fig.savefig(save_png, dpi=180, bbox_inches="tight")
        print(f"[OK] saved graph -> {save_png}")

    plt.show()


async def run_live_collect_then_plot(
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
    save_png: Optional[str],
    exit_on_plot: bool = False,   # <- если True, то после графика выходим
):
    ssl_ctx = make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    points: List[GraphPoint] = []
    print("[INFO] graph mode: collecting points.")
    print("[INFO] Ctrl+C once  -> plot/save and CONTINUE")
    print("[INFO] Ctrl+C twice -> exit")

    stop_now = False
    want_plot = False
    ctrlc_count = 0

    loop = asyncio.get_running_loop()

    def on_sigint():
        nonlocal stop_now, want_plot, ctrlc_count
        ctrlc_count += 1
        if ctrlc_count == 1:
            want_plot = True
            print("\n[STOP] Ctrl+C -> plotting once, then continuing...")
        else:
            stop_now = True
            print("\n[STOP] Ctrl+C x2 -> exiting...")

    # нормальный обработчик SIGINT вместо KeyboardInterrupt/CancelledError каши
    loop.add_signal_handler(signal.SIGINT, on_sigint)

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while not stop_now:
                for url in urls:
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

                    if gp.series:
                        points.append(gp)
                        if len(points) > max_points:
                            points = points[-max_points:]

                # если попросили график — рисуем/сохраняем и продолжаем
                if want_plot:
                    want_plot = False
                    if points:
                        plot_points_once(points=points, save_png=save_png, both=both, buy_outcome=buy_outcome)
                        if exit_on_plot:
                            break
                    else:
                        print("[WARN] no points collected yet -> nothing to plot.")

                    # сбрасываем счетчик, чтобы следующий Ctrl+C снова был "plot"
                    ctrlc_count = 0

                # сон делаем маленькими кусками, чтобы быстрее реагировать на Ctrl+C
                for _ in range(max(1, int(interval * 10))):
                    if stop_now or want_plot:
                        break
                    await asyncio.sleep(0.1)

    finally:
        # возвращаем стандартный SIGINT handler (чтобы в других местах не ломалось)
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except Exception:
            pass
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

    # graph = collect then plot once (no live updates)
    p_live.add_argument("--graph", action="store_true", help="Collect points; on Ctrl+C plot ONCE (no lag)")
    p_live.add_argument("--graph_points", type=int, default=200, help="Max points kept for graph")
    p_live.add_argument("--save_png", default=None, help="Save graph to PNG on stop (optional)")

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
            await run_live_collect_then_plot(
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
                save_png=args.save_png,
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
