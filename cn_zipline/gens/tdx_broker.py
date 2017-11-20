# encoding: UTF-8

from zipline.gens.brokers.broker import Broker
from six import iteritems
from zipline import protocol
from zipline.finance.execution import (
    MarketOrder,
    LimitOrder,
    StopOrder,
    StopLimitOrder
)
from zipline.finance.order import (
    Order as ZPOrder,
    ORDER_STATUS as ZP_ORDER_STATUS
)
from cn_zipline.gens.type import Transaction as TdxTransaction
from zipline.finance.transaction import Transaction
from zipline.api import symbol
from .type import *
import datetime
from logbook import Logger
import pandas as pd
from tdx.engine import Engine
import numpy as np
from abc import ABCMeta, abstractmethod, abstractproperty
import abc

log = Logger("TDX Broker")


class TdxBroker(Broker):
    def __init__(self, client):

        self._orders = {}
        self._client = client
        self.currency = 'RMB'
        self._subscribed_assets = []
        self._bars = {}
        self._bars_update_dt = None
        self._bars_update_interval = pd.tslib.Timedelta('5 S')
        self._mkt_client = Engine(auto_retry=True, best_ip=True)
        self._mkt_client.connect()

        super(self.__class__, self).__init__()

    def subscribe_to_market_data(self, asset):
        # TODO fix me subcribe_to_market_data
        if asset not in self.subscribed_assets:
            # remove str() cast to have a fun debugging journey
            # self._client.subscribe_to_market_data(str(asset.symbol))
            self._subscribed_assets.append(asset)
            self._bars_update_dt = None

    def _update_bars(self):
        now = pd.to_datetime('now')
        if self._bars_update_dt and (now - self._bars_update_dt < self._bars_update_interval):
            return
        for code in self.subscribed_assets:
            self._bars[code] = self._mkt_client.time_and_price(code)

        self._bars_update_dt = now

    @property
    def subscribed_assets(self):
        return self._subscribed_assets

    @property
    def positions(self):
        now = datetime.datetime.now()
        z_positions = protocol.Positions()
        for row in self._client.query_data(SHARES).iteritems():
            sid = row["证券代码"].values[0]
            available = row["可用数量"].values[0]
            z_position = protocol.Position(symbol(sid))
            z_position.amount = row["证券数量"].values[0]
            z_position.cost_basis = row["成本价"].values[0]
            z_position.last_sale_price = row["当前价"].values[0]
            z_position.last_sale_date = now
            z_positions[symbol(sid)] = z_position

        return z_positions

    @property
    def portfolio(self):
        z_portfolio = protocol.Portfolio()
        data = self._client.query_data(BALANCE)
        z_portfolio.capital_used = None  # TODO
        z_portfolio.starting_cash = None
        z_portfolio.portfolio_value = data[" 总资产"].values[0]
        z_portfolio.pnl = None
        z_portfolio.returns = None
        z_portfolio.cash = data["可用资金"].values[0]
        z_portfolio.start_date = None
        z_portfolio.positions = self.positions
        z_portfolio.positions_value = data["最新市值"].values[0]
        z_portfolio.position_exposure = z_portfolio.positions_value / (z_portfolio.positions_value + z_portfolio.cash)

        return z_portfolio

    @property
    def account(self):
        z_account = protocol.Account()
        z_account.settled_cash = self.portfolio.cash
        z_account.accrued_interest = None
        z_account.buying_power = self.portfolio.portfolio_value
        z_account.equity_with_loan = self.portfolio.portfolio_value
        z_account.total_positions_value = self.portfolio.positions_value
        z_account.total_positions_exposure = z_account.total_positions_value / (
            z_account.total_positions_value + z_account.settled_cash)

        return z_account

    @property
    def time_skew(self):
        return 0

    def order(self, asset, amount, limit_price, stop_price, style):
        code = asset.symbol

        if amount > 0:
            action = BUY
        else:
            action = SELL

        if isinstance(style, MarketOrder):
            order_type = FIVE_LEVEL_MARKET_ORDER
        elif isinstance(style, LimitOrder):
            order_type = LIMIT_CHARGE
        elif isinstance(style, StopOrder):
            raise Exception("stop order is not supported")
        elif isinstance(style, StopLimitOrder):
            raise Exception("stop limit order is not supported")

        price = limit_price or 0.0

        data, err = self._client.order(code, abs(amount), price, action, order_type)
        order_id = data["委托编号"].values[0]
        zp_order = self._get_or_create_zp_order(order_id)

        log.info("Placing order-{order_id}: "
                 "{action} {qty} {symbol} with {order_type}, price = {price}".format(
            order_id=order_id,
            action=action,
            qty=amount,
            symbol=code,
            order_type=order_type,
            price=price
        ))

        return zp_order

    @property
    def orders(self):
        self._update_orders()
        return self._orders

    def _get_or_create_zp_order(self, order_id, order=None):
        zp_order_id = self._tdx_to_zp_order_id(order_id)
        if zp_order_id in self._orders:
            return self._orders[zp_order_id]

        if not order:
            order = self._client.orders()[order_id]
        self._orders[zp_order_id] = self.tdx_order_to_zipline_order(order)
        return self._orders[zp_order_id]

    # amount 可正数可负数
    def _create_zp_order(self, order_id, asset, price, amount, order_type):
        zp_order_id = self._tdx_to_zp_order_id(order_id)
        if zp_order_id in self._orders:
            return self._orders[zp_order_id]

        dt = pd.to_datetime("now", utc=True)
        self._orders[zp_order_id] = ZPOrder(
            dt=dt,
            asset=asset,
            amount=amount,
            stop=None,
            limit=price if order_type is LIMIT_CHARGE else None,
            id=zp_order_id,
            broker_order_id=order_id
        )

        return self._orders[zp_order_id]

    def _tdx_to_zp_order_id(self, order_id):
        return "TDX-{date}-{account_id}-{order_id}".format(
            date=str(pd.to_datetime('today').date()),
            account_id=self._client.account_id,
            order_id=order_id
        )

    def tdx_order_to_zipline_order(self, order):
        if order.filled == 0:
            zp_status = ZP_ORDER_STATUS.OPEN
        else:
            zp_status = ZP_ORDER_STATUS.FILLED

        zp_order_id = self._tdx_to_zp_order_id(order.order_id)

        od = ZPOrder(
            dt=pd.to_datetime(order.dt),
            asset=symbol(order.symbol),
            amount=order.amount,
            filled=order.filled,
            stop=None,
            limit=order.price,  # TODO 市价单和限价单
            id=zp_order_id,
            broker_order_id=order.order_id
        )

        od.status = zp_status
        return od

    def _update_orders(self):
        ods = self._client.orders()
        for tdx_order_id, tdx_order in iteritems(ods):
            zp_order_id = self._tdx_to_zp_order_id(tdx_order_id)
            self._orders[zp_order_id] = self.tdx_order_to_zipline_order(tdx_order)

    @staticmethod
    def _tdx_transaction_to_zipline(transaction):
        return Transaction(
            asset=symbol(transaction.asset),
            amount=transaction.amount,
            dt=pd.to_datetime(transaction.dt),  # TODO timezone
            price=transaction.price,
            order_id=transaction.order_id,
            commission=transaction.commission,
        )

    @property
    def transactions(self):
        t = self._client.transactions()
        rt = {}
        print(t)
        for exec_id, transaction in t.items():
            if isinstance(transaction, list):  # handle rpc response for namedtuple object
                transaction = TdxTransaction(*transaction)
            rt[exec_id] = self._tdx_transaction_to_zipline(transaction)

        return rt

    def cancel_order(self, order_id):
        tdx_order_id = self.orders[order_id].broker_order_id
        broker_id = self._client.get_stock_type(self.orders[order_id].symbol)
        self._client.cancel(broker_id, tdx_order_id)

    def get_last_traded_dt(self, asset):
        self.subscribe_to_market_data(asset)
        self._update_bars()

        return self._bars[str(asset.symbol)].index[-1]

    def get_spot_value(self, assets, field, dt, data_frequency):
        symbol = str(assets.symbol)
        self.subscribe_to_market_data(assets)
        self._update_bars()

        bars = self._bars[symbol]
        last_event_time = bars.index[-1]

        minute_start = (last_event_time - pd.Timedelta('1 min')) \
            .time()
        minute_end = last_event_time.time()

        if bars.empty:
            return pd.NaT if field == 'last_traded' else np.NaN
        else:
            if field == 'price':
                return bars.price.iloc[-1]
            elif field == 'last_traded':
                return last_event_time or pd.NaT

            minute_df = bars.between_time(minute_start, minute_end,
                                          include_start=True, include_end=True)

            if minute_df.empty:
                return np.NaN
            else:
                if field == 'open':
                    return minute_df.price.iloc[0]
                elif field == 'close':
                    return minute_df.price.iloc[-1]
                elif field == 'high':
                    return minute_df.price.max()
                elif field == 'low':
                    return minute_df.price.min()
                elif field == 'volume':
                    return minute_df.vol.sum()

    def get_realtime_bars(self, assets, frequency):
        if frequency == '1m':
            resample_freq = ' Min'
        elif frequency == '1d':
            resample_freq = '24 H'
        else:
            raise ValueError("Invalid frequency specified: %s" % frequency)

        df = pd.DataFrame()

        for asset in assets:
            symbol = str(asset.symbol)
            self.subscribe_to_market_data(asset)
            self._update_bars()

            # TODO check resample
            trade_prices = self._bars[symbol]['price']
            trade_sizes = self._bars[symbol]['vol']
            ohlcv = trade_prices.resample(resample_freq).ohlc()
            ohlcv['volume'] = trade_sizes.resample(resample_freq).sum()

            ohlcv.columns = pd.MultiIndex.from_product([[asset, ],
                                                        ohlcv.columns])
            df = pd.concat([df, ohlcv], axis=1)

        return df
