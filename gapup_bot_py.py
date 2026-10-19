import os
import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import MarketMoversRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

KEY = os.environ["ALPACA_KEY"]
SECRET = os.environ["ALPACA_SECRET"]
ET = ZoneInfo("America/New_York")

MIN_GAP = 4.0
MIN_PRICE = 2.0
MIN_PM_VOL = 200_000
TOP_N = 10
OR_MIN = 15
VOL_MULT = 1.5
NOTIONAL = 1000

trading = TradingClient(KEY, SECRET, paper=True)
data = StockHistoricalDataClient(KEY, SECRET)
screener = ScreenerClient(KEY, SECRET)
stream = StockDataStream(KEY, SECRET, feed=DataFeed.IEX)

state = {}


def scan():
    gainers = screener.get_market_movers(MarketMoversRequest(top=50)).gainers
    cands = [g for g in gainers if g.percent_change >= MIN_GAP and g.price >= MIN_PRICE]
    if not cands:
        return []
    now = datetime.now(ET)
    start = now.replace(hour=4, minute=0, second=0, microsecond=0)
    bars = data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[c.symbol for c in cands],
        timeframe=TimeFrame.Minute, start=start, end=now, feed=DataFeed.IEX,
    )).data
    out = []
    for c in cands:
        vol = sum(b.volume for b in bars.get(c.symbol, []))
        if vol >= MIN_PM_VOL:
            out.append((c.symbol, c.percent_change, vol))
    out.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return out[:TOP_N]


def buy(sym):
    order = trading.submit_order(MarketOrderRequest(
        symbol=sym, notional=NOTIONAL, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
    ))
    print(f"LONG {sym} {order.id}")


async def on_bar(bar):
    s = state.get(bar.symbol)
    if not s or s["done"]:
        return
    t = bar.timestamp.astimezone(ET)
    open_dt = t.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = open_dt + timedelta(minutes=OR_MIN)
    if open_dt <= t < or_end:
        s["orh"] = max(s["orh"], bar.high)
        s["vols"].append(bar.volume)
        return
    if t < or_end or not s["vols"]:
        return
    avg = sum(s["vols"]) / len(s["vols"])
    if bar.close > s["orh"] and bar.volume >= avg * VOL_MULT:
        s["done"] = True
        await asyncio.to_thread(buy, bar.symbol)


async def wait_until(h, m):
    while datetime.now(ET).time() < time(h, m):
        await asyncio.sleep(15)


def main():
    asyncio.run(wait_until(9, 20))
    syms = scan()
    print("Scan:", syms)
    if not syms:
        return
    for sym, _, _ in syms:
        state[sym] = {"orh": 0.0, "vols": [], "done": False}
    stream.subscribe_bars(on_bar, *state)
    stream.run()


if __name__ == "__main__":
    main()
