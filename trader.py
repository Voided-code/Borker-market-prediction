import getpass, time, json, os, signal, sys, socket, math, threading, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

if os.name == 'nt':
    import msvcrt, ctypes
    _kernel32 = ctypes.windll.kernel32
    _kernel32.SetConsoleMode(_kernel32.GetStdHandle(-11), 7)
else:
    import select, termios, tty
from datetime import datetime, timezone, timedelta
import requests as _req
from main import BorkerClient, BASE_URL

# ── Config ─────────────────────────────────────────────────────────────────────

WIN_THRESHOLD   = 0.65    # minimum price to enter
MAX_COST        = 50      # max spend per trade in whole Barks (scales with score)
MIN_COST        = 2       # min spend per trade in whole Barks
MAX_POSITIONS   = 10      # max concurrent holdings
MIN_LIQUIDITY_Q = 500     # skip thin markets (Barks)
TAKE_PROFIT_PP  = 12      # sell when up this many pp from entry
STOP_LOSS_PP    = 10      # sell when down this many pp from entry
FLIP_THRESHOLD  = 0.55    # sell if our side drops below this (market flipped)
SLEEP_SECONDS      = 60     # seconds to wait between scans (can be set to 0 for no delay, but not recommended)
MAX_DAILY_SPEND    = 100  # max daily spend (Barks)
SCORE_TOP_UP_DELTA = 0.15    # top up if score rises this much above entry score
MAX_POSITION_COST  = 50   # max total spend on a single position (Barks)
FORCE_ABOVE     = 300  # above this balance, use scaled-up trade sizing (Barks)
AUTO_SYNC       = True # scan for untracked positions on startup

_DIR        = os.path.dirname(__file__)
CACHE_DIR   = os.path.join(_DIR, "cache")
CACHE_FILE  = os.path.join(CACHE_DIR, "positions.json")
KEY_FILE    = os.path.join(CACHE_DIR, ".borker_key")
CONFIG_FILE = os.path.join(CACHE_DIR, "config.json")
PROFIT_FILE = os.path.join(CACHE_DIR, "profit.json")
os.makedirs(CACHE_DIR, exist_ok=True)

# Migrate files from root to cache/ on first run with new layout
for _old, _new in [
    (os.path.join(_DIR, "positions.json"), CACHE_FILE),
    (os.path.join(_DIR, ".borker_key"),    KEY_FILE),
    (os.path.join(_DIR, "config.json"),    CONFIG_FILE),
    (os.path.join(_DIR, "profit.json"),    PROFIT_FILE),
]:
    if os.path.exists(_old) and not os.path.exists(_new):
        os.rename(_old, _new)

_current_handle = None
_caff_proc  = None
_sleep_active = False

_DEFAULTS = {
    "WIN_THRESHOLD":    0.65,
    "PRICE_SWEET_MAX":  0.88,
    "FLIP_THRESHOLD":   0.55,
    "TAKE_PROFIT_PP":   12,
    "STOP_LOSS_PP":     10,
    "MAX_POSITIONS":    10,
    "MIN_LIQUIDITY_Q":  500,
    "MIN_COST":         2,
    "MAX_COST":         50,
    "MAX_POSITION_COST":50,
    "MAX_DAILY_SPEND":  100,
    "SCORE_TOP_UP_DELTA":0.15,
    "SLEEP_SECONDS":    60,
    "FORCE_ABOVE":   300,
    "AUTO_SYNC":     True,
}

# ── Colours ────────────────────────────────────────────────────────────────────

R = "\033[0m"
BOLD = "\033[1m"; DIM = "\033[2m"
GRN = "\033[92m"; YEL = "\033[93m"; RED = "\033[91m"; CYN = "\033[96m"

