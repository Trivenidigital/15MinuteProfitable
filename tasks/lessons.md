# Lessons Learned

> Patterns, mistakes, and insights captured during development.
> Updated after every correction or discovery.

---

## Polymarket API Quirks

- **neg_risk auto-detection is broken** for BTC 15-min markets. The `/neg-risk` endpoint returns "Invalid token id". Always hardcode `neg_risk=True` in `PartialCreateOrderOptions`.
- **py-clob-client is synchronous only.** Must wrap with `asyncio.to_thread()` for async usage. Not thread-safe — one client instance per thread is safest.
- **Order signing latency (~1s)** is dominated by HTTP calls for tick_size and neg_risk. Pre-providing both in `PartialCreateOrderOptions` reduces signing to ~50ms.
- **Token prices/sizes in OrderBookSummary are strings**, not floats. Must cast explicitly.
- **USDC balance from API is in wei (6 decimals).** Divide by 1,000,000 for USD.
- **Magic.link accounts (signature_type=1):** The `funder` address must be the Polymarket proxy wallet, NOT the signer address. This is the #1 cause of "invalid signature" errors.

## Fee Structure

- **Conflicting fee data exists.** Some sources say max taker fee is 50 bps (0.5%), others say 3.15%. The user's spec says 3.15%. Must empirically verify with a test trade before going live.
- **Maker fee is 0%.** Using GTC limit orders (Strategy B) avoids taker fees entirely.
- **Winner fee (2%) is on profit only**, not total payout. Often forgotten in profit calculations.
- **Pure arb is nearly impossible at 50/50 odds** with the current fee structure. Need combined cost well below $0.94 to profit.

## Architecture Decisions

- (To be populated during implementation)

## Common Mistakes

- (To be populated after corrections)
