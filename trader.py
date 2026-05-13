"""
Borker Trader — smart edition

Philosophy
----------
1. Only enter markets where the crowd has strong consensus (≥65%) AND liquidity is solid.
2. Size each bet using a Kelly-inspired score: price strength × pool dominance × momentum.
3. Track every position. Auto-sell on:
     • Take profit  – price rises 12pp above entry  (lock in gains)
     • Stop loss    – price drops 10pp below entry   (cut losses fast)
     • Flip         – the side we bought drops below 55%  (market turned)
4. Never hold more than MAX_POSITIONS at once.
5. Show a live portfolio summary every scan.
"""

import time, json, os, signal, sys
from datetime import datetime, timezone
import requests as _req
from typing import Optional
from main import BorkerClient, API_KEY, BASE_URL

# ── Config ─────────────────────────────────────────────────────────────────────

WIN_THRESHOLD   = 0.65    # minimum price to enter
MAX_COST        = 10      # max spend per trade (scales down with lower confidence)
MIN_COST        = 2       # min spend per trade
MAX_POSITIONS   = 10      # max concurrent holdings
MIN_LIQUIDITY_Q = 500_000 # skip thin markets
TAKE_PROFIT_PP  = 12      # sell when up this many pp from entry
STOP_LOSS_PP    = 10      # sell when down this many pp from entry
FLIP_THRESHOLD  = 0.55    # sell if our side drops below this (market flipped)
SLEEP_SECONDS   = 60
MAX_DAILY_SPEND = 100_000

CACHE_FILE = os.path.join(os.path.dirname(__file__), "positions.json")

# ── Colours ────────────────────────────────────────────────────────────────────

R = "\033[0m"
BOLD = "\033[1m"; DIM = "\033[2m"
GRN = "\033[92m"; YEL = "\033[93m"; RED = "\033[91m"; CYN = "\033[96m"

def clear_screen():
    os.system("clear")
    os.system("cls")

def bar(p: float, w: int = 18) -> str:
    n = round(p * w)
    return GRN + "█" * n + DIM + "░" * (w - n) + R

def fmt_q(q: float) -> str:
    q = q / 1000
    if q >= 1_000_000: return f"{q/1e6:.1f}M"
    if q >= 1_000:     return f"{q/1e3:.1f}K"
    return f"{q:.0f}"

