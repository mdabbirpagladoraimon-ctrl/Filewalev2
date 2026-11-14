import threading
import requests
from config import Config


def _send(text: str):
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_ADMIN_IDS:
        return
    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in Config.TELEGRAM_ADMIN_IDS:
        try:
            requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=6,
            )
        except Exception:
            pass


def notify(text: str):
    if not text:
        return
    threading.Thread(target=_send, args=(text,), daemon=True).start()


def notify_deposit(dep_id, username, amount, method, txn_id):
    notify(
        f"💰 <b>New deposit pending</b>\n"
        f"user: <b>{username}</b>\n"
        f"amount: <b>৳{amount:.2f}</b>\n"
        f"method: {method.upper()}\n"
        f"trx: <code>{txn_id}</code>\n"
        f"deposit #{dep_id} — verify in admin panel"
    )


def notify_purchase(username, plan_name, amount):
    notify(
        f"🛒 <b>Purchase</b>\n"
        f"user: <b>{username}</b>\n"
        f"plan: {plan_name}\n"
        f"paid: <b>৳{amount:.2f}</b>"
    )