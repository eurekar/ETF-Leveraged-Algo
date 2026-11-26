# region imports
from AlgorithmImports import *
from starter_core import StarterSystem, position_size
# endregion


class CarverStarterSystem(QCAlgorithm):
    """
    Robert Carver's Starter System ("Leveraged Trading") on a basket of liquid ETFs.

    Changes vs Stage_1.py (single-ETF XLE version):
      - multi-ETF, equal instrument weights x IDM
      - instrument risk from 25 daily returns (about one month), not 252
      - no 1x notional cap per position; a portfolio max_leverage cap instead
      - stop checked against the daily close (exit next open), no intraday stop orders
      - first trade opens right after warm-up instead of waiting for a crossover

    Daily cycle (runs on each daily close):
      1. Update each ETF's moving averages and 25-day instrument risk.
      2. If in a trade: trail the stop with the close; if the close breaches it,
         exit at the next open.
      3. If flat: open in the direction of the 16/64 crossover (unless that is
         the same direction as the trade just stopped out), sized so that
         notional = equity x target_risk x weight x IDM / instrument_risk.
         Orders fill at the next open.
    Positions are not resized while open, as in the book.

    Tunable via QuantConnect project parameters (all optional):
      symbols       comma list, default the 10-ETF universe below
      target_risk   annual risk target, default 0.12
      idm           instrument diversification multiplier, default 2.0 (10 ETFs)
      signal_type   "sma" (book) or "ewma", default "sma"
      stop_fraction stop gap as a fraction of annual risk, default 0.5
      allow_short   "true"/"false", default "true"
      max_leverage  cap on gross notional / equity, default 2.0
      start_year    default 2008, end_year default 2025, cash default 100000
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
        signal_type = self._param("signal_type", "sma")
        stop_fraction = float(self._param("stop_fraction", 0.5))
        allow_short = str(self._param("allow_short", "true")).lower() == "true"

        self.systems = {}
        for t in tickers:
            sec = self.add_equity(t, Resolution.DAILY)
            sec.set_leverage(4)  # broker margin headroom; real exposure is capped by max_leverage
            self.systems[sec.symbol] = StarterSystem(
                signal_type=signal_type, stop_fraction=stop_fraction, allow_short=allow_short)

        # 64-day MA plus some slack, in trading days
        self.set_warm_up(timedelta(days=110))
        self.set_benchmark("SPY")

    def _param(self, name, default):
        v = self.get_parameter(name)
        return default if v is None or v == "" else v

    # ------------------------------------------------------------------
    def on_data(self, data: Slice):
        for symbol, sys in self.systems.items():
            bar = data.bars.get(symbol)
            if bar is None:
                continue
            close = float(bar.close)
            sys.add_close(close)

            if self.is_warming_up or not sys.ready:
                continue

            risk = sys.instrument_risk()

            # ---- closing rule: trailing stop on the daily close ----
            if sys.trade is not None:
                if sys.check_stop(close):
                    qty = self.portfolio[symbol].quantity
                    if qty != 0:
                        self.market_on_open_order(symbol, -qty, tag=f"Stop hit: close {close:.2f} vs stop {sys.trade.stop_price:.2f}")
                    self.debug(f"{self.time:%Y-%m-%d} {symbol.value} stop hit ({'long' if sys.trade.direction > 0 else 'short'})")
                    sys.close()
                continue

            # ---- opening rule ----
            direction = sys.wants_to_open()
            if direction == 0 or not risk:
                continue

            equity = float(self.portfolio.total_portfolio_value)
            shares = position_size(equity, self.target_risk, risk, close, self.weight, self.idm)
            shares = self._cap_for_leverage(symbol, shares, close, equity)
            if shares <= 0:
                continue

            sys.open(direction, close, risk)
            self.market_on_open_order(
                symbol, direction * shares,
                tag=f"Open {'long' if direction > 0 else 'short'}: risk {risk:.1%}, stop {sys.trade.stop_price:.2f}")

        if not self.is_warming_up:
            for symbol, sys in self.systems.items():
                if sys.trade is not None:
                    self.plot("Stops", f"{symbol.value} stop", sys.trade.stop_price)
                r = sys.instrument_risk()
                if r:
                    self.plot("Instrument risk", symbol.value, r)

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
