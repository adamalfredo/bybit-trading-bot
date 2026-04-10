# 🗺️ Roadmap — Miglioramenti futuri

> Dopo un periodo di monitoraggio (almeno 2-3 settimane di operatività reale), valutare i seguenti punti in ordine di priorità.

---

## 📊 1. Analisi dati storici

- **Win rate reale per simbolo**: calcolare WR, R medio vinto/perso e expectancy dai file `_trade_log` e CSV.
- **Heatmap oraria**: capire a che ora del giorno gli ingressi portano più profitto (evitare sessioni a bassa liquidità).
- **Performance BEAR-GATE**: verificare quante entry SHORT vengono bloccate e se il WR è migliorato rispetto al periodo pre-gate.
- **Confronto SL 1h vs 4h**: dopo il cambio ad ATR 4h, misurare se il numero di noise-stop è calato senza penalizzare troppo il rischio per trade.

---

## 🔧 2. Ottimizzazione parametri (solo su evidenza dati)

- **TP1_R**: se molti trade raggiungono 2R ma non 2.5R, valutare 2.0R su stabili e 2.5R solo su volatili.
- **TRAIL_ATR_MULT**: testare 1.5 vs 1.3 — trailing più largo riduce uscite precoci ma lascia più profitto a mercato.
- **MAX_VOLATILE_LONG**: se si osserva scarsa diversificazione settoriale, ridurre a 1.
- **MIN_CONFLUENCE SHORT**: se il WR SHORT post-BEAR-GATE è già alto, valutare se si può scendere a 2 anche senza BTC favorevole.
- **COOLDOWN SHORT**: verificare se 12h post-doppia-loss è troppo conservativo o necessario; aggiustare in base ai dati.

---

## 🛡️ 3. Protezione del capitale

- **Drawdown giornaliero hard-stop**: se la perdita del giorno supera X% dell'equity, sospendere nuovi ingressi fino al giorno dopo.
- **Equity curve filter**: se l'equity è sotto la media mobile a 7 giorni, passare a RISK_PCT dimezzato o bloccare nuovi SHORT.
- **Correlazione tra posizioni aperte**: se 3 delle 4 posizioni LONG sono sullo stesso settore (DeFi, L1, meme), ridurre la size dell'ultima entry.

---

## 🧠 4. Qualità dei segnali

- **Filtro eventi macro**: bloccare nuovi ingressi nelle 2h prima/dopo FOMC, CPI e simili — attualmente nessun filtro calendario.
- **Divergenza RSI/prezzo**: segnale aggiuntivo (prezzo fa nuovo massimo ma RSI no → debolezza potenziale).
- **Volume profile su 3 candele**: verificare che il volume sia in espansione sulle ultime 3 candele, non solo l'ultima.
- **OI 24h vs media settimanale**: attualmente si usa solo il delta OI a breve; confronto con la media settimanale darebbe più contesto.

---

## 📱 5. Monitoraggio e reportistica

- **Report giornaliero Telegram**: sintesi equity, PnL del giorno, numero trade aperti/chiusi, simboli attivi.
- **Alert soglia equity**: notifica se l'equity scende sotto una soglia configurabile (es. 45 USDT).
- **Log posizioni attive periodico**: ogni 4h, messaggio con PnL unrealizzato e distanza dal SL per ogni posizione aperta.

---

## ⚙️ 6. Infrastruttura

- ✅ **Sincronizzazione ordini a restart**: al riavvio su Railway, recuperare anche gli ordini TP/SL attivi — helper `_sync_tp_order_long/short` implementati in entrambi i bot.
- **Separazione account LONG/SHORT**: valutare due sub-account distinti per evitare interferenze tra i bot sullo stesso conto.
- **Ambiente di staging**: bot in paper trading (Bybit testnet) per testare modifiche prima del deploy in produzione.