def _toggle_sleep():
    global _caff_proc, _sleep_active
    if os.name == 'nt':
        if _sleep_active:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # reset — allow sleep
            _sleep_active = False
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # prevent sleep
            _sleep_active = True
    elif sys.platform == 'darwin':
        if _sleep_active:
            if _caff_proc:
                _caff_proc.terminate()
                _caff_proc = None
            _sleep_active = False
        else:
            _caff_proc = subprocess.Popen(
                ['caffeinate', '-i'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _sleep_active = True

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
           MAX_POSITION_COST, MAX_DAILY_SPEND, SCORE_TOP_UP_DELTA, SLEEP_SECONDS, \
           FORCE_ABOVE, AUTO_SYNC
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
        elif k == "MAX_POSITION_COST":MAX_POSITION_COST= float(v)
        elif k == "MAX_DAILY_SPEND":  MAX_DAILY_SPEND  = float(v)
        elif k == "SCORE_TOP_UP_DELTA":SCORE_TOP_UP_DELTA = float(v)
        elif k == "SLEEP_SECONDS":    SLEEP_SECONDS    = int(v)
        elif k == "FORCE_ABOVE":      FORCE_ABOVE      = float(v)
        elif k == "AUTO_SYNC":        AUTO_SYNC        = bool(v)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        cfg = _load_json_file(CONFIG_FILE)
        if not cfg.get("migrated_v2"):
            for key in ("MIN_LIQUIDITY_Q", "MAX_POSITION_COST", "MAX_DAILY_SPEND"):
                if key in cfg and cfg[key] > 1000:
                    cfg[key] /= 1000
            cfg["migrated_v2"] = True
            _save_json_file(CONFIG_FILE, cfg)
        _apply_config(cfg)
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
        "FORCE_ABOVE":   FORCE_ABOVE,
        "AUTO_SYNC":     AUTO_SYNC,
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
    liq_score = min(math.log10(max(total_q / 1000 / MIN_LIQUIDITY_Q, 1)) / 2.0, 1.0)

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
    _important = [
        ("AUTO_SYNC",       "bool"),
        ("MAX_DAILY_SPEND", "float"),
        ("MAX_POSITIONS",   "int"),
        ("MAX_COST",        "float"),
        ("MIN_COST",        "float"),
        ("WIN_THRESHOLD",   "pct"),
        ("FORCE_ABOVE",     "float"),
        ("SLEEP_SECONDS",   "int"),
    ]
    _other = [
        ("PRICE_SWEET_MAX",    "pct"),
        ("FLIP_THRESHOLD",     "pct"),
        ("TAKE_PROFIT_PP",     "float"),
        ("STOP_LOSS_PP",       "float"),
        ("MIN_LIQUIDITY_Q",    "float"),
        ("MAX_POSITION_COST",  "float"),
        ("SCORE_TOP_UP_DELTA", "float"),
    ]
    DISPLAY   = {"FORCE_ABOVE": "min barks owned"}
    ACTIONS   = ["Save & Exit", "Exit", "Reset & Exit"]
    OTHER_IDX = len(_important)          # index of the "Other Settings" row
    n_items   = OTHER_IDX + 1 + len(ACTIONS)

    vals = {
        "AUTO_SYNC": AUTO_SYNC, "WIN_THRESHOLD": WIN_THRESHOLD,
        "PRICE_SWEET_MAX": PRICE_SWEET_MAX, "FLIP_THRESHOLD": FLIP_THRESHOLD,
        "TAKE_PROFIT_PP": TAKE_PROFIT_PP, "STOP_LOSS_PP": STOP_LOSS_PP,
        "MAX_POSITIONS": MAX_POSITIONS, "MIN_LIQUIDITY_Q": MIN_LIQUIDITY_Q,
        "MIN_COST": MIN_COST, "MAX_COST": MAX_COST,
        "MAX_POSITION_COST": MAX_POSITION_COST, "MAX_DAILY_SPEND": MAX_DAILY_SPEND,
        "SCORE_TOP_UP_DELTA": SCORE_TOP_UP_DELTA, "SLEEP_SECONDS": SLEEP_SECONDS,
        "FORCE_ABOVE": FORCE_ABOVE,
    }
    _orig = dict(vals)
    selected = 0

    def fmt(name, kind):
        v = vals[name]
        if kind == "pct":  return f"{v*100:.1f}%"
        if kind == "bool": return "on" if v else "off"
        if kind == "int":  return str(int(v))
        return str(v)

    def p(s=""):
        sys.stdout.write(s + "\r\n")
        sys.stdout.flush()

    def draw():
        clear_screen()
        p()
        p(f"{BOLD}── Settings {'─'*46}{R}")
        p()
        for i, (name, kind) in enumerate(_important):
            label   = DISPLAY.get(name, name)
            v       = fmt(name, kind)
            changed = vals[name] != _orig[name]
            val_col = YEL if changed else ""
            if i == selected:
                p(f"  {CYN}{BOLD}→ {label:<22}{val_col}{v}{R}")
            else:
                p(f"    {DIM}{label:<22}{R}{val_col}{v}{R}")
        other_changed = any(vals[n] != _orig[n] for n, _ in _other)
        other_col = YEL if other_changed else DIM
        if OTHER_IDX == selected:
            p(f"  {CYN}{BOLD}→ Other Settings {'─'*3}►{R}")
        else:
            p(f"    {other_col}Other Settings {'─'*3}►{R}")
        p()
        p(f"  {'─'*54}")
        p()
        for j, action in enumerate(ACTIONS):
            idx = OTHER_IDX + 1 + j
            if idx == selected:
                p(f"  {CYN}{BOLD}→ {action}{R}")
            else:
                p(f"    {DIM}{action}{R}")
        p()
        p(f"  {DIM}[↑↓ / k i] navigate  [Enter] select  [Esc] cancel{R}")

    def read_key():
        if os.name == 'nt':
            ch = msvcrt.getwch()
            if ch == '\xe0':
                ch2 = msvcrt.getwch()
                if ch2 == 'H': return 'UP'
                if ch2 == 'P': return 'DOWN'
                return ''
            if ch in ('\r', '\n'): return 'ENTER'
            if ch == '\x1b':       return 'ESC'
            if ch == '\x03':       return 'CTRL_C'
            return ch
        else:
            fd = sys.stdin.fileno()
            ch = os.read(fd, 1)
            if ch == b'\x1b':
                if select.select([sys.stdin], [], [], 0.02)[0]:
                    rest = os.read(fd, 2)
                    if rest == b'[A': return 'UP'
                    if rest == b'[B': return 'DOWN'
                return 'ESC'
            if ch in (b'\r', b'\n'): return 'ENTER'
            if ch == b'\x03':        return 'CTRL_C'
            return ch.decode('utf-8', errors='replace')

    def cooked():
        if os.name != 'nt':
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_term)

    def raw():
        if os.name != 'nt':
            tty.setraw(sys.stdin.fileno())

    def edit_field(name, kind):
        cooked()
        clear_screen()
        v = vals[name]
        if kind == "pct":   display, hint = f"{v*100:.1f}%", "enter %, e.g. 65"
        elif kind == "bool": display, hint = ("on" if v else "off"), "on / off"
        elif kind == "int":  display, hint = str(int(v)), "integer"
        else:                display, hint = str(v), "number"
        print(f"\n  {BOLD}{DISPLAY.get(name, name)}{R}  current = {CYN}{display}{R}")
        try:
            user_in = input(f"  {DIM}[{hint}, r to reset, Enter to keep]:{R} ").strip()
        except (EOFError, KeyboardInterrupt):
            user_in = ""
        new_v = v
        if user_in.lower() == "r":
            new_v = _DEFAULTS[name]
        elif user_in:
            if kind == "bool":
                if user_in.lower() in ("on","1","yes","true"):   new_v = True
                elif user_in.lower() in ("off","0","no","false"): new_v = False
            elif kind == "pct":
                try: new_v = float(user_in) / 100
                except ValueError: pass
            elif kind == "int":
                try: new_v = int(float(user_in))
                except ValueError: pass
            else:
                try: new_v = float(user_in)
                except ValueError: pass
        vals[name] = new_v
        if new_v != v:
            if kind == "pct":   new_display = f"{new_v*100:.1f}%"
            elif kind == "bool": new_display = "on" if new_v else "off"
            elif kind == "int":  new_display = str(int(new_v))
            else:                new_display = str(new_v)
            print(f"  {YEL}{BOLD}✔ {name} → {new_display}{R}")
            time.sleep(0.8)
        raw()

    def confirm(msg):
        cooked()
        clear_screen()
        print(f"\n  {YEL}{BOLD}{msg}{R}")
        while True:
            try:
                answer = input(f"  {DIM}[y/n]:{R} ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                raw()
                return False
            if answer in ('y', 'yes'):
                raw()
                return True
            if answer in ('n', 'no'):
                raw()
                return False
            print(f"  {RED}Please type y or n.{R}")

    def edit_other():
        sub_sel = 0
        sub_n   = len(_other) + 1  # fields + Back

        def draw_other():
            clear_screen()
            p()
            p(f"{BOLD}── Other Settings {'─'*40}{R}")
            p()
            for i, (name, kind) in enumerate(_other):
                label   = DISPLAY.get(name, name)
                v       = fmt(name, kind)
                changed = vals[name] != _orig[name]
                val_col = YEL if changed else ""
                if i == sub_sel:
                    p(f"  {CYN}{BOLD}→ {label:<22}{val_col}{v}{R}")
                else:
                    p(f"    {DIM}{label:<22}{R}{val_col}{v}{R}")
            p()
            p(f"  {'─'*54}")
            p()
            back_idx = len(_other)
            if sub_sel == back_idx:
                p(f"  {CYN}{BOLD}→ ← Back{R}")
            else:
                p(f"    {DIM}← Back{R}")
            p()
            p(f"  {DIM}[↑↓ / k i] navigate  [Enter] select  [Esc] back{R}")

        while True:
            draw_other()
            key = read_key()
            if key in ('UP', 'k'):
                sub_sel = (sub_sel - 1) % sub_n
            elif key in ('DOWN', 'i'):
                sub_sel = (sub_sel + 1) % sub_n
            elif key in ('ESC', 'CTRL_C', 'BACK'):
                break
            elif key == 'ENTER':
                if sub_sel < len(_other):
                    name, kind = _other[sub_sel]
                    if kind == "bool":
                        vals[name] = not vals[name]
                    else:
                        edit_field(name, kind)
                else:
                    break  # Back

    _old_term = None
    if os.name != 'nt':
        _old_term = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())

    try:
        while True:
            draw()
            key = read_key()
            if key in ('UP', 'k'):
                selected = (selected - 1) % n_items
            elif key in ('DOWN', 'i'):
                selected = (selected + 1) % n_items
            elif key in ('ESC', 'CTRL_C'):
                _apply_config(_orig)
                break
            elif key == 'ENTER':
                if selected < OTHER_IDX:
                    name, kind = _important[selected]
                    if kind == "bool":
                        vals[name] = not vals[name]
                    else:
                        edit_field(name, kind)
                elif selected == OTHER_IDX:
                    edit_other()
                else:
                    action = selected - OTHER_IDX - 1
                    if action == 0:  # Save & Exit
                        _apply_config(vals)
                        save_config()
                        break
                    elif action == 1:  # Exit
                        if confirm("Exit without saving? Changes will be lost."):
                            _apply_config(_orig)
                            break
                    elif action == 2:  # Reset & Exit
                        if confirm("Reset all settings to defaults?"):
                            _apply_config(_DEFAULTS)
                            save_config()
                            break
    finally:
        if os.name != 'nt' and _old_term is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_term)

