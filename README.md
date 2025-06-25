# Bot di trading Bybit (versione per Render)

## ✅ Come usarlo

1. Vai su https://render.com
2. Clicca su "New + > Web Service"
3. Carica lo ZIP di questo progetto
4. Rinomina `.env.example` in `.env` ed inserisci le tue credenziali (puoi impostare `BYBIT_TESTNET=true` per usare la testnet)
5. Render leggerà automaticamente `render.yaml` e configurerà il bot

## ⚠️ Attenzione
- Il bot è attivo 24/7
- Usa almeno 50 USDT per ogni acquisto spot (variabile `ORDER_USDT` ma con
  soglia minima a 50). Il bot recupera i limiti di Bybit e, se necessari,
  aumenta automaticamente l'importo per rispettarli. Se il recupero fallisce usa
  un endpoint alternativo. In uscita il bot vende l'intero saldo della moneta.
- La quantità viene adeguata allo `qtyStep` di Bybit per evitare errori sui
  decimali degli ordini
- Riceverai notifiche su Telegram, compreso l'esito degli ordini eseguiti
- All'avvio il bot invia un messaggio di prova su Telegram e verifica la
  connessione a Bybit; **non** viene eseguito alcun ordine di test
- Per compatibilità con versioni precedenti, la funzione `initial_buy_test()`
  ora reindirizza a `test_bybit_connection()` evitando crash
- In questa versione il bot può inviare ordini automatici su Bybit se imposti le chiavi API
- Se i dati non contengono la colonna "Close" viene indicata nel log la lista delle colonne trovate
- Se il download dei dati fallisce per problemi di rete, il bot effettua alcuni tentativi automatici

## Aggiornamento
Il bot ora supporta l'invio di ordini automatici su Bybit utilizzando le chiavi API presenti nel file `.env`.
All'avvio viene eseguito un breve test di connessione alle API di Bybit per verificare che le credenziali siano corrette.
