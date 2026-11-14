"""Parses pipe-delimited Free Fire / ShadowGrow account lines.

Format:
  uid|access_token|username|region|external_id|md5|jwt|account_id|client_url

One line = one account. Pipe accounts skip the checker API (trusted source).
Category is inferred from the username prefix:
  zyeleo_*   → gold
  zyel1_*    → silver
  otherwise  → silver
"""

FIELDS = [
    "uid", "access_token", "username", "region",
    "external_id", "md5", "jwt", "account_id", "client_url",
]


def is_pipe_line(line: str) -> bool:
    return "|" in line and len(line.split("|")) >= 4


def _category_from_username(username: str) -> str:
    low = (username or "").lower()
    if low.startswith("zyeleo_"):
        return "gold"
    return "silver"


def parse_pipe_line(line: str) -> dict | None:
    parts = line.strip().split("|")
    if len(parts) < 4:
        return None
    parts = (parts + [""] * len(FIELDS))[:len(FIELDS)]
    d = dict(zip(FIELDS, parts))

    uid = d["uid"].strip()
    token = d["access_token"].strip()
    if not uid or not token:
        return None

    username = d["username"].strip()
    region = d["region"].strip()

    notes_bits = []
    if username:
        notes_bits.append(f"user={username}")
    if region:
        notes_bits.append(f"region={region}")
    if d["account_id"]:
        notes_bits.append(f"acct={d['account_id']}")

    return {
        "email": uid,
        "password": token,
        "plan_category": _category_from_username(username),
        "cost": 0,
        "notes": " | ".join(notes_bits),
        "_pipe": d,
    }


def parse_pipe_text(text: str) -> list[dict]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        row = parse_pipe_line(line)
        if row:
            out.append(row)
    return out