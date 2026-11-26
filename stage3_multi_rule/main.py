# region imports
from AlgorithmImports import *
from starter_core import position_size
from combine import MultiRuleSystem, DEFAULT_FAMILY_WEIGHTS
from rules import DEFAULT_RULES
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
      symbols, target_risk, idm, stop_fraction, allow_short, max_leverage,
      start_year, end_year, cash            as Stage 2
      rules           comma list, default all nine rules (see rules.py for syntax)
      ma_type         "sma" (default, as Stage 2) or "ewma" for the crossovers
      w_mac, w_breakout, w_carry   family weights, default 0.3 / 0.3 / 0.4
      funding_spread  added to the risk-free rate for carry, default 0.0
      funding_rate    fixed funding rate to use if LEAN's risk-free model is not
                      available, default 0.02
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

        rules_spec = self._param("rules", DEFAULT_RULES)
        ma_type = self._param("ma_type", "sma")
        family_weights = {
            "mac": float(self._param("w_mac", DEFAULT_FAMILY_WEIGHTS["mac"])),
            "breakout": float(self._param("w_breakout", DEFAULT_FAMILY_WEIGHTS["breakout"])),
            "carry": float(self._param("w_carry", DEFAULT_FAMILY_WEIGHTS["carry"])),
        }
        funding_spread = float(self._param("funding_spread", 0.0))
        self.fixed_funding_rate = float(self._param("funding_rate", 0.02))
        self._rf_date, self._rf = None, None
        self._rf_warned = False

        self.systems = {}
        for t in tickers:
            sec = self.add_equity(t, Resolution.DAILY)
            sec.set_leverage(4)  # broker margin headroom; real exposure is capped by max_leverage
            self.systems[sec.symbol] = MultiRuleSystem(
                rules_spec=rules_spec, family_weights=family_weights, ma_type=ma_type,
                funding_spread=funding_spread, stop_fraction=stop_fraction, allow_short=allow_short)

        first = next(iter(self.systems.values()))
        self.debug("Rule weights: " + ", ".join(f"{k} {v:.3f}" for k, v in first.weights.items()))

        self.set_warm_up(timedelta(days=int(self._param("warmup_days", 550))))
        self.set_benchmark("SPY")
        self.opens, self.stops = 0, 0

    def _param(self, name, default):
        v = self.get_parameter(name)
        return default if v is None or v == "" else v

    def _risk_free_rate(self):
        """Annual risk-free rate (decimal) from LEAN's model, once per day."""
        today = self.time.date()
        if self._rf_date == today:
            return self._rf
        try:
            rf = float(self.risk_free_interest_rate_model.get_interest_rate(self.time))
        except Exception as e:  # older LEAN or model not available
            if not self._rf_warned:
                self.debug(f"Risk-free model unavailable ({e}); using fixed funding_rate {self.fixed_funding_rate:.2%}")
                self._rf_warned = True
            rf = self.fixed_funding_rate
        self._rf_date, self._rf = today, rf
        return rf

    # ------------------------------------------------------------------
    def on_data(self, data: Slice):
        today = self.time.date()

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
            self.plot("Funding rate", "risk-free", rf)

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
        for symbol, sys in self.systems.items():
            c = sys.carry_rule
            carry = f"{c.carry():+.2%}" if c is not None and c.ready else "n/a"
            self.log(f"END {symbol.value}: vote {sys.combined_vote():+.2f}, carry {carry}, votes {sys.votes()}")