# ── Position discovery ─────────────────────────────────────────────────────────

def discover_positions(markets: list, api_key: str, cancel=None, progress=None,
                       known_slugs: set = None) -> dict:
    """Probe outcomes for shares in parallel, skipping already-tracked markets."""
    found = {}
    headers = {"Authorization": f"Bearer {api_key}"}
    to_check = [m for m in markets if not known_slugs or m["slug"] not in known_slugs]

    def check_market(m):
        if cancel and cancel.is_set():
            return None
        for o in m["outcomes"]:
            if cancel and cancel.is_set():
                return None
            for yn in ["yes", "no"]:
                try:
                    r = _req.post(f"{BASE_URL}/markets/{m['slug']}/trade",
                                  json={"outcomeId": o["id"], "shares": -999999, "yesNo": yn},
                                  headers=headers, timeout=10)
                    j = r.json()
                    if j.get("error") == "insufficient_shares" and j.get("have", 0) > 0:
                        return m["slug"], {
                            "outcome_id": o["id"], "label": o["label"],
                            "shares": j["have"], "buy_price": o["price"],
                            "yes_no": yn, "cost_spent": 0,
                        }
                except Exception:
                    pass
        return None

    executor = ThreadPoolExecutor(max_workers=10)
    futures = {executor.submit(check_market, m): m for m in to_check}
    for future in as_completed(futures):
        if cancel and cancel.is_set():
            for f in futures:
                f.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            break
        if progress is not None:
            progress[0] += 1
        result = future.result()
        if result:
            slug, data = result
            found[slug] = data
    else:
        executor.shutdown(wait=False)

    return found

