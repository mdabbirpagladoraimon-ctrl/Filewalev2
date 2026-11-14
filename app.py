import os
import re
import secrets
import logging
import threading
import asyncio
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, abort, send_from_directory,
)
from jinja2 import BaseLoader, TemplateNotFound
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import Config
from models import init_db, get_db, ensure_admin, enc, dec, BANNER_DIR
from importer import (
    parse_csv_bytes, import_csv_rows, import_one, import_pipe_rows,
)
from pipe_parser import is_pipe_line, parse_pipe_text
from notifier import notify_deposit, notify_purchase


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("filewale")


ALLOWED_MEDIA = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}


# ======================================================================
#                       TELEGRAM BOT (inline)
# ======================================================================
def _start_bot_thread():
    if not Config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN missing — bot disabled")
        return

    def _runner():
        try:
            asyncio.run(_bot_main())
        except Exception as e:
            log.exception("bot thread crashed: %s", e)

    t = threading.Thread(target=_runner, daemon=True, name="tg-bot")
    t.start()
    log.info("telegram bot thread started")


async def _bot_main():
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, ConversationHandler,
        CallbackQueryHandler, ContextTypes, filters,
    )

    blog = logging.getLogger("filewale-bot")

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = (
            "<b>FILEWALE PREMIUM — bot</b>\n\n"
            "<b>commands</b>\n"
            "/add — add single account (guided)\n"
            "/bulk — attach a .csv / .txt file\n"
            "/stock — last 15 unsold accounts\n"
            "/stats — stock / users / revenue\n"
            "/price — bulk-price update\n"
            "/del &lt;id&gt; — delete unsold account\n"
            "/cancel — abort current flow\n"
            "/help — this message\n\n"
            "<b>csv format</b>\n"
            "<code>email,password,plan_category,cost,notes</code>\n"
            "(only email,password required)\n\n"
            "<b>pipe format</b>\n"
            "paste lines or upload .txt — auto-detected:\n"
            "<code>uid|token|username|region|...|client_url</code>\n"
            "username prefix: <code>zyeleo_</code>→gold, "
            "<code>zyel1_</code>→silver\n\n"
            "<b>pricing</b>\n"
            "gold ৳160 / $22 · silver ৳80 / $11"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await cmd_start(update, ctx)

    async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data.clear()
        await update.message.reply_text("cancelled.")
        return ConversationHandler.END

    (ADD_EMAIL, ADD_PASS, ADD_CAT) = range(3)

    async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add"] = {}
        await update.message.reply_text(
            "step 1/3 — send the account <b>email</b> (or /cancel).",
            parse_mode=ParseMode.HTML,
        )
        return ADD_EMAIL

    async def add_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add"]["email"] = update.message.text.strip()
        await update.message.reply_text(
            "step 2/3 — send the <b>password</b>.", parse_mode=ParseMode.HTML,
        )
        return ADD_PASS

    async def add_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add"]["password"] = update.message.text
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("auto", callback_data="addcat:auto"),
            InlineKeyboardButton("silver ৳80", callback_data="addcat:silver"),
            InlineKeyboardButton("gold ৳160", callback_data="addcat:gold"),
        ]])
        await update.message.reply_text(
            "step 3/3 — pick <b>category</b>.",
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )
        return ADD_CAT

    async def add_cat_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        cat = q.data.split(":", 1)[1]
        d = ctx.user_data.get("add") or {}
        if not d.get("email"):
            await q.edit_message_text("session lost — /add again.")
            return ConversationHandler.END
        await q.edit_message_text(
            f"checking <code>{d['email']}</code>…",
            parse_mode=ParseMode.HTML,
        )
        conn = get_db()
        try:
            ok, err, info = import_one(
                conn, d["email"], d["password"], plan_category=cat,
            )
        finally:
            conn.close()
        ctx.user_data.clear()
        if ok:
            await q.edit_message_text(
                f"✅ saved\n"
                f"email: <code>{d['email']}</code>\n"
                f"plan: {info['plan_name']} [{info['category']}]\n"
                f"sell: ৳{info['selling_price']:.0f}\n"
                f"status: {info['status']}\n"
                f"fw wallet: {info['wallet']['currency']} "
                f"{info['wallet']['balance']:.2f}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await q.edit_message_text(f"❌ failed: {err}")
        return ConversationHandler.END

    async def cmd_bulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "attach a <b>.csv</b> or <b>.txt</b> file in the next message.\n\n"
            "csv: <code>email,password,plan_category,cost,notes</code>\n"
            "txt: pipe lines (auto-detected)\n\n"
            "max 15 MB. /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_bulk_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        doc = update.message.document
        if not doc:
            return
        name = (doc.file_name or "").lower()
        if not (name.endswith(".csv") or name.endswith(".txt")):
            await update.message.reply_text("send a .csv or .txt file.")
            return
        if doc.file_size and doc.file_size > 15 * 1024 * 1024:
            await update.message.reply_text("file too big (max 15 MB).")
            return
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
        text = raw.decode("utf-8-sig", errors="replace")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if is_pipe_line(first):
            await _import_pipe_text(update, text)
            return
        fieldnames, rows = parse_csv_bytes(raw)
        if rows is None:
            got = ", ".join(fieldnames or []) or "(none)"
            await update.message.reply_text(
                f"unrecognised file.\ncsv header found: {got}"
            )
            return
        if not rows:
            await update.message.reply_text("file has no data rows.")
            return
        await update.message.reply_text(
            f"CSV detected — importing {len(rows)} rows (API check per row)…"
        )
        conn = get_db()
        try:
            result = import_csv_rows(conn, rows)
        finally:
            conn.close()
        await _reply_summary(update, result, mode="csv")

    async def _import_pipe_text(update: Update, text: str):
        rows = parse_pipe_text(text)
        if not rows:
            await update.message.reply_text("no valid pipe lines found.")
            return
        await update.message.reply_text(
            f"pipe detected — importing {len(rows)} accounts (no API check)…"
        )
        conn = get_db()
        try:
            result = import_pipe_rows(conn, rows)
        finally:
            conn.close()
        await _reply_summary(update, result, mode="pipe")

    async def _reply_summary(update: Update, result, mode="csv"):
        lines = [
            f"<b>import done ({mode})</b>",
            f"added:   <b>{result['added']}</b>",
            f"skipped: {result.get('skipped', 0)}",
            f"failed:  {len(result['failed'])}",
        ]
        if result["failed"]:
            lines.append("")
            lines.append("<b>failures</b>")
            for m in result["failed"][:15]:
                lines.append(f"• {m}")
            if len(result["failed"]) > 15:
                lines.append(f"… and {len(result['failed']) - 15} more")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
        )

    async def handle_pasted_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = update.message.text or ""
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if not is_pipe_line(first):
            return
        await _import_pipe_text(update, text)

    async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id, plan_name, plan_category, selling_price, status "
                "FROM accounts WHERE sold=0 ORDER BY id DESC LIMIT 15"
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) c FROM accounts WHERE sold=0"
            ).fetchone()["c"]
        finally:
            conn.close()
        if not rows:
            await update.message.reply_text("no unsold accounts.")
            return
        lines = [f"<b>stock</b> (latest 15 of {total})"]
        for r in rows:
            lines.append(
                f"#{r['id']} [{r['plan_category']}] "
                f"{r['plan_name'] or '—'} — ৳{r['selling_price']:.0f} "
                f"({r['status']})"
            )
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
        )

    async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        conn = get_db()
        try:
            users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            stock = conn.execute(
                "SELECT COUNT(*) c FROM accounts WHERE sold=0"
            ).fetchone()["c"]
            sold = conn.execute(
                "SELECT COUNT(*) c FROM accounts WHERE sold=1"
            ).fetchone()["c"]
            pending = conn.execute(
                "SELECT COUNT(*) c FROM deposits WHERE status='PENDING'"
            ).fetchone()["c"]
            revenue = conn.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM purchases"
            ).fetchone()["s"]
            wallet_total = conn.execute(
                "SELECT COALESCE(SUM(balance),0) s FROM users WHERE is_admin=0"
            ).fetchone()["s"]
            fw_total = conn.execute(
                "SELECT COALESCE(SUM(fw_balance),0) s FROM accounts WHERE sold=0"
            ).fetchone()["s"]
        finally:
            conn.close()
        text = (
            f"<b>stats</b>\n"
            f"users: <b>{users}</b>\n"
            f"stock: <b>{stock}</b>\n"
            f"sold: {sold}\n"
            f"pending deposits: <b>{pending}</b>\n"
            f"revenue: <b>৳{revenue:.2f}</b>\n"
            f"user wallet total: ৳{wallet_total:.2f}\n"
            f"stock FW balance: ${fw_total:.2f}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    (PRICE_SILVER, PRICE_GOLD) = range(10, 12)

    async def price_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        conn = get_db()
        try:
            silver = conn.execute(
                "SELECT COUNT(*) c FROM accounts "
                "WHERE plan_category='silver' AND sold=0"
            ).fetchone()["c"]
            gold = conn.execute(
                "SELECT COUNT(*) c FROM accounts "
                "WHERE plan_category='gold' AND sold=0"
            ).fetchone()["c"]
        finally:
            conn.close()
        await update.message.reply_text(
            f"silver in stock: {silver}\ngold in stock: {gold}\n\n"
            f"send new <b>silver</b> price in BDT (or 'skip').",
            parse_mode=ParseMode.HTML,
        )
        return PRICE_SILVER

    async def price_silver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        t = update.message.text.strip().lower()
        if t == "skip":
            ctx.user_data["silver_price"] = None
        else:
            try:
                ctx.user_data["silver_price"] = float(t)
            except ValueError:
                await update.message.reply_text(
                    "invalid. send a number or 'skip'."
                )
                return PRICE_SILVER
        await update.message.reply_text(
            "send new <b>gold</b> price in BDT (or 'skip').",
            parse_mode=ParseMode.HTML,
        )
        return PRICE_GOLD

    async def price_gold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        t = update.message.text.strip().lower()
        gold = None
        if t != "skip":
            try:
                gold = float(t)
            except ValueError:
                await update.message.reply_text(
                    "invalid. send a number or 'skip'."
                )
                return PRICE_GOLD
        silver = ctx.user_data.get("silver_price")
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            upd = {"silver": 0, "gold": 0}
            if silver and silver > 0:
                cur = conn.execute(
                    "UPDATE accounts SET selling_price=? "
                    "WHERE plan_category='silver' AND sold=0", (silver,),
                )
                upd["silver"] = cur.rowcount
            if gold and gold > 0:
                cur = conn.execute(
                    "UPDATE accounts SET selling_price=? "
                    "WHERE plan_category='gold' AND sold=0", (gold,),
                )
                upd["gold"] = cur.rowcount
            conn.execute("COMMIT")
        except Exception as e:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            await update.message.reply_text(f"failed: {e}")
            ctx.user_data.clear()
            return ConversationHandler.END
        finally:
            conn.close()
        ctx.user_data.clear()
        await update.message.reply_text(
            f"updated {upd['silver']} silver + {upd['gold']} gold accounts."
        )
        return ConversationHandler.END

    async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args or not ctx.args[0].isdigit():
            await update.message.reply_text("usage: /del <account_id>")
            return
        acc_id = int(ctx.args[0])
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id, sold FROM accounts WHERE id=?", (acc_id,)
            ).fetchone()
            if not row:
                await update.message.reply_text("no such account.")
                return
            if row["sold"]:
                await update.message.reply_text(
                    "account is sold — cannot delete."
                )
                return
            conn.execute(
                "DELETE FROM accounts WHERE id=? AND sold=0", (acc_id,),
            )
        finally:
            conn.close()
        await update.message.reply_text(f"deleted account #{acc_id}.")

    app_tg = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_EMAIL: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, add_email)],
            ADD_PASS:  [MessageHandler(
                filters.TEXT & ~filters.COMMAND, add_pass)],
            ADD_CAT:   [CallbackQueryHandler(
                add_cat_cb, pattern=r"^addcat:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        allow_reentry=True,
    )

    price_conv = ConversationHandler(
        entry_points=[CommandHandler("price", price_start)],
        states={
            PRICE_SILVER: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, price_silver)],
            PRICE_GOLD:   [MessageHandler(
                filters.TEXT & ~filters.COMMAND, price_gold)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
    )

    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("cancel", cmd_cancel))
    app_tg.add_handler(add_conv)
    app_tg.add_handler(price_conv)
    app_tg.add_handler(CommandHandler("bulk", cmd_bulk))
    app_tg.add_handler(CommandHandler("stock", cmd_stock))
    app_tg.add_handler(CommandHandler("stats", cmd_stats))
    app_tg.add_handler(CommandHandler("del", cmd_del))
    app_tg.add_handler(MessageHandler(
        filters.Document.FileExtension("csv")
        | filters.Document.FileExtension("txt"),
        cmd_bulk_file,
    ))
    app_tg.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_pasted_text,
    ))

    blog.info("bot polling started (from app.py)")
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    while True:
        await asyncio.sleep(3600)


