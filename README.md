# Borker Market Trader

An automated trading bot for [borker.college](https://borker.college) markets.

---

## First run

On first run you will be prompted for your API key (input is hidden). The key is encrypted and saved to `.borker_key` — you won't be asked again on the same device.

If you run on a different device, it detects the mismatch, wipes all local data, and prompts for the key again.

---

## Usage

```bash
# Run the auto-trader
python3 trader.py

# Manual CLI commands
python3 main.py me
python3 main.py markets [open|closed|resolved|all]
python3 main.py market <slug>
python3 main.py buy  <slug> <outcome_id> <max_cost>
python3 main.py sell <slug> <outcome_id> <shares>
python3 main.py no   <slug> <outcome_id> <max_cost>
```

---

## Startup

On every launch the trader scans the API for positions you already hold that aren't in the cache (useful if you traded manually). This runs in the background — **press any key to skip it** and go straight to trading.

---

## How the trader works

### Entry

Every 60 seconds all open markets are fetched, filtered, and scored. A market must pass all hard filters before it is scored:

| Filter | Value |
|--------|-------|
| Minimum pool size | 500,000 Barks |
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
| Time to close | 10% | Prefers 1–7 day window |

The top-ranked markets fill open position slots. Spend per trade scales from 10 to 50,000 Barks based on score.

### Top-ups

If a held position's score rises **+0.15** above its entry score, the bot buys more into that position. Total spend per position is capped at 50,000 Barks.

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
- Max 100,000 Barks spent per session

---

## Security

All sensitive files are encrypted with a custom substitution cipher before being written to disk:

| File | Contents |
|------|----------|
| `.borker_key` | Encrypted API key + encrypted device name (chmod 600, gitignored) |
| `positions.json` | Encrypted open positions + account handle |
| `profit.json` | Encrypted profit tracking data |

Switching users resets positions and profit. The user `awa` is exempt from profit resets.

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | API client + manual CLI |
| `trader.py` | Automated trading bot |
| `.borker_key` | Stored credentials (auto-managed, gitignored) |
| `positions.json` | Open positions cache (auto-managed, gitignored) |
| `profit.json` | Profit tracking across sessions (auto-managed, gitignored) |
