# Borker Market Trader

An automated trading bot for [borker.college](https://borker.college) markets.

> **Note:** When you run the script, it will prompt for your API key. If you start typing and nothing appears — that's normal. Input is hidden for privacy.

---

## Usage

```bash
# Run the auto-trader
python3 trader.py

# Run individual CLI commands
python3 main.py me
python3 main.py markets
python3 main.py market <slug>
python3 main.py buy  <slug> <outcome_id> <max_cost>
python3 main.py sell <slug> <outcome_id> <shares>
python3 main.py no   <slug> <outcome_id> <max_cost>
```

---

## How the trader works

1. **Entry** — only buys when a market outcome is at ≥65% and the pool is large enough
2. **Sizing** — Kelly-inspired score based on price strength, pool dominance, and momentum
3. **Exit** — automatically sells on:
   - Take profit: price rises +12pp above entry
   - Stop loss: price drops −10pp below entry
   - Flip: the bought side falls below 55%
4. **Limits** — max 10 open positions at once

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | API client + CLI |
| `trader.py` | Automated trading bot |
| `positions.json` | Cached open positions (auto-managed) |
| `profit.json` | Profit tracking across sessions |
