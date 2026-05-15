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

import getpass, time, json, os, signal, sys, socket, math, threading

if os.name == 'nt':
    import msvcrt, ctypes
    _kernel32 = ctypes.windll.kernel32
    _kernel32.SetConsoleMode(_kernel32.GetStdHandle(-11), 7)
else:
    import select, termios, tty
from datetime import datetime, timezone
import requests as _req
from main import BorkerClient, BASE_URL

# ── Config ─────────────────────────────────────────────────────────────────────

WIN_THRESHOLD   = 0.65    # minimum price to enter
MAX_COST        = 50      # max spend per trade in whole Barks (scales with score)
MIN_COST        = 2       # min spend per trade in whole Barks
MAX_POSITIONS   = 10      # max concurrent holdings
MIN_LIQUIDITY_Q = 500_000 # skip thin markets
TAKE_PROFIT_PP  = 12      # sell when up this many pp from entry
STOP_LOSS_PP    = 10      # sell when down this many pp from entry
FLIP_THRESHOLD  = 0.55    # sell if our side drops below this (market flipped)
SLEEP_SECONDS      = 60
MAX_DAILY_SPEND    = 100_000
SCORE_TOP_UP_DELTA = 0.15    # top up if score rises this much above entry score
MAX_POSITION_COST  = 50_000  # max total API-barks spent on a single position

CACHE_FILE  = os.path.join(os.path.dirname(__file__), "positions.json")
KEY_FILE    = os.path.join(os.path.dirname(__file__), ".borker_key")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
_current_handle = None

_DEFAULTS = {
    "WIN_THRESHOLD":    0.65,
    "PRICE_SWEET_MAX":  0.88,
    "FLIP_THRESHOLD":   0.55,
    "TAKE_PROFIT_PP":   12,
    "STOP_LOSS_PP":     10,
    "MAX_POSITIONS":    10,
    "MIN_LIQUIDITY_Q":  500_000,
    "MIN_COST":         2,
    "MAX_COST":         50,
    "MAX_POSITION_COST":50_000,
    "MAX_DAILY_SPEND":  100_000,
    "SCORE_TOP_UP_DELTA":0.15,
    "SLEEP_SECONDS":    60,
}

# ── Colours ────────────────────────────────────────────────────────────────────

R = "\033[0m"
BOLD = "\033[1m"; DIM = "\033[2m"
GRN = "\033[92m"; YEL = "\033[93m"; RED = "\033[91m"; CYN = "\033[96m"

def clear_screen():
    print("\n" * 100, end="")
    os.system("cls" if os.name == "nt" else "clear")

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
    date_str = close_dt.strftime("%#d %b %I:%M %p") if os.name == 'nt' else close_dt.strftime("%-d %b %I:%M %p")
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

def _load_json_file(path: str) -> dict:
    with open(path) as f:
        raw = f.read().strip()
    try:
        return json.loads(_decrypt(raw))
    except Exception:
        return json.loads(raw)

def _save_json_file(path: str, data: dict):
    with open(path, "w") as f:
        f.write(_encrypt(json.dumps(data, separators=(",", ":"))))

def load_cache():
    if os.path.exists(CACHE_FILE):
        data = _load_json_file(CACHE_FILE)
        return data.get("_user"), data.get("positions", {})
    return None, {}

def save_cache(pos: dict, user: str):
    _save_json_file(CACHE_FILE, {"_user": user, "positions": pos})

