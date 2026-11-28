# region imports
from AlgorithmImports import *
from starter_core import position_size
from combine import MultiRuleSystem, DEFAULT_FAMILY_WEIGHTS
from rules import DEFAULT_RULES
from rates import StepRate, parse_fred_csv, FRED_DTB3_URL
# endregion


class CarverMultiRuleSystem(QCAlgorithm):
    """
    Stage 3: Carver's Starter System with several trading rules ("Leveraged Trading", ch. 8).

    Only the opening decision changes from Stage 2. Each ETF's direction is the sign
    of a weighted vote of:
        moving-average crossovers 8/32, 16/64, 32/128, 64/256   (30%)
        breakouts over 40, 80, 160, 320 days                    (30%)
        carry: 12-month distribution yield minus funding rate   (40%)
    Instrument risk, sizing fixed at entry, the trailing stop on the close and the
    no-same-direction re-entry rule are the Stage 2 code, unchanged.

    Regression check: rules=mac16 reproduces Stage 2 (same trades, same fills).

    Parameters (all optional, same names as Stage 2 where shared):
      symbols, target_risk, idm, stop_fraction, allow_short, wait_for_flip,
      max_leverage, start_year, end_year, cash   as Stage 2
      rules           comma list, default all nine rules (see rules.py for syntax)
      ma_type         "sma" (default, as Stage 2) or "ewma" for the crossovers
      w_mac, w_breakout, w_carry   family weights, default 0.3 / 0.3 / 0.4
      funding_spread  added to the risk-free rate for carry, default 0.0
      funding_rate    fixed funding rate to use if LEAN's risk-free model is not
                      available, default 0.02
      rf_source       which risk-free rate to use, default "lean" (the Stage 3 baseline):
                        lean      LEAN's default model, Fed primary credit (FRED PCREDIT8)
                        dtb3      3-month T-bill, FRED DTB3, read from the Object Store key
                                  dtb3_key (default "DTB3.csv") or downloaded from dtb3_url
                                  (default FRED). Falls back to "treasury" if neither works.
                        treasury  QC's US Treasury yield curve dataset, 3-month yield
                      With dtb3/treasury the rate is installed as the algorithm's risk-free
                      model, so QC's Sharpe uses it too, and carry funding = that rate +
                      funding_spread.

    Two jobs, one rate each: the risk-free rate measures performance (QC's Sharpe);
    carry's funding cost is risk-free + funding_spread (what financing a margin
    position costs). Charted as "Risk-free rate (%)": "LEAN risk-free" (primary credit,
    for reference), "Risk-free used" and "Carry funding (rf + spread)". Logged as
    "RF <date>: ..." on every change of a step series and once a quarter.
    research/check_risk_free.py compares a charted series with FRED DTB3.
      warmup_days     calendar days of warm-up, default 550 (320-day breakout + slack)
    """

    def initialize(self):
        self.set_start_date(int(self._param("start_year", 2008)), 1, 1)
        self.set_end_date(int(self._param("end_year", 2025)), 12, 31)
        self.set_cash(float(self._param("cash", 100_000)))
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)

        default_universe = "SPY,EFA,EEM,VNQ,IEF,TLT,GLD,DBC,FXE,FXY"
        tickers = [t.strip().upper() for t in self._param("symbols", default_universe).split(",") if t.strip()]
        self.target_risk = float(self._param("target_risk", 0.12))
        self.idm = float(self._param("idm", 2.0))
        self.max_leverage = float(self._param("max_leverage", 2.0))
        self.weight = 1.0 / len(tickers)
        stop_fraction = float(self._param("stop_fraction", 0.5))
        allow_short = str(self._param("allow_short", "true")).lower() == "true"
        wait_for_flip = str(self._param("wait_for_flip", "true")).lower() == "true"

        rules_spec = self._param("rules", DEFAULT_RULES)
        ma_type = self._param("ma_type", "sma")
        family_weights = {
            "mac": float(self._param("w_mac", DEFAULT_FAMILY_WEIGHTS["mac"])),
            "breakout": float(self._param("w_breakout", DEFAULT_FAMILY_WEIGHTS["breakout"])),
            "carry": float(self._param("w_carry", DEFAULT_FAMILY_WEIGHTS["carry"])),
        }
        funding_spread = float(self._param("funding_spread", 0.0))
        self.funding_spread = funding_spread
        self.fixed_funding_rate = float(self._param("funding_rate", 0.02))
        self._rf_date, self._rf = None, None
        self._rf_warned = False
        self._rf_source = "LEAN"          # "LEAN", "DTB3", "UST3M" or "fixed" (fallback)
        self._rf_logged = None            # last rate written to the log
        self._rf_quarter = None           # (year, quarter) of the last periodic log

        # Dashboard chart for the rate the carry rule uses, in percent (same units as FRED DTB3)
        rf_chart = Chart("Risk-free rate (%)")
        rf_chart.add_series(Series("LEAN risk-free", SeriesType.LINE, 0, "%"))
        rf_chart.add_series(Series("Risk-free used", SeriesType.LINE, 0, "%"))
        rf_chart.add_series(Series("Carry funding (rf + spread)", SeriesType.LINE, 0, "%"))
        self.add_chart(rf_chart)

        self.systems = {}
        for t in tickers:
            sec = self.add_equity(t, Resolution.DAILY)
            sec.set_leverage(4)  # broker margin headroom; real exposure is capped by max_leverage
            self.systems[sec.symbol] = MultiRuleSystem(
                rules_spec=rules_spec, family_weights=family_weights, ma_type=ma_type,
                funding_spread=funding_spread, stop_fraction=stop_fraction, allow_short=allow_short,
                wait_for_flip=wait_for_flip)

        first = next(iter(self.systems.values()))
        self.debug("Rule weights: " + ", ".join(f"{k} {v:.3f}" for k, v in first.weights.items()))

        self.set_warm_up(timedelta(days=int(self._param("warmup_days", 550))))
        self._lean_rates = None   # LEAN's primary-credit provider, kept for the chart
        self._rf_series = None    # StepRate when rf_source is dtb3 / treasury
        self._treasury = None     # US Treasury yield curve symbol (rf_source=treasury)
        self._lean_rf_value = None
        self._rf_model_checked = False
        self.rf_source = self._param("rf_source", "lean").strip().lower()
        if self.rf_source != "lean":
            self._setup_external_rate()
        self.set_benchmark("SPY")
        self.opens, self.stops = 0, 0

    def _param(self, name, default):
        v = self.get_parameter(name)
        return default if v is None or v == "" else v

    # ------------------------------------------------------------------
    # Risk-free rate other than LEAN's default (rf_source = dtb3 / treasury)
    def _setup_external_rate(self):
        try:
            self._lean_rates = InterestRateProvider()
        except Exception as e:
            self.debug(f"LEAN InterestRateProvider not available for the chart ({e})")
        src = self.rf_source
        if src == "dtb3":
            pts = self._load_dtb3()
            if pts:
                # DTB3 for day t is published on t+1: use values up to the day before.
                self._rf_series = StepRate(pts, lag_days=1, name="DTB3")
                self._rf_source = "DTB3"
            else:
                self.debug("DTB3 not available; falling back to rf_source=treasury")
                src = "treasury"
        if src == "treasury":
            try:
                self._treasury = self.add_data(USTreasuryYieldCurveRate, "USTYCR", Resolution.DAILY).symbol
                # Filled in on_data as each day's yields arrive (warm-up included), so no look-ahead.
                self._rf_series = StepRate(lag_days=0, name="UST3M")
                self._rf_source = "UST3M"
            except Exception as e:
                self.debug(f"US Treasury yield curve not available ({e}); using LEAN's default rate")
                self._rf_series, self._rf_source = None, "LEAN"
        if src not in ("dtb3", "treasury"):
            self.debug(f"Unknown rf_source '{self.rf_source}'; using LEAN's default rate")
        if self._rf_series is not None:
            try:
                self.set_risk_free_interest_rate_model(SeriesRiskFreeModel(self))
            except Exception as e:
                self.debug(f"Could not install {self._rf_source} as the risk-free model ({e}); "
                           f"carry uses it, QC's Sharpe still uses LEAN's default")

    def _load_dtb3(self):
        key = self._param("dtb3_key", "DTB3.csv")
        text, where = None, None
        try:
            if self.object_store.contains_key(key):
                text, where = self.object_store.read(key), f"Object Store '{key}'"
        except Exception as e:
            self.debug(f"Object Store read of '{key}' failed ({e})")
        if not text:
            url = self._param("dtb3_url", FRED_DTB3_URL)
            try:
                text, where = self.download(url), url
            except Exception as e:
                self.debug(f"Download of {url} failed ({e})")
        pts = parse_fred_csv(text) if text else []
        if pts:
            self.debug(f"DTB3 from {where}: {len(pts)} days, {pts[0][0]} to {pts[-1][0]}, "
                       f"last {pts[-1][1]:.2%}")
            end = datetime(int(self._param("end_year", 2025)), 12, 1).date()
            if pts[-1][0] < end:
                self.debug(f"DTB3 ends {pts[-1][0]}, before the backtest end; the last value is carried forward")
        elif text:
            self.debug(f"DTB3 from {where} had no usable rows (first 80 chars: {text[:80]!r})")
        return pts

    def _series_rate(self, when):
        """Rate from the external series, else the first value it has, else LEAN / fixed."""
        rf = self._rf_series.rate_at(when) if self._rf_series is not None else None
        if rf is None and self._rf_series is not None and len(self._rf_series):
            rf = self._rf_series.values[0]
        if rf is None:
            rf = self._lean_rate(when)
        return rf if rf is not None else self.fixed_funding_rate

    def _lean_rate(self, when):
        try:
            if self._lean_rates is not None:
                return float(self._lean_rates.get_interest_rate(when))
            return float(self.risk_free_interest_rate_model.get_interest_rate(when))
        except Exception:
            return None

    def _risk_free_rate(self):
        """Annual risk-free rate (decimal), once per day: LEAN's model or the rf_source series."""
        today = self.time.date()
        if self._rf_date == today:
            return self._rf
        if self._rf_series is not None:
            rf = self._series_rate(self.time)
            self._lean_rf_value = self._lean_rate(self.time)
            if not self._rf_model_checked:
                self._rf_model_checked = True
                try:
                    qc = float(self.risk_free_interest_rate_model.get_interest_rate(self.time))
                    self.debug(f"Risk-free model check {today}: {self._rf_source} {rf:.2%}, "
                               f"QC model returns {qc:.2%} (should match)")
                except Exception as e:
                    self.debug(f"Risk-free model check failed ({e})")
            changed = False   # daily series: log quarterly only, plot weekly
            self._rf_date, self._rf = today, rf
            self._log_risk_free(today, rf, changed)
            return rf
        try:
            rf = float(self.risk_free_interest_rate_model.get_interest_rate(self.time))
            self._rf_source = "LEAN"
        except Exception as e:  # older LEAN or model not available
            if not self._rf_warned:
                self.debug(f"Risk-free model unavailable ({e}); using fixed funding_rate {self.fixed_funding_rate:.2%}")
                self._rf_warned = True
            rf = self.fixed_funding_rate
            self._rf_source = "fixed"
        changed = self._rf is not None and rf != self._rf
        self._lean_rf_value = rf
        self._rf_date, self._rf = today, rf
        self._log_risk_free(today, rf, changed)
        return rf

    def _log_risk_free(self, today, rf, changed):
        """Plot the rate on every change (exact step dates) and log it on change and once a quarter."""
        quarter = (today.year, (today.month - 1) // 3)
        if changed or self._rf_logged is None:
            prev = "start" if self._rf_logged is None else f"{self._rf_logged:.2%}"
            self.log(f"RF {today}: {prev} -> {rf:.2%} (source {self._rf_source}, "
                     f"carry funding {rf + self.funding_spread:.2%})")
            self._rf_logged, self._rf_quarter = rf, quarter
            self._plot_risk_free(rf)
        elif quarter != self._rf_quarter:
            self.log(f"RF {today}: {rf:.2%} (source {self._rf_source}, "
                     f"carry funding {rf + self.funding_spread:.2%})")
            self._rf_quarter = quarter

    def _plot_risk_free(self, rf):
        if self._lean_rf_value is not None:
            self.plot("Risk-free rate (%)", "LEAN risk-free", 100.0 * self._lean_rf_value)
        self.plot("Risk-free rate (%)", "Risk-free used", 100.0 * rf)
        self.plot("Risk-free rate (%)", "Carry funding (rf + spread)", 100.0 * (rf + self.funding_spread))

    # ------------------------------------------------------------------
    def on_data(self, data: Slice):
        today = self.time.date()

        # ---- 3-month Treasury yield (rf_source=treasury), percent -> decimal ----
        if self._treasury is not None:
            try:
                if data.contains_key(self._treasury):
                    pt = data[self._treasury]
                    if pt.three_month is not None:
                        self._rf_series.add(pt.time, float(pt.three_month) / 100.0)
            except Exception as e:
                self.debug(f"Treasury yield read failed on {today} ({e})")

        # ---- distributions (dividends / bond coupons) for carry ----
        for symbol in list(data.dividends.keys()):
            sys = self.systems.get(symbol)
            if sys is None:
                continue
            div = data.dividends[symbol]
            ref = float(getattr(div, "reference_price", 0) or 0)
            if ref <= 0:
                ref = float(self.securities[symbol].price)
            sys.add_distribution(today, float(div.distribution), ref)

        rf = self._risk_free_rate()

        for symbol, sys in self.systems.items():
            bar = data.bars.get(symbol)
            if bar is None:
                continue
            close = float(bar.close)
            sys.add_close(close, today, rf)

            if self.is_warming_up or not sys.ready:
                continue

            risk = sys.instrument_risk()

            # ---- closing rule: trailing stop on the daily close (Stage 2, unchanged) ----
            if sys.trade is not None:
                if sys.check_stop(close):
                    qty = self.portfolio[symbol].quantity
                    if qty != 0:
                        self.market_on_open_order(symbol, -qty, tag=f"Stop hit: close {close:.2f} vs stop {sys.trade.stop_price:.2f}")
                    self.stops += 1
                    sys.close()
                continue

            # ---- opening rule: sign of the combined vote ----
            direction = sys.wants_to_open()
            if direction == 0 or not risk:
                continue

            equity = float(self.portfolio.total_portfolio_value)
            shares = position_size(equity, self.target_risk, risk, close, self.weight, self.idm)
            shares = self._cap_for_leverage(symbol, shares, close, equity)
            if shares <= 0:
                continue

            sys.open(direction, close, risk)
            self.opens += 1
            votes = " ".join(f"{k}:{'+' if v > 0 else '-' if v < 0 else '0'}"
                             for k, v in sys.votes().items() if v is not None)
            self.market_on_open_order(
                symbol, direction * shares,
                tag=f"Open {'long' if direction > 0 else 'short'}: vote {sys.combined_vote():+.2f}, "
                    f"risk {risk:.1%}, stop {sys.trade.stop_price:.2f} | {votes}")

        # weekly plots keep the chart under QC's point limits
        if not self.is_warming_up and self.time.weekday() == 4:
            for symbol, sys in self.systems.items():
                if sys.ready:
                    self.plot("Combined vote", symbol.value, sys.combined_vote())
                c = sys.carry_rule
                if c is not None and c.ready:
                    self.plot("Carry (annual)", symbol.value, c.carry())
            self._plot_risk_free(rf)

    def _cap_for_leverage(self, symbol, shares, price, equity):
        """Shrink a new position so gross notional stays under max_leverage x equity."""
        gross = sum(abs(float(h.holdings_value)) for h in self.portfolio.values() if h.symbol != symbol)
        room = self.max_leverage * equity - gross
        if room <= 0:
            return 0
        return min(shares, int(room / price))

    def on_order_event(self, event: OrderEvent):
        if event.status == OrderStatus.FILLED:
            self.log(f"{self.time:%Y-%m-%d} FILL {event.symbol.value} {event.fill_quantity:+.0f} @ {event.fill_price:.2f}")

    def on_end_of_algorithm(self):
        self.set_runtime_statistic("Trades opened", str(self.opens))
        self.set_runtime_statistic("Stops hit", str(self.stops))
        if self._rf is not None:
            self.set_runtime_statistic("Risk-free rate", f"{self._rf:.2%} ({self._rf_source})")
            self.set_runtime_statistic("Carry funding", f"{self._rf + self.funding_spread:.2%}")
        for symbol, sys in self.systems.items():
            c = sys.carry_rule
            carry = f"{c.carry():+.2%}" if c is not None and c.ready else "n/a"
            self.log(f"END {symbol.value}: vote {sys.combined_vote():+.2f}, carry {carry}, votes {sys.votes()}")


class SeriesRiskFreeModel:
    """Risk-free model for QC's statistics backed by the algorithm's rf_source series
    (LEAN IRiskFreeInterestRateModel, duck-typed). Returns an annual rate as a decimal."""

    def __init__(self, algorithm):
        self.algorithm = algorithm

    def get_interest_rate(self, date):
        try:
            when = datetime(date.year, date.month, date.day)
        except AttributeError:       # a .NET DateTime
            when = datetime(date.Year, date.Month, date.Day)
        return float(self.algorithm._series_rate(when))

    GetInterestRate = get_interest_rate
