# CONTEXT — Bybit Trading Bot

> Questo file è la memoria permanente del progetto.
> Aggiornalo ogni volta che cambi parametri, fai un nuovo backtest, o prendi decisioni architetturali importanti.
> È leggibile da qualsiasi dispositivo, account, e da qualunque AI assistant.

---

## 1. Infrastruttura

| Voce | Dettaglio |
|---|---|
| Exchange | Bybit UNIFIED, hedge mode, linear perpetuals USDT-settled |
| Leva | 10x |
| Timeframe principale | 4h |
| Platform deploy | Railway |
| Repo GitHub | `adamalfredo/bybit-trading-bot` |
| Python | 3.12, venv `.venv/` |
| Railway LONG | Custom Start Command: `python main-long-v2.py` |
| Railway SHORT | Custom Start Command: `python main-short.py` |

---

## 2. Bot LONG — `main-long-v2.py`

### Strategia: EMA20-Pullback 4h (Walk-Forward Run H)

**Validazione backtest:**
- TRAIN: 2024-01-01 → 2025-06-30
- TEST: 2025-07-01 → 2026-05-13
- Risultati TEST: **77 trade | WR 57.1% | Ratio 1.68x | Expectancy +0.3655R**

**Parametri operativi:**
```
SL_ATR_MULT      = 2.0      # SL a 2×ATR sotto entry
TRAIL_ATR_MULT   = 3.0      # Trailing stop a 3×ATR di distanza
TRAIL_START_R    = 1.0      # Trailing si attiva quando gain >= 1R
RISK_PCT         = 0.0075   # Rischio per trade: 0.75% equity
MAX_OPEN_POSITIONS = 4      # Max 4 LONG contemporanei
DEFAULT_LEVERAGE = 10
DAILY_DD_CAP_PCT = 0.05     # Circuit breaker: blocca nuovi LONG se -5% in giornata
```

**Segnale di ingresso — 6 condizioni (tutte necessarie):**
1. BTC 4h > EMA50 4h (gate di regime)
2. `Close <= EMA20 + 2×ATR` (vicino alla media — pullback)
3. `Close >= EMA20 - 2×ATR` (non troppo lontano sotto)
4. `RSI(14) < 50` (momentum non esaurito)
5. `Volume > 1.2× media volume 20 barre` (volume confermato)
6. `EMA20 > EMA50 AND slope EMA20 > 0` (trend micro e macro up)
7. Candela corrente bullish (`Close > Open`)

**Coin fisse (Run H — 61 validate):**
```
BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, ADAUSDT, DOTUSDT, AVAXUSDT, LINKUSDT,
UNIUSDT, NEARUSDT, TONUSDT, SUIUSDT, TAOUSDT, ONDOUSDT, ENAUSDT,
1000PEPEUSDT, AAVEUSDT, LDOUSDT, COMPUSDT, WLDUSDT, INJUSDT, TIAUSDT,
SEIUSDT, OPUSDT, ARBUSDT, STXUSDT, APTUSDT, RENDERUSDT, JUPUSDT, PYTHUSDT,
WIFUSDT, BOMEUSDT, NOTUSDT, EIGENUSDT, POLUSDT, HYPEUSDT, MOVEUSDT,
LTCUSDT, ATOMUSDT, XLMUSDT, VETUSDT, FILUSDT, ICPUSDT, RUNEUSDT, ARUSDT,
CRVUSDT, SANDUSDT, MANAUSDT, AXSUSDT, GALAUSDT, SNXUSDT, GRTUSDT,
APEUSDT, GMTUSDT, ORDIUSDT, 1000BONKUSDT, CFXUSDT, KASUSDT, PENDLEUSDT,
BLURUSDT, FLUXUSDT
```

**Perché lista fissa:**
La validazione statistica vale SOLO su queste coin. Tradare su coin non validate annulla la statistica del backtest.

