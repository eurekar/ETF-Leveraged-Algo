# region imports
from AlgorithmImports import *
from combine import DEFAULT_FAMILY_WEIGHTS
from rules import DEFAULT_RULES
from buffer import ContinuousSystem, DEFAULT_BUFFER
# endregion


class CarverContinuousSystem(QCAlgorithm):
    """
    Stage 4: continuous trading ("Leveraged Trading", the chapter after multiple rules).

    Direction is Stage 3's binary combined vote, with Stage 3's default family weights
    (combine.DEFAULT_FAMILY_WEIGHTS; override with w_mac, w_breakout, w_carry).
    What changes from Stage 3 is position management:
      - no trailing stop and no re-entry rule;
      - every day the target position is recomputed with current equity and instrument risk:
            target = direction x equity x target_risk x (1/N) x IDM / risk / price
      - trade only when the position is outside target +/- buffer x average position,
        and then only to the nearest buffer edge;
      - a signal flip closes and reverses the position in one order at the next open;
      - if the targets of all ETFs together exceed max_leverage x equity, every target
        (and its buffer) is scaled down by the same factor.

    Check before comparing: resize=flip with buffer=100 sizes a position only when the
    direction changes and never adjusts it, so positions change only on signal flips
    (a stop-free Stage 3).

    Parameters (all optional, same names as Stages 2 and 3 where shared):
      symbols, target_risk, idm, allow_short, max_leverage, start_year, end_year, cash
      rules, ma_type, w_mac, w_breakout, w_carry, funding_spread, funding_rate, warmup_days
      buffer          buffer half-width as a fraction of the average position, default 0.10
      resize          "daily" (default) or "flip" (size only on a direction change)
      wait_for_flip   default false here: hold the signal's position from the first ready day
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
        allow_short = str(self._param("allow_short", "true")).lower() == "true"
        wait_for_flip = str(self._param("wait_for_flip", "false")).lower() == "true"
        buffer_fraction = float(self._param("buffer", DEFAULT_BUFFER))
        resize = str(self._param("resize", "daily")).lower()

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
            self.systems[sec.symbol] = ContinuousSystem(
                rules_spec=rules_spec, family_weights=family_weights, ma_type=ma_type,
                funding_spread=funding_spread, allow_short=allow_short,
                buffer_fraction=buffer_fraction, resize=resize, wait_for_flip=wait_for_flip)

        first = next(iter(self.systems.values()))
        self.debug("Rule weights: " + ", ".join(f"{k} {v:.3f}" for k, v in first.weights.items()))
        self.debug(f"Stage 4: buffer {buffer_fraction:.0%}, resize {resize}, wait_for_flip {wait_for_flip}")

        self.set_warm_up(timedelta(days=int(self._param("warmup_days", 550))))
        self.set_benchmark("SPY")
        self.counts = {"open": 0, "flip": 0, "adjust": 0}
        self.capped_days, self.trading_days = 0, 0

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

    def _current_shares(self, symbol):
        """Shares held plus shares in orders sent but not yet filled."""
        pending = sum(o.quantity for o in self.transactions.get_open_orders(symbol))
        return int(self.portfolio[symbol].quantity + pending)

    # ------------------------------------------------------------------
    def on_data(self, data: Slice):
        today = self.time.date()

        # ---- distributions (dividends / bond coupons) for carry, as Stage 3 ----
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
        equity = float(self.portfolio.total_portfolio_value)

        # ---- 1. targets for every ETF with a bar today ----
        plan = {}
        for symbol, sys in self.systems.items():
            bar = data.bars.get(symbol)
            if bar is None:
                continue
            close = float(bar.close)
            sys.add_close(close, today, rf)
            if self.is_warming_up or not sys.ready:
                continue
            d, target, avg = sys.target(equity, self.target_risk, close, self.weight, self.idm)
            plan[symbol] = (sys, close, d, target, avg)

        if self.is_warming_up or not plan:
            return

        # ---- 2. portfolio leverage cap: scale all targets by one factor ----
        # Gross target notional, using the held position for ETFs with no bar today.
        gross = sum(abs(t) * px for _, px, _, t, _ in plan.values())
        gross += sum(abs(float(self.portfolio[s].holdings_value)) for s in self.systems if s not in plan)
        scale = 1.0
        if gross > self.max_leverage * equity > 0:
            scale = self.max_leverage * equity / gross
            self.capped_days += 1
        self.trading_days += 1

        # ---- 3. buffered trades ----
        for symbol, (sys, close, d, target, avg) in plan.items():
            target, avg = target * scale, avg * scale
            current = self._current_shares(symbol)
            qty, reason = sys.order_for(current, d, target, avg)
            if qty == 0:
                continue
            self.counts[reason] += 1
            risk = sys.instrument_risk()
            label = {"open": "Open", "flip": "Open", "adjust": "Adjust"}[reason]
            side = "long" if d > 0 else "short" if d < 0 else "flat"
            what = f"{label} {side}" + (" (flip)" if reason == "flip" else "")
            votes = " ".join(f"{k}:{'+' if v > 0 else '-' if v < 0 else '0'}"
                             for k, v in sys.votes().items() if v is not None)
            self.market_on_open_order(
                symbol, qty,
                tag=f"{what}: {current:+d} -> {current + qty:+d} (target {target:+.0f}, "
                    f"buffer +/-{sys.buffer_fraction * avg:.0f}), vote {sys.combined_vote():+.2f}, "
                    f"risk {risk:.1%} | {votes}")

        # weekly plots keep the chart under QC's point limits
        if self.time.weekday() == 4:
            for symbol, (sys, *_rest) in plan.items():
                self.plot("Combined vote", symbol.value, sys.combined_vote())
            held = sum(abs(float(h.holdings_value)) for h in self.portfolio.values())
            self.plot("Gross leverage", "held", held / equity if equity > 0 else 0)
            self.plot("Gross leverage", "scale", scale)
            self.plot("Funding rate", "risk-free", rf)

    def on_order_event(self, event: OrderEvent):
        if event.status == OrderStatus.FILLED:
            self.log(f"{self.time:%Y-%m-%d} FILL {event.symbol.value} {event.fill_quantity:+.0f} @ {event.fill_price:.2f}")

    def on_end_of_algorithm(self):
        self.set_runtime_statistic("Opens", str(self.counts["open"]))
        self.set_runtime_statistic("Flips", str(self.counts["flip"]))
        self.set_runtime_statistic("Adjusts", str(self.counts["adjust"]))
        pct = self.capped_days / self.trading_days if self.trading_days else 0
        self.set_runtime_statistic("Days leverage-capped", f"{pct:.0%}")
        for symbol, sys in self.systems.items():
            c = sys.carry_rule
            carry = f"{c.carry():+.2%}" if c is not None and c.ready else "n/a"
            self.log(f"END {symbol.value}: vote {sys.combined_vote():+.2f}, carry {carry}, "
                     f"shares {self.portfolio[symbol].quantity:+.0f}")
