# Stage 3: multiple trading rules (Leveraged Trading, ch. 8)

Stage 2's Starter System with one change: the opening direction for each ETF is the sign of a
weighted vote of nine rules instead of the single 16/64 crossover. Sizing (fixed at entry),
the 25-day instrument risk, the trailing stop on the close and the re-entry rule are Stage 2's code,
untouched. Universe, dates, capital, risk target, IDM and leverage cap are the same as Stage 2.

## Rules and weights

| Family | Variants | Raw value | Weight |
|---|---|---|---|
| Moving-average crossover | 8/32, 16/64, 32/128, 64/256 | fast MA minus slow MA (SMA like Stage 2; `ma_type=ewma` for EWMAC) | 30% (7.5% each) |
| Breakout | N = 40, 80, 160, 320 | (close − range midpoint) / range of the last N closes, EWMA-smoothed with span N/4 | 30% (7.5% each) |
| Carry | one | 12-month distribution yield − funding rate | 40% |

Each ready rule votes +1/−1. Combined vote = Σ wᵢ·voteᵢ / Σ wᵢ (over ready rules), in [−1, +1].
Trade direction = sign of the combined vote. A rule that is still warming up sits out and the
weights of the others are renormalised (this only happens for carry in an ETF's first year).

**Why 30/30/40 and not 50/50 trend/carry:** with binary votes, a 50% carry weight gives an exact
tie every time all eight trend rules point against carry, and the system would sit flat. With
30/30/40 a tie is impossible once all rules are ready (`tests/test_stage3.py` checks all 512 cases).
Change it with `w_mac`, `w_breakout`, `w_carry`.

**Forecast scaling and FDM:** not used at this stage on purpose. Multiplying a forecast by a positive
scalar or by the FDM never changes its sign, so with binary positions they have no effect.
They come in with non-binary forecasts (Stage 5). `rules.py` already exposes each rule's raw value
for that.

## Carry for ETFs

An ETF has no futures roll, so carry is what you earn for holding it minus what it costs to finance:

```
carry = Σ over the last 365 days of (distribution / price before ex-date)  −  (risk-free rate + funding_spread)
```

- Distributions come from LEAN `Dividend` events (`distribution`, `reference_price`), so they work in
  the default adjusted-price mode.
- Risk-free rate comes from LEAN's `risk_free_interest_rate_model` (Fed primary rate). If that model
  is not available the algo falls back to the fixed `funding_rate` parameter and says so in the log.
- `funding_spread` (default 0) is the broker's markup; IB charges roughly benchmark + 0.5% to 1.5%.

What this proxy captures, per ETF:

| ETF | Distribution is | Carry reads as |
|---|---|---|
| IEF, TLT | bond coupons | term spread (bond yield − cash rate) |
| SPY, EFA, EEM, VNQ | dividends | dividend yield − cash rate |
| FXE, FXY | interest earned on the deposits | foreign − USD short rate (close to FX carry) |
| GLD | nothing | −cash rate (gold's true carry) |
| DBC | annual T-bill income | about 0; **misses futures roll yield** (known gap) |

Expense ratios are not deducted (they are already in the price).

## Regression check against Stage 2

Run Stage 3 with parameter `rules=mac16` (and nothing else changed). It must give the same orders
and fills as Stage 2. `tests/test_stage3.py::test_mac16_only_reproduces_stage2` checks the same
thing offline on synthetic prices. If the QC runs differ, something besides the rules has changed.

Note: Stage 3 trading waits until every price rule is ready (320 days for the slowest breakout).
The 550-day warm-up covers that for all ten ETFs before Jan 2008 except FXY (listed Feb 2007),
which starts trading a few weeks later than in Stage 2.

## Running on QuantConnect

1. Create a new Python project, e.g. `CarverStage3`.
2. Replace `main.py` with `stage3_multi_rule/main.py`.
3. Add three files at the project root with the contents of `carver_lib/starter_core.py`,
   `carver_lib/rules.py` and `carver_lib/combine.py` (same names).
4. Backtest. Charts: *Combined vote*, *Carry (annual)*, *Funding rate*. Each opening order tag lists
   the combined vote and every rule's vote.

With the LEAN CLI: copy `main.py` and the three `carver_lib` files into the project folder, then
`lean cloud push --project CarverStage3 && lean cloud backtest CarverStage3 --open`.

## Parameters

Same as Stage 2 (`symbols`, `target_risk`, `idm`, `stop_fraction`, `allow_short`, `max_leverage`,
`start_year`, `end_year`, `cash`), plus:

| Name | Default | Meaning |
|---|---|---|
| `rules` | all nine | Comma list: `mac16` (= 16/64), `mac16_64`, `breakout40`, `carry`, ... |
| `ma_type` | sma | `sma` (as Stage 2) or `ewma` |
| `w_mac`, `w_breakout`, `w_carry` | 0.3, 0.3, 0.4 | Family weights, split evenly inside each family |
| `funding_spread` | 0.0 | Added to the risk-free rate in carry |
| `funding_rate` | 0.02 | Fallback funding rate if LEAN's risk-free model is unavailable |
| `warmup_days` | 550 | Calendar days of warm-up |

## Suggested runs for the comparison

1. Stage 2, defaults (baseline).
2. Stage 3, `rules=mac16` (regression check, must equal run 1).
3. Stage 3, defaults.
4. Stage 3, `rules=mac8,mac16,mac32,mac64,breakout40,breakout80,breakout160,breakout320` (trend only, isolates carry).
5. Stage 3, `ma_type=ewma`.

Then compare with `research/compare_stages.py` (see the root README).