def fmt_closes(close_ms) -> str:
    if close_ms is None:
        return None
    now      = datetime.now(tz=timezone.utc)
    close_dt = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).astimezone()
    secs     = (close_dt - now).total_seconds()
    date_str = close_dt.strftime("%-d %b %I:%M %p")
    if secs <= 0:
        countdown = "closed"
    elif secs < 12 * 3600:
        h, m = divmod(int(secs) // 60, 60)
        countdown = f"{h}h {m}m" if h else f"{m}m"
    elif secs < 24 * 3600:
        countdown = f"{round(secs/3600)}h"
    else:
        countdown = f"{round(secs/86400)}d"
    return f"{date_str} / {countdown}"

def hdr(title: str):
    print(f"\n{BOLD}── {title} {'─'*(54-len(title))}{R}")

# ── Cache ──────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(pos: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(pos, f, indent=2)

# ── Scoring ────────────────────────────────────────────────────────────────────

def score_market(winner: dict, outcomes: list, prev_prices: dict) -> float:
    """
    Returns a confidence score 0→1 combining:
      • Price strength   (50%) – how far above WIN_THRESHOLD
      • Pool dominance   (30%) – winner's share of total pool
      • Momentum         (20%) – is the winning price rising since last scan?
    """
    total_q   = sum(o["q"] for o in outcomes)
    price     = winner["price"]
    strength  = (price - WIN_THRESHOLD) / (1.0 - WIN_THRESHOLD)
    dominance = winner["q"] / total_q if total_q else 0

    prev  = prev_prices.get(winner["id"], price)
    delta = price - prev                          # positive = moving our way
    momentum = min(max((delta + 0.05) / 0.10, 0), 1)  # normalise –5pp→+5pp to 0→1

    return strength * 0.5 + dominance * 0.3 + momentum * 0.2


def trade_cost(s: float) -> int:
    return round(MIN_COST + s * (MAX_COST - MIN_COST))

# ── Position discovery ─────────────────────────────────────────────────────────

def discover_positions(markets: list) -> dict:
    """Probe every outcome for shares without executing a real sell."""
    found = {}
    sess = _req.Session()
    sess.headers["Authorization"] = f"Bearer {API_KEY}"
    for m in markets:
        for o in m["outcomes"]:
            for yn in ["yes", "no"]:
                r = sess.post(f"{BASE_URL}/markets/{m['slug']}/trade",
                              json={"outcomeId": o["id"], "shares": -999999, "yesNo": yn})
                j = r.json()
                if j.get("error") == "insufficient_shares" and j.get("have", 0) > 0:
                    found[m["slug"]] = {
                        "outcome_id": o["id"], "label": o["label"],
                        "shares": j["have"], "buy_price": o["price"],
                        "yes_no": yn, "cost_spent": 0,
                    }
                    break
    return found

# ── Main ───────────────────────────────────────────────────────────────────────

PROFIT_FILE = os.path.join(os.path.dirname(__file__), "profit.json")

def load_profit() -> dict:
    if os.path.exists(PROFIT_FILE):
        with open(PROFIT_FILE) as f:
            return json.load(f)
    return {}

def save_profit(data: dict):
    with open(PROFIT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def run(positions: dict):
    client = BorkerClient()
    me = client.me()
    display_bal = me['balanceBarks'] / 1000

    # Load or initialise profit tracking
    profit_data = load_profit()
    if "start_balance" not in profit_data:
        profit_data["start_balance"] = me["balanceBarks"]
        save_profit(profit_data)
    start_balance = profit_data["start_balance"]

    if "trade" not in me.get("scopes", []):
        print(f"{RED}API key missing 'trade' scope.{R}"); return

    positions.update(load_cache())

    # seed momentum baseline
    prev_prices: dict[str, float] = {}

    session_spend = 0

    while True:
        if session_spend >= MAX_DAILY_SPEND:
            print(f"\n{RED}Daily spend limit reached. Stopping.{R}"); break

        try:
            markets = client.list_markets("open")
        except Exception as e:
            print(f"{RED}fetch error: {e}{R}"); time.sleep(SLEEP_SECONDS); continue

        by_slug = {m["slug"]: m for m in markets}

        me = client.me()
        display_bal = me["balanceBarks"] / 1000

        clear_screen()
        print(f"\n{BOLD}Borker Trader{R}  @{me['handle']}  "
              f"balance: {display_bal:,.2f} Barks")
        print(f"buy≥{WIN_THRESHOLD*100:.0f}%  "
              f"TP+{TAKE_PROFIT_PP}pp  SL-{STOP_LOSS_PP}pp  "
              f"flip<{FLIP_THRESHOLD*100:.0f}%  "
              f"max {MAX_POSITIONS} positions")

        # ── Portfolio summary ──────────────────────────────────────────────────
        hdr("Portfolio")
        if not positions:
            print(f"  {DIM}No open positions.{R}")
        else:
            total_pl = 0.0
            for slug, pos in list(positions.items()):
                if pos.get("closed"): continue
                m = by_slug.get(slug)
                if not m:
                    print(f"  {DIM}✔ resolved  {slug[:52]}{R}")
                    del positions[slug]
                    save_cache(positions)
                    continue
                cur = next((o for o in m["outcomes"] if o["id"] == pos["outcome_id"]), None)
                if not cur: continue
                cp      = cur["price"]
                bp      = pos["buy_price"]
                pp      = (cp - bp) * 100
                spent   = pos.get("cost_spent", 0)
                est_pl  = spent * (cp / bp - 1) if bp else 0
                total_pl += est_pl
                col     = GRN if pp >= 0 else RED
                mo      = "↑" if cp > prev_prices.get(pos["outcome_id"], cp) else \
                          "↓" if cp < prev_prices.get(pos["outcome_id"], cp) else "→"
                outcomes  = m["outcomes"]
                total_q   = sum(o["q"] for o in outcomes)
                winner    = max(outcomes, key=lambda o: o["price"])
                close_str  = fmt_closes(m["closeAt"])
                close_part = f"closes {close_str}" if close_str else "no close date"

                print(f"  {col}{mo} {BOLD}{m['title']}{R}")
                if m["type"] == "binary" and len(outcomes) == 2:
                    yes_o = next((o for o in outcomes if o["label"].lower() == "yes"), outcomes[0])
                    no_o  = next((o for o in outcomes if o["label"].lower() == "no"),  outcomes[1])
                    yp, np_ = yes_o["price"], no_o["price"]
                    print(f"  {DIM}pool {fmt_q(total_q)} Barks | {close_part} | Yes {yp*100:.1f}pp / No {np_*100:.1f}pp{R}")
                    print(f"    {yp*100:.1f}% Yes {bar(yp)} No")
                else:
                    others_p = sum(o["price"] for o in outcomes if o["id"] != winner["id"])
                    print(f"  {DIM}pool {fmt_q(total_q)} Barks | {close_part} | {winner['label']} {winner['price']*100:.1f}pp / Others {others_p*100:.1f}pp{R}")
                    print(f"    {winner['price']*100:.1f}% {winner['label']} {bar(winner['price'])} Others")
                pl_col = GRN if est_pl >= 0 else RED
                print(f"  {col}entry {bp*100:5.1f}%  now {cp*100:5.1f}%  {pp:+.1f}pp  {pl_col}P&L: {est_pl/1000:+.2f} Barks{R}")
                print()
            sign = GRN if total_pl >= 0 else RED
            print(f"\n  {sign}Est. session P&L: {total_pl:+,.0f} costBarks{R}")

        # ── Sell section ───────────────────────────────────────────────────────
        hdr("Sell")
        sold_any = False
        for slug, pos in list(positions.items()):
            if pos.get("closed"): continue
            m = by_slug.get(slug)
            if not m:
                print(f"  {DIM}✔ resolved  {slug[:52]}{R}")
                del positions[slug]; save_cache(positions); continue

            cur = next((o for o in m["outcomes"] if o["id"] == pos["outcome_id"]), None)
            if not cur: continue

            cp, bp = cur["price"], pos["buy_price"]
            pp     = (cp - bp) * 100

            reason = None
            if cp < FLIP_THRESHOLD:
                reason = f"flipped below {FLIP_THRESHOLD*100:.0f}%"
            elif pp <= -STOP_LOSS_PP:
                reason = f"stop loss ({pp:+.1f}pp)"
            elif pp >= TAKE_PROFIT_PP:
                reason = f"take profit ({pp:+.1f}pp)"

            if reason:
                sold_any = True
                sell_lots = pos["shares"] // 1000
                col = GRN if pp >= 0 else RED
                print(f"  {col}selling {pos['label']:16s} {pp:+.1f}pp  reason: {reason}{R}")
                if sell_lots > 0:
                    try:
                        result = client.trade(slug, pos["outcome_id"],
                                              shares=-sell_lots,
                                              yes_no=pos["yes_no"])
                        recovered = -result["costBarks"]
                        print(f"  {col}✔ recovered {recovered:,}  "
                              f"balance={result['newBalance']:,.0f}{R}")
                        del positions[slug]; save_cache(positions)
                    except Exception as e:
                        resp = getattr(e, "response", None)
                        body = getattr(resp, "text", str(e))
                        if resp is not None and "market_not_open" in body:
                            positions[slug]["closed"] = True
                            save_cache(positions)
                        else:
                            print(f"  {RED}✘ sell failed: {body}{R}")
                else:
                    print(f"  {DIM}too few shares to sell{R}")

        if not sold_any:
            print(f"  {DIM}Nothing to sell this round.{R}")

        # ── Buy (silent) ───────────────────────────────────────────────────────
        trades_made = 0

        if len(positions) < MAX_POSITIONS and session_spend < MAX_DAILY_SPEND:
            for m in markets:
                if m["slug"] in positions: continue
                if len(positions) >= MAX_POSITIONS or session_spend >= MAX_DAILY_SPEND: break

                outcomes = m["outcomes"]
                total_q  = sum(o["q"] for o in outcomes)
                winner   = max(outcomes, key=lambda o: o["price"])

                if total_q < MIN_LIQUIDITY_Q: continue
                if winner["price"] < WIN_THRESHOLD: continue

                s    = score_market(winner, outcomes, prev_prices)
                cost = min(trade_cost(s), MAX_DAILY_SPEND - session_spend)

                try:
                    result = client.trade(m["slug"], winner["id"],
                                          max_cost=cost, yes_no="yes")
                    session_spend += result["costBarks"]
                    trades_made   += 1
                    pp_moved = (result["priceAfter"] - result["priceBefore"]) * 100
                    positions[m["slug"]] = {
                        "outcome_id": winner["id"],
                        "label":      winner["label"],
                        "shares":     result["shares"],
                        "buy_price":  result["priceAfter"],
                        "yes_no":     "yes",
                        "cost_spent": result["costBarks"],
                    }
                    save_cache(positions)
                    print(f"\n  {GRN}✔ new position  {m['title']}  "
                          f"{result['priceBefore']*100:.1f}%→{result['priceAfter']*100:.1f}% ({pp_moved:+.2f}pp){R}")
                except Exception as e:
                    pass  # silent on failure

        # Update momentum baseline for next scan
        for m in markets:
            for o in m["outcomes"]:
                prev_prices[o["id"]] = o["price"]

        # Profit summary
        current_bal = client.me()["balanceBarks"]
        profit      = current_bal - start_balance
        profit_data["last_profit"] = profit
        save_profit(profit_data)
        col = GRN if profit >= 0 else RED
        print(f"\n{'─'*58}")
        print(f"Holding {len(positions)}  |  "
              f"New buys: {trades_made}  |  "
              f"{BOLD}{col}Profit: {profit/1000:+.2f} Barks{R}  "
              f"Sleeping {SLEEP_SECONDS}s…")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    _pos = {}

    def _shutdown(sig, frame):
        save_cache(_pos)
        print(f"\n{YEL}Stopped — positions saved.{R}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    run(_pos)
