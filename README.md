# ETF-Leveraged-Algo

A QuantConnect project based on Robert Carver's *Leveraged Trading*, built in stages. Each stage is its
own QuantConnect algorithm that changes one thing from the stage before, so results can be compared
like-for-like.

| Stage | Folder | What changes |
|---|---|---|
| 1 | `Stage_1.py` | Starter System on one ETF (XLE). Reference only. |
| 2 | `stage2_starter/` | Ten ETFs (SPY, EFA, EEM, VNQ, IEF, TLT, GLD, DBC, FXE, FXY), 16/64 crossover, trailing stop, IDM 2.0 |
| 3 | `stage3_multi_rule/` | Direction from a vote of 4 crossovers, 4 breakouts and carry (ch. 8). Stops and sizing as Stage 2 |
| 4 | planned | Continuous trading: daily resizing with a buffer, no stops |
| 5 | planned | Non-binary forecasts: scaled, capped, FDM |

## Layout

```
carver_lib/          shared logic, no LEAN imports, tested offline
  starter_core.py    Stage 2: instrument risk, sizing, trailing stop, re-entry rule
  rules.py           Stage 3: crossover, breakout, carry
  combine.py         Stage 3: rule weights, combined vote, MultiRuleSystem
  rates.py           FRED CSV parsing and lagged rate lookup (DTB3 risk-free, rf_source=dtb3)
stage2_starter/      main.py (QC algorithm) + README
stage3_multi_rule/   main.py (QC algorithm) + README
tests/               python -m pytest tests   (needs numpy, pandas, pytest)
research/
  compare_stages.py  scorecard across backtests
```

On QuantConnect, copy a stage's `main.py` plus the `carver_lib` files it imports into the project root
(flat, same file names). See each stage's README.

## Comparing stages

1. Backtest each stage on QuantConnect with the same parameters.
2. Download each result as JSON (web IDE: *Download results*; LEAN CLI: the `backtests/<time>/<id>.json` file).
3. Run:

```bash
python research/compare_stages.py stage2=stage2.json stage3=stage3.json --split 2016-01-01
```

It prints annual return, realised vol, Sharpe, max and longest drawdown, monthly skew, orders, opens,
stops, turnover and fees, for the full sample and both halves. Count a change as an improvement only
if it holds in both halves; a Sharpe gain under about 0.1 over 18 years is within noise.