def _apply_config(cfg: dict):
    global WIN_THRESHOLD, PRICE_SWEET_MAX, FLIP_THRESHOLD, TAKE_PROFIT_PP, \
           STOP_LOSS_PP, MAX_POSITIONS, MIN_LIQUIDITY_Q, MIN_COST, MAX_COST, \
           MAX_POSITION_COST, MAX_DAILY_SPEND, SCORE_TOP_UP_DELTA, SLEEP_SECONDS
    for k, v in cfg.items():
        if   k == "WIN_THRESHOLD":    WIN_THRESHOLD    = float(v)
        elif k == "PRICE_SWEET_MAX":  PRICE_SWEET_MAX  = float(v)
        elif k == "FLIP_THRESHOLD":   FLIP_THRESHOLD   = float(v)
        elif k == "TAKE_PROFIT_PP":   TAKE_PROFIT_PP   = float(v)
        elif k == "STOP_LOSS_PP":     STOP_LOSS_PP     = float(v)
        elif k == "MAX_POSITIONS":    MAX_POSITIONS    = int(v)
        elif k == "MIN_LIQUIDITY_Q":  MIN_LIQUIDITY_Q  = float(v)
        elif k == "MIN_COST":         MIN_COST         = float(v)
        elif k == "MAX_COST":         MAX_COST         = float(v)
        elif k == "MAX_POSITION_COST":MAX_POSITION_COST= int(v)
        elif k == "MAX_DAILY_SPEND":  MAX_DAILY_SPEND  = int(v)
        elif k == "SCORE_TOP_UP_DELTA":SCORE_TOP_UP_DELTA = float(v)
        elif k == "SLEEP_SECONDS":    SLEEP_SECONDS    = int(v)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        _apply_config(_load_json_file(CONFIG_FILE))
    except Exception:
        pass

def reset_config():
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)
    _apply_config(_DEFAULTS)

def save_config():
    _save_json_file(CONFIG_FILE, {
        "WIN_THRESHOLD":    WIN_THRESHOLD,
        "PRICE_SWEET_MAX":  PRICE_SWEET_MAX,
        "FLIP_THRESHOLD":   FLIP_THRESHOLD,
        "TAKE_PROFIT_PP":   TAKE_PROFIT_PP,
        "STOP_LOSS_PP":     STOP_LOSS_PP,
        "MAX_POSITIONS":    MAX_POSITIONS,
        "MIN_LIQUIDITY_Q":  MIN_LIQUIDITY_Q,
        "MIN_COST":         MIN_COST,
        "MAX_COST":         MAX_COST,
        "MAX_POSITION_COST":MAX_POSITION_COST,
        "MAX_DAILY_SPEND":  MAX_DAILY_SPEND,
        "SCORE_TOP_UP_DELTA":SCORE_TOP_UP_DELTA,
        "SLEEP_SECONDS":    SLEEP_SECONDS,
    })

# ── Encryption ────────────────────────────────────────────────────────────────

_ALPHABET = "".join(chr(c) for c in [104,93,95,96,66,59,34,101,48,94,52,43,85,107,84,121,82,74,110,63,88,108,78,65,113,100,123,41,70,117,112,80,77,75,126,83,32,45,69,56,72,73,51,97,106,89,40,67,111,79,35,49,71,99,55,91,57,38,39,54,81,120,42,102,76,116,53,33,124,90,122,86,119,58,105,114,98,62,60,118,125,37,87,109,44,103,46,50,61,64,68,36,47,115])
_ENC_KEY  = "!v8M}3 hQ`^qT.2YbK;>Rz[=6Xp@,&fA#0W$jI/~{eU'9Gs*-Ln(4Cd)7OF+:ZiP\"<Bk?|wNDogElm_Jy]V5rHuaxtcS%1"

def _encrypt(text: str) -> str:
    out, shift = [], 0
    for ch in text:
        j = _ALPHABET.find(ch)
        if j == -1:
            raise ValueError(f"Cannot encrypt character: {ch!r}")
        shift = (shift + j) % len(_ENC_KEY)
        out.append(_ENC_KEY[shift])
    return "".join(out)

def _decrypt(text: str) -> str:
    out, shift = [], 0
    for ch in text:
        j = _ENC_KEY.find(ch)
        if j == -1:
            raise ValueError(f"Cannot decrypt character: {ch!r}")
        alpha = (j - shift + len(_ENC_KEY)) % len(_ENC_KEY)
        shift = (shift + alpha) % len(_ENC_KEY)
        out.append(_ALPHABET[alpha])
    return "".join(out)

# ── API key storage ────────────────────────────────────────────────────────────

def load_api_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            data = json.load(f)
        return _decrypt(data["key"]), _decrypt(data["device"])
    return None, None

