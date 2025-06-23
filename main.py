import os
import time
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} {msg}")


def notify_telegram(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=data, timeout=10)
            log("✅ Messaggio Telegram inviato.")
        except Exception as e:
            log(f"❌ Errore invio Telegram: {e}")
    else:
        log("⚠️ TELEGRAM_TOKEN o TELEGRAM_CHAT_ID non sono configurati.")


def send_entry_signal(symbol, price, strategy="default"):
    message = f"📈 Segnale di ENTRATA\nAsset: {symbol}\nPrezzo: {price}\nStrategia: {strategy}"
    notify_telegram(message)


def send_exit_signal(symbol, price, strategy="default"):
    message = f"📉 Segnale di USCITA\nAsset: {symbol}\nPrezzo: {price}\nStrategia: {strategy}"
    notify_telegram(message)


if __name__ == "__main__":
    log("🟢 Bot di segnali avviato.")
    
    # Esempi di utilizzo: puoi sostituire questi con logica tua o input da file / webhook
    send_entry_signal("BTCUSDT", "101000.00", strategy="Breakout")
    time.sleep(3)
    send_exit_signal("BTCUSDT", "101800.00", strategy="Take Profit")