# ── Main ───────────────────────────────────────────────────────────────────────


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
        print(f"{YEL}Different user detected ({cached_user} → {me['handle']}) — resetting all cache.{R}")
        reset_config()
    else:
        positions.update(cached_pos)

    # Sync positions — runs in background, press any key to skip
    if not AUTO_SYNC:
        print(f"{DIM}Auto sync disabled.{R}")
    else:
        _cancel      = threading.Event()
        _result      = {}
        _all_mkts    = client.list_markets("open")
        _known_slugs = set(positions.keys())
        _to_check    = max(len(_all_mkts) - len(_known_slugs), 1)
        _progress    = [0, _to_check]

        def _sync():
            try:
                _result.update(discover_positions(_all_mkts, api_key, _cancel, _progress, _known_slugs))
            except Exception:
                pass

        def _draw_sync_bar():
            cur, total = _progress
            w = 28
            filled = round(cur / total * w)
            bar_str = GRN + "█" * filled + DIM + "░" * (w - filled) + R
            print(f"\r  {DIM}Syncing positions{R} {bar_str} {CYN}{cur}/{total}{R}  {DIM}[any key to skip]{R}   ",
                  end="", flush=True)

        _t = threading.Thread(target=_sync, daemon=True)
        _t.start()

        if os.name == 'nt':
            while _t.is_alive():
                _draw_sync_bar()
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    _cancel.set()
                    if ch in ('\x1b', '\x03'):
                        raise KeyboardInterrupt
                    break
                time.sleep(0.2)
        else:
            _old_term = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                while _t.is_alive():
                    _draw_sync_bar()
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        ch = sys.stdin.read(1)
                        _cancel.set()
                        if ch in ('\x1b', '\x03'):
                            raise KeyboardInterrupt
                        break
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_term)

        _draw_sync_bar()  # ensure final state is shown
        _t.join(timeout=1.0 if _cancel.is_set() else None)
        print()  # newline after the progress bar

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

    # One-time migration: divide milli-bark values saved before the whole-Bark refactor
    if not profit_data.get("migrated_v2"):
        if "start_balance" in profit_data:
            profit_data["start_balance"] /= 1000
        if "daily_spend" in profit_data:
            profit_data["daily_spend"] /= 1000
        for pos in positions.values():
            if "cost_spent" in pos:
                pos["cost_spent"] /= 1000
        profit_data["migrated_v2"] = True
        save_profit(profit_data)
        if positions:
            save_cache(positions, cached_user or me["handle"])

    is_new = "start_balance" not in profit_data
    if is_new:
        profit_data["start_balance"] = me["balanceBarks"] / 1000
        save_profit(profit_data)
        print(f"\n{BOLD}New account — configure your settings:{R}")
        edit_settings()
        input(f"\n{DIM}Press Enter to start trading…{R}")
    start_balance = profit_data["start_balance"]

    # Restore or reset daily spend and day-start balance
    today = datetime.now().strftime("%Y-%m-%d")
    if profit_data.get("daily_spend_date") == today:
        session_spend     = profit_data.get("daily_spend", 0)
        day_start_balance = profit_data.get("day_start_balance", start_balance)
        if session_spend > 0:
            print(f"{YEL}Resumed today's spend: {session_spend:,.2f} / {MAX_DAILY_SPEND:,.0f} Barks{R}")
    else:
        session_spend     = 0
        day_start_balance = me["balanceBarks"] / 1000
        profit_data["daily_spend"]       = 0
        profit_data["daily_spend_date"]  = today
        profit_data["day_start_balance"] = day_start_balance
        save_profit(profit_data)

    # seed momentum baseline
    prev_prices: dict[str, float] = {}

    run_count = 0

    while True:
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
                print(f"  {col}entry {bp*100:5.1f}%  now {cp*100:5.1f}%  {pp:+.1f}pp  {pl_col}P&L: {est_pl:+.2f} Barks{R}")
                print()
            sign = GRN if total_pl >= 0 else RED
            print(f"\n  {sign}Est. session P&L: {total_pl:+,.2f} Barks{R}")

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
                        recovered = -result["costBarks"] / 1000
                        print(f"  {col}✔ recovered {recovered:,.2f} Barks  "
                              f"balance {result['newBalance']/1000:,.2f} Barks{R}")
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
        all_scored = []   # includes held markets — for top-picks display
        for m in markets:
            outcomes = m["outcomes"]
            if not outcomes: continue
            total_q  = sum(o["q"] for o in outcomes)
            winner   = max(outcomes, key=lambda o: o["price"])
            close_ms = m.get("closeAt")

            # Hard filters
            if total_q / 1000 < MIN_LIQUIDITY_Q: continue
            if winner["price"] < WIN_THRESHOLD: continue
            if winner["price"] > 0.95: continue
            if close_ms and close_ms / 1000 - time.time() < MIN_CLOSE_SECS: continue

            s = score_market(winner, outcomes, prev_prices, close_ms)
            all_scored.append((s, m, winner))
            if m["slug"] not in positions:
                candidates.append((s, m, winner))

        candidates.sort(key=lambda x: x[0], reverse=True)
        all_scored.sort(key=lambda x: x[0], reverse=True)

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

        rich = display_bal > FORCE_ABOVE

        for s, m, winner in candidates:
            if session_spend >= MAX_DAILY_SPEND or slots <= 0: break
            base_cost = trade_cost(s)
            cost = round(base_cost * (1 + s)) if rich else base_cost
            cost = int(min(cost, MAX_DAILY_SPEND - session_spend))
            try:
                result = client.trade(m["slug"], winner["id"],
                                      max_cost=cost, yes_no="yes")
                session_spend += result["costBarks"] / 1000
                trades_made   += 1
                slots         -= 1
                pp_moved = (result["priceAfter"] - result["priceBefore"]) * 100
                positions[m["slug"]] = {
                    "outcome_id": winner["id"],
                    "label":      winner["label"],
                    "shares":     result["shares"],
                    "buy_price":  result["priceAfter"],
                    "yes_no":     "yes",
                    "cost_spent": result["costBarks"] / 1000,
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
            if session_spend >= MAX_DAILY_SPEND: break
            slug = m["slug"]
            if slug not in positions: continue
            pos = positions[slug]
            if pos.get("closed"): continue
            if pos.get("cost_spent", 0) >= MAX_POSITION_COST: continue

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
            cost       = int(min(cost, headroom, MAX_DAILY_SPEND - session_spend))
            if cost <= 0: continue

            try:
                result = client.trade(slug, pos["outcome_id"],
                                      max_cost=cost, yes_no=pos["yes_no"])
                session_spend     += result["costBarks"] / 1000
                trades_made       += 1
                pos["shares"]     += result["shares"]
                pos["cost_spent"] += result["costBarks"] / 1000
                pos["buy_score"]   = cur_score
                save_cache(positions, _current_handle)
                spent_bark = result["costBarks"] / 1000
                pp_moved   = (result["priceAfter"] - result["priceBefore"]) * 100
                print(f"\n  {CYN}↑ topped up  {m['title']}  score {cur_score:.2f}  "
                      f"spent {spent_bark:.2f} Barks  ({pp_moved:+.2f}pp){R}")
            except Exception as e:
                resp = getattr(e, "response", None)
                body = getattr(resp, "text", str(e))
                print(f"  {RED}✘ top-up failed: {body}{R}")

        # Update momentum baseline for next scan
        for m in markets:
            for o in m["outcomes"]:
                prev_prices[o["id"]] = o["price"]

        # Profit summary
        total_invested = sum(p.get("cost_spent", 0) for p in positions.values() if not p.get("closed"))
        current_bal    = client.me()["balanceBarks"] / 1000
        portfolio_val  = current_bal

        # Record daily snapshot for 3-day profit tracking
        snapshots = profit_data.setdefault("daily_snapshots", {})
        snapshots[today] = portfolio_val
        cutoff = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        profit_data["daily_snapshots"] = {k: v for k, v in snapshots.items() if k >= cutoff}

        # 3-day profit: compare to snapshot at or before 3 days ago
        three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        past_dates = [d for d in snapshots if d <= three_days_ago]
        if past_dates:
            baseline_3d  = snapshots[max(past_dates)]
            profit_3d    = portfolio_val - baseline_3d
        else:
            profit_3d    = portfolio_val - start_balance  # not enough history

        profit_data["daily_spend"]      = session_spend
        profit_data["daily_spend_date"] = today
        save_profit(profit_data)
        run_count += 1
        col       = GRN if profit_3d >= 0 else RED
        spend_col = RED if session_spend >= MAX_DAILY_SPEND else YEL

        # Top 3 markets — pinned above footer so they're visible during sleep
        print(f"\n{BOLD}  Top picks{R}")
        if all_scored:
            for rank, (s, m, winner) in enumerate(all_scored[:3], 1):
                bar_w   = round(s * 10)
                bar_str = GRN + "█" * bar_w + DIM + "░" * (10 - bar_w) + R
                held    = f" {CYN}◆ Held{R}" if m["slug"] in positions else ""
                print(f"  {DIM}#{rank}{R} {bar_str} {BOLD}{winner['price']*100:.1f}%{R}  "
                      f"{m['title'][:44]}{held}")
        else:
            print(f"  {DIM}No qualifying markets.{R}")

        print(f"\n{'─'*58}")
        print(f"  {DIM}Daily spend:{R} {spend_col}{BOLD}{session_spend:,.2f} / {MAX_DAILY_SPEND:,.0f} Barks{R}")
        if session_spend >= MAX_DAILY_SPEND:
            print(f"  {RED}{BOLD}Daily spend limit reached — buys paused, watching for sells.{R}")
        print(f"  Run #{run_count}  |  "
              f"Holding {len(positions)}  |  "
              f"New buys: {trades_made}  |  "
              f"{BOLD}{col}3d Profit: {profit_3d:+.2f} Barks{R}  |  "
              f"{CYN}{BOLD}Active markets: {total_invested:.2f} Barks{R}")
        t_col = YEL if _sleep_active else DIM
        print(f"\n{DIM}  Sleeping {SLEEP_SECONDS}s…  [s] settings  {t_col}[t] keep awake{R}{DIM}  [Esc] stop{R}")

        def _reprint_footer():
            t_col = YEL if _sleep_active else DIM
            line = f"{DIM}  Sleeping {SLEEP_SECONDS}s…  [s] settings  {t_col}[t] keep awake{R}{DIM}  [Esc] stop{R}"
            print(f"\033[A\r\033[K{line}", flush=True)

        # Sleep, but watch for 's' (settings), 't' (sleep toggle), or Esc/Ctrl-C (stop)
        if os.name == 'nt':
            deadline = time.time() + SLEEP_SECONDS
            while time.time() < deadline:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch.lower() == "s":
                        clear_screen()
                        edit_settings()
                        break
                    elif ch.lower() == "t":
                        _toggle_sleep()
                        _reprint_footer()
                    elif ch in ('\x1b', '\x03'):
                        raise KeyboardInterrupt
                time.sleep(0.2)
        else:
            _old = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                deadline = time.time() + SLEEP_SECONDS
                while time.time() < deadline:
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        ch = sys.stdin.read(1)
                        if ch.lower() == "s":
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old)
                            clear_screen()
                            edit_settings()
                            tty.setraw(sys.stdin.fileno())
                            break
                        elif ch.lower() == "t":
                            _toggle_sleep()
                            _reprint_footer()
                        elif ch in ('\x1b', '\x03'):
                            raise KeyboardInterrupt
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
            recovered   = -result["costBarks"] / 1000
            new_balance = result["newBalance"] / 1000
            cp  = result["priceAfter"]
            bp  = pos["buy_price"]
            pp  = (cp - bp) * 100
            col = GRN if pp >= 0 else RED
            print(f"  {col}✔ sold  {m['title'][:50]}")
            print(f"     {pp:+.1f}pp  recovered {recovered:,.2f} Barks  "
                  f"balance {new_balance:,.2f} Barks{R}")
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
            print(f"{YEL}New device detected — resetting all cache and re-authenticating.{R}")
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

    # Prevent the device from sleeping while the trader runs
    _toggle_sleep()  # starts active by default

    def _shutdown(*_):
        global _sleep_active
        if _sleep_active:
            _toggle_sleep()
        save_cache(_pos, _current_handle)
        print(f"\n{YEL}Stopped — positions saved.{R}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, _shutdown)
    try:
        run(_pos, _api_key)
    except KeyboardInterrupt:
        _shutdown()
