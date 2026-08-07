# Weekly Go/No-Go Report Template

Periodo analisi: YYYY-MM-DD -> YYYY-MM-DD
Compilato il: YYYY-MM-DD HH:MM UTC
Versione strategia (commit): <hash>

## 1) Dati base

- Trade chiusi periodo (LONG+SHORT):
- Trade chiusi cumulati ciclo corrente:
- Equity inizio periodo (USDT):
- Equity fine periodo (USDT):
- Delta equity (USDT):

## 2) KPI principali

| KPI | Valore | Soglia Go | Esito |
|---|---:|---:|---|
| Profit Factor |  | >= 1.15 | GO/NO-GO |
| Expectancy (USDT/trade) |  | > 0 | GO/NO-GO |
| Max Drawdown periodo |  | <= ciclo precedente | GO/NO-GO |
| Avg Loss / Avg Win |  | <= 2.2 | GO/NO-GO |
| Trade count ciclo |  | >= 30 | GO/NO-GO |

## 3) Breakdown operativita

### LONG
- Trade:
- Win rate:
- PnL netto (USDT):
- Top motivi reject da log:

### SHORT
- Trade:
- Win rate:
- PnL netto (USDT):
- Tempo in IDLE (% scansioni):
- Top motivi reject da log:

## 4) Diagnosi del ciclo

- Cosa ha funzionato:
- Cosa non ha funzionato:
- Bottleneck principale (scegliere uno):
  - Under-trading
  - Losses troppo ampie
  - Regime filter troppo restrittivo
  - Entry quality insufficiente
  - Altro: ...

## 5) Decisione formale

Esito ciclo: GO / NO-GO

Motivazione sintetica (max 5 righe):

## 6) Azione successiva (una sola)

- Se GO: mantenere parametri invariati per il prossimo ciclo.
- Se NO-GO: applicare una sola modifica strutturale nel prossimo ciclo.
- Se NO-GO per 2 cicli consecutivi: STOP tuning e pivot strategia (paper + walk-forward + montecarlo).

Modifica scelta (se NO-GO):

Commit pianificato:

## 7) Checklist anti-loop

- Nessuna modifica parametri durante il ciclo appena analizzato: SI/NO
- Numero modifiche nel ciclo: 0 oppure 1
- Decisione coerente con protocollo in CONTEXT.md: SI/NO