# ======================================================================
#                       FLASK APP
# ======================================================================
class SingleFileLoader(BaseLoader):
    def __init__(self, path):
        self.path = path
        self.cache = {}
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        parts = re.split(r"\{#\s*TEMPLATE:\s*([\w\-\.]+)\s*#\}", raw)
        for i in range(1, len(parts), 2):
            self.cache[parts[i]] = parts[i + 1]

    def get_source(self, environment, template):
        if template not in self.cache:
            raise TemplateNotFound(template)
        return self.cache[template], self.path, lambda: True


app = Flask(__name__, static_folder=None)
app.config.from_object(Config)
app.jinja_loader = SingleFileLoader(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.html")
)

init_db()
ensure_admin(
    Config.ADMIN_USERNAME, Config.ADMIN_PASSWORD,
    aliases=("ZYLECO", "ZYELEO"),
)


# ======================================================================
#     bot starts ONCE — file lock prevents multi-worker duplication
# ======================================================================
def _try_start_bot_once():
    """Start bot in exactly one worker. Others skip via file lock."""
    if not Config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN missing — bot disabled")
        return

    try:
        import fcntl
    except ImportError:
        # non-linux dev — single-process assumption
        _start_bot_thread()
        return

    lock_path = os.path.join(BANNER_DIR, ".bot.lock")
    try:
        f = open(lock_path, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        globals()["_BOT_LOCK_FD"] = f
        _start_bot_thread()
    except (IOError, OSError):
        log.info("bot lock held by another worker — skipping this one")


if os.environ.get("RUN_BOT", "1") == "1":
    _try_start_bot_once()


# ======================================================================
#                       ROUTES
# ======================================================================
@app.route("/static.css")
def static_css():
    return send_from_directory(".", "static.css", mimetype="text/css")


@app.route("/static.js")
def static_js():
    return send_from_directory(".", "static.js", mimetype="application/javascript")


@app.route("/logo.jpg")
def serve_logo():
    return send_from_directory(".", "logo.jpg", mimetype="image/jpeg")


@app.route("/banners/<path:filename>")
def serve_banner(filename):
    safe = secure_filename(filename)
    return send_from_directory(BANNER_DIR, safe)


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def require_csrf():
    token = session.get("_csrf")
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF")
    if not token or not sent or not secrets.compare_digest(token, sent):
        abort(400, "CSRF token invalid")


app.jinja_env.globals["csrf_token"] = csrf_token


@app.context_processor
def inject_globals():
    user = None
    if session.get("uid"):
        c = get_db()
        user = c.execute(
            "SELECT id, username, balance, is_admin FROM users WHERE id=?",
            (session["uid"],),
        ).fetchone()
        c.close()
    settings = {}
    c = get_db()
    for row in c.execute("SELECT key, value FROM settings"):
        settings[row["key"]] = row["value"]
    c.close()
    return {
        "current_user": user,
        "is_admin": bool(session.get("admin")),
        "cfg": Config,
        "site": settings,
    }


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*a, **k)
    return w


