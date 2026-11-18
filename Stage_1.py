# region imports
from AlgorithmImports import *
import numpy as np
# endregion

# 07-28-2025 Reviewed and revised 
# 10-26-2026 Revised using Claude

'''
Single asset,
Single trading rule
Single position sizing rule
'''


class Starter_System_Robert_Carver(QCAlgorithm):
    
    
   
    def initialize(self):

        self.set_start_date(2012, 1, 1)
        self.set_end_date(2023, 1, 1)
        self.set_cash(7500)

        # self.default_order_properties.time_in_force = TimeInForce.GOOD_TIL_CANCELED

        # Add underlying
        self.underlying = "XLE" #"TNA"  # "IWM" #"UUP", "XLK", "DBA", "USO", "GLD", "DIA", "SPY"

        # self.add_equity(self.underlying, Resolution.MINUTE, data_normalization_mode=DataNormalizationMode.RAW)
        
        self.security = self.add_equity(self.underlying, Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED)
        
        self.settings.daily_precise_end_time = False
        
        self.set_benchmark(self.underlying) # set the benchmark



        # Build history for indicators
        
        self.history[TradeBar](self.underlying, 252, Resolution.DAILY)#, dataNormalizationMode=DataNormalizationMode.SCALED_RAW)


        

        # Risk management parameters
        self.target_risk = 0.12 # percentage for the whole account
        self.instrument_look_back = 252 # number of days 
        self.instrument_risk_annualizer = 16 #math.sqrt(256/self.instrument_look_back)
        self.stop_price_instrument_ratio = 0.5 # percentage

        self.stop_price = None
        self.stop_distance = None
        self.stop_market_ticket = None



        # make the window big enough for the vol lookback
        self.warm_up_days = self.instrument_look_back + 2

        # Warm up the close price and trade bar rolling windows with the previous 100-day trade bar data
        self.rollingwindow = RollingWindow[TradeBar](self.warm_up_days)
        self.consolidate(self.underlying, Resolution.DAILY, self.CustomBarHandler)

        # Strategy parameters
        short_look_back = 16
        long_look_back = 64

        self.short_ma = self.sma(self.underlying, short_look_back) #, Resolution.DAILY)
        self.long_ma = self.sma(self.underlying, long_look_back) #, Resolution.DAILY)
        self.set_warm_up(self.warm_up_days) # Warm up for the long ma

        self.strategy_direction = ""
        self.new_strategy_direction = ""

        self.prev_signal_direction = ""     # signal on the previous trading day
        self.signal_changed = False         # True only on the bar where the signal flips
     
        # Order ticket for our stop order, Datetime when stop order was last hit
        self.stop_market_ticket = None


        
    def on_data(self, data):
        
        # Warming up the data and indicator 
        
        #if not self.long_ma.is_ready: return
        if self.is_warming_up or not self.rollingwindow.is_ready: return


        # Calculate the trade signal
        self.cal_trade_signal()
           

        # Get current close
        self.trade_bar_df = self.pandas_converter.get_data_frame[TradeBar](list(self.rollingwindow)[::-1]) 
        self.current_close = self.security.close #self.trade_bar_df.close[-1] 

        #self.debug(self.current_close)


        # Calculate annualized instrument risk
        self.cal_annualized_instrument_risk()

        self.plot("Risks", "Annualized Instrument Risk", self.annalized_instrument_risk)


        # Do not trade if the position was liquidated and no signal change happened to trigger an open trade.

        # if not self.portfolio.invested and self.new_strategy_direction == self.strategy_direction: return

        #if self.new_strategy_direction == self.strategy_direction: return

        # Calculate the trade size
        self.cal_trade_size()
        

        # Trade logic
        if self.portfolio.invested:
            # Hold until the stop fills; signal changes are ignored while in a position
            if self.stop_market_ticket is not None:
                self.update_tsl_order()

        else:
            # Flat: clear any stale (filled or canceled) stop ticket
            self.stop_market_ticket = None

            # Enter only if the signal differs from the previous trading day's signal
            if self.signal_changed and self.size >= 1:
                self.open_trade()
     

        #Plot the moving stop price on "Data Chart" with "Stop Price" series name
        self.plot_data()
    

            

    def on_order_event(self, order_event):
        if order_event.status != OrderStatus.FILLED: return

        self.debug(order_event)

        # Only the entry order triggers a new stop
        order = self.transactions.get_order_by_id(order_event.order_id) # If the order is pending, do nothing.
        if order.tag != "entry":
            return

        # Position after the fill (signed: + long, - short)
        qty = self.portfolio[self.underlying].quantity         
        fill = order_event.fill_price


        # Stop sits stop_distance away from the fill price, on the losing side
        self.stop_price = fill - self.stop_distance if qty > 0 else fill + self.stop_distance
        self.stop_market_ticket = self.stop_market_order(self.underlying, -qty, self.stop_price)

        # Reset the trailing-stop reference prices
        self.highest_price = self.lowest_price = fill    
        


    def CustomBarHandler(self, bar):
        self.rollingwindow.add(bar)      


    def Instrument_Risk(self, df) -> float:

        self.daily_returns = df.close.pct_change()

        self.risk = self.current_close * self.daily_returns[-self.instrument_look_back-1:-1].std() # offset by -1 for not include the EoD price on the trading day

        return self.risk

    

    def cal_annualized_instrument_risk(self):
    
        self.instrument_risk = self.Instrument_Risk(self.trade_bar_df) 

        self.annalized_instrument_risk = self.instrument_risk * self.instrument_risk_annualizer # self.instrument_risk

        # self.Debug(f"Annualized risk by price points is {round(self.annalized_instrument_risk, 2)}")

    
    def cal_trade_size(self):
        '''
        sizing: risk-targeted, capped at 100% notional
        '''
        
        self.target_exposure = self.target_risk * self.portfolio.total_portfolio_value

        # self.debug(f"Holdings value is {round(self.portfolio[self.underlying].holdings_value, 2)}.")

        self.notional_exposure = self.target_exposure / max(self.annalized_instrument_risk, 1e-6)
        cap_shares = self.portfolio.total_portfolio_value / self.current_close
        self.size = math.floor(min(self.notional_exposure, cap_shares))
        
        # self.notional_exposure = self.target_exposure/self.annalized_instrument_risk

        # self.notional_exposure = self.target_exposure/self.current_close #annalized_instrument_risk

        # self.size = math.floor(self.notional_exposure)

        # self.Debug(f"Trade size is {str(self.size)}.")



    def cal_trade_signal(self):
        if self.short_ma.current.value > self.long_ma.current.value:
            self.new_strategy_direction = "long"
        else:
            self.new_strategy_direction = "short"

        # Signal changed = today's signal differs from the previous trading day's signal.
        # The first bar after warm-up has no previous day, so it never counts as a change.
        self.signal_changed = (self.prev_signal_direction != "" and
                            self.new_strategy_direction != self.prev_signal_direction)

        if self.signal_changed:
            self.debug(f"Signal changed to: {self.new_strategy_direction}.")

        # Remember today's signal for tomorrow's comparison (updated every bar, invested or not)
        self.prev_signal_direction = self.new_strategy_direction


    def open_trade(self):
        '''
        determine the trade direction, tag the entry and remember the stop distance
        '''    


        self.strategy_direction = self.new_strategy_direction   
        
        qty = self.size if self.strategy_direction == "long" else -self.size
            
        self.stop_distance = self.annalized_instrument_risk * self.stop_price_instrument_ratio

        self.market_on_open_order(self.underlying, qty, tag="entry")
        

        


    def update_tsl_order(self):

        self.start_stop_price = self.stop_price

        update_fields = UpdateOrderFields()

        if self.strategy_direction == "long": 
            if self.current_close > self.highest_price:
                delta = self.current_close - self.highest_price 
                update_fields.stop_price = self.start_stop_price + delta 
                self.stop_market_ticket.update(update_fields)
                self.highest_price = self.current_close
                self.stop_price = update_fields.stop_price
            else:
                pass #self.stop_price = self.start_stop_price

            

        if self.strategy_direction == "short":
            if self.current_close < self.lowest_price:
                delta = self.lowest_price - self.current_close
                update_fields.stop_price = self.start_stop_price - delta
                self.stop_market_ticket.update(update_fields)
                self.lowest_price = self.current_close
                self.stop_price = update_fields.stop_price
            else:
                pass #self.stop_price = self.start_stop_price 
           



    def plot_data(self):
        self.plot("Data Chart", "Asset Price", self.current_close)

        if self.stop_price is not None and self.portfolio.invested:
            self.plot("Data Chart", "Stop Price", self.stop_price)

        self.plot("Data Chart", "Short MA", self.short_ma.current.value)
        self.plot("Data Chart", "Long MA", self.long_ma.current.value)

