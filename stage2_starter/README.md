# Carver Starter System on ETFs (QuantConnect / LEAN)

Robert Carver's Starter System from *Leveraged Trading*, run on a basket of liquid ETFs.

| File | What it is |
|---|---|
| `main.py` | The QuantConnect algorithm (`CarverStarterSystem`). |
| `../carver_lib/starter_core.py` | Pure-Python rules (signal, risk, sizing, stop). No LEAN imports, so it can be tested offline. |
| `../tests/test_starter_core.py` | Offline checks: `python tests/test_starter_core.py` (needs numpy, pandas). Not needed on QuantConnect. |

## Rules

- **Opening rule**: 16-day MA above 64-day MA, go long; below, go short. Simple MAs by default (as in the book); set `signal_type=ewma` for exponential MAs.
- **Instrument risk**: standard deviation of the last 25 daily % returns × 16 (same as `scheduler/update_intrument_risks.py`).
- **Position size**: `notional = equity × target_risk × weight × IDM / instrument_risk`, weight = 1/N. Fixed at entry, not resized while open.
- **Closing rule**: trailing stop. Gap = 0.5 × instrument risk × entry price, fixed at entry, trailing the highest close (long) or lowest close (short). Checked on the daily close; the exit fills at the next open.
- **Re-entry**: after a stop-out, no new trade in the same direction until the signal has flipped (the overtrading rule in `signal_detector.py`).
- **Portfolio cap**: new positions are trimmed so gross notional stays under `max_leverage × equity`.

Default universe: SPY, EFA, EEM, VNQ, IEF, TLT, GLD, DBC, FXE, FXY, 2008 to 2025, $100,000.

## Backtest on QuantConnect (cloud)

1. Log in at quantconnect.com, **Algorithm Lab → Create new algorithm → Python**.
2. Replace the generated `main.py` with this `main.py`.
3. Add a file named `starter_core.py` (right-click project → New File) and paste `carver_lib/starter_core.py`.
4. Optional: open **Project → Parameters** and add any of the parameters below.
5. Click **Backtest**. Check the *Stops* and *Instrument risk* charts and the order log (each order tag gives the risk and stop level).

## Backtest locally with LEAN CLI

```bash
pip install lean
lean login
lean init              # once, in an empty folder
lean create-project "CarverStarter" --language python
cp main.py starter_core.py CarverStarter/
lean backtest CarverStarter          # needs local data or `--download-data`
# or run it in the cloud from the CLI:
lean cloud push --project CarverStarter && lean cloud backtest CarverStarter --open
```

## Parameters

| Name | Default | Meaning |
|---|---|---|
| `symbols` | 10-ETF list | Comma-separated tickers |
| `target_risk` | 0.12 | Annual portfolio risk target |
| `idm` | 2.0 | Instrument diversification multiplier. Lower it (about 1.0 to 1.5) for 2 to 4 ETFs. |
| `signal_type` | sma | `sma` or `ewma` |
| `stop_fraction` | 0.5 | Stop gap as a fraction of annual instrument risk |
| `allow_short` | true | `false` for long-only |
| `wait_for_flip` | true | Treat the start like a stop-out: the first trade waits for the first crossover after warm-up. `false` opens on the current signal right after warm-up. |
| `max_leverage` | 2.0 | Cap on gross notional / equity |
| `start_year`, `end_year`, `cash` | 2008, 2025, 100000 | Backtest window and capital |

## Changes from `Stage_1.py`

- Multiple ETFs with equal weights and an IDM, instead of XLE alone.
- 25-day risk window instead of 252.
- No 1× notional cap per position (that cap bound most of the time for low-vol ETFs); a portfolio leverage cap instead.
- Stops checked on the close with next-open exits, instead of intraday stop-market orders.
- By default the first trade waits for the first crossover after warm-up, as Stage 1 did, and as the re-entry rule does after a stop-out. Set `wait_for_flip=false` to open on the current signal right after warm-up.

## Known simplifications

- The stop gap uses the signal-day close, not the next-open fill price.
- No borrow costs for shorts beyond what LEAN's IB fee model charges; currency ETFs (FXE, FXY) and DBC have shorter or thinner history.