**Cosa è stato rimosso rispetto al vecchio `main-long.py` (2790 righe):**
- F&G gate (`fear_greed_value < threshold`)
- BTC dump guard (variazione % rapida BTC)
- BTC daily regime (EMA50 daily — diverso dall'EMA50 4h del backtest)
- ALT worker (trailing alternativo non testato)
- BE lock automatico (breakeven forzato non nel backtest)
- Ratchet ROI tiers (uscite parziali non nel backtest)
- Weekly drawdown cap
- TP1 parziale a 1.5R (non era nel backtest)
- Strength score (pesi per nome strategia)
- Cooldown post-loss
- Blacklist dinamica consecutivi
- MAX_VOLATILE_LONG gate
- MAX_LARGE_CAP_POSITIONS gate
- FUNDING gate
- Signal expiry / zombie check
- Swing level cache

**Cosa è rimasto (necessario in live):**
- BTC gate EMA50 4h ✅ (era nel backtest)
- MAX_OPEN_POSITIONS=4 ✅ (era nel backtest)
- Daily DD cap 5% ✅ (sicurezza emergenza)
- SL watchdog ogni 5 min ✅ (sicurezza operativa)
- Trailing worker ✅ (logica backtest)

---

## 3. Bot SHORT — `main-short.py`

**Stato:** Attivo su Railway, ma nessun backtest validato su questa strategia.
**Nota:** Non toccare fin quando non c'è una Run validata per gli SHORT.

**Condizioni per entrare SHORT (dalla lettura dei log):**
- `bear_confirmed=True` (richiede: BTC 4h down + daily down in combinazione)
- `btc_fav=False` (BTC non favorevole ai long)
- Trend filter per singola coin
- OI filter (Open Interest deve essere crescente)
- ADX, RSI, funding rate

---

## 4. Manutenzione periodica

### Backtest rolling (ogni 3-6 mesi)
File: `backtest_new.py` — engine EMA20-Pullback 4h walk-forward.

Procedura:
1. Shifta la finestra temporale:
   - Nuovo TRAIN: es. 2025-01-01 → 2025-12-31
   - Nuovo TEST: es. 2026-01-01 → oggi
2. Esegui walk-forward per trovare la nuova Run ottimale
3. Confronta WR, Ratio, Expectancy con Run H
4. Se la nuova Run è statisticamente superiore, aggiorna `main-long-v2.py`:
   - Lista coin (aggiungi/rimuovi validate)
   - Parametri SL/Trail se cambiano

### Quando rifarlo in anticipo:
- WR live scende sotto ~45% per 20+ trade consecutivi
- BTC cambia struttura strutturalmente (fine bull market → bear prolungato)
- Nuove coin rilevanti appaiono su Bybit con liquidità adeguata

---

## 5. Storico decisioni architetturali

| Data | Decisione | Motivazione |
|---|---|---|
| 2026-05-13 | Riscrittura completa `main-long-v2.py` da 2790 → ~400 righe | Il vecchio file portava filtri non testati che divergevano dal backtest, causando comportamenti imprevedibili e probabilmente performance peggiori |
| 2026-05-13 | Lista coin fissa invece di selezione dinamica | La validazione statistica vale solo sulle coin del backtest |
| 2026-05-13 | Rimosso TP1 parziale a 1.5R | Non era nel backtest Run H — alterava il rapporto win/loss |
| 2026-05-13 | Mantenuto solo BTC gate EMA50 4h | Era l'unico gate nel backtest. Gli altri (daily regime, dump guard, F&G) erano stati aggiunti post-backtest |

---

## 6. Aspettative performance live

| Metrica | Backtest Run H | Attesa live (range realistico) |
|---|---|---|
| Win Rate | 57.1% | 50-58% |
| Win/Loss Ratio | 1.68x | 1.4-1.7x |
| Expectancy | +0.3655R | +0.2 - +0.35R |
| Trade per mese | ~5-8 | 3-8 (dipende da regime BTC) |

**Note:**
- In mercato BTC < EMA50 4h, il bot non entra. Periodi di gate chiuso sono normali e attesi.
- Slippage, funding e spread live riducono leggermente la performance rispetto al backtest.
- Non aspettarsi performance identiche al backtest — lo scarto del 10-20% è fisiologico.

---

## 7. File del progetto

| File | Scopo |
|---|---|
| `main-long-v2.py` | Bot LONG in produzione (Run H) |
| `main-short.py` | Bot SHORT (nessun backtest validato — non toccare) |
| `main-long.py` | Vecchio bot LONG — backup di riferimento, NON in produzione |
| `backtest_new.py` | Engine backtest EMA20-Pullback 4h per future ottimizzazioni |
| `bybit_mcp_server.py` | MCP server per VS Code Copilot (monitoring Railway/Bybit) |
| `requirements.txt` | Dipendenze Python |
| `Strategia.md` | Note strategia originale |
| `Roadmap.md` | Roadmap sviluppo |
| `CONTEXT.md` | Questo file — memoria permanente del progetto |

---

## 8. Come leggere i log Railway

**Bot LONG:**
```
[LOOP] equity=X USDT | pos=N/4 | btc_fav=True/False
```
- `btc_fav=False` → gate chiuso, nessun ingresso (normale in bear)
- `btc_fav=True` → bot attivo, scansiona segnali
- `[ENTRY]` → trade aperto
- `[CLEANUP]` → posizione chiusa dall'exchange (SL o TP hit)
- `[TRAIL]` → trailing stop attivato

**Bot SHORT:**
```
[CTX] BTC 4h_down=X | daily_down=X | bear_confirmed=X | btc_4h_chg=X%
[CICLO][SHORT] simboli=N | pos_aperte=N | btc_fav=X | equity=X
```
- `bear_confirmed=False` → no SHORT (normale in fase di rimbalzo o laterale)
- `[TREND-FILTER] BTC sfavorevole` → singola coin filtrata
- `[OI-FILTER]` → Open Interest in calo, skip

---

*Ultimo aggiornamento: 2026-05-14*
