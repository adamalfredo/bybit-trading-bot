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
| RISK_PCT | 1.0% | Rischio per trade su equity |
| DEFAULT_LEVERAGE | 5x | Leva cross |
| MAX_OPEN_POSITIONS | 5 | Posizioni simultanee per bot |
| SL_ATR_BUFFER | 0.3 | Buffer ATR oltre swing low/high |
| TRAIL_ATR_MULT | 2.0 | Moltiplicatore ATR per il trail |
| PARTIAL_TP_R | 2.0 | R multiplo per il partial TP (50%) |
| MIN_BODY_PCT | 40% | Corpo candela segnale minimo |
| MIN_VOL_RATIO | 1.5x | Volume candela vs media 20 |
| MAX_DIST_EMA | 3.0% | Distanza max close da EMA20 |
| MAX_SL_PCT | 8.0% | SL max accettabile |
| EMA_TOUCH_TOL | 1.2% | Tolleranza tocco EMA20 |
| MAX_DIST_EMA50_D | 20.0% | Distanza max da EMA50 daily |
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
4. RSI 30-65
5. close entro 3% sopra EMA20
6. body >= 40% del range
7. volume >= 1.5x media 20
8. SL = swing_low(3 barre) - 0.3xATR

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
4. RSI 35-65
5. close entro 3% sotto EMA20
6. body >= 40% del range
7. volume >= 1.5x media 20
8. SL = swing_high(3 barre) + 0.3xATR

**Regime gate (BTC_SHORT_REGIME_CHECK = True):**
- ATTIVO quando: BTC < EMA50 daily E EMA50 slope negativa
- IDLE automatico quando: BTC rimbalza sopra EMA50 o slope si inverte

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

*Ultimo aggiornamento: 2026-06-30*