@app.route("/")
def home():
    c = get_db()
    stock = c.execute("SELECT COUNT(*) c FROM accounts WHERE sold=0").fetchone()["c"]
    c.close()
    return render_template("home.html", stock_count=stock)


@app.route("/store")
def store():
    return _render_store(None)


@app.route("/store/silver")
def silver():
    return _render_store("silver")


@app.route("/store/golden")
def golden():
    return _render_store("gold")


def _render_store(cat):
    c = get_db()
    q = "SELECT * FROM accounts WHERE sold=0"
    args = []
    if cat:
        q += " AND plan_category=?"
        args.append(cat)
    q += " ORDER BY plan_category DESC, created_at DESC"
    rows = c.execute(q, args).fetchall()
    c.close()
    items = []
    for r in rows:
        days_left = None
        if r["expiry_date"]:
            try:
                end = datetime.fromisoformat(str(r["expiry_date"]).replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                days_left = max(0, (end - datetime.now(timezone.utc)).days)
            except Exception:
                days_left = None
        keys = r.keys()
        items.append({
            "id": r["id"],
            "plan_name": r["plan_name"] or "FILEWALE PLAN",
            "plan_category": r["plan_category"],
            "original_price": r["original_price"],
            "original_currency": r["original_currency"],
            "selling_price": r["selling_price"],
            "status": r["status"],
            "days_left": days_left,
            "expiry_date": r["expiry_date"],
            "discount": 50,
            "fw_balance": r["fw_balance"] if "fw_balance" in keys else 0,
            "fw_currency": r["fw_currency"] if "fw_currency" in keys else "USD",
        })
    return render_template("store.html", items=items, category=cat)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        require_csrf()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        email = (request.form.get("email") or "").strip() or None
        if not username or len(password) < 6:
            flash("username required, password >= 6 chars", "err")
            return redirect(url_for("register"))
        c = get_db()
        try:
            c.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?,?,?)",
                (username, email, generate_password_hash(password)),
            )
            row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        except Exception:
            c.close()
            flash("username taken", "err")
            return redirect(url_for("register"))
        c.close()
        session["uid"] = row["id"]
        session["uname"] = username
        return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        require_csrf()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        c = get_db()
        row = c.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username,)
        ).fetchone()
        c.close()
        if not row or not check_password_hash(row["password_hash"], password):
            flash("invalid credentials", "err")
            return redirect(url_for("login"))
        session["uid"] = row["id"]
        session["uname"] = row["username"]
        if row["is_admin"]:
            session["admin"] = True
        return redirect(request.args.get("next") or url_for("home"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/wallet")
@login_required
def wallet():
    c = get_db()
    txns = c.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 40",
        (session["uid"],),
    ).fetchall()
    deposits = c.execute(
        "SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (session["uid"],),
    ).fetchall()
    c.close()
    return render_template("wallet.html", txns=txns, deposits=deposits)


@app.route("/add-balance", methods=["GET", "POST"])
@login_required
def add_balance():
    if request.method == "POST":
        require_csrf()
        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            amount = 0
        method = (request.form.get("method") or "").lower()
        txn_id = (request.form.get("txn_id") or "").strip()
        if amount <= 0 or method not in ("bkash", "nagad") or len(txn_id) < 4:
            flash("invalid deposit data", "err")
            return redirect(url_for("add_balance"))
        c = get_db()
        try:
            c.execute(
                "INSERT INTO deposits (user_id, amount, method, txn_id) VALUES (?,?,?,?)",
                (session["uid"], amount, method, txn_id),
            )
        except Exception:
            c.close()
            flash("this transaction id is already submitted", "err")
            return redirect(url_for("add_balance"))
        c.close()
        flash("deposit submitted — pending admin verification", "ok")
        return redirect(url_for("wallet"))
    return render_template("add_balance.html")


@app.route("/purchases")
@login_required
def purchases():
    c = get_db()
    rows = c.execute(
        """SELECT p.*, a.plan_name, a.plan_category, a.email_enc, a.password_enc,
                  a.expiry_date FROM purchases p
           JOIN accounts a ON a.id=p.account_id
           WHERE p.user_id=? ORDER BY p.id DESC""",
        (session["uid"],),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "plan_name": r["plan_name"],
            "plan_category": r["plan_category"],
            "amount": r["amount"],
            "email": dec(r["email_enc"]),
            "password": dec(r["password_enc"]),
            "expiry_date": r["expiry_date"],
            "created_at": r["created_at"],
        })
    return render_template("purchases.html", purchases=out)


