# Bot di trading Bybit

## ✅ Come usarlo con Railway

1. Vai su https://railway.app
2. Crea un nuovo progetto e carica questo repository
3. Rinomina `.env.example` in `.env` ed inserisci le tue credenziali (puoi impostare `BYBIT_TESTNET=true` per usare la testnet). Se utilizzi l'account unificato non cambiare `BYBIT_ACCOUNT_TYPE` (di default `UNIFIED`)
4. Imposta `pip install -r requirements.txt` come comando di build e `python main.py` come comando di start

## ⚠️ Attenzione
- Il bot è attivo 24/7
- Usa almeno 50 USDT per ogni acquisto spot (variabile `ORDER_USDT` ma con
  soglia minima a 50). Il bot recupera i limiti di Bybit e, se necessari,
  aumenta automaticamente l'importo per rispettarli. Se il recupero fallisce usa
  un endpoint alternativo. In uscita il bot vende l'intero saldo della moneta.
- Il recupero del saldo spot è stato reso più robusto e viene segnalato nel log
  se la coin richiesta non è presente nella risposta dell'API di Bybit
- La quantità viene adeguata allo `qtyStep` di Bybit e arrotondata verso l'alto
  così che il valore rispetti sempre i minimi imposti dall'exchange
- Prima di ogni acquisto viene controllato il saldo USDT disponibile
- Riceverai notifiche su Telegram, compreso l'esito degli ordini eseguiti
- Se non usi l'account unificato imposta `BYBIT_ACCOUNT_TYPE=SPOT` nel file `.env`
- All'avvio il bot invia un messaggio di prova su Telegram, verifica la
  connessione a Bybit ed esegue un acquisto iniziale di BTC utilizzando
  l'importo `ORDER_USDT`
- In questa versione il bot può inviare ordini automatici su Bybit se imposti le chiavi API
- Se i dati non contengono la colonna "Close" viene indicata nel log la lista delle colonne trovate
- Se il download dei dati fallisce per problemi di rete, il bot effettua alcuni tentativi automatici

## 🔄 Aggiornamento
Il bot ora supporta l'invio di ordini automatici su Bybit utilizzando le chiavi API presenti nel file `.env`.
All'avvio viene eseguito un breve test di connessione alle API di Bybit per verificare che le credenziali siano corrette.

## 📋 Debug
LOG_DEBUG_ASSETS = os.getenv("LOG_DEBUG_ASSETS", "0") == "1"  
LOG_DEBUG_DECIMALS = os.getenv("LOG_DEBUG_DECIMALS", "0") == "1"  
LOG_DEBUG_SYNC = os.getenv("LOG_DEBUG_SYNC", "0") == "1"  
LOG_DEBUG_STRATEGY = os.getenv("LOG_DEBUG_STRATEGY", "0") == "1"  
LOG_DEBUG_TRAILING = os.getenv("LOG_DEBUG_TRAILING", "0") == "1"  
LOG_DEBUG_PORTFOLIO = os.getenv("LOG_DEBUG_PORTFOLIO", "0") == "1"  

## 📋 Debug 2
LOG_LEVEL=INFO  
LOG_DEBUG_ASSETS=0  
LOG_DEBUG_DECIMALS=0  
LOG_DEBUG_SYNC=0  
LOG_DEBUG_STRATEGY=0  
LOG_DEBUG_PORTFOLIO=0  

Per un’analisi efficace delle cause di perdita dopo 48h:
- ⚙️ LOG_DEBUG_STRATEGY = 1  
Così vedo tutti i segnali, le strategie scelte, le condizioni di entry/exit e i motivi per cui un trade viene tentato o saltato.

- ⚙️ LOG_DEBUG_TRAILING = 1  
Così posso analizzare come e quando si attiva il trailing stop, se viene gestito correttamente e se chiude troppo presto/tardi.

- ⚙️ LOG_DEBUG_PORTFOLIO = 1  
Così posso vedere l’evoluzione del portafoglio, la ripartizione tra USDT e posizioni, e se il sizing è coerente.

## 🚀 Servizi separati LONG/SHORT su Railway

Per eseguire i due bot come servizi indipendenti:

- Variabili d’ambiente (uguali per entrambi i servizi):
  - `BYBIT_API_KEY`, `BYBIT_API_SECRET`
  - `BYBIT_ACCOUNT_TYPE` (es. `UNIFIED`)
  - `BYBIT_TESTNET` (`true` per testnet, `false` per produzione)
  - `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (opzionali, per notifiche)

- Servizio LONG
  - Build: `pip install -r requirements.txt`
  - Start: `python main-long.py`

- Servizio SHORT
  - Build: `pip install -r requirements.txt`
  - Start: `python main-short.py`

Note:
- Le funzioni di Google Sheets sono state rimosse da `main-long.py` e `main-short.py`. Il file `requirements.txt` è stato snellito: se usi script legacy che richiedono Google Sheets, aggiungi manualmente i pacchetti necessari o un file `requirements-sheets.txt` dedicato.
- Imposta `PYTHONUNBUFFERED=1` su Railway per log in tempo reale. Impostare `TZ=Etc/UTC` aiuta ad avere timestamp coerenti.

## 📈 Reports

fai un controllo su bybit e sui log di short e long su railway e fammi un report su come sta andando il trade delle ultime 24 ore. Verifica se ci sono stati errori o problemi di connessione e se le operazioni di short e long sono state eseguite correttamente. Fornisci un'analisi dettagliata delle performance del trade, inclusi eventuali guadagni o perdite (elenca anche le spese dovute alle fee di bybit), e suggerimenti per migliorare la strategia di trading in futuro.

### tabella trailing SL/TP (ratchet) 

ROI raggiunto	Floor garantito

P&L lev	  Floor garantito	  Gap
≥15%	    +7%	              8%
≥25%	    +15%	            10%
≥40%	    +25%	            15%
≥60%	    +40%	            20%
≥80%	    +60%	            20%
≥100%	    +80%	            20%
≥125%	    +100%	            25%
≥150%	    +120%	            30%
≥175%	    +148%	            27%
≥200%	    +173%	            27%
≥250%	    +223%	            27%
≥300%	    +273%	            27%
≥400%	    +370%	            30%
≥500%	    +465%	            35%


### tabella EMA

Aggiungi "Moving Average Exponential" tre volte con questi periodi:

EMA	Timeframe	Ruolo nel bot
20	1h	Target pullback entry (prezzo deve tornare qui per entrare)
100	1h	Filtro trend 1h — prezzo deve essere sopra, pendenza crescente
200	1h	Filtro macro — equivale all'EMA200 4h (200 candele × 1h = ~8 giorni di 4h trend)