def save_api_key(key: str):
    with open(KEY_FILE, "w") as f:
        json.dump({"key": _encrypt(key), "device": _encrypt(socket.gethostname())}, f)
    if os.name != 'nt':
        os.chmod(KEY_FILE, 0o600)

# ── Scoring ────────────────────────────────────────────────────────────────────

PRICE_SWEET_MAX = 0.88   # above this profit margin shrinks fast
MIN_CLOSE_SECS  = 2 * 3600   # skip markets closing in < 2 hours

def score_market(winner: dict, outcomes: list, prev_prices: dict, close_ms=None) -> float:
    """
    Score 0→1 across six factors:
      • Sweet-spot price  (25%) – 65–88% scores best; near-certain markets penalised
      • Consensus gap     (25%) – distance between #1 and #2 outcome
      • Pool dominance    (15%) – winner's share of total pool
      • Liquidity         (15%) – log-scaled pool size above minimum
      • Momentum          (10%) – price rising since last scan
      • Time to close     (10%) – prefers 1–7 day window; penalises same-hour or month+
    """
    total_q = sum(o["q"] for o in outcomes)
    price   = winner["price"]

    # 1. Sweet-spot price: ramps up 65→88%, fades above 88%
    if price <= PRICE_SWEET_MAX:
        strength = (price - WIN_THRESHOLD) / (PRICE_SWEET_MAX - WIN_THRESHOLD)
    else:
        strength = max(0.0, 1.0 - (price - PRICE_SWEET_MAX) / (1.0 - PRICE_SWEET_MAX))

    # 2. Consensus gap between winner and runner-up
    sorted_prices = sorted((o["price"] for o in outcomes), reverse=True)
    gap       = sorted_prices[0] - sorted_prices[1] if len(sorted_prices) >= 2 else sorted_prices[0]
    gap_score = min(gap / 0.40, 1.0)   # 40pp gap = perfect score

    # 3. Pool dominance
    dominance = winner["q"] / total_q if total_q else 0

    # 4. Liquidity (log scale above minimum)
    liq_score = min(math.log10(max(total_q / MIN_LIQUIDITY_Q, 1)) / 2.0, 1.0)

    # 5. Momentum
    prev     = prev_prices.get(winner["id"], price)
    momentum = min(max(((price - prev) + 0.05) / 0.10, 0), 1)

    # 6. Time-to-close scoring
    if close_ms:
        secs = close_ms / 1000 - time.time()
        if   secs < 6 * 3600:    time_score = 0.1   # closing too soon
        elif secs < 86400:        time_score = 0.6   # same day
        elif secs < 7 * 86400:   time_score = 1.0   # sweet spot 1–7 days
        elif secs < 30 * 86400:  time_score = 0.7   # 1–4 weeks
        else:                     time_score = 0.3   # very long-dated
    else:
        time_score = 0.4   # no close date — uncertain

    return (strength  * 0.25 +
            gap_score * 0.25 +
            dominance * 0.15 +
            liq_score * 0.15 +
            momentum  * 0.10 +
            time_score* 0.10)


def trade_cost(s: float) -> int:
    return round(MIN_COST + s * (MAX_COST - MIN_COST))

