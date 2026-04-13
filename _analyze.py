import os, sys, requests
import pandas as pd
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

def fetch_klines(symbol, interval, limit=200):
    url = 'https://api.bybit.com/v5/market/kline'
    r = requests.get(url, params={'category':'linear','symbol':symbol,'interval':interval,'limit':limit})
    data = r.json()['result']['list']
    df = pd.DataFrame(data, columns=['ts','Open','High','Low','Close','Volume','Turnover'])
    for c in ['Open','High','Low','Close','Volume']:
        df[c] = df[c].astype(float)
    df['ts'] = pd.to_numeric(df['ts'])
    df = df.sort_values('ts').reset_index(drop=True)
    return df

def get_oi_change(symbol):
    try:
        url = 'https://api.bybit.com/v5/market/open-interest'
        r = requests.get(url, params={'category':'linear','symbol':symbol,'intervalTime':'1h','limit':2})
        data = r.json()['result']['list']
        if len(data) >= 2:
            oi_new = float(data[0]['openInterest'])
            oi_old = float(data[1]['openInterest'])
            return (oi_new - oi_old) / oi_old * 100
    except:
        pass
    return None

sym = sys.argv[1] if len(sys.argv) > 1 else 'WLDUSDT'

oi_chg = get_oi_change(sym)

for tf_label, tf in [('1h','60'), ('4h','240'), ('1d','D')]:
    df = fetch_klines(sym, tf)
    close = df['Close']
    high  = df['High']
    low   = df['Low']

    ema20  = EMAIndicator(close, 20).ema_indicator().iloc[-1]
    ema50  = EMAIndicator(close, 50).ema_indicator().iloc[-1]
    ema100 = EMAIndicator(close, 100).ema_indicator().iloc[-1]
    ema200 = EMAIndicator(close, 200).ema_indicator().iloc[-1]
    rsi    = RSIIndicator(close, 14).rsi().iloc[-1]
    macd_o = MACD(close)
    macd_h = macd_o.macd_diff().iloc[-1]
    adx_o  = ADXIndicator(high, low, close, 14)
    adx    = adx_o.adx().iloc[-1]
    adx_p  = adx_o.adx_pos().iloc[-1]
    adx_n  = adx_o.adx_neg().iloc[-1]
    bb     = BollingerBands(close, 20, 2)
    bb_pct = bb.bollinger_pband().iloc[-1]
    atr    = AverageTrueRange(high, low, close, 14).average_true_range().iloc[-1]
    price  = close.iloc[-1]

    chg1  = (price - close.iloc[-2])  / close.iloc[-2]  * 100
    chg5  = (price - close.iloc[-6])  / close.iloc[-6]  * 100
    chg20 = (price - close.iloc[-21]) / close.iloc[-21] * 100

    if ema20 > ema50 > ema100:
        trend = 'BULLISH'
    elif ema20 < ema50 < ema100:
        trend = 'BEARISH'
    else:
        trend = 'MISTO'

    macd_dir = 'BULL' if macd_h > 0 else 'BEAR'

    if rsi > 70:
        rsi_state = 'IPERCOMPRATO'
    elif rsi < 30:
        rsi_state = 'IPERVENDUTO'
    else:
        rsi_state = f'{rsi:.1f}'

    adx_label = 'Trend FORTE' if adx > 25 else 'Trend debole'
    di_dir = '+DI domina (forza rialzista)' if adx_p > adx_n else '-DI domina (forza ribassista)'

    print(f'\n{"="*45}')
    print(f'  {sym}  [{tf_label}]  —  Prezzo: {price:.4f}')
    print(f'{"="*45}')
    print(f'  Var candela prec: {chg1:+.2f}%')
    print(f'  Var 5 candele:    {chg5:+.2f}%')
    print(f'  Var 20 candele:   {chg20:+.2f}%')
    print(f'  ATR:              {atr:.4f}  ({atr/price*100:.1f}% del prezzo)')
    print(f'  EMA trend:        {trend}  (20={ema20:.4f}  50={ema50:.4f}  100={ema100:.4f})')
    print(f'  Prezzo vs EMA200: {(price/ema200-1)*100:+.1f}%  (EMA200={ema200:.4f})')
    print(f'  RSI 14:           {rsi_state}')
    print(f'  MACD hist:        {macd_h:+.6f}  -> {macd_dir}')
    print(f'  ADX:              {adx:.1f}  ({adx_label})  —  {di_dir}')
    print(f'  BB %B:            {bb_pct:.2f}  (0=inf  0.5=mid  1=sup)')

if oi_chg is not None:
    print(f'\n  OI 1h change:     {oi_chg:+.2f}%')