@app.route("/transactions")
@login_required
def transactions():
    c = get_db()
    rows = c.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 200",
        (session["uid"],),
    ).fetchall()
    c.close()
    return render_template("transactions.html", txns=rows)


@app.route("/profile")
@login_required
def profile():
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    c.close()
    return render_template("profile.html", u=u)


@app.route("/buy/<int:acc_id>", methods=["POST"])
@login_required
def buy(acc_id):
    require_csrf()
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        acc = c.execute("SELECT * FROM accounts WHERE id=? AND sold=0", (acc_id,)).fetchone()
        if not acc:
            c.execute("ROLLBACK")
            flash("account no longer available", "err")
            return redirect(url_for("store"))
        user = c.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
        if user["balance"] < acc["selling_price"]:
            c.execute("ROLLBACK")
            flash("insufficient balance — add balance first", "err")
            return redirect(url_for("wallet"))
        new_balance = round(user["balance"] - acc["selling_price"], 2)
        now = datetime.utcnow().isoformat(timespec="seconds")
        c.execute("UPDATE users SET balance=? WHERE id=?", (new_balance, user["id"]))
        c.execute(
            "UPDATE accounts SET sold=1, sold_to=?, sold_at=? WHERE id=?",
            (user["id"], now, acc["id"]),
        )
        c.execute(
            "INSERT INTO purchases (user_id, account_id, amount) VALUES (?,?,?)",
            (user["id"], acc["id"], acc["selling_price"]),
        )
        c.execute(
            "INSERT INTO transactions (user_id, type, amount, balance_after, reason) "
            "VALUES (?,?,?,?,?)",
            (user["id"], "purchase", -acc["selling_price"], new_balance,
             f"buy account #{acc['id']}"),
        )
        c.execute("COMMIT")
        flash("purchase successful — credentials revealed in My Purchases", "ok")
        return redirect(url_for("purchases"))
    except Exception as e:
        try: c.execute("ROLLBACK")
        except Exception: pass
        flash(f"purchase failed: {e}", "err")
        return redirect(url_for("store"))
    finally:
        c.close()


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        require_csrf()
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        c = get_db()
        row = c.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?) AND is_admin=1", (u,)
        ).fetchone()
        c.close()
        if row and check_password_hash(row["password_hash"], p):
            session["uid"] = row["id"]
            session["uname"] = row["username"]
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("invalid admin credentials", "err")
        return redirect(url_for("admin_login"))
    return render_template("admin_login.html")


