import requests

url = 'https://api.bybit.com/v5/market/tickers'
r = requests.get(url, params={'category': 'linear'})
tickers = r.json()['result']['list']

# Solo USDT perpetuals con volume > 1M USDT 24h
filtered = [t for t in tickers if t['symbol'].endswith('USDT') and float(t.get('turnover24h', 0)) > 1_000_000]

sorted_gain = sorted(filtered, key=lambda x: float(x.get('price24hPcnt', 0)), reverse=True)
sorted_loss  = sorted(filtered, key=lambda x: float(x.get('price24hPcnt', 0)))

# Trade aperti ultimamente dai bot (ultimi 7 giorni dai PnL)
bot_long_symbols  = {'ENAUSDT','ARBUSDT','ETHUSDT','TAOUSDT','MONUSDT','PENGUUSDT','1000PEPEUSDT',
                     'ONTUSDT','VVVUSDT','LITUSDT','COMPUSDT','LPTUSDT','CHILLGUYUSDT','ZENUSDT',
                     'LABUSDT','AKEUSDT','ZROUSDT','ARBUSDT','WLDUSDT','BTCUSDT','DOTUSDT',
                     'NEARUSDT','AVAXUSDT','TAOUSDT','1000BONKUSDT','FARTCOINUSDT','SUIUSDT',
                     'WLFIUSDT','XRPUSDT','TRIAUSDT','PYTHUSDT','ONGUSDT','HYPEUSDT','SOLUSDT',
                     'REDUSDT','ZECUSDT','TRXUSDT','DOGEUSDT','XAUTUSDT','STOUSDT'}
bot_short_symbols = {'ARBUSDT','ETHUSDT','TAOUSDT','MONUSDT','PENGUUSDT','1000PEPEUSDT',
                     'ONTUSDT','VVVUSDT','LITUSDT','ZENUSDT','LABUSDT','ZROUSDT','BTCUSDT'}

print('=== TOP 25 GAINERS 24h (mercato completo) ===')
for i, t in enumerate(sorted_gain[:25], 1):
    pct  = float(t['price24hPcnt']) * 100
    sym  = t['symbol']
    flag = ' << BOT LONG ha tradato' if sym in bot_long_symbols else ''
    print(f"  {i:>2}. {sym:<22} {pct:+.2f}%   vol={float(t['turnover24h'])/1e6:.0f}M{flag}")

print()
print('=== TOP 25 LOSERS 24h (mercato completo) ===')
for i, t in enumerate(sorted_loss[:25], 1):
    pct  = float(t['price24hPcnt']) * 100
    sym  = t['symbol']
    flag = ' << BOT SHORT ha tradato' if sym in bot_short_symbols else ''
    print(f"  {i:>2}. {sym:<22} {pct:+.2f}%   vol={float(t['turnover24h'])/1e6:.0f}M{flag}")

print()
print('=== COIN IN TOP GAINERS che il bot LONG NON ha preso ===')
missed = [(t['symbol'], float(t['price24hPcnt'])*100) for t in sorted_gain[:30] if t['symbol'] not in bot_long_symbols]
for sym, pct in missed[:15]:
    print(f"  {sym:<22} {pct:+.2f}%")
