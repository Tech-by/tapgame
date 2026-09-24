# admin_bot.py
"""
Private admin bot. Only responds to ADMIN_USER_ID.
Runs as a background thread inside the FastAPI process (so Render only needs one service).
"""
import os
import asyncio
import threading
from datetime import datetime, date, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from sqlmodel import Session, select

from database import engine
from models import User, Impression

load_dotenv()

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")


def _parse_admin_id(raw: str) -> int:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        print(f"[admin_bot] WARNING: ADMIN_USER_ID='{raw}' is invalid. Admin commands disabled.")
        return 0


ADMIN_USER_ID = _parse_admin_id(os.getenv("ADMIN_USER_ID", "0"))


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_USER_ID)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text(
        "🤖 Admin bot online.\n\n"
        "/stats — platform stats\n"
        "/find <telegram_id> — user details\n"
        "/broadcast <message> — message all users\n"
        "/grant <id> <points> — add points"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    with Session(engine) as s:
        users = s.exec(select(User)).all()
        today = datetime.combine(date.today(), datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        imps = s.exec(
            select(Impression).where(Impression.created_at >= today)
        ).all()
    await update.message.reply_text(
        f"📊 *Stats*\n"
        f"Total users: *{len(users)}*\n"
        f"Impressions today: *{len(imps)}*\n"
        f"Server time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        parse_mode="Markdown",
    )


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /find <telegram_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID")
        return
    with Session(engine) as s:
        u = s.exec(select(User).where(User.telegram_id == tid)).first()
    if not u:
        await update.message.reply_text("User not found.")
        return
    await update.message.reply_text(
        f"👤 User `{u.telegram_id}`\n"
        f"Username: @{u.username or 'none'}\n"
        f"Balance: {u.balance}\n"
        f"Farm: {u.farm_remaining}/{u.farm_cap}\n"
        f"Ads today: {u.ads_watched_today}\n"
        f"Referrals: {u.referral_count}",
        parse_mode="Markdown",
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    text = " ".join(context.args)
    with Session(engine) as s:
        tids = [u.telegram_id for u in s.exec(select(User)).all()]
    sent = 0
    for tid in tids:
        try:
            await context.bot.send_message(chat_id=tid, text=text)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ Sent to {sent}/{len(tids)} users.")


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /grant <id> <points>")
        return
    try:
        tid = int(context.args[0])
        amt = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid args")
        return
    with Session(engine) as s:
        u = s.exec(select(User).where(User.telegram_id == tid)).first()
        if not u:
            await update.message.reply_text("User not found")
            return
        u.balance += amt
        s.add(u)
        s.commit()
    await update.message.reply_text(f"✅ Granted {amt} to {tid}.")


def _build_app() -> Application:
    app = Application.builder().token(ADMIN_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("grant", cmd_grant))
    return app


def start_admin_bot_background() -> None:
    """Launch the bot in a daemon thread (used by main.py on startup)."""
    if not ADMIN_BOT_TOKEN or ADMIN_BOT_TOKEN.startswith("YOUR"):
        print("[admin_bot] ADMIN_BOT_TOKEN not set — skipping.")
        return

    def _run():
        # Create a fresh event loop for this thread — required by python-telegram-bot
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = _build_app()
            print("[admin_bot] running")
            app.run_polling(stop_signals=None)
        except Exception as e:
            print(f"[admin_bot] crashed: {e}")

    threading.Thread(target=_run, daemon=True).start()


def run_admin_bot() -> None:
    """Run standalone (for local testing)."""
    _build_app().run_polling()


if __name__ == "__main__":
    run_admin_bot()
