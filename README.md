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
- All'avvio il bot invia un messaggio di prova su Telegram e verifica la
  connessione a Bybit; **non** viene eseguito alcun ordine di test
- In questa versione il bot può inviare ordini automatici su Bybit se imposti le chiavi API
- Se i dati non contengono la colonna "Close" viene indicata nel log la lista delle colonne trovate
- Se il download dei dati fallisce per problemi di rete, il bot effettua alcuni tentativi automatici

## Aggiornamento
Il bot ora supporta l'invio di ordini automatici su Bybit utilizzando le chiavi API presenti nel file `.env`.
All'avvio viene eseguito un breve test di connessione alle API di Bybit per verificare che le credenziali siano corrette.