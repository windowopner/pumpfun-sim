# Paper-trading simulator design

> Written: 2026-10-15. Based on the data the collector saves (`data/pump.db`: `coins`, `trades`, `lifecycle`, `connections`).
> Goal: replay the recorded on-chain events to answer "if I had entered this coin this way at that moment, how much would I have made or lost?" No real money is involved.

## 0. Summary

- **Event replay.** Replay the recorded `CreateEvent` and `TradeEvent`s one coin at a time in on-chain order. The strategy can only see events from before the moment it would have observed them.
- **Prices are not estimated; the curve is recomputed.** On the bonding curve, price is fully determined by the reserves, so fills are computed exactly with the formulas from the official SDK.
- **Time is measured in slots, not seconds.** The on-chain `timestamp` has 1-second resolution, which is too coarse for a latency model. Minutes and hours given as options are turned into slots with the slot time **measured from the data** (`replay.seconds_per_slot`: about 0.216 s in October 2026, well under Solana's nominal 0.4 s). Until 2026-10-16 a fixed 0.4 s was used, so minute figures in older notes were too long by a factor of about 1.85; they are corrected below.
- **Be conservative by default.** Latency, fill position within a slot, slippage failures, network fees and account rent are all applied by default. Optimistic settings are only for sensitivity analysis.
- **Graduation:** in v1, a position is force-closed when the coin graduates. Holding after graduation needs PumpSwap data (v2).

## 1. What the data shows (from the collector, 2026-10-15)

Measured over about 5 minutes of collection (57 new coins, 5,082 trades). The sample is small, so re-check after a longer run (section 10). These are the observations behind several of the design decisions.

| Observation | Number | Effect on the design |
| --- | --- | --- |
| Share of transactions touching the Pump program that failed | ~55-65% | Model failures too (slippage failures, sniper competition) |
| Creates with a buy in the same tx (dev buy) | 50/57 (88%) | The launch price we can actually get is usually after the dev buy |
| Mayhem coins | 30/57 (53%) | Their quote reserves don't follow the chain and an AI agent trades them at 0% fees → **excluded in v1** |
| Coins paired with something other than SOL (other pump coins, etc.) | 9/57 | Fee structure differs (e.g. creator 1%, protocol 0%) and `sol_amount = 0` → **excluded in v1** |
| Order fully reconstructed by reserve chaining | SOL normal coins **19/19**, mayhem 3/31 | Order within a slot can be recovered without `getBlock` (section 3) |
| Graduation during collection | `CompleteEvent` ×3, of which 2 had a synthetic migration (`PostCompleteBuyEvent`) | Graduation does happen. The synthetic migration from the v3 buys is already in use |
| Price change after launch (SOL normal coins, n=19, relative to the state just after the dev buy) | Right away: median 1.03×, p90 1.44× → after 25 slots: median 1.05×, p90 1.75× | Median small, upper tail big: a few coins rise sharply within seconds of launch, so latency matters a lot for those |
| Trades on coins created before collection started | 77% after 3 minutes | Without the create there is no starting state → only coins whose create we saw can be simulated |

## 2. Simulation universe

A coin enters the simulation only if all of these hold:

1. The collector saw its `CreateEvent` (that's the starting state).
2. `quote_mint = 11111111111111111111111111111111` (SOL pair) and `is_mayhem_mode = 0`.
3. Ordering by reserve chaining succeeds for the coin's whole recorded life (section 3). If it fails anywhere, the coin is excluded and the reason is logged.
4. The create falls inside a covered slot range (`first_slot`..`last_slot`) in the `connections` table. **The coin's observation stops at the end of that range** (`observed_until`); later trades are dropped, because some may have been missed while the collector wasn't listening. Excluding these coins would lose too many, since most coins stop trading within a few minutes. A position still open when observation ends is closed at the last state we saw and counted separately (section 8).
5. Rows whose signature is `111…1` (phantom notifications) are excluded.

Implemented in `pumpfun/sim/replay.py` `load_universe()`. On 2026-10-15 data: 941 of 2,123 seen coins are in the universe; the rest are mayhem (797) or not SOL-paired (383). **No coin was excluded because ordering failed.**

The excluded count and reasons go in every run report. A big jump in exclusions is a sign of a protocol change.

## 3. Ordering events: reserve chaining

The `logsSubscribe` stream doesn't preserve transaction order within a block, so trades in the same slot come in an unknown order. Because a `TradeEvent` carries **the reserves right after the trade**, we can work back to the reserves right before it:

```text
buy : pre = (vQ_post - quote_amount, vT_post + token_amount)
sell: pre = (vQ_post + quote_amount, vT_post - token_amount)
```

Starting from the `CreateEvent` reserves, we repeatedly pick the one trade whose `pre` equals the current state. That reconstructs the exact on-chain order.

- If no trade matches (`chain broken`), or more than one does (`chain ambiguous`), ordering fails for that coin → it's excluded. There's no backtracking: after removing phantom notifications, no ambiguous case has shown up in the real data. If they start appearing, add backtracking or a `getBlock` fallback.
- If the reconstructed order disagrees with slot order, the coin is excluded too (`chain out of slot order`).
- With this ordering, each trade gets a `(slot, seq)` sort key. `seq` is the order within the coin.
- Ordering is only needed within a coin. Comparing across coins at slot granularity is enough.

## 4. Market model (bonding curve)

`pumpfun/sim/market.py` (implemented). Integer (lamport / token base unit) arithmetic only. **The formulas follow what the program actually records, not the SDK.** The SDK's exact-SOL quote formula matched the token count in only 5 of 18 real transactions.

```text
buy, exact tokens:  cost   = floor(amount * vQ / (vT - amount)) + 1
                    fees   = ceil(cost * bps / 10000)                 # protocol and creator rounded up separately
buy, exact SOL:     x0     = floor(in * 10000 / (10000 + total_bps))
                    fees   = ceil(x0 * bps / 10000)                   # computed on x0
                    quote  = x0 - max(0, x0 + fees - in)              # quote absorbs any overage
                    tokens = floor((quote - 1) * vT / (vQ + quote - 1))
                    if floor(quote * vT / (vQ + quote)) >= tokens left: buys all of them at the exact-tokens cost
sell:               gross  = floor(amount * vQ / (vT + amount)), fees = ceil(gross * bps / 10000)
```

- Fee rates: protocol 95 bps + creator 30 bps (on-chain `FeeConfig`, 2026-10-15). **If the coin's `creator` is the zero address, no creator fee is charged even though the event reports `creator_fee_basis_points = 30`.** For this reason the collector now saves the trade's `creator` too (from 2026-10-15 19:4x).
- `buyback_fee` is half of the protocol fee (47.5 bps). It isn't an extra cost; it's part of the 95 bps.
- A buy can't take more than `real_token_reserves`. An exact-tokens buy that asks for more fails; an exact-SOL buy that reaches the end takes everything left, and the rest of the input stays with the buyer. The v3 synthetic migration overflow isn't modeled.
- The completion threshold for exact-SOL buys is inferred: a completing buy's event doesn't record the input amount, so it can't be checked directly.
- **Legacy cashback coins** (no new ones can be created): the coin has a creator, but `creator_fee_basis_points = 0` and the 30 bps goes to buyers as `cashback`. On SOL-in buys this cashback enters the fee denominator like any other fee, which was verified by search on 1,386/1,386 buys saved before the collector stored `cashback`. Since 2026-10-16 00:17 the collector stores `cashback_fee_basis_points` and `cashback` too. Whether exact-token buys and sells pay cashback the same way is **an assumption, to be checked against those columns**. These coins are excluded from the simulation universe.
- `multi_hop_swap` bonding curve hops: the curve math is the same as other trades. **The creator fee is charged at whatever rate the event reports**: most hops (1,830) report 0 even when the coin has a creator, because the fee is held on the curve, but 14 hops on 2026-10-16 12:29–13:20 reported 30 bps. The protocol fee is computed on the hop's input, which isn't recorded, so it can be off by up to 1 lamport. We don't place multi-hop orders ourselves; we only replay them.
- **Completion condition for SOL-in buys:** only when the tokens computed from the quote reach the remaining tokens does the buy take everything left, at the exact-tokens cost. It is a token check, not a quote check: in the data there is a buy whose quote exactly equalled the cost of everything left, yet the program left 1,227 tokens. (Completing buys don't record their input amount, so in tests they're checked as exact-token buys.)

**Verification (tests/test_market.py, 2026-10-15):**

| Test | Result |
| --- | --- |
| Every recorded SOL, non-mayhem trade, from its own pre-state | 99,990 / 99,990 (only the protocol fee of some `multi_hop_swap` hops is off by 1 lamport; the hop input isn't recorded, so it can't be pinned down) |
| Each coin from its `CreateEvent`, trade by trade, in on-chain order | 769 / 769 coins exact (131 skipped for overlapping a collector gap) |
| Exact-SOL buys checked against the real input amount from the instruction data | 21 / 21 exact (including both cases of where the rounding lamport goes) |

### Data quality issues found

- **Phantom notifications:** the public RPC sometimes sends `logsSubscribe` notifications whose signature is all 1s (`111…1`, i.e. 64 zero bytes). These trades never landed: the state they start from is taken by a different, real trade. The collector now discards them (`phantom` count). Exclude rows saved before that.
- **Collector gaps:** on public RPC the stream sometimes stalls without disconnecting, and reconnects get rejected with HTTP 413 for a minute or so. The collector now records its covered slot range in `connections` every second and reconnects when no notification arrives for 60 seconds. **The simulation universe (section 2) should be judged by these slot ranges**, not by connection times.
- Transaction format v1 exists now. If you fetch transactions with `getTransaction`, `maxSupportedTransactionVersion` must be 1 or higher.

## 5. Execution model: latency, fills, failures

### 5.1 Timeline

```text
event lands in slot s ──(observe_delay)──► strategy decides at s+d ──(send/land)──► fill in slot s+d+L
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `observe_delay_slots` (d) | 2 | Delay before our websocket sees `confirmed` (public RPC). Lower with Geyser |
| `land_slots` (L) | 2 | From sending a transaction until it lands in a block |
| `intra_slot` | `end` | Where in the landing slot we land: `start` (optimistic), `end` (conservative), `random` |

- What the strategy sees is limited to events up to `slot ≤ s+d`. It never sees the future (lookahead).
- The strategy learns an order's result (filled, failed, dropped) at `landing + d`, the same observation delay as everything else. Until then, `pos` doesn't include the fill.
- A creation transaction is seen whole: the creation observation already includes the dev buy in the same transaction. Other bots' buys in the creation slot come in as separate observations after it.
- Fill price: the curve state after all trades before `slot s+d+L`, then the landing-slot trades according to `intra_slot`. With `end`, that's after the last trade of the landing slot.
- Report results across `(d, L) ∈ {(1,1), (2,2), (3,5)}` side by side. A strategy that's sensitive to latency isn't real.

### 5.2 Slippage and failures

- Buys are exact-SOL (`buy_exact_quote_in`). Protection is `min_tokens_out = tokens expected at decision time × (1 − slippage_bps)`; if the landing slot gives fewer tokens, the **transaction fails**. Sells use `min_sol_output` the same way. An order sent against a curve that is already complete is still sent and fails on chain (`curve complete`).
- A failed transaction still pays the network fees (base + priority). Retrying is the strategy's choice.
- `drop_rate` (default 0): extra probability that a transaction never lands. Use for sensitivity analysis.
- Our own trade's price impact is in the formula above. **Its effect on other people's trades is ignored in v1** (the recorded trades are applied as-is). That error grows with position size, so we cap entry size at `max_size_vs_reserves` (default 2% of the virtual SOL reserves).
  - The one exception is the curve's real SOL reserves. If we bought and nobody else did, the recorded curve has 0 SOL and our sell couldn't be paid out, so the SOL our fills put in is carried forward on the real-reserve side only. Price (the virtual reserves) stays as recorded.
  - v2 option: "shadow curve" mode. With our trade added, replay other people's trades by intent (`buy` = exact tokens, `buy_exact_*_in` = exact SOL, `sell` = exact tokens) on a shadow curve. We don't know their slippage limits, so their failures aren't modeled.

### 5.3 Costs

| Item | Default | Note |
| --- | --- | --- |
| Base fee | 5,000 lamports / signature | |
| Priority fee | 0.0005 SOL / tx | Setting. Sniping in practice costs more (Jito tips etc.) |
| Token account (ATA) rent | 0.00207408 SOL | Token-2022 ATA (170 bytes). Paid on first buy, refunded when the account is closed after a full exit (assumed closed in the same tx as the last sell) |
| `user_volume_accumulator` rent | 0.0018444 SOL | Once per wallet (one-time cost for the whole simulation) |

## 6. Graduation

- When a coin's `CompleteEvent` comes in, no trades are possible on the curve anymore.
- **v1: forced exit at graduation.** The position is sold into the PumpSwap pool. The pool's opening reserves come from `CompletePumpAmmMigrationEvent` (`mint_amount`, `sol_amount`), or from the `pool_*_after` fields of a `PostCompleteBuyEvent` if one exists. The fill is computed with:
  - a constant-product sell formula plus the first PumpSwap fee tier (LP 0.02% + protocol 0.93% + creator 0.30% = 1.25%);
  - the same latency L after migration; in the meantime other people's PumpSwap trades are unknown, so as a conservative correction we apply a `post_grad_haircut` (default 10%) to the opening price.
  - A sell order placed in the instant between completion and migration fails (`BondingCurveComplete`).
- **v2:** add PumpSwap `BuyEvent`/`SellEvent` collection for canonical pools of coins we've seen. Then strategies can hold after graduation, and the pool math has to use `effective_quote_reserves` (signed `virtual_quote_reserves`).
- Graduation is rare, so v1 is fine for most strategies. Section 10 checks how rare.
- If a coin graduated but its migration is outside the collected data, it's valued at the last curve state with the PumpSwap fees and the haircut applied (`data_end (graduated)`).

Implemented in `pumpfun/sim/engine.py` (`fill_pool_sell`). PumpSwap sell: `gross = floor(Q' × tokens / (B + tokens))`, where `Q' = opening quote reserves × (1 − haircut)`; fees are rounded up per tier (LP 2 / protocol 93 / creator 30 bps). Strategy sell orders sent after completion fail, but the engine's graduation exit happens regardless.

## 7. Strategy interface

`pumpfun/sim/strategies/*.py`. A strategy can only see the state as of its observation point and can only return orders.

```python
class Strategy:
    def on_create(self, coin: CoinView, ctx: Ctx) -> list[Order]: ...
    def on_trade(self, coin: CoinView, trade: TradeView, pos: Position | None, ctx: Ctx) -> list[Order]: ...
    def on_complete(self, coin: CoinView, pos: Position | None, ctx: Ctx) -> list[Order]: ...
```

- `CoinView`: current curve state (as of the observation point), market cap, curve progress (sold / 793.1M), elapsed slots since launch, number of unique buyers, net SOL inflow, whether the creator has sold, etc. **Only derived values computed up to the observation point.**
- `Order`: `Buy(sol=0.1, slippage_bps=1500)`, `Sell(fraction=1.0, slippage_bps=1500)`.
- `ctx.slot` is the observation slot. The engine applies latency and fills afterwards.

Implemented (`pumpfun/sim/engine.py`):

- `Strategy` hooks: `on_create(coin, ctx)`, `on_trade(coin, trade, pos, ctx)`, `on_complete(coin, pos, ctx)`, plus `on_tick(coin, pos, ctx)` and `on_fill(fill, pos, ctx)`. **A dead coin produces no events, so time stops need ticks:** while a coin has a position or orders in flight, `on_tick` is called every `tick_slots` (default 25, ~5 seconds).
- Orders: `Buy(quote=lamports, slippage_bps=1500)`, `Sell(fraction=1.0, slippage_bps=1500)`, each with a `reason` (shown as the exit reason). `ctx.pending_orders(mint)` helps avoid sending duplicates.
- `pos.exit_value(state, rates)`: SOL received if everything were sold at that state (curve fees included, tx costs not). Use it for take profit / stop loss.
- Results: `RoundTrip` (entry→exit for one coin: in/out SOL, tx costs, net rent, failed and dropped count, exit reason, `pnl`, `pnl_pct`, every fill), plus wallet-level one-time costs (`one_time_costs`). If every buy fails, it's still a round trip with `entry failed` and the fees as its loss.

Three examples to start with (for learning only, not something that's expected to make money):

| Name | Entry | Exit |
| --- | --- | --- |
| `launch_sniper` | Buy X SOL as soon as a create is seen | +100% take profit / -50% stop loss / 10-minute time stop |
| `momentum` | Within the first N slots: k+ unique buyers, net inflow ≥ y SOL, creator hasn't sold | 30% trailing stop, exit immediately if the creator sells |
| `near_grad` | Buy when curve progress ≥ 80% | Forced exit at graduation (v1), or -30% stop loss |

Plus a **random-entry baseline** (same size and exit rules, entering at a random moment). If a strategy can't beat it, the strategy has no edge.

## 8. Engine and outputs

```
pumpfun sim --strategy launch_sniper --hours 6 --observe-delay 2 --land 2 --size 0.1
```

1. Build the universe (section 2) and order events (section 3).
2. Replay each coin: event → strategy at the observation point → orders → engine fills at the landing slot (or fails) → position.
3. Positions still open at the end of observation are marked to the last state (counted separately in the report).

Outputs go to `sim_runs` / `sim_fills` tables (or `data/sims/<run_id>.json`):

- **Per trade:** entry/exit slot, fill SOL, tokens, each fee and cost, failure count, exit reason, PnL (SOL, %).
- **Summary:** number of trades, win rate, mean and **median** return (memecoin returns are very fat-tailed, so the mean alone misleads), profit factor, total PnL, maximum drawdown, fee and cost share of PnL, failure rate, exclusion reasons, comparison to the baseline.
- **Sensitivity table:** results across latency settings × position sizes.

## 9. Pitfalls this design is meant to prevent

- **Lookahead:** the strategy only sees data up to the observation slot. Computing features from "the future" while building the dataset is the most common mistake.
- **Survivorship:** don't pick coins after the fact (e.g. only coins that graduated). The universe is every coin created during collection.
- **In-sample fitting:** split time into tune (first half) and test (second half). Only report test-period results as performance.
- **Same-slot competition:** the dev buy (88%) and bots buying in the same slot are always ahead of us. With `observe_delay + land_slots ≥ 2`, simulated entry happens at least 2 slots after launch.
- **Collection gaps:** exclude coins in gaps using the `connections` table.

## 10. Data to check once more has accumulated

- [x] Graduation rate: 7 of 254 coins with at least 9,000 slots (~32 min) of coverage (2.8%). **5 of the 7 completed in the same slot they were created** (the creator buys the whole curve at launch); the other two took 1 and 141 slots. So most graduations can't be entered at all, and section 6 rarely comes into play.
- [x] Price change by slot after launch, relative to the end of the create slot (i.e. after the dev buy and same-slot snipers), 254 coins: after 2 slots median 1.00× / p90 1.03× / max 1.3×; after 10 slots 1.00× / 1.06× / 1.7×; after 150 slots (~32 s) 1.00× / 1.11× / 2.3×. Most coins barely move; latency costs a lot only on the few that take off.
- [x] Coin lifetime (create → last trade), 254 coins: median 685 slots (~2.5 min), p75 4,160 (~15 min), p90 7,514 (~27 min). No trades after 150 slots (~32 s): 23%; after 750 (~2.7 min): 51%; after 2,250 (~8 min): 67%. Size time stops around this.
- [x] Whether reserve-chain ordering still succeeds 100% for normal coins over long periods: 769/769 coins over ~2 hours, after removing phantom notifications and gaps
- [x] Entry failures for a buy-on-create strategy (engine smoke run, 1,130 coins, 0.1 SOL, 15% slippage, d=2/L=2): 22% of entries fail. Most are slippage failures (230): **94% of those coins had other bots buy in the creation slot itself (median 4)**, and at our landing the price was already ≥1.18× (median 1.38×) what the strategy saw right after the dev buy. Another 21 hit an already-completed curve (instant graduation).
- [ ] In `fee bps (0,0)` trades, separate out the mayhem agent's trades from everything else
- [ ] Whether the mayhem share stays this high (45%), and whether to add support for it

## 11. Build order

1. ~~`pumpfun/sim/market.py` + tests (every recorded trade must reproduce exactly)~~ done
2. ~~`pumpfun/sim/replay.py`: universe, ordering, observation timing~~ done (`load_universe`, `order_by_reserve_chain`, `observations`, `CoinHistory.state_at`)
3. ~~`pumpfun/sim/engine.py`: latency, slippage, failures, costs, forced exit at graduation~~ done
4. ~~`launch_sniper` + random baseline, `pumpfun sim`, summary report~~ done (`pumpfun/sim/strategies/`, `pumpfun/sim/report.py`, `pumpfun/sim/runner.py`)
5. Sensitivity table, the other strategies
6. (v2) PumpSwap collection, shadow-curve mode

## 12. First runs (2026-10-16, for learning only)

Data: about 22 hours of collection (2026-10-15 10:00 to 2026-10-16 08:40 UTC), 20,809 coins usable (SOL-paired, non-mayhem, covered for at least 20 minutes after creation). Size 0.1 SOL, 15% slippage, take profit +100% / stop loss −50% / 10-minute time stop (2,776 slots at the measured 0.216 s), d=2 / L=2.

| | Entered | Win | Median | Mean | PnL (SOL) | Before costs | PF | Tx failures |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `launch_sniper` | 14,568 | 4.1% | −4.5% | −6.4% | −97.31 | −44.01 | 0.30 | 17.7% |
| `random_baseline` | 20,400 | 2.6% | −4.1% | −5.6% | −114.91 | −44.43 | 0.18 | 0.3% |

- Both lose money, and lose even before costs. Holding the median coin for 10 minutes makes its price go down.
- Entering at launch loses less in total than entering at a random time, but only because fewer entries succeed; before costs the two lose the same (−44.0 vs −44.4 SOL). No edge (PF 0.30).
- **30% of launch entries fail** (6,241 of 20,809; slippage from same-slot sniper bots), while the random baseline almost never fails. Cost is the network fees on failed transactions.
- Sensitivity (all coins): within latency (1,1)→(3,5), PnL only gets slightly worse (−95.7 → −103.7 SOL at 0.1 SOL). **Size has a big effect**: at 0.5 SOL the pre-cost loss is about 20× the 0.05 SOL loss (−392.8 vs −19.8 SOL at d=2/L=2) for 10× the money, because our own price impact grows.
- These numbers mean the simulator works and the simple rules don't. For the next strategies (`momentum`, `near_grad`), tune on `--period tune` and judge on `--period test`.

An earlier version of this section used about 7 hours of data with time converted at a fixed 0.4 s per slot, so its "10-minute" stop was really about 5.4 minutes. The conclusions held after the correction; the numbers above replace it.
