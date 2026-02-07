# Dynamic Take-Profit Analysis

## Date: 2026-02-07

## Problem

The bot was exiting winning trades too early. With a flat 15% take-profit threshold,
winners were being sold at +15% ($0.575 on a $0.50 entry) even when the market was
about to resolve — leaving the full $1.00 binary payoff on the table.

Meanwhile, losers could go to near-zero at resolution, creating an asymmetric payoff:
- Winners capped at +15% ($0.075/share profit)
- Losers uncapped down to -100% ($0.50/share loss)

This made the strategy net-negative even with a >50% win rate.

## Solution: Time-Based Dynamic Take-Profit

Replace the flat `take_profit_pct` with a curve that adapts based on market progress:

| Market Phase | Progress | Effective Take-Profit | Rationale |
|-------------|----------|----------------------|-----------|
| First third (0–5 min) | 0.0–0.33 | `take_profit_pct` (10%) | Early in market, odds are volatile. Lock in quick wins. |
| Middle third (5–10 min) | 0.33–0.67 | `take_profit_pct * 2` (20%) | Odds stabilizing, let winners run more. |
| Last third (10–15 min) | 0.67–1.0 | Disabled | Near resolution, binary payoff dominates. |

## Rationale for Each Phase

### First Third: Take Profit Quickly
- Market just opened, odds are most uncertain
- Spot-price signals may reverse
- Quick 10% profit is a reliable capture

### Middle Third: Let Winners Run
- Odds are trending toward resolution price
- If we're up 15%, the position is likely correct
- Raising to 20% avoids exiting positions that will go to $1.00

### Last Third: Disable Take-Profit
- Binary payoff dominates: the token will go to $1.00 or $0.00
- A winning position (e.g. YES at $0.70) has high probability of resolving at $1.00
- Selling at $0.70 captures $0.20 profit, but holding captures $0.50
- The "lottery ticket" logic handles the losing side (don't sell near-worthless positions)

## Configuration

```python
take_profit_pct: float = 0.10          # base threshold (first third)
take_profit_time_decay: bool = True    # enable dynamic behavior
```

Set `take_profit_time_decay = False` to revert to flat take-profit.

## Alternative: Flat Raise

If dynamic take-profit underperforms, the simplest alternative is to raise the flat
threshold from 10% to 25-30%. This captures more upside without the complexity of
time-based logic. To switch:

```python
take_profit_pct: float = 0.25
take_profit_time_decay: bool = False
```

## Metrics to Monitor

1. **Average win amount** — should increase vs. pre-change baseline
2. **Win rate** — may decrease slightly (holding losers longer in last third)
3. **Net profit per trade** — primary metric, should improve
4. **`take_profit_triggered` events** — check `effective_threshold` and `progress` fields
5. **Resolution outcomes in last third** — are disabled-take-profit positions winning?

## Review Cadence

- Check after 24 hours of live trading
- Compare win/loss amounts before and after the change
- If average win amount doesn't increase by >20%, consider switching to flat raise