@app.route("/admin")
@admin_required
def admin_dashboard():
    c = get_db()
    stats = {
        "users": c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "stock": c.execute("SELECT COUNT(*) c FROM accounts WHERE sold=0").fetchone()["c"],
        "sold": c.execute("SELECT COUNT(*) c FROM accounts WHERE sold=1").fetchone()["c"],
        "pending": c.execute("SELECT COUNT(*) c FROM deposits WHERE status='PENDING'").fetchone()["c"],
        "revenue": c.execute("SELECT COALESCE(SUM(amount),0) s FROM purchases").fetchone()["s"],
    }
    c.close()
    return render_template("admin_dashboard.html", stats=stats)


@app.route("/api/admin/total-balance")
@admin_required
def admin_total_balance():
    c = get_db()
    total = c.execute(
        "SELECT COALESCE(SUM(balance),0) s FROM users WHERE is_admin=0"
    ).fetchone()["s"]
    users = c.execute(
        "SELECT COUNT(*) c FROM users WHERE is_admin=0"
    ).fetchone()["c"]
    fw_total = c.execute(
        "SELECT COALESCE(SUM(fw_balance),0) s FROM accounts WHERE sold=0"
    ).fetchone()["s"]
    c.close()
    return jsonify({
        "wallet_total": round(total, 2),
        "user_count": users,
        "fw_stock_total": round(fw_total, 2),
    })


