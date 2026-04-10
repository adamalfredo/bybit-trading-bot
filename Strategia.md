# 🧮 Strategia (aggiornata aprile 2026)

Due bot indipendenti su Railway, stesso conto Bybit, futures linear USDT-margined.

---

## 🤖 Bot LONG (`main-long.py`)

### Universe e filtri
- Asset dinamici: top per volume USDT, split in **stabili (≤5% 24h)** e **volatili (>5% 24h)**.
- Filtro trend:
  - BTC favorevole (4h uptrend): basta il 4h in uptrend.
  - BTC sfavorevole: richiede 4h **e** 1h entrambi in uptrend.
- LARGE-CAP-GATE: massimo 1 posizione tra BTC/ETH/BNB/SOL aperta contemporaneamente.
- VOLATILE-GATE: massimo 2 asset volatili (>5% 24h) aperti contemporaneamente.
- Funding filter: skip se funding > +0.05% (longs sovraccaricati).

### Segnali di ingresso
- Timeframe: 60m (candele chiuse, no repaint).
- Indicatori: EMA20/50 cross, MACD cross/stato, RSI>55, OI change.
- Confluenza minima: **≥2** su 4 (alzata a 3 se BTC non favorevole o drawdown attivo).
- ADX ≥ 24 (stabile) / ≥ 27 (volatile); rilassato di `ADX_RELAX_EVENT` se c'è un evento fresco.
- Evento fresco obbligatorio se BTC non favorevole (cross EMA, MACD o RSI break).
- Filtro volume: volume ultima candela ≥ 60% della media 20 periodi.
- Override pullback BULL: ingresso su RSI 1h < 32 con 4h ancora up, senza aspettare EMA/MACD.
- Filtro estensione: no LONG se prezzo > EMA20 + 1.5×ATR (non inseguire).

### Sizing
- Rischio per trade: **0.75% dell'equity**.
- SL basato su **ATR 4h × 2.0** (più stabile del 1h, meno noise-stop).
- `r_dist = ATR_4h × SL_ATR_MULT` → qty = riskUSDT / r_dist.
- Tetto: min(notional_calcolato, budget_gruppo, saldo × leva × 35%).
- Leva: **10×**.

### Gestione posizione
- **TP1 parziale**: 50% della qty a 2.5R (regime-aware: 1.8R se BTC non favorevole).
- **Trailing stop** (worker interno): attivato dopo 0.5R di profitto, distanza = ATR × 1.3.
- **Ratchet floor**: man mano che il ROI sale, il SL viene alzato su soglie fisse (lock profitti crescenti).
- **BE lock**: attivato quando il prezzo sale di 2.5% dall'ingresso, SL portato a entry + buffer.
- **PNL lock**: se unrealizzato ≥ 3.2 USDT, SL alzato a garantire almeno 3.0 USDT netti.
- **DUMP-BE**: se BTC sta dumpando forte (15m), le posizioni LONG in profitto vengono portate a BE.
- Cooldown ri-ingresso: 1h post-win, 4h post-loss singola.

### MAX_OPEN_POSITIONS = 4

---

## 🤖 Bot SHORT (`main-short.py`)

### Filosofia
Operativo **solo in mercato bear confermato**. Non apre SHORT in trend rialzista BTC.

### Universe e filtri
- Stesse liste asset del bot LONG (aggiornate indipendentemente).
- **BEAR-GATE**: entry consentita solo se BTC 4h **e** BTC daily entrambi in downtrend.
- **UPTREND-GATE**: blocco totale se BTC 4h in uptrend (EMA200 4h crescente + price > EMA200).
- **PUMP-GATE**: skip entry se BTC +1.5% in 30 minuti (protezione rally improvvisi).
- **BTC 24h override**: anche se 4h è down, se BTC 24h > +0.5% il contesto è considerato sfavorevole.
- OI filter **obbligatorio**: skip se OI change < 0 (pressione in calo, nessun short genuino).
- Funding filter: skip se funding < -0.05% (shorts sovraccaricati = pressione rialzista).

### Segnali di ingresso
- Timeframe: 60m (stesso del LONG, candele chiuse).
- Indicatori: EMA20/50 bear cross, MACD bearish cross/stato, RSI<45, OI change.
- Confluenza minima: **≥2** su 4 (alzata a 3 se BTC non favorevole o drawdown attivo).
- ADX ≥ **25** (stabile) / ≥ 27 (volatile); stessa logica rilassamento con evento fresco.
- Override rimbalzo BEAR: ingresso su RSI 1h > 68 con 4h ancora down.
- Filtro estensione: no SHORT se prezzo < EMA20 - 1.5×ATR (non inseguire il ribasso).

### Sizing
- Identico al LONG: 0.75% equity, ATR **4h** × 2.0 per r_dist.

### Gestione posizione
- **TP1 parziale**: 50% qty a 2.5R (1.8R se BTC non favorevole).
- **Trailing stop** (worker): attivato dopo 0.5R, distanza ATR × 1.3.
- **Ratchet floor** e **BE lock** speculari al LONG.
- **PUMP-BE**: se BTC pompa forte, le posizioni SHORT in profitto vengono portate a BE.
- Cooldown: **12h** post-loss se ≥2 loss consecutive, **4h** post-loss singola, **1h** post-win.
- Loss-guard: se ≥2 loss consecutive e prezzo sopra EMA50 → attendi ulteriore conferma.

### MAX_OPEN_POSITIONS = 2

---

## ⚙️ Parametri chiave comuni

| Parametro | LONG | SHORT |
|---|---|---|
| Leva | 10× | 10× |
| Rischio/trade | 0.75% equity | 0.75% equity |
| ATR SL | 4h × 2.0 | 4h × 2.0 |
| TP1 | 50% @ 2.5R | 50% @ 2.5R |
| Trailing attivazione | 0.5R | 0.5R |
| Trailing distanza | ATR × 1.3 | ATR × 1.3 |
| BE lock soglia | prezzo +2.5% | prezzo -2.5% |
| MAX_LOSS_CAP | 3% | 3% |
| Margin use | 35% saldo | 35% saldo |
| Segnali TF | 60m | 60m |
| Min confluence | 2/4 | 2/4 |

---

## 🏁 Uscite (entrambi i bot)

- **Exchange**: TP1 Limit reduceOnly, SL Stop-Market conditional, Trailing stop nativo Bybit.
- **Segnale strategico**: chiusura Market + cancel_all_orders ordini residui.
- **PnL notifica Telegram**: calcolato netto (fee round-trip 0.12% detratte), emoji 📈/📉.
