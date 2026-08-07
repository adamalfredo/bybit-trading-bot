# Weekly Go/No-Go Report

Periodo analisi: 2026-08-01 -> 2026-08-07
Compilato il: 2026-08-07 UTC
Versione strategia (commit): e994613 (filtri allentati); deploy attivo verificato su Railway

## 1) Dati base

- Trade chiusi periodo (LONG+SHORT): 2
- Trade chiusi cumulati ciclo corrente: 2
- Equity inizio periodo (USDT): n/d
- Equity fine periodo (USDT): 33.4290
- Delta equity (USDT): -0.1459 (su trade chiusi periodo)

## 2) KPI principali

| KPI | Valore | Soglia Go | Esito |
|---|---:|---:|---|
| Profit Factor | 0.30 | >= 1.15 | NO-GO |
| Expectancy (USDT/trade) | -0.0730 | > 0 | NO-GO |
| Max Drawdown periodo | ~0.2097 USDT (stima da trade chiusi) | <= ciclo precedente | NO-GO* |
| Avg Loss / Avg Win | 3.28 | <= 2.2 | NO-GO |
| Trade count ciclo | 2 | >= 30 | NO-GO |

Nota: * confronto drawdown col ciclo precedente non robusto per assenza baseline formalizzata nel report precedente.

## 3) Breakdown operativita

### LONG
- Trade: probabilmente gli unici 2 trade del periodo (attribuzione lato exchange non completamente separata nel summary)
- Win rate: 50%
- PnL netto (USDT): -0.1459
- Top motivi reject da log: breakout_not_confirmed, base_too_wide, top_mover_too_extended

### SHORT
- Trade: 0 nelle ultime scansioni osservate
- Win rate: n/d
- PnL netto (USDT): ~0 nel periodo recente osservato
- Tempo in IDLE (% scansioni): ~100% nelle scansioni recenti loggate
- Top motivi reject da log: non applicabile (prevalente blocco regime BTC OFF)

## 4) Diagnosi del ciclo

- Cosa ha funzionato:
  - Rischio per trade contenuto (0.5%) e nessuna esposizione aperta eccessiva.
  - Deploy e stabilita servizi corretti.
- Cosa non ha funzionato:
  - Campione trade insufficiente per far emergere edge statistico.
  - Rapporto perdite/vincite ancora sfavorevole.
  - SHORT bloccato dal regime BTC non bear.
- Bottleneck principale: Under-trading

## 5) Decisione formale

Esito ciclo: NO-GO

Motivazione sintetica:
KPI core sotto soglia (PF 0.30, expectancy negativa, loss/win troppo alto) e campione troppo piccolo (2 trade su minimo 30). Non c'e evidenza che la strategia sia profittevole in questo ciclo.

## 6) Azione successiva (una sola)

- Azione scelta: congelare i parametri attuali (commit e994613) e NON modificare nulla fino al raggiungimento di almeno 30 trade chiusi oppure 14 giorni completi, poi rivalutazione.
- Razionale: evitare ulteriore tuning su campione insufficiente.

Commit pianificato: nessun commit strategico fino a fine finestra di valutazione.

## 7) Checklist anti-loop

- Nessuna modifica parametri durante il ciclo appena analizzato: NO (modifica fatta il 07/08)
- Numero modifiche nel ciclo: 1
- Decisione coerente con protocollo in CONTEXT.md: SI
