# Bot di trading Bybit (versione per Render)

## ✅ Come usarlo

1. Vai su https://render.com
2. Clicca su "New + > Web Service"
3. Carica lo ZIP di questo progetto
4. Rinomina `.env.example` in `.env` ed inserisci le tue credenziali (puoi impostare `BYBIT_TESTNET=true` per usare la testnet)
5. Render leggerà automaticamente `render.yaml` e configurerà il bot

## ⚠️ Attenzione
- Il bot è attivo 24/7
- Usa 10 USDT per ogni trade spot (modificabile con `ORDER_USDT`);
  il bot prova a recuperare i limiti minimi di ordine direttamente da Bybit;
  se la richiesta fallisce usa un endpoint alternativo e, in caso di ulteriori
  problemi, salta l'ordine di test
- Riceverai notifiche su Telegram
- All'avvio il bot invia un messaggio di prova su Telegram
- In questa versione il bot può inviare ordini automatici su Bybit se imposti le chiavi API
- All'avvio viene effettuato un piccolo acquisto di BTC (circa 10 USDT) per
  verificare la connessione; la quantità viene adeguata al minimo recuperato e
  l'operazione viene saltata se i dati sui minimi non sono disponibili
- Se i dati non contengono la colonna "Close" viene indicata nel log la lista delle colonne trovate
- Se il download dei dati fallisce per problemi di rete, il bot effettua alcuni tentativi automatici

## Aggiornamento
Il bot ora supporta l'invio di ordini automatici su Bybit utilizzando le chiavi API presenti nel file `.env`.
All'avvio viene eseguito un breve test di connessione alle API di Bybit per verificare che le credenziali siano corrette.
