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

**Why 30/30/40:** with 30/30/40 a tie is impossible once all rules are ready (`tests/test_stage3.py`
checks all 512 vote patterns), and trend can still outvote carry on a 7-1 or 8-0 split. Carver's
handcrafted 25/25/50 (equal weight trend vs carry) was tried as the default and reverted: in this
stage it freezes the system (see below). It stays available with `w_mac=0.25`, `w_breakout=0.25`,
`w_carry=0.5`.

**Ties:** any tie gives signal 0. A flat ETF waits until a rule changes, and an open trade is left to
its stop. That is the binary version of a zero forecast. 25/25/50 ties only when all eight trend
rules oppose carry; trend-only runs (8 equal rules) tie on any 4-4 split.

**Results so far (QuantConnect, 2008 to Dec 2025):**

| Run | Sharpe |
|---|---|
| Stage 2 baseline | 0.60 |
| Stage 3, `rules=mac16` | matches Stage 2 exactly (regression check passed) |
| Stage 3, 30/30/40 (default) | 0.54 |
| Stage 3, trend only | 0.44 |
| Stage 3, 25% carry | 0.34 |
| Stage 3, 25/25/50 (tried, reverted) | 0.25 (46 opens) |

All are within about one standard error of each other (roughly 0.24 over 18 years), so the
weights are not tuned on these numbers.

**25/25/50 run: Sharpe 0.25, only 46 opens in 18 years, 2.7% vol. Cause confirmed: carry
decides the direction and the re-entry rule then locks the ETF out.**

1. With carry at 50%, the eight trend rules together weigh 50%. They can at most tie carry
   (all eight against it), never outvote it. So the trade direction is simply the sign of carry.
2. After a stop-out, the re-entry rule waits for the direction to flip. That now means waiting
   for carry to change sign, which for an ETF happens only when the short rate crosses its yield
   (a few times in 18 years at most). Until then the ETF sits flat, often for years.
3. `wait_for_flip` does the same at the start: no first trade until carry flips.

Offline check (`python research/reentry_lockout_sim.py`, 20 synthetic 18-year paths with carry
always positive): median trades per path are Stage 2 42, trend only 30, 30/30/40 26, 25/25/50 0.
At 30/30/40 trend can still override carry, but only on a 7-1 or 8-0 split, which is why that
run also traded less than Stage 2.

Conclusion: with binary votes, stops and a flip-to-re-enter rule, any slow rule with half the
weight or more freezes the system. This is a property of combining Stage 3's mechanics, not
evidence against carry. Stage 4 drops stops and the re-entry rule, so carry's weight works there
as intended.

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
- The rate comes from `rf_source`: LEAN's `risk_free_interest_rate_model` (Fed primary rate) in the
  baseline, or the 3-month T-bill with `rf_source=dtb3` (see below). If the LEAN model is not available
  the algo falls back to the fixed `funding_rate` parameter and says so in the log.
- `funding_spread` (default 0) is the broker's markup; IB charges roughly benchmark + 0.5% to 1.5%.

## T-bill risk-free variant (`rf_source=dtb3`)

The baseline uses one rate for two different jobs:

| Job | What it should be | Baseline | `rf_source=dtb3` |
|---|---|---|---|
| Performance: QC's Sharpe subtracts it | risk-free rate: 3-month T-bill | primary credit (about 0.6 pp too high) | DTB3 |
| Carry: financing cost of a margin position | funding rate: cash rate + broker spread | primary credit + `funding_spread` | DTB3 + `funding_spread` |

With `rf_source=dtb3` the algo reads FRED DTB3, installs it as the algorithm's risk-free model (so QC's
statistics use it) and passes DTB3 + `funding_spread` to carry. The default stays `rf_source=lean`, so the
Stage 3 baseline is unchanged and every earlier result still reproduces.

**Which funding spread.** If the ETFs were held fully paid, the carry cost is the interest given up on cash,
i.e. DTB3 alone (`funding_spread=0`). On margin with IB the cost is roughly fed funds + 1% to 1.5%, so
`funding_spread=0.01` is the realistic margin case. Run both.

