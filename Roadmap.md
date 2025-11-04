## 🗺️ Elenco interventi da replicare su main-short.py

- (Opzionale) Log di sintesi periodico.
- Modificare leva

## 🚦 Settings

Richiede almeno 2 condizioni su 4 per l’entry, sia per asset volatili che non volatili. 

```python
if len(entry_conditions) >= 2: 
```

Richiede almeno 3 condizioni su 4 per l’entry, sia per asset volatili che non volatili.  

```python
if len(entry_conditions) >= 3:
```

BREAKEVEN 

```python
BREAKEVEN_LOCK_PCT = 0.01   # attiva BE al +1% di prezzo (~+10% PnL con leva 10x)
BREAKEVEN_BUFFER   = 0.0005 # stop a BE + 0.05% per evitare micro-slippage
```