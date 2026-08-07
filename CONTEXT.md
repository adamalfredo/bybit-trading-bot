# CONTEXT - Bybit Trading Bot

> Memoria permanente del progetto. Aggiornare ad ogni cambiamento architetturale, parametrico o di strategia.

---

## 1. Infrastruttura

| Voce | Dettaglio |
|---|---|
| Exchange | Bybit UNIFIED, hedge mode, linear perpetuals USDT-settled |
| Leva | 5x |
| Timeframe segnale | 4h (trend filter: daily) |
| Platform deploy | Railway (2 servizi separati) |
| Repo GitHub | adamalfredo/bybit-trading-bot |
| Branch | main |
| Python | 3.12 |
| Railway LONG | Start command: python main-pullback.py |
| Railway SHORT | Start command: python main-short-pullback.py |

---

## 2. Parametri comuni (identici per LONG e SHORT)

| Parametro | Valore | Descrizione |
|---|---|---|
| RISK_PCT | 0.5% | Rischio per trade su equity |
| DEFAULT_LEVERAGE | 5x | Leva cross |
| MAX_OPEN_POSITIONS | 3 | Posizioni simultanee per bot |
| MAX_TOTAL_OPEN_RISK_PCT | 2.5% | Rischio totale aperto massimo |
| SL_ATR_BUFFER | 0.1 | Buffer ATR oltre swing low/high |
| TRAIL_ATR_MULT | 2.0 | Moltiplicatore ATR per il trail |
| PARTIAL_TP_R | 2.0 | R multiplo per il partial TP (20%) |
| MIN_BODY_PCT | 25% | Corpo candela segnale minimo |
| MIN_VOL_RATIO | 0.8x | Volume candela vs media 20 |
| MAX_DIST_EMA | 3.0% | Distanza max close da EMA20 |
| MAX_SL_PCT | 5.0% | SL max accettabile |
| EMA_TOUCH_TOL | 1.2% | Tolleranza tocco EMA20 |
| MAX_DIST_EMA50_D | 20.0% | Distanza max da EMA50 daily |
| ADAPTIVE_BASE_MAX_PCT | 16.0% | Larghezza massima base ammessa (allentata il 07/08) |
| BREAK_CONFIRM_ATR_TOL | 0.35 | Tolleranza conferma breakout in multipli ATR |
| BREAK_CONFIRM_PCT_TOL | 0.22% | Tolleranza conferma breakout in percentuale prezzo |
| TIME_STOP_DAYS | 10 | Giorni max in posizione |
| TIME_STOP_MIN_LEV | 10% | P&L lev minimo dopo TIME_STOP_DAYS |
| CIRCUIT_BREAKER_PCT | 3.0% | Drawdown giornaliero max |
| SCAN_INTERVAL_SEC | 1800 | Intervallo scan (30 min) |

---

## 3. Bot LONG - main-pullback.py

**Strategia:** EMA20(4h) Pullback in uptrend daily

**Filtro daily (is_daily_uptrend):**
- close > EMA50 daily
- EMA50 slope positiva (oggi > 5gg fa)
- Distanza da EMA50 <= 20%

**Segnale 4h (check_entry_signal):**
1. low <= EMA20 * 1.012  (tocco al supporto)
2. close > EMA20         (rimbalzo confermato)
3. close > open          (candela verde)
4. RSI 30-70
5. close entro 3% sopra EMA20
6. body >= 25% del range
7. volume >= 0.8x media 20
8. SL = swing low base - 0.1xATR (con max SL 5%)

**Regime gate:** BTC_BULL_CHECK = False (disabilitato da giugno 2026 - BTC sotto EMA50 per mesi dopo ATH 100k; filtro daily per singola coin e sufficiente)

**Log diagnostico:** [DIAG-SLOPE] - slope EMA20(4h) loggata ma non ancora filtrante

---

## 4. Bot SHORT - main-short-pullback.py

**Strategia:** EMA20(4h) Bounce Rejection in downtrend daily

**Filtro daily (is_daily_downtrend):**
- close < EMA50 daily
- EMA50 slope negativa (oggi < 5gg fa)
- Distanza da EMA50 <= 20% (evita coin gia in freefall)