@app.route("/admin/stock")
@admin_required
def admin_stock():
    c = get_db()
    rows = c.execute("SELECT * FROM accounts ORDER BY sold ASC, id DESC").fetchall()
    c.close()
    return render_template("admin_stock.html", rows=rows)


@app.route("/admin/add-account", methods=["GET", "POST"])
@admin_required
def admin_add_account():
    if request.method == "POST":
        require_csrf()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        category = (request.form.get("plan_category") or "auto").lower()
        cost = request.form.get("cost")
        cost = float(cost) if cost else 0
        notes = (request.form.get("notes") or "").strip()
        if not email or not password:
            flash("email and password required", "err")
            return redirect(url_for("admin_add_account"))
        c = get_db()
        try:
            ok, err, info = import_one(
                c, email, password,
                plan_category=category, cost=cost, notes=notes,
            )
        finally:
            c.close()
        if not ok:
            flash(f"add failed: {err}", "err")
            return redirect(url_for("admin_add_account"))
        flash(
            f"added — {info['plan_name']} [{info['category']}] "
            f"sell ৳{info['selling_price']:.0f} ({info['status']})",
            "ok",
        )
        return redirect(url_for("admin_stock"))
    return render_template("admin_add_account.html")


@app.route("/admin/bulk-import", methods=["GET", "POST"])
@admin_required
def admin_bulk_import():
    if request.method == "POST":
        require_csrf()
        f = request.files.get("csv") or request.files.get("file")
        if not f or not f.filename:
            flash("no file uploaded", "err")
            return redirect(url_for("admin_bulk_import"))
        raw = f.read()
        text = raw.decode("utf-8-sig", errors="replace")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        c = get_db()
        try:
            if is_pipe_line(first):
                rows = parse_pipe_text(text)
                if not rows:
                    flash("no valid pipe lines found", "err")
                    return redirect(url_for("admin_bulk_import"))
                result = import_pipe_rows(c, rows)
                mode = "pipe"
            else:
                fieldnames, rows = parse_csv_bytes(raw)
                if rows is None:
                    got = ", ".join(fieldnames or []) or "(none)"
                    flash(f"unrecognised file. csv header: {got}", "err")
                    return redirect(url_for("admin_bulk_import"))
                result = import_csv_rows(c, rows)
                mode = "csv"
        finally:
            c.close()
        flash(
            f"[{mode}] imported {result['added']}, skipped "
            f"{result.get('skipped', 0)}, failed {len(result['failed'])}",
            "ok",
        )
        for m in result["failed"][:12]:
            flash(m, "err")
        return redirect(url_for("admin_stock"))
    return render_template("admin_bulk_import.html")


@app.route("/admin/bulk-price", methods=["GET", "POST"])
@admin_required
def admin_bulk_price():
    if request.method == "POST":
        require_csrf()
        try:
            silver_price = float(request.form.get("silver_price") or 0)
            gold_price = float(request.form.get("gold_price") or 0)
        except ValueError:
            flash("invalid prices", "err")
            return redirect(url_for("admin_bulk_price"))
        c = get_db()
        try:
            c.execute("BEGIN IMMEDIATE")
            upd = {"silver": 0, "gold": 0}
            if silver_price > 0:
                cur = c.execute(
                    "UPDATE accounts SET selling_price=? "
                    "WHERE plan_category='silver' AND sold=0",
                    (silver_price,),
                )
                upd["silver"] = cur.rowcount
            if gold_price > 0:
                cur = c.execute(
                    "UPDATE accounts SET selling_price=? "
                    "WHERE plan_category='gold' AND sold=0",
                    (gold_price,),
                )
                upd["gold"] = cur.rowcount
            c.execute("COMMIT")
            flash(f"updated {upd['silver']} silver + {upd['gold']} gold accounts", "ok")
        except Exception as e:
            try: c.execute("ROLLBACK")
            except Exception: pass
            flash(f"failed: {e}", "err")
        finally:
            c.close()
        return redirect(url_for("admin_stock"))
    c = get_db()
    counts = {
        "silver": c.execute(
            "SELECT COUNT(*) c FROM accounts WHERE plan_category='silver' AND sold=0"
        ).fetchone()["c"],
        "gold": c.execute(
            "SELECT COUNT(*) c FROM accounts WHERE plan_category='gold' AND sold=0"
        ).fetchone()["c"],
        "silver_avg": c.execute(
            "SELECT COALESCE(AVG(selling_price),0) a FROM accounts WHERE plan_category='silver' AND sold=0"
        ).fetchone()["a"],
        "gold_avg": c.execute(
            "SELECT COALESCE(AVG(selling_price),0) a FROM accounts WHERE plan_category='gold' AND sold=0"
        ).fetchone()["a"],
    }
    c.close()
    return render_template("admin_bulk_price.html", counts=counts)


