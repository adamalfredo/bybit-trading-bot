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

### 🔴 Da valutare dopo ~20 trade (discusso 22/05/2026)

> Logging diagnostico `[DIAG-SLOPE]` già attivo in produzione — raccoglie i dati necessari per decidere.

- **[PRIORITÀ 1 — difetto strutturale] Slope EMA20(4h) positiva**
  Attualmente il bot verifica che il prezzo sia sopra EMA20, ma non che EMA20 stia salendo.
  Se EMA20 è piatta o in calo, si compra in un trend che si sta indebolendo (pattern pre-breakdown).
  Fix: aggiungere `ema20[-2] > ema20[-5]` come condizione obbligatoria.
  Verifica: guardare i log `[DIAG-SLOPE]` — se le perdite coincidono sempre con `WARN piatta/discesa`, implementare.

- **[PRIORITÀ 2] Struttura pre-pullback: 2+ candele sopra EMA20 prima del ritocco**
  Se una coin oscilla attorno a EMA20 da 5-10 barre, qualsiasi candela verde qualifica come segnale.
  Un pullback vero presuppone che le ultime 2-3 candele chiuse fossero *chiaramente sopra* EMA20.
  Fix: `min(close[-3], close[-4], close[-5]) > ema20[-3]` come controllo aggiuntivo.

- **[PRIORITÀ 3] RSI minimo: riportare a 35-38 (era 30)**
  Il parametro attuale è `RSI_MIN_4H = 30`. RSI 30 = oversold, possibile coltello che cade.
  La strategia è pensata per pullback sani (RSI 38-45 = zona ideale).
  Fix: `RSI_MIN_4H = 38`. Verificare prima sui dati quante entry sarebbero state escluse.

- **[PRIORITÀ 4] Profondità del pullback (EMA_TOUCH_TOL)**
  `EMA_TOUCH_TOL = 0.012` permette al low di essere 1.2% *sopra* EMA20 e qualificare.
  Il pullback ideale tocca o sfora EMA20 verso il basso. Abbassare la tolleranza a 0.005 (0.5%).
  Attenzione: filtro molto restrittivo, potrebbe ridurre frequenza segnali significativamente.

### Esistenti

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