**Data: QuantConnect has no built-in DTB3 this algo can rely on.** LEAN's own interest-rate file is
primary credit, and QC's FRED coverage is not something to depend on for this series. The algo gets DTB3 in this order, and logs which one it used:

1. Object Store key `DTB3.csv` (parameter `dtb3_key`). Recommended: download the CSV from
   https://fred.stlouisfed.org/series/DTB3 (Download -> CSV) and upload it in the QC web IDE under
   *Object Store*, or with `lean cloud object-store set DTB3.csv DTB3.csv`. Same file every run, so results
   reproduce.
2. Download from `dtb3_url` (default the FRED CSV link). Works if QC's servers can reach FRED; the file
   then changes as FRED adds days, which only affects the tail of the sample.
3. Fallback `rf_source=treasury`: QC's free US Treasury yield curve dataset, 3-month constant-maturity yield
   (the Treasury's own series, close to FRED DGS3MO). Bond-equivalent basis, so a few bp above DTB3's discount
   basis; otherwise the same rate. You can also choose it directly.

If everything fails it uses LEAN's default, and the log says so. Check the log line
`Risk-free model check ...: DTB3 x%, QC model returns x% (should match)`: if the two differ, QC did not
accept the custom model and its Sharpe still uses primary credit (carry still uses DTB3).

**Look-ahead.** DTB3 for day t is published on t+1, so the algo uses values up to the day before (one-day lag).
Treasury yield curve data arrives through a normal subscription, so it has no look-ahead either.

**What changes and what doesn't.** P&L is not charged margin interest in either version (LEAN's default),
so the only thing that changes trades is carry: a funding rate about 0.6 pp lower makes carry more positive,
mostly in 2008-2021, which tilts bond and equity ETFs toward long. QC's Sharpe changes for two reasons: those
trades, and a lower rate subtracted. `compare_stages.py` does not subtract any rate, so its Sharpe moves only
with the trades. Because this equity curve already earns no interest on cash, it is close to an excess return,
which is how Carver measures Sharpe; QC's figure subtracts a rate on top, so the `compare_stages.py` Sharpe is
the better like-for-like number.

**Charts and checks.** Chart `Risk-free rate (%)` now has three lines: `LEAN risk-free` (primary credit, for
reference), `Risk-free used` (DTB3 in this variant) and `Carry funding (rf + spread)`. DTB3 changes daily, so
with it the chart is weekly and the `RF` log line is quarterly. To confirm the run used DTB3:
`python research/check_risk_free.py backtest.json --fred DTB3.csv --series "Risk-free used"` should show a
gap near zero in every year.

## Risk-free rate: source, units, how to check it

- **Source.** LEAN's default `InterestRateProvider` reads `alternative/interest-rate/usa/interest-rate.csv`,
  which is the Fed **primary credit rate** (discount window, FRED series `PCREDIT8`). It is a step series
  that changes only on Fed decision dates; LEAN forward-fills it to every calendar day.
- **Units.** The file is in percent; LEAN divides by 100, so the algo gets an **annual rate as a decimal**
  (0.055 = 5.5% a year). Carry uses it directly against the 12-month distribution yield, also annual decimal.
- **Frequency and lag.** Looked up once a day with the bar time, so a Fed change reaches the carry rule
  the next trading day. Before 1998 or after the file's last date LEAN uses the first/last value.
- **Fallback.** If the model call fails, the fixed `funding_rate` (default 2%) is used, the log says so
  and the runtime statistic shows `(fixed)`.
- **Where it enters.** Only the carry rule: `carry = distribution yield - (rf + funding_spread)`.
  It does not change P&L: the backtest charges no margin interest and pays none on cash (LEAN's default
  for US equities). QC's own Sharpe statistic subtracts this same rate.
