# Stage 4: continuous trading

Stage 3's direction, with Carver's continuous position management in place of stop-losses
(*Leveraged Trading*, the chapter after multiple rules). Universe, dates, capital, risk target,
IDM, leverage cap, rules and rule weights are the same as Stage 3.

## What changes from Stage 3

| | Stage 3 | Stage 4 |
|---|---|---|
| Direction | sign of the combined vote | same |
| Entry | only on a vote flip after a stop-out (re-entry rule) | always: hold what the vote says, every day |
| Exit | trailing stop on the close | vote flips: close and reverse in one order |
| Size | fixed at entry | re-sized daily with current equity and risk, inside a buffer |
| Leverage cap | new trades trimmed | all targets scaled by one factor when gross > 2x |

Why: in Stage 3 a stopped-out ETF stays flat until the vote flips. Carry rarely flips, so with a
high carry weight the system spent most of its time flat (46 trades in 18 years at 25/25/50).
Stage 4 has no stops, so there is nothing to be locked out of.

## Daily loop, for each ETF

```
direction    = sign of the combined vote             (+1, -1, or 0 on an exact tie)
average_pos  = equity x 12% x (1/N) x IDM / instrument risk / price     (shares)
target       = direction x average_pos
buffer       = 10% x average_pos

flat, or holding the opposite sign      -> trade straight to target (open, or close-and-reverse)
current < target - buffer               -> buy up to target - buffer
current > target + buffer               -> sell down to target + buffer
otherwise                               -> no trade
```

Orders are market-on-open, the day after the signal, as in Stages 2 and 3. Instrument risk is the
same 25-day estimate. The buffer is a fraction of the average position, so a tie (target 0) trims
the position to 10% of normal size rather than closing it; that is the binary version of a zero
forecast. With 30/30/40 weights ties cannot happen once carry is ready; before that (an ETF's first year)
a 4-4 split of the trend rules is a tie.

If the ten targets together would exceed `max_leverage` x equity, every target and buffer is scaled
by the same factor. The *Gross leverage* chart shows the held leverage and that factor; the runtime
statistic *Days leverage-capped* gives the share of days it bound.

Order tags start with `Open` (new position or flip, flips say `(flip)`) or `Adjust` (buffer trade),
and give current and new shares, target, buffer width, vote, risk and every rule's vote.
`research/compare_stages.py` counts them as *Opens* and *Adjusts*; *Stops* is always 0.

## Running on QuantConnect

1. Create a new Python project, e.g. `CarverStage4`.
2. Replace `main.py` with `stage4_continuous/main.py`.
3. Add four files at the project root (same names), with the contents of
   `carver_lib/starter_core.py`, `carver_lib/rules.py`, `carver_lib/combine.py` and
   `carver_lib/buffer.py`.
4. Backtest with no parameters for the default run.

With the LEAN CLI: copy `main.py` and the four `carver_lib` files into the project folder, then
`lean cloud push --project CarverStage4 && lean cloud backtest CarverStage4 --open`.

## Parameters

Same as Stage 3 (`symbols`, `target_risk`, `idm`, `allow_short`, `max_leverage`, `start_year`,
`end_year`, `cash`, `rules`, `ma_type`, `w_mac`, `w_breakout`, `w_carry`, `funding_spread`,
`funding_rate`, `warmup_days`), plus:

| Name | Default | Meaning |
|---|---|---|
| `buffer` | 0.10 | Buffer half-width as a fraction of the average position |
| `resize` | daily | `daily` (Stage 4) or `flip`: size only when the direction changes, then hold |
| `wait_for_flip` | **false** | `true` ignores the first vote and waits for it to flip, as Stages 2 and 3 |

`stop_fraction` is not used (no stops). `wait_for_flip` defaults to false here because a continuous
system has no entry trigger to wait for: it holds the signal's position from the first ready day.
Set it to `true` if you want the start to match Stage 3 exactly.

## Runs, in order

1. **Check:** `resize=flip`, `buffer=100`. Each position is sized when the direction changes and
   never adjusted, so the order count equals the number of vote flips (plus the first entry) and
   *Adjusts* is 0. That is a stop-free Stage 3. `tests/test_stage4.py` checks the same offline.
2. **Default:** no parameters (10% buffer).
3. **Buffer sweep:** `buffer=0.05` and `buffer=0.2`, to see cost against tracking.
4. Compare:

```bash
python research/compare_stages.py stage2="stage2_starter/Stage 2 baseline.json" \
    stage3=stage3.json s4_check=s4_flip.json s4=s4.json s4_b05=s4_b05.json s4_b20=s4_b20.json \
    --split 2016-01-01
```

## What to look for

- Realised vol much closer to the 12% target than Stage 3's 3% to 6%, because the system is
  always invested and sized to current risk.
- More orders, each smaller. Fees are the cost of that: compare *Fees % equity/yr* across the
  buffer sweep. At $100k many trades are small, so IB's $1 minimum commission matters.
- Whether Sharpe holds in both halves. A gain under about 0.1 is within noise.
- How often the 2x leverage cap binds (*Days leverage-capped*). If it binds most days, the
  effective risk target is lower than 12%.
