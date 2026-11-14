import threading
import requests
from config import Config


def _send(text: str):
    if not Config.TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    # telegram admin ids — none needed, open bot
    return


def notify(text: str):
    if not text:
        return
    threading.Thread(target=_send, args=(text,), daemon=True).start()


def notify_deposit(dep_id, username, amount, method, txn_id):
    pass


def notify_purchase(username, plan_name, amount):
    pass