def edit_settings():
    global WIN_THRESHOLD, PRICE_SWEET_MAX, FLIP_THRESHOLD, TAKE_PROFIT_PP, \
           STOP_LOSS_PP, MAX_POSITIONS, MIN_LIQUIDITY_Q, MIN_COST, MAX_COST, \
           MAX_POSITION_COST, MAX_DAILY_SPEND, SCORE_TOP_UP_DELTA, SLEEP_SECONDS

    fields = [
        ("WIN_THRESHOLD",      WIN_THRESHOLD,        "pct"),
        ("PRICE_SWEET_MAX",    PRICE_SWEET_MAX,      "pct"),
        ("FLIP_THRESHOLD",     FLIP_THRESHOLD,       "pct"),
        ("TAKE_PROFIT_PP",     TAKE_PROFIT_PP,       "float"),
        ("STOP_LOSS_PP",       STOP_LOSS_PP,         "float"),
        ("MAX_POSITIONS",      MAX_POSITIONS,        "int"),
        ("MIN_LIQUIDITY_Q",    MIN_LIQUIDITY_Q/1000, "liq"),
        ("MIN_COST",           MIN_COST,             "float"),
        ("MAX_COST",           MAX_COST,             "float"),
        ("MAX_POSITION_COST",  MAX_POSITION_COST,    "int"),
        ("MAX_DAILY_SPEND",    MAX_DAILY_SPEND,      "int"),
        ("SCORE_TOP_UP_DELTA", SCORE_TOP_UP_DELTA,   "float"),
        ("SLEEP_SECONDS",      SLEEP_SECONDS,        "int"),
    ]

    print(f"\n{BOLD}── Settings {'─'*46}{R}\n")

    for name, current, kind in fields:
        if kind == "pct":
            display = f"{current*100:.1f}%"
        elif kind == "liq":
            display = f"{current:.0f}"
        else:
            display = str(current)

        print(f"  {BOLD}{name}{R}  current = {CYN}{display}{R}")
        try:
            raw = input(f"  {DIM}[r to reset, Enter to skip]:{R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            print(f"  {DIM}─ kept {display}{R}")
            print()
            continue

        if raw.lower() == "r":
            val = _DEFAULTS[name]
        else:
            try:
                val = float(raw)
                if kind == "pct": val = val / 100
                if kind == "int": val = int(val)
                if kind == "liq": val = val * 1000
            except ValueError:
                print(f"  {RED}✘ invalid value, keeping {display}{R}")
                print()
                continue

        if   name == "WIN_THRESHOLD":    WIN_THRESHOLD    = val
        elif name == "PRICE_SWEET_MAX":  PRICE_SWEET_MAX  = val
        elif name == "FLIP_THRESHOLD":   FLIP_THRESHOLD   = val
        elif name == "TAKE_PROFIT_PP":   TAKE_PROFIT_PP   = val
        elif name == "STOP_LOSS_PP":     STOP_LOSS_PP     = val
        elif name == "MAX_POSITIONS":    MAX_POSITIONS    = int(val)
        elif name == "MIN_LIQUIDITY_Q":  MIN_LIQUIDITY_Q  = val
        elif name == "MIN_COST":         MIN_COST         = val
        elif name == "MAX_COST":         MAX_COST         = val
        elif name == "MAX_POSITION_COST":MAX_POSITION_COST= int(val)
        elif name == "MAX_DAILY_SPEND":  MAX_DAILY_SPEND  = int(val)
        elif name == "SCORE_TOP_UP_DELTA":SCORE_TOP_UP_DELTA = val
        elif name == "SLEEP_SECONDS":    SLEEP_SECONDS    = int(val)

        if raw.lower() == "r":
            print(f"  {YEL}↺ {name} reset to default{R}")
        else:
            print(f"  {GRN}✔ {name} updated{R}")
        print()

    save_config()
    print(f"\n{BOLD}{'─'*58}{R}\n")

# ── Position discovery ─────────────────────────────────────────────────────────

def discover_positions(markets: list, api_key: str, cancel=None) -> dict:
    """Probe every outcome for shares without executing a real sell."""
    found = {}
    sess = _req.Session()
    sess.headers["Authorization"] = f"Bearer {api_key}"
    for m in markets:
        if cancel and cancel.is_set():
            break
        for o in m["outcomes"]:
            if cancel and cancel.is_set():
                break
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
        return _load_json_file(PROFIT_FILE)
    return {}

def save_profit(data: dict):
    _save_json_file(PROFIT_FILE, data)


def run(positions: dict, api_key: str):
    global _current_handle
    client = BorkerClient(api_key)
    me = client.me()
    _current_handle = me["handle"]
    display_bal = me['balanceBarks'] / 1000

    if "trade" not in me.get("scopes", []):
        print(f"{RED}API key missing 'trade' scope.{R}"); return

    cached_user, cached_pos = load_cache()
    user_changed = bool(cached_user and cached_user != me["handle"])
    if user_changed:
        print(f"{YEL}Different user detected ({cached_user} → {me['handle']}) — resetting cache.{R}")
        reset_config()
    else:
        positions.update(cached_pos)

    # Sync positions — runs in background, press any key to skip
    _cancel   = threading.Event()
    _result   = {}
    _all_mkts = client.list_markets("open")

    def _sync():
        try:
            _result.update(discover_positions(_all_mkts, api_key, _cancel))
        except Exception:
            pass

    _t = threading.Thread(target=_sync, daemon=True)
    _t.start()

    print(f"{DIM}Scanning for existing positions… press any key to skip{R}", end="", flush=True)
    if os.name == 'nt':
        while _t.is_alive():
            if msvcrt.kbhit():
                msvcrt.getch()
                _cancel.set()
                break
            time.sleep(0.2)
    else:
        _old_term = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            while _t.is_alive():
                if select.select([sys.stdin], [], [], 0.2)[0]:
                    sys.stdin.read(1)
                    _cancel.set()
                    break
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_term)

    _t.join()
    print()  # newline after the prompt

    if _cancel.is_set():
        print(f"{DIM}Position sync skipped.{R}")
    else:
        new_found = {k: v for k, v in _result.items() if k not in positions}
        if new_found:
            positions.update(new_found)
            save_cache(positions, _current_handle)
            print(f"{YEL}Synced {len(new_found)} existing position(s) from account.{R}")
        else:
            print(f"{DIM}No untracked positions found.{R}")

    # Load or initialise profit tracking
    profit_data = load_profit()
    if user_changed and me["handle"] != "awa":
        profit_data = {}
        print(f"{YEL}Profit reset for new user.{R}")
    is_new = "start_balance" not in profit_data
    if is_new:
        profit_data["start_balance"] = me["balanceBarks"]
        save_profit(profit_data)
        print(f"\n{BOLD}New account — configure your settings:{R}")
        edit_settings()
        input(f"\n{DIM}Press Enter to start trading…{R}")
    start_balance = profit_data["start_balance"]

    # seed momentum baseline
    prev_prices: dict[str, float] = {}

    session_spend = 0
    run_count     = 0

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
                    save_cache(positions, _current_handle)
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
                del positions[slug]; save_cache(positions, _current_handle); continue

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
                        del positions[slug]; save_cache(positions, _current_handle)
                    except Exception as e:
                        resp = getattr(e, "response", None)
                        body = getattr(resp, "text", str(e))
                        if resp is not None and "market_not_open" in body:
                            positions[slug]["closed"] = True
                            save_cache(positions, _current_handle)
                        else:
                            print(f"  {RED}✘ sell failed: {body}{R}")
                else:
                    print(f"  {DIM}too few shares to sell{R}")

        if not sold_any:
            print(f"  {DIM}Nothing to sell this round.{R}")

        # ── Score and rank all candidates ──────────────────────────────────────
        candidates = []
        for m in markets:
            if m["slug"] in positions: continue
            outcomes = m["outcomes"]
            if not outcomes: continue
            total_q  = sum(o["q"] for o in outcomes)
            winner   = max(outcomes, key=lambda o: o["price"])
            close_ms = m.get("closeAt")

            # Hard filters
            if total_q < MIN_LIQUIDITY_Q: continue
            if winner["price"] < WIN_THRESHOLD: continue
            if winner["price"] > 0.95: continue          # profit margin too thin
            if close_ms and close_ms / 1000 - time.time() < MIN_CLOSE_SECS: continue

            s = score_market(winner, outcomes, prev_prices, close_ms)
            candidates.append((s, m, winner))

        candidates.sort(key=lambda x: x[0], reverse=True)

        hdr("Top Markets")
        if not candidates:
            print(f"  {DIM}No qualifying markets this scan.{R}")
        else:
            for rank, (s, m, winner) in enumerate(candidates[:8], 1):
                bar_w   = round(s * 10)
                bar_str = GRN + "█" * bar_w + DIM + "░" * (10 - bar_w) + R
                held    = "  ◆ held" if m["slug"] in positions else ""
                print(f"  {BOLD}#{rank}{R} {bar_str} {s:.2f}  "
                      f"{winner['price']*100:.1f}%  {m['title'][:44]}{DIM}{held}{R}")

        # ── Buy top-ranked candidates ───────────────────────────────────────────
        trades_made = 0
        slots = MAX_POSITIONS - len(positions)

        rich = display_bal > 300  # above 300 Barks — scale spend with score

        for s, m, winner in candidates:
            if slots <= 0 or session_spend >= MAX_DAILY_SPEND: break
            base_cost = trade_cost(s)
            cost = round(base_cost * (1 + s)) if rich else base_cost
            cost = min(cost, MAX_DAILY_SPEND - session_spend)
            try:
                result = client.trade(m["slug"], winner["id"],
                                      max_cost=cost, yes_no="yes")
                session_spend += result["costBarks"]
                trades_made   += 1
                slots         -= 1
                pp_moved = (result["priceAfter"] - result["priceBefore"]) * 100
                positions[m["slug"]] = {
                    "outcome_id": winner["id"],
                    "label":      winner["label"],
                    "shares":     result["shares"],
                    "buy_price":  result["priceAfter"],
                    "yes_no":     "yes",
                    "cost_spent": result["costBarks"],
                    "buy_score":  s,
                }
                save_cache(positions, _current_handle)
                print(f"\n  {GRN}✔ bought #{candidates.index((s,m,winner))+1}  {m['title']}  "
                      f"{result['priceBefore']*100:.1f}%→{result['priceAfter']*100:.1f}% ({pp_moved:+.2f}pp){R}")
            except Exception as e:
                resp = getattr(e, "response", None)
                body = getattr(resp, "text", str(e))
                print(f"  {RED}✘ buy failed: {body}{R}")

        # ── Top up existing positions ───────────────────────────────────────────
        for m in markets:
            slug = m["slug"]
            if slug not in positions: continue
            pos = positions[slug]
            if pos.get("closed"): continue
            if pos.get("cost_spent", 0) >= MAX_POSITION_COST: continue
            if session_spend >= MAX_DAILY_SPEND: break

            outcomes = m["outcomes"]
            if not outcomes: continue
            winner   = max(outcomes, key=lambda o: o["price"])
            close_ms = m.get("closeAt")

            if winner["price"] < WIN_THRESHOLD or winner["price"] > 0.95: continue
            if close_ms and close_ms / 1000 - time.time() < MIN_CLOSE_SECS: continue

            cur_score   = score_market(winner, outcomes, prev_prices, close_ms)
            entry_score = pos.get("buy_score", cur_score)

            # Rich: top up any held position with a decent score
            # Normal: only top up if score has risen by the delta
            if rich:
                if cur_score < 0.5: continue
            else:
                if cur_score < entry_score + SCORE_TOP_UP_DELTA: continue

            headroom   = MAX_POSITION_COST - pos.get("cost_spent", 0)
            base_cost  = trade_cost(cur_score)
            cost       = round(base_cost * (1 + cur_score)) if rich else base_cost
            cost       = min(cost, headroom, MAX_DAILY_SPEND - session_spend)
            if cost <= 0: continue

            try:
                result = client.trade(slug, pos["outcome_id"],
                                      max_cost=cost, yes_no=pos["yes_no"])
                session_spend     += result["costBarks"]
                trades_made       += 1
                pos["shares"]     += result["shares"]
                pos["cost_spent"] += result["costBarks"]
                pos["buy_score"]   = cur_score
                save_cache(positions, _current_handle)
                pp_moved = (result["priceAfter"] - result["priceBefore"]) * 100
                print(f"\n  {CYN}↑ topped up  {m['title']}  score {cur_score:.2f}  "
                      f"({pp_moved:+.2f}pp){R}")
            except Exception as e:
                resp = getattr(e, "response", None)
                body = getattr(resp, "text", str(e))
                print(f"  {RED}✘ top-up failed: {body}{R}")

        # Update momentum baseline for next scan
        for m in markets:
            for o in m["outcomes"]:
                prev_prices[o["id"]] = o["price"]

        # Profit summary
        current_bal = client.me()["balanceBarks"]
        profit      = current_bal - start_balance
        profit_data["last_profit"] = profit
        save_profit(profit_data)
        run_count += 1
        col = GRN if profit >= 0 else RED
        print(f"\n{'─'*58}")
        print(f"Run #{run_count}  |  "
              f"Holding {len(positions)}  |  "
              f"New buys: {trades_made}  |  "
              f"{BOLD}{col}Profit: {profit/1000:+.2f} Barks{R}  "
              f"Sleeping {SLEEP_SECONDS}s…  {DIM}[s] settings{R}")

        # Sleep, but watch for 's' to show settings
        if os.name == 'nt':
            deadline = time.time() + SLEEP_SECONDS
            while time.time() < deadline:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch().lower()
                    if ch == "s":
                        edit_settings()
                        break
                time.sleep(0.2)
        else:
            _old = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                deadline = time.time() + SLEEP_SECONDS
                while time.time() < deadline:
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        ch = sys.stdin.read(1).lower()
                        if ch == "s":
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old)
                            edit_settings()
                            tty.setraw(sys.stdin.fileno())
                            break
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old)


