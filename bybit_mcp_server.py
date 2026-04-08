"""
Bybit MCP Server — read-only tools per GitHub Copilot
Espone posizioni aperte, bilancio, P&L, ordini recenti e ticker live.
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Carica .env dalla cartella del server, ovunque VS Code lo avvii
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)

API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET    = os.getenv("BYBIT_TESTNET", "0") == "1"
BASE_URL   = "https://api-testnet.bybit.com" if TESTNET else "https://api.bybit.com"

mcp = FastMCP("bybit")


# ── helpers ─────────────────────────────────────────────────────────────────

def _f(val, default: float = 0.0) -> float:
    """Converte in float gestendo stringhe vuote restituite da Bybit."""
    try:
        return float(val) if val not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _sign(params: dict) -> dict:
    """Aggiunge firma HMAC-SHA256 ai parametri per endpoint autenticati."""
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    payload = f"{ts}{API_KEY}{recv_window}{query}"
    sig = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
    }


async def _get(path: str, params: dict, auth: bool = True) -> dict:
    headers = _sign(params) if auth else {}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        r = await client.get(BASE_URL + path, params=params, headers=headers)
        if not r.text:
            return {"retCode": -1, "retMsg": f"Risposta vuota da Bybit (HTTP {r.status_code})"}
        try:
            return r.json()
        except Exception as e:
            return {"retCode": -1, "retMsg": f"Risposta non-JSON (HTTP {r.status_code}): {r.text[:300]}"}


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_wallet_balance() -> str:
    """
    Restituisce il bilancio del wallet UNIFIED Bybit:
    equity totale, available balance, unrealized PnL e margine usato.
    """
    data = await _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if data.get("retCode") != 0:
        return f"Errore API: {data.get('retMsg')}"

    coins = data["result"]["list"][0].get("coin", [])
    usdt = next((c for c in coins if c["coin"] == "USDT"), None)
    if not usdt:
        return "USDT non trovato nel wallet"

    equity          = _f(usdt.get("equity", 0))
    available       = _f(usdt.get("availableToWithdraw", 0))
    unrealized_pnl  = _f(usdt.get("unrealisedPnl", 0))
    margin_used     = equity - available

    lines = [
        f"💰 Equity totale:     {equity:.4f} USDT",
        f"✅ Disponibile:       {available:.4f} USDT",
        f"📊 Margine impiegato: {margin_used:.4f} USDT",
        f"📈 Unrealized PnL:    {unrealized_pnl:+.4f} USDT",
    ]
    return "\n".join(lines)


@mcp.tool()
async def get_open_positions() -> str:
    """
    Restituisce tutte le posizioni futures LONG e SHORT attualmente aperte,
    con entry price, mark price, size, unrealized PnL, leva e SL/TP impostati.
    """
    data = await _get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if data.get("retCode") != 0:
        return f"Errore API: {data.get('retMsg')}"

    positions = [p for p in data["result"]["list"] if _f(p.get("size", 0)) > 0]
    if not positions:
        return "Nessuna posizione aperta."

    lines = []
    for p in positions:
        sym         = p["symbol"]
        side        = p["side"]
        size        = _f(p["size"])
        entry       = _f(p["avgPrice"])
        mark        = _f(p["markPrice"])
        upnl        = _f(p["unrealisedPnl"])
        pct         = _f(p.get("unrealisedPnlPcnt", 0)) * 100
        lev         = p.get("leverage", "?")
        sl          = p.get("stopLoss", "—")
        tp          = p.get("takeProfit", "—")
        emoji = "📈" if side == "Buy" else "📉"
        lines.append(
            f"{emoji} {sym} {side.upper()}\n"
            f"   Size: {size} | Entry: {entry} | Mark: {mark}\n"
            f"   PnL: {upnl:+.4f} USDT ({pct:+.2f}%)\n"
            f"   SL: {sl} | TP: {tp} | Leva: {lev}x"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def get_recent_closed_pnl(limit: int = 20) -> str:
    """
    Mostra gli ultimi N trade chiusi con il P&L realizzato per ognuno.
    Default: ultimi 20. Max: 100.
    """
    limit = min(max(1, limit), 100)
    data = await _get("/v5/position/closed-pnl", {"category": "linear", "limit": str(limit)})
    if data.get("retCode") != 0:
        return f"Errore API: {data.get('retMsg')}"

    trades = data["result"]["list"]
    if not trades:
        return "Nessun trade chiuso trovato."

    total_pnl = 0.0
    lines = []
    for t in trades:
        sym   = t["symbol"]
        side  = t["side"]
        qty   = t["qty"]
        entry = _f(t["avgEntryPrice"])
        close = _f(t["avgExitPrice"])
        pnl   = _f(t["closedPnl"])
        total_pnl += pnl
        ts    = int(t["updatedTime"]) // 1000
        dt    = time.strftime("%d/%m %H:%M", time.gmtime(ts))
        emoji = "✅" if pnl > 0 else "❌"
        lines.append(f"{emoji} {dt} | {sym} {side} x{qty} | entry {entry} → close {close} | PnL {pnl:+.4f} USDT")

    lines.append(f"\n📊 Totale P&L ({len(trades)} trade): {total_pnl:+.4f} USDT")
    wins = sum(1 for t in trades if _f(t["closedPnl"]) > 0)
    lines.append(f"🏆 Win rate: {wins}/{len(trades)} ({wins/len(trades)*100:.0f}%)")
    return "\n".join(lines)


@mcp.tool()
async def get_ticker(symbol: str) -> str:
    """
    Restituisce il ticker live per un simbolo (es. BTCUSDT):
    last price, bid1, ask1, variazione 24h, volume e funding rate.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    data = await _get("/v5/market/tickers", {"category": "linear", "symbol": symbol}, auth=False)
    if data.get("retCode") != 0:
        return f"Errore API: {data.get('retMsg')}"

    lst = data["result"]["list"]
    if not lst:
        return f"Simbolo {symbol} non trovato."

    t = lst[0]
    last    = _f(t["lastPrice"])
    bid1    = _f(t.get("bid1Price") or last, last)
    ask1    = _f(t.get("ask1Price") or last, last)
    pct     = _f(t.get("price24hPcnt", 0)) * 100
    vol     = _f(t.get("turnover24h", 0))
    funding = _f(t.get("fundingRate", 0)) * 100

    return (
        f"🪙 {symbol}\n"
        f"   Last: {last} | Bid: {bid1} | Ask: {ask1}\n"
        f"   Variazione 24h: {pct:+.2f}%\n"
        f"   Volume 24h: {vol/1_000_000:.1f}M USDT\n"
        f"   Funding rate: {funding:+.4f}%"
    )