- **Dashboard.** Chart `Risk-free rate (%)`: `LEAN risk-free` and `Carry funding (rf + spread)`, in percent,
  plotted weekly and on each change. Log lines `RF <date>: old -> new` on each change and once a quarter.
  Runtime statistic `Risk-free rate` shows the last value and its source.
- **Not a T-bill rate.** Primary credit is a penalty rate: fed funds target + 0.5% to 1% before March 2020,
  the top of the target range since. It runs above the 3-month T-bill (FRED `DTB3`), by roughly half a
  point to three quarters in 2008-2019 and less since 2020. As a funding cost for a margin account it is
  still below what IB charges (benchmark + 0.5% to 1.5%), so `funding_spread` can close that gap.
- **Check it.** Download DTB3 from FRED as CSV, download the backtest JSON, then
  `python research/check_risk_free.py backtest.json --fred DTB3.csv`. It lists every rate change the
  backtest saw (compare with the Fed's primary credit table) and the yearly gap to DTB3.

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

**Start of trading (`wait_for_flip`, default true, same in Stage 2):** the start is treated like a
stop-out. The direction of the first combined vote is blocked, and the first trade opens only when
the vote flips. This matches Carver's re-entry rule and avoids entering mid-trend. With 18 years of
data the few weeks or months lost at the start do not matter. `wait_for_flip=false` opens on the
current vote right after warm-up. Keep the setting the same in both stages for the regression check.

## Running on QuantConnect

1. Create a new Python project, e.g. `CarverStage3`.
2. Replace `main.py` with `stage3_multi_rule/main.py`.
3. Add four files at the project root with the contents of `carver_lib/starter_core.py`,
   `carver_lib/rules.py`, `carver_lib/combine.py` and `carver_lib/rates.py` (same names).
4. Backtest. Charts: *Combined vote*, *Carry (annual)*, *Risk-free rate (%)*. Each opening order tag lists
   the combined vote and every rule's vote.

With the LEAN CLI: copy `main.py` and the four `carver_lib` files into the project folder, then
`lean cloud push --project CarverStage3 && lean cloud backtest CarverStage3 --open`.

## Parameters

Same as Stage 2 (`symbols`, `target_risk`, `idm`, `stop_fraction`, `allow_short`, `wait_for_flip`, `max_leverage`,
`start_year`, `end_year`, `cash`), plus:

| Name | Default | Meaning |
|---|---|---|
| `rules` | all nine | Comma list: `mac16` (= 16/64), `mac16_64`, `breakout40`, `carry`, ... |
| `ma_type` | sma | `sma` (as Stage 2) or `ewma` |
| `w_mac`, `w_breakout`, `w_carry` | 0.3, 0.3, 0.4 | Family weights, split evenly inside each family |
| `funding_spread` | 0.0 | Added to the risk-free rate in carry |
| `funding_rate` | 0.02 | Fallback funding rate if LEAN's risk-free model is unavailable |
| `rf_source` | lean | `lean` (primary credit, baseline), `dtb3` (3-month T-bill from FRED) or `treasury` (QC US Treasury 3-month) |
| `dtb3_key` | DTB3.csv | Object Store key holding the FRED DTB3 CSV |
| `dtb3_url` | FRED DTB3 CSV link | Where to download DTB3 if the Object Store key is missing |
| `warmup_days` | 550 | Calendar days of warm-up |

## Suggested runs for the comparison

1. Stage 2, defaults (baseline).
2. Stage 3, `rules=mac16` (regression check, must equal run 1).
3. Stage 3, defaults.
4. Stage 3, `rules=mac8,mac16,mac32,mac64,breakout40,breakout80,breakout160,breakout320` (trend only, isolates carry).
5. Stage 3, `ma_type=ewma`.
6. Stage 3, `rf_source=dtb3` (T-bill risk-free, carry funding = DTB3). Compare with run 3.
7. Stage 3, `rf_source=dtb3`, `funding_spread=0.01` (T-bill + 1% margin cost in carry). Compare with run 6.

Then compare with `research/compare_stages.py` (see the root README).
