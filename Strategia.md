# 🧮 Strategia

## 🧭 Universe e filtri

- Lista asset dinamica: 
    - top per volume spot USDT, filtrati per futures linear (turnover minimo), esclusi stablecoin/blacklist.
    - Split: meno volatili vs volatili.
- Trend filter multi‑TF:
    - LONG: ok se 4h uptrend oppure 1h uptrend e 4h non fortemente down. Block se 4h+1h entrambi down.
    - SHORT: speculare (downtrend permesso; block se 4h+1h entrambi up).
- Timeframe segnali: 60m.

## 🎯 Segnali di ingresso (serve conferma multipla)

- Asset volatili:
    - LONG: combinazioni tra MACD cross up, MACD>signal+ADX, breakout BB, RSI>55. Richieste ≥3 condizioni.
    - SHORT: MACD cross down, MACD<signal+ADX, breakdown BB, RSI<45. Richieste ≥3 condizioni.
- Asset meno volatili:
    - LONG: EMA20>EMA50, MACD bullish, Trend EMA+RSI; richiede ≥3.
    - SHORT: EMA20<EMA50, MACD bearish, Trend EMA+RSI; richiede ≥3.

## 💰 Sizing e budget

- Leva di conto: DEFAULT_LEVERAGE (15). Tetto notional per trade: min(TARGET_NOTIONAL_PER_TRADE, budget di gruppo, margine disponibile = saldo USDT × leva × MARGIN_USE_PCT).
- “Strength” del segnale pondera la size. Riduzione automatica se ATR/Prezzo alto. Rispetto di minQty/minNotional Bybit; quantità allineata a qtyStep.

## 🚀 Apertura posizione

- Ordine Market (Buy LONG / Sell SHORT) su positionIdx (1 LONG, 2 SHORT).
- Subito dopo l’ingresso, il bot piazza 3 protezioni lato exchange:
1. TP Limit reduceOnly PostOnly:
    - LONG: price_now + ATR × TP_FACTOR (con adattamento dinamico e clamp tra TP_MIN e TP_MAX).
    - SHORT: price_now − ATR × TP_FACTOR.
2. SL Conditional (Stop‑Market, reduceOnly, closeOnTrigger=True, triggerBy=MarkPrice; fallback LastPrice se serve):
    - LONG: price_now − ATR × SL_FACTOR (clamp tra SL_MIN e SL_MAX).
    - SHORT: price_now + ATR × SL_FACTOR.
3. Trailing Stop nativo Bybit via /v5/position/trading-stop:
- Distanza = ATR × 1.5. Nessun worker interno: è gestito interamente da Bybit.

## 🛡️ Breakeven lock (non perdere dopo X% di profitto)

- Worker dedicato attivo (uno per LONG, uno per SHORT).
- Soglia: BREAKEVEN_LOCK_PCT = 0.01 (≈ +10% PnL con leva 10x).
- Azione:
    - LONG: imposta stopLoss di posizione a ≈ entry × (1 + 0.0005), slTriggerBy=MarkPrice.
    - SHORT: imposta stopLoss a ≈ entry × (1 − 0.0005), slTriggerBy=MarkPrice.
- Dopo il lock, una chiusura non può andare in rosso.

## 🏁 Uscite

- Lato exchange: TP, SL o Trailing chiudono automaticamente (possibile che non arrivi notifica Telegram perché la chiusura avviene su Bybit).
- Segnale di exit strategico: chiusura a Market (reduceOnly) e cancellazione ordini residui (TP/SL) con cancel_all_orders.
- Cooldown per ri‑ingressi sullo stesso simbolo: 60 minuti.

## 🧹 Pulizia e coerenza ordini

- Se una posizione risulta chiusa (qty < minQty), il bot rimuove la posizione interna e chiama cancel_all_orders per evitare “conditional” orfani.
- Tutte le chiusure a Market usano reduceOnly=True.

## 📈 Cosa appare su Bybit per ogni posizione

- Positions: la posizione aperta (con eventuale Stop Loss di posizione quando scatta il BE‑lock).
- Limit & Market Orders: il TP Limit reduceOnly.
- Conditional: lo SL Stop‑Market reduceOnly.
- Trailing Stop: la voce di trailing con “Retracement” (distanza) e stato.

## ⚙️ Parametri chiave (modificabili)

- ATR_WINDOW=14; TP_FACTOR/SL_FACTOR con clamp; trailing distance = ATR×1.5.
- BREAKEVEN_LOCK_PCT=0.01 (10% PnL) e buffer ±0.05%.
- Budget split: 60% meno volatili / 40% volatili (sia LONG che SHORT).

## ✅ In sintesi: 

- ingresso multi‑segnale su 60m con filtro trend 4h/1h; 
- size dinamica e guardie minimi Bybit; 
- subito TP, SL e Trailing nativo; 
- BE‑lock al +1% di prezzo evita perdite dopo che sei in profitto; 
- cleanup automatico degli ordini residui.