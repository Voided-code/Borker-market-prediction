# Borker Market Trader

An automated trading bot for [borker.college](https://borker.college) markets. Works on macOS, Windows, and Linux.

---

## First run

On first run you will be prompted for your API key (input is hidden). The key is encrypted and saved to `.borker_key` — you won't be asked again on the same device.

If you run on a different device, it detects the mismatch, wipes all local data (positions, profit, config), and prompts for the key again.

On a brand new account the settings editor opens automatically before trading begins.

---

## Usage

```bash
# Run the auto-trader (sleep prevention is automatic on macOS and Windows)
python3 trader.py

# Sell all cached positions immediately
python3 trader.py --sell-all

# Manual CLI commands
python3 main.py me
python3 main.py markets [open|closed|resolved|all]
python3 main.py market <slug>
python3 main.py buy  <slug> <outcome_id> <max_cost>
python3 main.py sell <slug> <outcome_id> <shares>
python3 main.py no   <slug> <outcome_id> <max_cost>
```

Requires Python 3.9+.

---

## Controls

| Key | Action |
|-----|--------|
| **s** | Open settings editor (during sleep interval) |
| **Esc** or **Ctrl+C** | Stop cleanly |
| **Any other key** | Skip position sync on startup |

---

## Startup

On every launch the trader scans all open markets in parallel to find positions you already hold that aren't in the local cache (useful after manual trades). A progress bar shows scan status — **press any key to skip** and jump straight to trading.

---

## How the trader works

### Entry

Every 60 seconds all open markets are fetched, filtered, and scored. A market must pass all hard filters before it is scored:

| Filter | Value |
|--------|-------|
| Minimum pool size | 500 Barks |
| Minimum winner price | 65% |
| Maximum winner price | 95% (profit margin too thin above this) |
| Minimum time to close | 2 hours |

Qualifying markets are ranked by a weighted score across six factors:

| Factor | Weight | Logic |
|--------|--------|-------|
| Sweet-spot price | 25% | 65–88% scores best; fades above 88% |
| Consensus gap | 25% | Gap between #1 and #2 outcome; 40pp = perfect |
| Pool dominance | 15% | Winner's share of total pool |
| Liquidity | 15% | Log-scaled pool size above minimum |
| Momentum | 10% | Price rising since last scan |
| Time to close | 10% | 1–7 days = perfect; same day = lower; very long-dated = lower |

The top-ranked markets fill open position slots. Spend per trade scales from 2 to 50 Barks based on score.

### Scaled-up mode

When your balance exceeds `FORCE_ABOVE` (default: 300 Barks) the bot uses larger trade sizes. Spend per trade is boosted by the position's score (`cost × (1 + score)`), and top-ups trigger on any held position scoring ≥ 0.5 rather than requiring a score rise.

### Top-ups

If a held position's score rises **+0.15** above its entry score (normal mode), or scores ≥ 0.5 (scaled-up mode), the bot buys more into that position. Total spend per position is capped at 50 Barks.

### Exit

Each held position is checked every scan and sold when any condition triggers:

| Condition | Trigger |
|-----------|---------|
| Take profit | Price rises +12pp above entry |
| Stop loss | Price drops −10pp below entry |
| Flip | Bought side falls below 55% |
| Resolved | Market no longer in open list |

### Limits

- Max 10 open positions at once
- Max 100 Barks spent per day — resets at midnight; buys pause at the limit but sells continue

---

## Settings

Press **`s`** during the sleep interval to open the settings editor.

For each field the editor shows:

```
  WIN_THRESHOLD  current = 65.0%
  [r to reset, Enter to skip]: _
```

- **Enter** — keep the current value
- **`r`** — reset to the built-in default
- **Any number** — set a new value (percentages entered as e.g. `65`, stored as `0.65`)

All settings are saved encrypted to `config.json` when you exit the editor and restored on next launch. Switching device or user resets all settings to defaults.

| Setting | Default | Description |
|---------|---------|-------------|
| WIN_THRESHOLD | 65% | Minimum winner price to enter |
| PRICE_SWEET_MAX | 88% | Price above which score fades |
| FLIP_THRESHOLD | 55% | Sell if our side drops below this |
| TAKE_PROFIT_PP | 12pp | Take profit threshold |
| STOP_LOSS_PP | 10pp | Stop loss threshold |
| MAX_POSITIONS | 10 | Max concurrent holdings |
| MIN_LIQUIDITY_Q | 500 Barks | Minimum pool size |
| MIN_COST | 2 Barks | Min spend per trade |
| MAX_COST | 50 Barks | Max spend per trade (score-scaled) |
| MAX_POSITION_COST | 50 Barks | Max total spend per position |
| MAX_DAILY_SPEND | 100 Barks | Daily spend cap |
| FORCE_ABOVE | 300 Barks | Balance above which scaled-up trade sizing kicks in |
| SCORE_TOP_UP_DELTA | 0.15 | Score rise needed to top up (normal mode) |
| SLEEP_SECONDS | 60s | Scan interval |

---

## Footer

The footer shown after each scan:

- **Daily spend** — Barks spent today vs the daily cap (turns red at limit)
- **Run / Holding / New buys** — scan count, open positions, trades this run
- **3d Profit** — change in total portfolio value (balance + invested) over the last 3 days
- **Active markets** — total Barks currently committed to open positions

---

## Security

All sensitive files are encrypted with a custom substitution cipher before being written to disk:

| File | Contents |
|------|----------|
| `.borker_key` | Encrypted API key + encrypted device name (chmod 600 on Unix) |
| `positions.json` | Encrypted open positions + account handle |
| `profit.json` | Encrypted profit tracking + daily portfolio snapshots |
| `config.json` | Encrypted settings |

---

## Files

| File | Purpose |
|------|---------|
| `trader.py` | Automated trading bot |
| `main.py` | API client + manual CLI |
| `cache/` | Auto-managed data folder (gitignored) |
| `cache/.borker_key` | Encrypted API key + device binding |
| `cache/positions.json` | Open positions |
| `cache/profit.json` | Profit tracking + daily snapshots |
| `cache/config.json` | Saved settings |