@app.route("/admin/edit-account/<int:acc_id>", methods=["GET", "POST"])
@admin_required
def admin_edit_account(acc_id):
    c = get_db()
    row = c.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        c.close()
        abort(404)
    if request.method == "POST":
        require_csrf()
        selling = request.form.get("selling_price")
        category = request.form.get("plan_category")
        notes = request.form.get("notes")
        status = request.form.get("status")
        try:
            selling_f = float(selling) if selling else row["selling_price"]
        except ValueError:
            selling_f = row["selling_price"]
        c.execute(
            "UPDATE accounts SET selling_price=?, plan_category=?, notes=?, status=? WHERE id=?",
            (selling_f,
             category if category in ("silver", "gold") else row["plan_category"],
             notes if notes is not None else row["notes"],
             status or row["status"], acc_id),
        )
        c.close()
        flash("account updated", "ok")
        return redirect(url_for("admin_stock"))
    c.close()
    return render_template("admin_edit_account.html", row=row)


@app.route("/admin/delete-account/<int:acc_id>", methods=["POST"])
@admin_required
def admin_delete_account(acc_id):
    require_csrf()
    c = get_db()
    c.execute("DELETE FROM accounts WHERE id=? AND sold=0", (acc_id,))
    c.close()
    flash("account removed", "ok")
    return redirect(url_for("admin_stock"))


@app.route("/admin/users")
@admin_required
def admin_users():
    c = get_db()
    rows = c.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    c.close()
    return render_template("admin_users.html", rows=rows)


@app.route("/admin/wallet-op", methods=["POST"])
@admin_required
def admin_wallet_op():
    require_csrf()
    try:
        uid = int(request.form.get("user_id"))
        amount = float(request.form.get("amount"))
    except (ValueError, TypeError):
        flash("invalid amount/user", "err")
        return redirect(url_for("admin_users"))
    reason = (request.form.get("reason") or "manual adjust").strip()
    if amount == 0:
        flash("amount cannot be zero", "err")
        return redirect(url_for("admin_users"))
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            c.execute("ROLLBACK")
            flash("user not found", "err")
            return redirect(url_for("admin_users"))
        new_balance = round(u["balance"] + amount, 2)
        if new_balance < 0:
            c.execute("ROLLBACK")
            flash("balance cannot go negative", "err")
            return redirect(url_for("admin_users"))
        c.execute("UPDATE users SET balance=? WHERE id=?", (new_balance, uid))
        c.execute(
            "INSERT INTO transactions (user_id, type, amount, balance_after, reason) "
            "VALUES (?,?,?,?,?)",
            (uid, "admin_credit" if amount > 0 else "admin_debit",
             amount, new_balance, reason),
        )
        c.execute("COMMIT")
        flash("balance updated", "ok")
    except Exception as e:
        try: c.execute("ROLLBACK")
        except Exception: pass
        flash(f"failed: {e}", "err")
    finally:
        c.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/deposits")
@admin_required
def admin_deposits():
    c = get_db()
    rows = c.execute(
        """SELECT d.*, u.username FROM deposits d
           JOIN users u ON u.id=d.user_id ORDER BY d.id DESC"""
    ).fetchall()
    c.close()
    return render_template("admin_deposits.html", rows=rows)


