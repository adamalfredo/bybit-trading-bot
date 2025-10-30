## 🗺️ Elenco interventi da replicare su main-short.py

- Alza il timeframe operativo: 
INTERVAL_MINUTES = 60 
ENTRY_TF_MINUTES = 60

- Segnali di ingresso più severi: 
Richiedi almeno 3 condizioni su 4 per l’entry (sia volatile che non volatile). 

- Segnali di uscita meno reattivi: 
Esci per segnale solo se il trade è in profitto > 0.5R o holding > 60min.

- Trailing e TP/SL più larghi: 
Allarga le soglie come fatto sopra.

- (Opzionale) Conferma exit su timeframe superiore: 
Esci solo se anche su 1h c’è inversione.

- (Opzionale) Log di sintesi periodico.

## 🚦 Segnali di ingresso più severi

Richiedi almeno 3 condizioni su 4 per l’entry, sia per asset volatili che non volatili. 

```python
if len(entry_conditions) >= 2: 
```

e sostituisci con: 

```python
if len(entry_conditions) >= 3:
```