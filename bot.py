import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

from config import Config
from models import get_db
from importer import (
    parse_csv_bytes, import_csv_rows, import_one, import_pipe_rows,
)
from pipe_parser import is_pipe_line, parse_pipe_text


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("filewale-bot")


def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return uid in Config.TELEGRAM_ADMIN_IDS


async def guard(update: Update) -> bool:
    if is_admin(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            f"not authorized. your telegram id: {update.effective_user.id}"
        )
    return False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = (
        "<b>FILEWALE PREMIUM — admin bot</b>\n\n"
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
        "username prefix decides category: <code>zyeleo_</code>→gold, "
        "<code>zyel1_</code>→silver\n\n"
        "<b>pricing</b>\n"
        "gold ৳160 / $22 · silver ৳80 / $11"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    ctx.user_data.clear()
    await update.message.reply_text("cancelled.")
    return ConversationHandler.END


# ---------------- /add ----------------

(ADD_EMAIL, ADD_PASS, ADD_CAT) = range(3)


async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return ConversationHandler.END
    ctx.user_data["add"] = {}
    await update.message.reply_text(
        "step 1/3 — send the account <b>email</b> (or /cancel).",
        parse_mode=ParseMode.HTML,
    )
    return ADD_EMAIL


async def add_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add"]["email"] = update.message.text.strip()
    await update.message.reply_text("step 2/3 — send the <b>password</b>.",
                                    parse_mode=ParseMode.HTML)
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

    await q.edit_message_text(f"checking <code>{d['email']}</code>…",
                              parse_mode=ParseMode.HTML)

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
            f"fw wallet: {info['wallet']['currency']} {info['wallet']['balance']:.2f}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await q.edit_message_text(f"❌ failed: {err}")
    return ConversationHandler.END


# ---------------- /bulk ----------------

async def cmd_bulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "attach a <b>.csv</b> or <b>.txt</b> file in the next message.\n\n"
        "csv: <code>email,password,plan_category,cost,notes</code>\n"
        "txt: pipe lines (auto-detected)\n\n"
        "max 15 MB. /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_bulk_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
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
            f"unrecognised file.\n"
            f"neither pipe format nor CSV with email,password.\n"
            f"csv header found: {got}"
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
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def handle_pasted_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = update.message.text or ""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not is_pipe_line(first):
        return
    await _import_pipe_text(update, text)


# ---------------- /stock ----------------

async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
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
            f"{r['plan_name'] or '—'} — ৳{r['selling_price']:.0f} ({r['status']})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------- /stats ----------------

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    conn = get_db()
    try:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        stock = conn.execute("SELECT COUNT(*) c FROM accounts WHERE sold=0").fetchone()["c"]
        sold = conn.execute("SELECT COUNT(*) c FROM accounts WHERE sold=1").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM deposits WHERE status='PENDING'").fetchone()["c"]
        revenue = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM purchases").fetchone()["s"]
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


# ---------------- /price ----------------

(PRICE_SILVER, PRICE_GOLD) = range(10, 12)


async def price_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return ConversationHandler.END
    conn = get_db()
    try:
        silver = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE plan_category='silver' AND sold=0"
        ).fetchone()["c"]
        gold = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE plan_category='gold' AND sold=0"
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
            await update.message.reply_text("invalid. send a number or 'skip'.")
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
            await update.message.reply_text("invalid. send a number or 'skip'.")
            return PRICE_GOLD
    silver = ctx.user_data.get("silver_price")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        upd = {"silver": 0, "gold": 0}
        if silver and silver > 0:
            cur = conn.execute(
                "UPDATE accounts SET selling_price=? "
                "WHERE plan_category='silver' AND sold=0", (silver,)
            )
            upd["silver"] = cur.rowcount
        if gold and gold > 0:
            cur = conn.execute(
                "UPDATE accounts SET selling_price=? "
                "WHERE plan_category='gold' AND sold=0", (gold,)
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


# ---------------- /del ----------------

async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("usage: /del <account_id>")
        return
    acc_id = int(ctx.args[0])
    conn = get_db()
    try:
        row = conn.execute("SELECT id, sold FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not row:
            await update.message.reply_text("no such account.")
            return
        if row["sold"]:
            await update.message.reply_text("account is sold — cannot delete.")
            return
        conn.execute("DELETE FROM accounts WHERE id=? AND sold=0", (acc_id,))
    finally:
        conn.close()
    await update.message.reply_text(f"deleted account #{acc_id}.")


# ---------------- main ----------------

def main():
    if not Config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if not Config.TELEGRAM_ADMIN_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS not set — add your telegram id")

    app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_email)],
            ADD_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_pass)],
            ADD_CAT:   [CallbackQueryHandler(add_cat_cb, pattern=r"^addcat:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        allow_reentry=True,
    )

    price_conv = ConversationHandler(
        entry_points=[CommandHandler("price", price_start)],
        states={
            PRICE_SILVER: [MessageHandler(filters.TEXT & ~filters.COMMAND, price_silver)],
            PRICE_GOLD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, price_gold)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(add_conv)
    app.add_handler(price_conv)
    app.add_handler(CommandHandler("bulk", cmd_bulk))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("csv") | filters.Document.FileExtension("txt"),
        cmd_bulk_file,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_pasted_text,
    ))

    log.info("bot starting — polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()