def sell_all(api_key: str):
    client = BorkerClient(api_key)
    _, positions = load_cache()

    if not positions:
        print(f"{YEL}No cached positions to sell.{R}")
        return

    print(f"{BOLD}Selling all {len(positions)} position(s)…{R}\n")
    markets = {m["slug"]: m for m in client.list_markets("open")}

    for slug, pos in list(positions.items()):
        if pos.get("closed"): continue
        m = markets.get(slug)
        if not m:
            print(f"  {DIM}✔ {slug} — already resolved{R}")
            del positions[slug]
            continue

        sell_lots = pos["shares"] // 1000
        if sell_lots <= 0:
            print(f"  {DIM}✘ {slug} — too few shares to sell{R}")
            continue

        try:
            result = client.trade(slug, pos["outcome_id"],
                                  shares=-sell_lots, yes_no=pos["yes_no"])
            recovered = -result["costBarks"]
            cp  = result["priceAfter"]
            bp  = pos["buy_price"]
            pp  = (cp - bp) * 100
            col = GRN if pp >= 0 else RED
            print(f"  {col}✔ sold  {m['title'][:50]}")
            print(f"     {pp:+.1f}pp  recovered {recovered/1000:,.2f} Barks  "
                  f"balance {result['newBalance']/1000:,.2f} Barks{R}")
            del positions[slug]
        except Exception as e:
            resp = getattr(e, "response", None)
            body = getattr(resp, "text", str(e))
            print(f"  {RED}✘ {slug} — sell failed: {body}{R}")

    save_cache(positions, load_cache()[0] or "unknown")
    print(f"\n{BOLD}Done.{R}")


if __name__ == "__main__":
    _stored_key, _stored_device = load_api_key()
    _this_device = socket.gethostname()

    if _stored_key and _stored_device == _this_device:
        _api_key = _stored_key
    else:
        if _stored_device and _stored_device != _this_device:
            print(f"{YEL}New device detected — resetting and re-authenticating.{R}")
            for _f in (CACHE_FILE, PROFIT_FILE, CONFIG_FILE):
                if os.path.exists(_f):
                    os.remove(_f)
        _api_key = getpass.getpass("Enter your API key: ")
        save_api_key(_api_key)

    while True:
        try:
            BorkerClient(_api_key).me()
            break
        except _req.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                print(f"{RED}Invalid API key — clearing stored credentials.{R}")
                if os.path.exists(KEY_FILE):
                    os.remove(KEY_FILE)
                _api_key = getpass.getpass("Enter your API key: ")
                save_api_key(_api_key)
            else:
                raise

    load_config()

    if "--sell-all" in sys.argv:
        sell_all(_api_key)
        sys.exit(0)

    _pos = {}

    def _shutdown(*_):
        save_cache(_pos, _current_handle)
        print(f"\n{YEL}Stopped — positions saved.{R}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, _shutdown)
    run(_pos, _api_key)