**Segnale 4h (check_short_signal):**
1. high >= EMA20 * 0.988  (bounce tocca la resistenza)
2. close < EMA20          (rifiuto confermato)
3. close < open           (candela rossa)
4. RSI 30-70
5. close entro 3% sotto EMA20
6. body >= 25% del range
7. volume >= 0.8x media 20
8. SL = swing high base + 0.1xATR (con max SL 5%)

**Regime gate (BTC_SHORT_REGIME_CHECK = True):**
- ATTIVO quando: BTC < EMA50 daily E EMA50 slope negativa
- IDLE automatico quando: BTC rimbalza sopra EMA50 o slope si inverte
- Soglia score short: BTC_SHORT_REGIME_SCORE_MIN = 0.20 (ridotta il 07/08)

**Trailing:** low_water + 2xATR (SL scende man mano che il prezzo cala)
**Ratchet floor SHORT:** entry * (1 - floor_lev/100/lev) - SL si abbassa verso profit

---

## 5. Exit - Ratchet Table (uguale per LONG e SHORT)

| Trigger (P&L lev) | Floor garantito (lev) |
|---|---|
| >=15% | +7% |
| >=25% | +15% |
| >=40% | +25% |
| >=60% | +40% |
| >=80% | +60% |
| >=100% | +80% |
| >=125% | +100% |
| >=150% | +120% |
| >=175% | +148% |
| >=200% | +173% |
| >=250% | +223% |
| >=300% | +273% |
| >=400% | +370% |
| >=500% | +465% |

---

## 6. Storico decisioni architetturali

| Data | Decisione | Motivazione |
|---|---|---|
| 2026-08-07 | Allentati i filtri setup (base e breakout) su LONG/SHORT | Aumentare il numero di ingressi dopo fase di eccessivo under-trading |
| 2026-08-07 | Ridotta soglia regime short (score min 0.55 -> 0.20) | Ridurre i periodi di idle totale del bot SHORT |
| 2026-07-30 | Rischio per trade ridotto a 0.5% e max posizioni a 3 | Ridurre l’impatto delle perdite e contenere il drawdown di singolo trade |
| 2026-07-30 | Max total open risk ridotto a 2.5% | Evitare concentrazione eccessiva di rischio su più posizioni aperte |
| 2026-07-30 | SL massimo accettabile ridotto a 5% e buffer ATR a 0.1 | Limitare l’ampiezza delle perdite e rendere gli stop più reattivi |
| 2026-06-30 | Creato main-short-pullback.py | Mercato bearish (BTC -40% da ATH), opportunita short sistematiche; strategia speculare al long |
| 2026-06-30 | Eliminato main-short.py (vecchio) | Troppo complesso (~200 parametri), mai validato con backtest, rimosso da Railway a maggio |
| 2026-06-16 | BTC_BULL_CHECK = False | EMA50 daily BTC ancora a 73k dopo calo da 100k; filtro daily per singola coin e sufficiente |
| 2026-06-03 | ATR trail dal massimo (TRAIL_ATR_MULT=2.0) | Sostituisce trailing nativo Bybit; high_water - 2xATR come tiebreaker sopra ratchet floor |
| 2026-05-22 | Fix partial TP restart | Sync al restart non ri-eseguiva partial TP se prezzo gia sopra 2R |
| 2026-05-22 | Log DIAG-SLOPE EMA20(4h) | Pre-filtro osservativo slope; da convertire in filtro dopo 20+ trade |

---

## 7. Performance storico

| Periodo | Trade | WR | PF | Note |
|---|---|---|---|---|
| 2026-05-20/22 | 5 trade | 82% | 8.34 | Migliore periodo, mercato bull |
| 2026-06-16/30 | 2 trade | 50% | 0.18 | TIAUSDT -0.50, HYPEUSDT +0.09 |
| Totale live | ~7 trade | ~70% | ~2.5 | Stima approssimativa |

**Equity tracking:**
- 2026-05-22: 43.87 USDT (picco)
- 2026-06-30: 40.81 USDT (attuale)

---

## 8. File del progetto