@app.route("/admin/deposits/<int:dep_id>/<action>", methods=["POST"])
@admin_required
def admin_deposit_action(dep_id, action):
    require_csrf()
    if action not in ("approve", "reject"):
        abort(400)
    note = (request.form.get("admin_note") or "").strip()
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        d = c.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
        if not d or d["status"] != "PENDING":
            c.execute("ROLLBACK")
            flash("deposit not pending", "err")
            return redirect(url_for("admin_deposits"))
        if action == "reject":
            c.execute(
                "UPDATE deposits SET status='REJECTED', admin_note=?, "
                "verified_at=datetime('now'), verified_by=? WHERE id=?",
                (note, session["uid"], dep_id),
            )
            c.execute("COMMIT")
            flash("deposit rejected", "ok")
            return redirect(url_for("admin_deposits"))
        u = c.execute("SELECT * FROM users WHERE id=?", (d["user_id"],)).fetchone()
        new_balance = round(u["balance"] + d["amount"], 2)
        c.execute("UPDATE users SET balance=? WHERE id=?", (new_balance, u["id"]))
        c.execute(
            "INSERT INTO transactions (user_id, type, amount, balance_after, reason) "
            "VALUES (?,?,?,?,?)",
            (u["id"], "deposit", d["amount"], new_balance,
             f"{d['method']} deposit #{d['id']}"),
        )
        c.execute(
            "UPDATE deposits SET status='APPROVED', admin_note=?, "
            "verified_at=datetime('now'), verified_by=? WHERE id=?",
            (note, session["uid"], dep_id),
        )
        c.execute("COMMIT")
        flash("deposit approved and credited", "ok")
    except Exception as e:
        try: c.execute("ROLLBACK")
        except Exception: pass
        flash(f"failed: {e}", "err")
    finally:
        c.close()
    return redirect(url_for("admin_deposits"))


@app.route("/admin/transactions")
@admin_required
def admin_transactions():
    c = get_db()
    rows = c.execute(
        """SELECT t.*, u.username FROM transactions t
           JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 300"""
    ).fetchall()
    c.close()
    return render_template("admin_transactions.html", rows=rows)


@app.route("/admin/banner", methods=["GET", "POST"])
@admin_required
def admin_banner():
    if request.method == "POST":
        require_csrf()
        c = get_db()

        def set_setting(k, v):
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )

        for color in ("silver", "golden"):
            set_setting(f"{color}_banner_enabled",
                        "1" if request.form.get(f"{color}_banner_enabled") else "0")
            set_setting(f"{color}_banner_text",
                        (request.form.get(f"{color}_banner_text") or "").strip())
            mtype = request.form.get(f"{color}_banner_media_type") or "text"
            if mtype not in ("text", "image", "video"):
                mtype = "text"
            url_input = (request.form.get(f"{color}_banner_media_url") or "").strip()
            f = request.files.get(f"{color}_banner_file")
            if f and f.filename:
                ext = f.filename.rsplit(".", 1)[-1].lower()
                if ext in ALLOWED_MEDIA:
                    mtype = "video" if ext in ("mp4", "webm") else "image"
                    fname = f"{color}_{secrets.token_hex(8)}.{ext}"
                    f.save(os.path.join(BANNER_DIR, fname))
                    url_input = f"/banners/{fname}"
            if request.form.get(f"{color}_clear"):
                url_input = ""
                mtype = "text"
            set_setting(f"{color}_banner_media_type", mtype)
            set_setting(f"{color}_banner_media_url", url_input)
        c.close()
        flash("banners updated", "ok")
        return redirect(url_for("admin_banner"))

    c = get_db()
    s = {row["key"]: row["value"] for row in c.execute("SELECT * FROM settings")}
    c.close()
    return render_template("admin_banner.html", s=s)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        require_csrf()
        c = get_db()
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('site_title', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ((request.form.get("site_title") or "FILEWALE PREMIUM").strip(),),
        )
        c.close()
        flash("settings saved", "ok")
        return redirect(url_for("admin_settings"))
    c = get_db()
    s = {row["key"]: row["value"] for row in c.execute("SELECT * FROM settings")}
    c.close()
    return render_template("admin_settings.html", s=s)


@app.route("/admin/sold")
@admin_required
def admin_sold():
    c = get_db()
    rows = c.execute(
        """SELECT a.*, u.username AS buyer FROM accounts a
           LEFT JOIN users u ON u.id=a.sold_to
           WHERE a.sold=1 ORDER BY a.sold_at DESC"""
    ).fetchall()
    c.close()
    return render_template("admin_sold.html", rows=rows)


@app.route("/api/live-stock")
def live_stock():
    c = get_db()
    total = c.execute("SELECT COUNT(*) c FROM accounts WHERE sold=0").fetchone()["c"]
    live = c.execute("SELECT COUNT(*) c FROM accounts WHERE sold=0 AND status='LIVE'").fetchone()["c"]
    c.close()
    return jsonify({"total": total, "live": live})


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def handle_error(e):
    code = getattr(e, "code", 500)
    msg = "File too large (max 50MB)" if code == 413 else str(e)
    return render_template("error.html", code=code, msg=msg), code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