@mcp.tool()
async def get_open_orders() -> str:
    """
    Mostra tutti gli ordini aperti (limit, stop, TP, SL condizionali)
    attualmente in attesa di esecuzione.
    """
    data = await _get("/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"})
    if data.get("retCode") != 0:
        return f"Errore API: {data.get('retMsg')}"

    orders = data["result"]["list"]
    if not orders:
        return "Nessun ordine aperto."

    lines = []
    for o in orders:
        sym   = o["symbol"]
        side  = o["side"]
        otype = o["orderType"]
        price = o.get("price", "market")
        qty   = o["qty"]
        status = o["orderStatus"]
        lines.append(f"  {sym} {side} {otype} | qty={qty} price={price} | {status}")
    return f"{len(orders)} ordini aperti:\n" + "\n".join(lines)


@mcp.tool()
async def get_pnl_summary(days: int = 7) -> str:
    """
    Statistiche aggregate sui trade chiusi negli ultimi N giorni (default 7).
    Mostra: PnL totale, win rate, profit factor, avg vincita/perdita,
    miglior/peggior trade, e breakdown per simbolo ordinato per PnL.
    Utile per valutare se il bot sta performando bene e quali simboli penalizzano.
    """
    days = max(1, min(days, 90))
    start_ms = int((time.time() - days * 86400) * 1000)
    data = await _get("/v5/position/closed-pnl", {
        "category": "linear",
        "limit": "200",
        "startTime": str(start_ms),
    })
    if data.get("retCode") != 0:
        return f"Errore API: {data.get('retMsg')}"

    trades = data["result"]["list"]
    if not trades:
        return f"Nessun trade chiuso negli ultimi {days} giorni."

    pnls  = [_f(t["closedPnl"]) for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl     = sum(pnls)
    win_rate      = len(wins) / len(pnls) * 100
    avg_win       = sum(wins) / len(wins) if wins else 0.0
    avg_loss      = sum(losses) / len(losses) if losses else 0.0
    profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
    best  = max(pnls)
    worst = min(pnls)

    # Breakdown per simbolo
    by_symbol: dict = {}
    for t in trades:
        sym = t["symbol"]
        pnl = _f(t["closedPnl"])
        if sym not in by_symbol:
            by_symbol[sym] = {"pnl": 0.0, "n": 0, "wins": 0}
        by_symbol[sym]["pnl"]  += pnl
        by_symbol[sym]["n"]    += 1
        if pnl > 0:
            by_symbol[sym]["wins"] += 1

    sorted_syms = sorted(by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True)

    lines = [
        f"📅 Ultimi {days} giorni — {len(trades)} trade",
        f"",
        f"💰 PnL netto totale:  {total_pnl:+.4f} USDT",
        f"🏆 Win rate:          {len(wins)}/{len(pnls)} ({win_rate:.0f}%)",
        f"📊 Profit Factor:     {profit_factor:.2f}",
        f"📈 Avg vincita:       {avg_win:+.4f} USDT",
        f"📉 Avg perdita:       {avg_loss:+.4f} USDT",
        f"🥇 Miglior trade:     {best:+.4f} USDT",
        f"💀 Peggior trade:     {worst:+.4f} USDT",
        f"",
        f"── Breakdown per simbolo ──",
    ]
    for sym, s in sorted_syms:
        wr    = s["wins"] / s["n"] * 100
        emoji = "✅" if s["pnl"] > 0 else "❌"
        lines.append(
            f"{emoji} {sym:<16} {s['pnl']:+.4f} USDT   {s['n']} trade  WR {wr:.0f}%"
        )

    return "\n".join(lines)


@mcp.tool()
async def get_risk_exposure() -> str:
    """
    Analisi del rischio sulle posizioni aperte.
    Per ogni posizione mostra: distanza % dal mark price allo SL,
    perdita stimata in USDT se lo SL viene colpito, e % dell'equity a rischio.
    Evidenzia posizioni senza SL. Utile per decidere se aggiustare gli SL.
    """
    pos_data = await _get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if pos_data.get("retCode") != 0:
        return f"Errore API: {pos_data.get('retMsg')}"

    positions = [p for p in pos_data["result"]["list"] if _f(p.get("size", 0)) > 0]
    if not positions:
        return "Nessuna posizione aperta."

    # Prendi equity per calcolare % a rischio
    bal_data = await _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    equity = 0.0
    if bal_data.get("retCode") == 0:
        coins = bal_data["result"]["list"][0].get("coin", [])
        usdt  = next((c for c in coins if c["coin"] == "USDT"), None)
        if usdt:
            equity = _f(usdt.get("equity", 0))

    lines = []
    total_risk_usdt = 0.0

    for p in positions:
        sym   = p["symbol"]
        side  = p["side"]
        size  = _f(p["size"])
        entry = _f(p["avgPrice"])
        mark  = _f(p["markPrice"])
        sl    = _f(p.get("stopLoss", 0))
        upnl  = _f(p["unrealisedPnl"])
        emoji = "📉" if side == "Sell" else "📈"

        if sl > 0:
            # Distanza % da mark a SL (quanta strada deve fare il prezzo per colpire SL)
            if side == "Sell":  # SHORT: SL è sopra mark
                sl_dist_pct  = (sl - mark) / mark * 100
            else:               # LONG: SL è sotto mark
                sl_dist_pct  = (mark - sl) / mark * 100

            # Perdita massima dal prezzo attuale se SL viene colpito
            loss_from_mark = size * abs(sl - mark)
            total_risk_usdt += loss_from_mark
            risk_pct = loss_from_mark / equity * 100 if equity > 0 else 0.0

            bar = "🟢" if sl_dist_pct > 3 else ("🟡" if sl_dist_pct > 1 else "🔴")
            lines.append(
                f"{emoji} {sym} {side.upper()} × {size}\n"
                f"   Entry: {entry} | Mark: {mark} | SL: {sl}\n"
                f"   {bar} Distanza SL: {sl_dist_pct:.2f}%  |  PnL attuale: {upnl:+.4f} USDT\n"
                f"   ⚠️  Max ulteriore perdita se SL colpito: -{loss_from_mark:.4f} USDT ({risk_pct:.1f}% equity)"
            )
        else:
            lines.append(
                f"{emoji} {sym} {side.upper()} × {size}\n"
                f"   Entry: {entry} | Mark: {mark} | PnL: {upnl:+.4f} USDT\n"
                f"   🚨 NESSUN STOP LOSS IMPOSTATO"
            )

    total_risk_pct = total_risk_usdt / equity * 100 if equity > 0 else 0.0
    lines.append(
        f"────────────────────────────────\n"
        f"💰 Equity: {equity:.4f} USDT\n"
        f"⚠️  Rischio totale aperto: -{total_risk_usdt:.4f} USDT ({total_risk_pct:.1f}% equity)"
    )
    return "\n\n".join(lines)


@mcp.tool()
async def get_bot_summary() -> str:
    """
    Riepilogo completo dei bot: bilancio, posizioni aperte con analisi rischio,
    ultimi 10 trade chiusi e statistiche degli ultimi 7 giorni.
    Punto di partenza ideale per capire lo stato attuale dei bot.
    """
    balance_str   = await get_wallet_balance()
    positions_str = await get_open_positions()
    risk_str      = await get_risk_exposure()
    pnl_str       = await get_recent_closed_pnl(10)
    summary_str   = await get_pnl_summary(7)

    return (
        "═══ BILANCIO ═══\n" + balance_str +
        "\n\n═══ POSIZIONI APERTE ═══\n" + positions_str +
        "\n\n═══ RISCHIO APERTO ═══\n" + risk_str +
        "\n\n═══ ULTIMI 10 TRADE ═══\n" + pnl_str +
        "\n\n═══ STATISTICHE 7 GIORNI ═══\n" + summary_str
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
