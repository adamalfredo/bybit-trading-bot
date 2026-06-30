# Bybit Trading Bot - EMA20 Pullback

Sistema dual-bot per trading di futures Bybit in modalita hedge:
- **Bot LONG** (main-pullback.py): EMA20(4h) pullback su coin in uptrend daily
- **Bot SHORT** (main-short-pullback.py): EMA20(4h) bounce rejection su coin in downtrend daily

I due bot operano in parallelo su Railway come servizi indipendenti con regime gate automatico basato su BTC.

---

## Deploy su Railway

### Servizio LONG
- **Build command:** pip install -r requirements.txt
- **Start command:** python main-pullback.py

### Servizio SHORT
- **Build command:** pip install -r requirements.txt
- **Start command:** python main-short-pullback.py

### Variabili d ambiente (identiche per entrambi i servizi)

| Variabile | Descrizione |
|---|---|
| BYBIT_API_KEY | API key Bybit |
| BYBIT_API_SECRET | API secret Bybit |
| BYBIT_ACCOUNT_TYPE | UNIFIED (default) |
| BYBIT_TESTNET | 	rue per testnet, alse per produzione |
| TELEGRAM_TOKEN | Token bot Telegram per notifiche |
| TELEGRAM_CHAT_ID | Chat ID Telegram |
| PYTHONUNBUFFERED | 1 (obbligatorio per log in tempo reale su Railway) |
| TZ | Etc/UTC (timestamp coerenti) |

---

## Strategia

### Logica comune (LONG e SHORT sono speculari)

**Filtro daily** - seleziona sole coin con trend strutturale confermato:
- LONG: close > EMA50 + slope positiva + max 20% sopra EMA50
- SHORT: close < EMA50 + slope negativa + max 20% sotto EMA50

**Segnale 4h** - entry sulla reazione alla EMA20:
- LONG: pullback al supporto EMA20, candela verde che rimbalza sopra
- SHORT: bounce alla resistenza EMA20, candela rossa che rifiuta sotto

**Filtri segnale 4h (entrambi i bot):**
- RSI(14): 30-65 (LONG) / 35-65 (SHORT)
- Corpo candela >= 40% del range
- Volume >= 1.5x media 20 candele
- Close entro 3% dalla EMA20
- SL: sotto/sopra swing low/high ultime 3 barre + 0.3xATR

**Regime gate BTC:**
- Bot LONG: sempre attivo (filtro daily per coin e sufficiente)
- Bot SHORT: attivo solo quando BTC < EMA50 daily E slope EMA50 negativa; altrimenti idle automatico

**Exit:**
- SL iniziale + Ratchet floor fissi (>=15%->+7%lev ... >=500%->+465%lev)
- ATR trail dal massimo/minimo (2xATR), attivo dal primo ratchet
- Partial TP: 50% chiuso a +2R
- Time stop: chiude dopo 10 giorni se P&L < 10% lev
- Circuit breaker: blocca tutto se drawdown giornaliero > 3%

---

## Ratchet Table

| P&L lev | Floor garantito |
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

## MCP Server (VS Code Copilot)

ybit_mcp_server.py espone strumenti MCP per monitorare i bot direttamente da VS Code Copilot:
- get_bot_summary - equity, posizioni aperte, ultimi 10 trade, statistiche 7gg
- get_railway_status - stato deploy LONG e SHORT
- get_railway_logs - ultimi N log di un bot (bot: long o short)
- get_open_orders - ordini aperti su Bybit

Configurazione in .vscode/mcp.json.

---

## Roadmap

Vedere Roadmap.md per le migliorie segnali pendenti (da valutare dopo 20+ trade aggiuntivi):
1. [PRIORITA 1] Slope EMA20(4h) positiva - log DIAG-SLOPE gia attivo
2. [PRIORITA 2] Struttura pre-pullback: 2+ candele sopra EMA20 prima del ritocco
3. [PRIORITA 3] RSI min da 30 a 38
4. [PRIORITA 4] EMA_TOUCH_TOL da 1.2% a 0.5%