| File | Scopo | Stato |
|---|---|---|
| main-pullback.py | Bot LONG in produzione | Live su Railway |
| main-short-pullback.py | Bot SHORT in produzione | Live su Railway (dal 30/06/2026) |
| acktest_pullback.py | Engine backtest EMA20-Pullback 4h | Locale |
| acktest_walkforward.py | Walk-forward validation | Locale |
| ybit_mcp_server.py | MCP server per VS Code Copilot | Locale + Railway |
| 
equirements.txt | Dipendenze Python | Repo |
| Roadmap.md | Migliorie pendenti segnali | Repo |
| Strategia.md | Note strategia originale | Archivio |
| CONTEXT.md | Questo file - memoria permanente | Repo |

---

## 9. Come leggere i log Railway

**Bot LONG:**
`
[SCAN] X coin in uptrend daily | Y ingressi | posizioni: Z
[DIAG-SLOPE] SYMBOL: EMA20_slope=+X.XXX% (OK salita / WARN piatta)
[SIGNAL] SYMBOL LONG | EMA20: X | dist: +X% | RSI: X | SL: -X%
[CLOSE] SYMBOL chiusa ~+X.X%
[TRAIL] SYMBOL Ratchet: P&L=+X% -> floor +X% lev SL->X
`

**Bot SHORT:**
`
[REGIME] BTC=X EMA50d=X (X%) slope=X%/5gg | SHORT=ON/OFF
[SCAN] X coin in downtrend daily | Y ingressi | posizioni: Z
[SIGNAL] SYMBOL SHORT | EMA20: X | dist: -X% | RSI: X | SL: +X%
[CLOSE] SYMBOL SHORT chiusa ~+X.X%
[TRAIL] SYMBOL Ratchet SHORT: P&L=+X% -> floor +X% lev SL->X
`

---

## 10. Roadmap segnali (LONG - da valutare dopo 20+ trade)

1. [PRIORITA 1] Slope EMA20(4h) positiva - log DIAG-SLOPE gia attivo, convertire in filtro
2. [PRIORITA 2] Struttura pre-pullback: 2+ candele sopra EMA20 prima del ritocco
3. [PRIORITA 3] RSI min da 30 a 38 (evita coltelli)
4. [PRIORITA 4] EMA_TOUCH_TOL da 1.2% a 0.5% (pullback piu preciso, attenzione: filtra molto)

---

## 11. Protocollo Go/No-Go (anti-tuning infinito)

Regola base: ogni modifica entra in una finestra di valutazione fissa, senza ulteriori ritocchi durante il test.

**Finestra di test:**
1. Durata minima: 14 giorni
2. Campione minimo: 30 trade chiusi complessivi (LONG+SHORT)
3. Se non si raggiungono 30 trade: estendere finestra fino a 30 trade

**KPI di promozione (Go):**
1. Profit Factor >= 1.15
2. Expectancy > 0
3. Max drawdown contenuto e non peggiore del ciclo precedente
4. Avg loss non superiore a 2.2x avg win

**KPI di bocciatura (No-Go):**
1. Profit Factor < 1.00 su campione valido
2. Expectancy <= 0
3. Drawdown peggiorativo rispetto al ciclo precedente

**Regole operative:**
1. Nessun cambio parametri durante la finestra in corso
2. Una sola modifica strutturale per ciclo (non pacchetti multipli)
3. Se 2 cicli consecutivi sono No-Go: stop tuning e pivot strategia

**Pivot strategy (se No-Go x2):**
1. Mettere bot in paper/sandbox
2. Rieseguire validazione walk-forward + montecarlo su set aggiornato
3. Riattivare live solo con KPI minimi passati in test

---

## 12. Report settimanale standard

File template ufficiale:
- WEEKLY_GO_NO_GO_REPORT_TEMPLATE.md

Regola operativa:
1. Compilare il report una volta a settimana sempre sullo stesso orizzonte temporale.
2. Non decidere modifiche senza report compilato.
3. Se esito NO-GO, pianificare una sola modifica per il ciclo successivo.
4. Se due NO-GO consecutivi, applicare pivot strategy senza eccezioni.

---

*Ultimo aggiornamento: 2026-08-07*
