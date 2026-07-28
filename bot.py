# -*- coding: utf-8 -*-
"""
🏛 ربات دادگاه عدالت کملوت — نسخهٔ اصلاح‌شده و پایدار
نیازمندی: python-telegram-bot >= 21   (pip install "python-telegram-bot>=21")
اجرا:  BOT_TOKEN=xxxx python bot.py
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ======================================================================
#  تنظیمات
# ======================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "توکن_ربات_را_اینجا_بگذارید")
OWNER_ID = 1275490079
TEHRAN = ZoneInfo("Asia/Tehran")
DB_PATH = os.getenv("DB_PATH", "court_bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("camelot-court-bot")

MAX_MSG = 3800          # سقف امن برای هر پیام تلگرام
H = ParseMode.HTML

BTN_START_COMPLAINT = "📝 ثبت شکایت جدید"
BTN_ADMIN = "🛠 پنل مدیریت"

# --- استیت‌های مکالمه ---
S_PLAINTIFF, S_DEFENDANT, S_EVIDENCE, S_CONFIRM = range(1, 5)
S_REPLY_TEXT = 10
S_BACKUP_FILE, S_BACKUP_CONFIRM = 20, 21

# ======================================================================
#  دیتابیس
# ======================================================================

_db_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row

TABLES = ("complaints", "logs", "settings", "owner_msgs")

def db_exec(query: str, params: tuple = ()) -> sqlite3.Cursor:
    with _db_lock:
        cur = _db.execute(query, params)
        _db.commit()
        return cur

def db_one(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with _db_lock:
        return _db.execute(query, params).fetchone()

def db_all(query: str, params: tuple = ()) -> List[sqlite3.Row]:
    with _db_lock:
        return _db.execute(query, params).fetchall()

def init_db() -> None:
    with _db_lock:
        _db.execute("PRAGMA journal_mode=WAL;")
        _db.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plaintiff_telegram_id INTEGER,
                plaintiff_username TEXT,
                plaintiff_name TEXT,
                plaintiff_national_id TEXT,
                plaintiff_account TEXT,
                plaintiff_tg_id TEXT,
                plaintiff_raw TEXT,
                defendant_info TEXT,
                evidence_text TEXT,
                evidence_files TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reply_text TEXT,
                replied_at TEXT
            )
            """
        )
        _db.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # نگاشت پیام‌های ارسال‌شده به مالک ← شمارهٔ شکایت (برای قابلیت ریپلای)
        _db.execute(
            """
            CREATE TABLE IF NOT EXISTS owner_msgs (
                message_id INTEGER PRIMARY KEY,
                complaint_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        _db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('bot_status','on')")
        _db.commit()

        # مهاجرت ستون‌های جدید برای دیتابیس‌های قدیمی
        cols = {r["name"] for r in _db.execute("PRAGMA table_info(complaints)")}
        for col in ("plaintiff_username", "plaintiff_raw"):
            if col not in cols:
                _db.execute(f"ALTER TABLE complaints ADD COLUMN {col} TEXT")
        _db.commit()

init_db()

# ======================================================================
#  ابزارهای کمکی
# ======================================================================

def now_tehran() -> str:
    return datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M:%S")

def esc(v: Any) -> str:
    """escape برای HTML — جلوگیری از خطای parse تلگرام روی متن کاربر"""
    return html.escape(str(v)) if v is not None else ""

def clip(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"

def split_message(text: str, limit: int = MAX_MSG) -> List[str]:
    """شکستن پیام‌های طولانی به تکه‌های امن (خط به خط، تگ‌ها نمی‌شکنند)"""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                # خط بسیار طولانی
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or ["—"]

def bot_is_on() -> bool:
    row = db_one("SELECT value FROM settings WHERE key='bot_status'")
    return (row["value"] == "on") if row else True

def set_bot_status(status: str) -> None:
    db_exec(
        "INSERT INTO settings(key,value) VALUES('bot_status',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (status,),
    )

ACTION_MAP = {
    "start": "شروع ربات",
    "complaint_started": "شروع ثبت شکایت",
    "complaint_submitted": "ثبت شکایت جدید",
    "complaint_cancelled": "لغو ثبت شکایت",
    "admin_reply": "پاسخ به شکایت (دکمه)",
    "owner_reply": "پاسخ مالک با ریپلای",
    "toggle_bot": "تغییر وضعیت ربات",
    "backup_export": "گرفتن پشتیبان",
    "backup_import": "بازیابی از پشتیبان",
    "logs_cleared": "پاک‌سازی لاگ‌ها",
    "admin_panel": "ورود به پنل مدیریت",
    "blocked": "تلاش دسترسی در حالت خاموش",
    "error": "خطای سیستمی",
}

def log_action(user_id: Optional[int], action: str, details: str = "") -> None:
    try:
        db_exec(
            "INSERT INTO logs(user_id, action, details, created_at) VALUES(?,?,?,?)",
            (user_id, ACTION_MAP.get(action, action), details, now_tehran()),
        )
    except Exception as e:  # لاگ هرگز نباید جریان کار را بشکند
        logger.error("log_action failed: %s", e)

def is_owner(uid: Optional[int]) -> bool:
    return uid == OWNER_ID

def clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("temp", "reply_complaint_id", "backup_json_data"):
        context.user_data.pop(k, None)

def get_temp(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("temp", {})

# ======================================================================
#  کیبوردها
# ======================================================================

def main_menu_kb(uid: Optional[int]) -> ReplyKeyboardMarkup:
    rows = [[BTN_START_COMPLAINT]]
    if is_owner(uid):
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]]
    )

def evidence_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ پایان ارسال مدارک", callback_data="evidence_done")],
            [InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")],
        ]
    )

def confirm_kb(yes_data: str, no_data: str = "cancel_action") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ بله", callback_data=yes_data),
                InlineKeyboardButton("❌ نه", callback_data=no_data),
            ]
        ]
    )

def admin_kb() -> InlineKeyboardMarkup:
    status = "🔴 خاموش کردن ربات" if bot_is_on() else "🟢 روشن کردن ربات"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(status, callback_data="admin_toggle_bot")],
            [InlineKeyboardButton("📊 آمار", callback_data="admin_stats")],
            [InlineKeyboardButton("📋 لاگ‌ها", callback_data="admin_logs")],
            [InlineKeyboardButton("💾 پشتیبان‌گیری و بازیابی", callback_data="admin_backup")],
            [InlineKeyboardButton("❌ بستن پنل", callback_data="cancel_action")],
        ]
    )

def logs_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 دریافت فایل کامل لاگ‌ها", callback_data="admin_logs_file")],
            [InlineKeyboardButton("🗑 پاک کردن همهٔ لاگ‌ها", callback_data="admin_logs_clear")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")],
        ]
    )

def backup_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 گرفتن پشتیبان", callback_data="admin_backup_export")],
            [InlineKeyboardButton("📤 بازیابی از پشتیبان", callback_data="admin_backup_import")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")],
        ]
    )

def complaint_notification_kb(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📩 پاسخ به این شکایت", callback_data=f"admin_reply_{cid}")]]
    )

# ======================================================================
#  ارسال/ویرایش ایمن پیام  (کلید رفع باگ‌ها)
# ======================================================================

async def safe_edit(
    query,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = H,
) -> None:
    """ویرایش پیام؛ در صورت هر خطا، پیام جدید می‌فرستد تا کاربر بی‌پاسخ نماند."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logger.warning("edit failed (%s) → fallback to new message", e)
    except TelegramError as e:
        logger.warning("edit failed (%s) → fallback to new message", e)

    try:
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramError:
        try:  # آخرین تلاش: بدون parse_mode
            await query.message.reply_text(re.sub(r"<[^>]+>", "", text), reply_markup=reply_markup)
        except TelegramError as e:
            logger.error("safe_edit totally failed: %s", e)

async def safe_reply(update: Update, text: str, reply_markup=None, parse_mode: Optional[str] = H):
    try:
        return await update.effective_message.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except BadRequest:
        return await update.effective_message.reply_text(
            re.sub(r"<[^>]+>", "", text), reply_markup=reply_markup
        )

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "🏛 منوی اصلی:"):
    """ارسال کیبورد اصلی به شکل پیام جدید (ReplyKeyboard را نمی‌توان با edit فرستاد)"""
    uid = update.effective_user.id if update.effective_user else None
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=main_menu_kb(uid)
        )
    except TelegramError as e:
        logger.warning("send_menu failed: %s", e)

# ======================================================================
#  کنترل دسترسی
# ======================================================================

async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return False
    if not bot_is_on() and not is_owner(uid):
        log_action(uid, "blocked")
        msg = "⛔ ربات در حال حاضر خاموش است. لطفاً بعداً تلاش کنید."
        if update.callback_query:
            try:
                await update.callback_query.answer(msg, show_alert=True)
            except TelegramError:
                pass
        elif update.message:
            try:
                await update.message.reply_text(msg)
            except TelegramError:
                pass
        return False
    return True

def owner_only_cb(update: Update) -> bool:
    return is_owner(update.effective_user.id if update.effective_user else None)

# ======================================================================
#  پشتیبان‌گیری / بازیابی
# ======================================================================

def export_full_backup() -> str:
    data: Dict[str, Any] = {"_meta": {"exported_at": now_tehran(), "version": 2}}
    with _db_lock:
        for table in TABLES:
            rows = _db.execute(f"SELECT * FROM {table}").fetchall()
            data[table] = [dict(r) for r in rows]
    return json.dumps(data, indent=2, ensure_ascii=False)

def import_full_backup(json_data: str) -> Tuple[bool, str]:
    try:
        data = json.loads(json_data)
    except json.JSONDecodeError as e:
        return False, f"فایل JSON معتبر نیست: {e}"

    if not isinstance(data, dict):
        return False, "ساختار فایل پشتیبان صحیح نیست."
    if "complaints" not in data:
        return False, "فایل پشتیبان ناقص است (جدول complaints یافت نشد)."

    present = [t for t in TABLES if t in data and isinstance(data[t], list)]
    with _db_lock:
        try:
            _db.execute("BEGIN")
            for table in present:
                _db.execute(f"DELETE FROM {table}")
            for table in present:
                rows = data[table]
                if not rows:
                    continue
                valid_cols = {r["name"] for r in _db.execute(f"PRAGMA table_info({table})")}
                for row in rows:
                    cols = [c for c in row.keys() if c in valid_cols]
                    if not cols:
                        continue
                    ph = ",".join("?" for _ in cols)
                    _db.execute(
                        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})",
                        [row.get(c) for c in cols],
                    )
            _db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('bot_status','on')")
            _db.commit()
            return True, f"بازیابی انجام شد. جداول: {', '.join(present)}"
        except Exception as e:
            _db.rollback()
            return False, f"خطا در بازیابی: {e}"

# ======================================================================
#  /start  و  لغو
# ======================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return
    clear_flow(context)
    uid = update.effective_user.id
    log_action(uid, "start")
    text = (
        "🏛 <b>به دادگاه عدالت کملوت خوش آمدید.</b>\n\n"
        "برای ثبت شکایت خود بر روی دکمهٔ زیر بزنید و مدارک و شواهد خود را ثبت کنید."
    )
    await safe_reply(update, text, reply_markup=main_menu_kb(uid))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات جاری — برای کالبک و پیام و دستور /cancel"""
    was_in_flow = bool(context.user_data.get("temp") or context.user_data.get("reply_complaint_id"))
    clear_flow(context)
    uid = update.effective_user.id if update.effective_user else None
    if was_in_flow:
        log_action(uid, "complaint_cancelled")

    text = "❌ عملیات لغو شد. به منوی اصلی بازگشتید."

    if update.callback_query:
        try:
            await update.callback_query.answer("لغو شد")
        except TelegramError:
            pass
        await safe_edit(update.callback_query, text, reply_markup=None)
        await send_menu(update, context)
    elif update.message:
        await safe_reply(update, text, reply_markup=main_menu_kb(uid))

    return ConversationHandler.END

# ======================================================================
#  جریان ثبت شکایت
# ======================================================================

FIELD_KEYS = {
    "name": ("نام کملوتی", "نام"),
    "nid": ("کدملی", "کد ملی", "کد‌ملی", "کد ملي"),
    "account": ("شماره حساب", "حساب"),
    "tg": ("آیدی تلگرام", "ایدی تلگرام", "آیدی تلگرامی", "آیدی", "ایدی", "یوزرنیم", "یوزر"),
}

def parse_plaintiff(raw: str) -> Dict[str, str]:
    res = {"name": "", "nid": "", "account": "", "tg": ""}
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("•-*").strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        for field, needles in FIELD_KEYS.items():
            if res[field]:
                continue
            if any(n in key for n in needles):
                res[field] = val
                break
    return res

def extract_evidence(msg) -> Optional[dict]:
    """استخراج هر نوع مدرک از پیام"""
    cap = msg.caption or ""
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id, "caption": cap}
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": cap}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id, "caption": cap}
    if msg.video_note:
        return {"type": "video_note", "file_id": msg.video_note.file_id, "caption": cap}
    if msg.voice:
        return {"type": "voice", "file_id": msg.voice.file_id, "caption": cap}
    if msg.audio:
        return {
            "type": "audio",
            "file_id": msg.audio.file_id,
            "caption": cap,
            "file_name": msg.audio.file_name,
        }
    if msg.sticker:
        return {"type": "sticker", "file_id": msg.sticker.file_id, "caption": cap}
    if msg.document:
        return {
            "type": "document",
            "file_id": msg.document.file_id,
            "caption": cap,
            "file_name": msg.document.file_name,
        }
    return None

async def send_evidence_file(bot, chat_id: int, f: dict, caption: str):
    t, fid = f.get("type"), f.get("file_id")
    if not fid:
        return None
    caption = caption[:1000]
    if t == "photo":
        return await bot.send_photo(chat_id, fid, caption=caption)
    if t == "document":
        return await bot.send_document(chat_id, fid, caption=caption)
    if t == "video":
        return await bot.send_video(chat_id, fid, caption=caption)
    if t == "animation":
        return await bot.send_animation(chat_id, fid, caption=caption)
    if t == "voice":
        return await bot.send_voice(chat_id, fid, caption=caption)
    if t == "audio":
        return await bot.send_audio(chat_id, fid, caption=caption)
    if t == "video_note":
        return await bot.send_video_note(chat_id, fid)
    if t == "sticker":
        return await bot.send_sticker(chat_id, fid)
    return None

async def complaint_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update, context):
        return ConversationHandler.END

    clear_flow(context)
    get_temp(context)
    log_action(update.effective_user.id, "complaint_started")

    msg = (
        "📝 <b>لطفاً اطلاعات خود (شاکی) را در یک پیام و به شکل زیر وارد کنید:</b>\n\n"
        "<code>نام کملوتی: ...\n"
        "کدملی کملوتی: ...\n"
        "شماره حساب کملوتی: ...\n"
        "آیدی تلگرام: ...</code>\n\n"
        "🔸 <b>نمونه:</b>\n"
        "نام کملوتی: علی رضایی\n"
        "کدملی کملوتی: 123456\n"
        "شماره حساب کملوتی: 789012\n"
        "آیدی تلگرام: @alireza"
    )

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except TelegramError:
            pass
        await safe_edit(update.callback_query, msg, reply_markup=cancel_kb())
    else:
        await safe_reply(update, msg, reply_markup=cancel_kb())

    return S_PLAINTIFF

async def plaintiff_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update, context):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if text in (BTN_START_COMPLAINT, BTN_ADMIN):
        await safe_reply(update, "ℹ️ لطفاً اطلاعات شاکی را وارد کنید یا لغو بزنید.", reply_markup=cancel_kb())
        return S_PLAINTIFF
    if len(text) < 5:
        await safe_reply(
            update,
            "❌ اطلاعات وارد شده کافی نیست. لطفاً نام، کدملی، شماره حساب و آیدی تلگرام را بفرستید.",
            reply_markup=cancel_kb(),
        )
        return S_PLAINTIFF

    get_temp(context)["plaintiff_raw"] = text
    await safe_reply(
        update,
        "👤 <b>حالا اطلاعات فردی که از او شکایت دارید (متهم) را بفرستید.</b>\n\n"
        "هر نام، آیدی تلگرام، شماره حساب یا هر اطلاعاتی که از او دارید.",
        reply_markup=cancel_kb(),
    )
    return S_DEFENDANT

async def defendant_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update, context):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if len(text) < 2:
        await safe_reply(update, "❌ لطفاً اطلاعات متهم را واضح‌تر وارد کنید.", reply_markup=cancel_kb())
        return S_DEFENDANT

    temp = get_temp(context)
    temp["defendant_info"] = text
    temp["evidence_texts"] = []
    temp["evidence_files"] = []

    await safe_reply(
        update,
        "📎 <b>مدارک و شواهد خود را ارسال کنید.</b>\n\n"
        "می‌توانید اسکرین‌شات، فایل، ویدیو، ویس، لینک پیام تلگرامی یا هر توضیحی بفرستید.\n"
        "هر تعداد که خواستید بفرستید؛ در پایان دکمهٔ «✅ پایان ارسال مدارک» را بزنید.",
        reply_markup=evidence_kb(),
    )
    return S_EVIDENCE

async def evidence_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update, context):
        return ConversationHandler.END

    temp = get_temp(context)
    temp.setdefault("evidence_texts", [])
    temp.setdefault("evidence_files", [])
    msg = update.message

    text = (msg.text or "").strip()
    if text in ("لغو", "❌ لغو", "لغو عملیات"):
        return await cancel(update, context)

    item = extract_evidence(msg)
    if item:
        temp["evidence_files"].append(item)
        await safe_reply(
            update,
            f"✅ مدرک ثبت شد. (تعداد فایل‌ها: {len(temp['evidence_files'])})\n"
            "مدرک بعدی را بفرستید یا «پایان ارسال مدارک» را بزنید.",
            reply_markup=evidence_kb(),
        )
    elif text:
        temp["evidence_texts"].append(text)
        await safe_reply(
            update,
            "✅ متن به عنوان مدرک ثبت شد.\nمدرک بعدی را بفرستید یا «پایان ارسال مدارک» را بزنید.",
            reply_markup=evidence_kb(),
        )
    else:
        await safe_reply(
            update,
            "❌ این نوع پیام پشتیبانی نمی‌شود. لطفاً عکس، فایل، ویدیو، ویس یا متن بفرستید.",
            reply_markup=evidence_kb(),
        )
    return S_EVIDENCE

def build_summary(temp: dict) -> str:
    texts = temp.get("evidence_texts", [])
    files = temp.get("evidence_files", [])
    out = (
        "📋 <b>خلاصهٔ شکایت شما</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "👤 <b>شاکی:</b>\n"
        f"{esc(clip(temp.get('plaintiff_raw', '—'), 700))}\n\n"
        "⚖️ <b>متهم:</b>\n"
        f"{esc(clip(temp.get('defendant_info', '—'), 400))}\n\n"
        "📎 <b>مدارک:</b>\n"
    )
    if texts:
        for i, t in enumerate(texts, 1):
            out += f"  {i}. 📝 {esc(clip(t, 70))}\n"
    if files:
        for i, f in enumerate(files, 1):
            label = f.get("file_name") or f.get("caption") or ""
            out += f"  {i}. 📁 {esc(f.get('type', '?'))} {esc(clip(label, 40))}\n"
    if not texts and not files:
        out += "  (هیچ مدرکی ارسال نشده است)\n"
    out += "\n❓ آیا اطلاعات صحیح است و شکایت ثبت شود؟"
    return out

async def evidence_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass
    if not await check_access(update, context):
        return ConversationHandler.END

    temp = get_temp(context)
    if not temp.get("plaintiff_raw"):
        await safe_edit(query, "❌ اطلاعات ناقص است. لطفاً از ابتدا شروع کنید.")
        await send_menu(update, context)
        return ConversationHandler.END

    summary = build_summary(temp)
    if len(summary) > MAX_MSG:
        summary = summary[: MAX_MSG - 60] + "\n…\n\n❓ آیا شکایت ثبت شود؟"

    await safe_edit(query, summary, reply_markup=confirm_kb("submit_complaint", "cancel_action"))
    return S_CONFIRM

async def submit_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ثبت نهایی شکایت — ⚠️ اینجا بود که کد قبلی crash می‌کرد"""
    query = update.callback_query
    try:
        await query.answer("در حال ثبت…")
    except TelegramError:
        pass
    if not await check_access(update, context):
        return ConversationHandler.END

    temp = get_temp(context)
    user = update.effective_user
    uid = user.id

    plaintiff_raw = temp.get("plaintiff_raw", "")
    if not plaintiff_raw:
        await safe_edit(query, "❌ اطلاعات شکایت در حافظه یافت نشد. لطفاً دوباره شروع کنید.")
        await send_menu(update, context)
        return ConversationHandler.END

    parsed = parse_plaintiff(plaintiff_raw)
    username = f"@{user.username}" if user.username else ""
    tg_id_field = parsed["tg"] or username or str(uid)

    evidence_files = temp.get("evidence_files", [])
    evidence_text = "\n".join(temp.get("evidence_texts", [])).strip()

    cur = db_exec(
        """
        INSERT INTO complaints (
            plaintiff_telegram_id, plaintiff_username, plaintiff_name,
            plaintiff_national_id, plaintiff_account, plaintiff_tg_id,
            plaintiff_raw, defendant_info, evidence_text, evidence_files,
            status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)
        """,
        (
            uid,
            username,
            parsed["name"],
            parsed["nid"],
            parsed["account"],
            tg_id_field,
            plaintiff_raw,
            temp.get("defendant_info", ""),
            evidence_text,
            json.dumps(evidence_files, ensure_ascii=False) if evidence_files else None,
            now_tehran(),
        ),
    )
    complaint_id = cur.lastrowid
    log_action(uid, "complaint_submitted", f"شکایت #{complaint_id} — شاکی: {parsed['name'] or username or uid}")

    clear_flow(context)

    # ✅ فقط متن (بدون ReplyKeyboardMarkup) — علت باگ قبلی
    await safe_edit(
        query,
        "✅ <b>درخواست شما با موفقیت ثبت شد،</b> بزودی پیامی حاوی نام شاکی و متهم و تاریخ "
        "برگزاری دادگاه، در بخش دادگاه کملوت ارسال خواهد شد.\n\n"
        f"🔖 شمارهٔ پرونده: <code>{complaint_id}</code>",
        reply_markup=None,
    )
    # کیبورد اصلی به شکل پیام جدید
    await send_menu(update, context, "🏛 منوی اصلی:")

    # اطلاع به مالک
    await notify_owner(context, complaint_id)
    return ConversationHandler.END

# ======================================================================
#  اطلاع‌رسانی به مالک
# ======================================================================

def register_owner_msg(message_id: int, complaint_id: int) -> None:
    try:
        db_exec(
            "INSERT OR REPLACE INTO owner_msgs(message_id, complaint_id, created_at) VALUES(?,?,?)",
            (message_id, complaint_id, now_tehran()),
        )
    except Exception as e:
        logger.error("register_owner_msg failed: %s", e)

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, complaint_id: int) -> None:
    try:
        c = db_one("SELECT * FROM complaints WHERE id=?", (complaint_id,))
        if not c:
            return

        files: List[dict] = []
        if c["evidence_files"]:
            try:
                files = json.loads(c["evidence_files"]) or []
            except Exception:
                files = []

        msg = (
            "📌 <b>ثبت شکایت جدید در دادگاه کملوت</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"🔖 شماره شکایت: <code>{complaint_id}</code>\n"
            f"🕒 تاریخ ثبت: <code>{esc(c['created_at'])}</code>\n\n"
            "👤 <b>اطلاعات شاکی</b>\n"
            f"• نام کملوتی: {esc(c['plaintiff_name'] or 'نامشخص')}\n"
            f"• کدملی کملوتی: {esc(c['plaintiff_national_id'] or 'نامشخص')}\n"
            f"• شماره حساب: {esc(c['plaintiff_account'] or 'نامشخص')}\n"
            f"• آیدی تلگرام: {esc(c['plaintiff_tg_id'] or 'نامشخص')}\n"
            f"• آیدی عددی: <code>{c['plaintiff_telegram_id']}</code>"
            f"{'  ' + esc(c['plaintiff_username']) if c['plaintiff_username'] else ''}\n\n"
            "📄 <b>متن خام ارسالی شاکی</b>\n"
            f"<blockquote>{esc(c['plaintiff_raw'] or '—')}</blockquote>\n"
            "⚖️ <b>اطلاعات متهم</b>\n"
            f"<blockquote>{esc(c['defendant_info'] or '—')}</blockquote>\n"
            "📝 <b>توضیحات و مدارک متنی</b>\n"
            f"<blockquote>{esc(c['evidence_text'] or '—')}</blockquote>\n"
            "📎 <b>فهرست فایل‌های مدرک</b>\n"
        )
        if files:
            for i, f in enumerate(files, 1):
                label = f.get("file_name") or f.get("caption") or "—"
                msg += f"• {i}. {esc(str(f.get('type', '?')).upper())} — {esc(clip(label, 40))}\n"
        else:
            msg += "• بدون فایل\n"
        msg += "\n💬 برای پاسخ، روی همین پیام <b>ریپلای</b> کنید یا دکمهٔ زیر را بزنید."

        parts = split_message(msg)
        for i, part in enumerate(parts):
            kb = complaint_notification_kb(complaint_id) if i == len(parts) - 1 else None
            try:
                sent = await context.bot.send_message(
                    chat_id=OWNER_ID, text=part, parse_mode=H, reply_markup=kb
                )
            except BadRequest:
                sent = await context.bot.send_message(
                    chat_id=OWNER_ID, text=re.sub(r"<[^>]+>", "", part), reply_markup=kb
                )
            register_owner_msg(sent.message_id, complaint_id)

        for idx, f in enumerate(files, 1):
            try:
                cap = f"📎 مدرک {idx} — شکایت #{complaint_id}\n{f.get('file_name') or f.get('caption') or ''}".strip()
                sent = await send_evidence_file(context.bot, OWNER_ID, f, cap)
                if sent:
                    register_owner_msg(sent.message_id, complaint_id)
            except TelegramError as e:
                logger.error("send evidence to owner failed: %s", e)
                await context.bot.send_message(
                    OWNER_ID, f"⚠️ ارسال مدرک {idx} شکایت #{complaint_id} ناموفق بود: {esc(e)}", parse_mode=H
                )
    except Exception as e:
        logger.exception("notify_owner failed: %s", e)

# ======================================================================
#  پاسخ مالک با دکمه
# ======================================================================

async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass
    if not owner_only_cb(update):
        await safe_edit(query, "⛔ شما دسترسی ندارید.")
        return ConversationHandler.END

    try:
        complaint_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_edit(query, "❌ شمارهٔ شکایت نامعتبر است.")
        return ConversationHandler.END

    c = db_one("SELECT id FROM complaints WHERE id=?", (complaint_id,))
    if not c:
        await safe_edit(query, "❌ این شکایت در دیتابیس یافت نشد.")
        return ConversationHandler.END

    context.user_data["reply_complaint_id"] = complaint_id
    await safe_edit(
        query,
        f"📩 <b>پاسخ به شکایت #{complaint_id}</b>\n\nمتن پاسخ خود را بفرستید:\n(برای لغو /cancel)",
        reply_markup=cancel_kb(),
    )
    return S_REPLY_TEXT

async def deliver_reply(
    context: ContextTypes.DEFAULT_TYPE, complaint_id: int, answer_text: str, owner_id: int
) -> Tuple[bool, str]:
    c = db_one(
        "SELECT plaintiff_telegram_id, plaintiff_name FROM complaints WHERE id=?", (complaint_id,)
    )
    if not c:
        return False, "شکایت یافت نشد."

    db_exec(
        "UPDATE complaints SET status='replied', reply_text=?, replied_at=? WHERE id=?",
        (answer_text, now_tehran(), complaint_id),
    )
    try:
        await context.bot.send_message(
            chat_id=c["plaintiff_telegram_id"],
            text=(
                f"📩 <b>پاسخ دادگاه عدالت کملوت به شکایت #{complaint_id}</b>\n"
                "━━━━━━━━━━━━━━━\n"
                f"{esc(answer_text)}\n\n"
                f"🕐 {esc(now_tehran())}"
            ),
            parse_mode=H,
        )
        log_action(
            owner_id, "admin_reply", f"شکایت #{complaint_id} — شاکی: {c['plaintiff_name'] or c['plaintiff_telegram_id']}"
        )
        return True, "ارسال شد."
    except TelegramError as e:
        logger.error("send reply to plaintiff failed: %s", e)
        log_action(owner_id, "error", f"ارسال پاسخ شکایت #{complaint_id} ناموفق: {e}")
        return False, f"پاسخ در دیتابیس ثبت شد اما ارسال به کاربر ناموفق بود: {e}"

async def admin_reply_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if not is_owner(uid):
        return ConversationHandler.END

    complaint_id = context.user_data.get("reply_complaint_id")
    if not complaint_id:
        await safe_reply(update, "❌ شناسهٔ شکایت یافت نشد. دوباره تلاش کنید.")
        return ConversationHandler.END

    answer = (update.message.text or "").strip()
    if not answer:
        await safe_reply(update, "❌ متن پاسخ خالی است.", reply_markup=cancel_kb())
        return S_REPLY_TEXT

    ok, msg = await deliver_reply(context, complaint_id, answer, uid)
    prefix = "✅" if ok else "⚠️"
    await safe_reply(update, f"{prefix} {esc(msg)}", reply_markup=main_menu_kb(uid))

    clear_flow(context)
    return ConversationHandler.END

# ======================================================================
#  پاسخ مالک با ریپلای مستقیم
# ======================================================================

async def owner_direct_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    uid = update.effective_user.id
    if not is_owner(uid):
        return

    replied = msg.reply_to_message
    row = db_one("SELECT complaint_id FROM owner_msgs WHERE message_id=?", (replied.message_id,))
    complaint_id = row["complaint_id"] if row else None

    if complaint_id is None:  # fallback: استخراج از متن
        src = replied.text or replied.caption or ""
        m = re.search(r"شماره\s*شکایت[:\s]*`?#?(\d+)", src) or re.search(r"شکایت\s*#(\d+)", src)
        if m:
            complaint_id = int(m.group(1))

    if complaint_id is None:
        await handle_message(update, context)  # پیام معمولی است
        return

    answer = (msg.text or msg.caption or "").strip()
    if not answer and not (msg.photo or msg.document or msg.video or msg.voice):
        await safe_reply(update, "❌ متن پاسخ خالی است.")
        return

    ok, info = await deliver_reply(context, complaint_id, answer or "(فایل ارسال شد)", uid)
    log_action(uid, "owner_reply", f"ریپلای به شکایت #{complaint_id}")

    # اگر پاسخ شامل فایل بود، فایل را هم برای شاکی کپی کن
    if ok and (msg.photo or msg.document or msg.video or msg.voice or msg.audio or msg.animation):
        c = db_one("SELECT plaintiff_telegram_id FROM complaints WHERE id=?", (complaint_id,))
        try:
            await msg.copy(chat_id=c["plaintiff_telegram_id"])
        except TelegramError as e:
            logger.error("copy owner file failed: %s", e)

    prefix = "✅ پاسخ شما برای شاکی ارسال شد." if ok else f"⚠️ {info}"
    await safe_reply(update, f"{prefix}\n🔖 شکایت #{complaint_id}")

# ======================================================================
#  پنل مدیریت
# ======================================================================

async def show_admin_panel_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    status = "🟢 روشن" if bot_is_on() else "🔴 خاموش"
    log_action(uid, "admin_panel")
    await safe_reply(
        update, f"🛠 <b>پنل مدیریت</b>\nوضعیت فعلی ربات: {status}", reply_markup=admin_kb()
    )

async def admin_panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    status = "🟢 روشن" if bot_is_on() else "🔴 خاموش"
    await safe_edit(
        query, f"🛠 <b>پنل مدیریت</b>\nوضعیت فعلی ربات: {status}", reply_markup=admin_kb()
    )

async def admin_toggle_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    new_status = "off" if bot_is_on() else "on"
    set_bot_status(new_status)
    log_action(uid, "toggle_bot", "خاموش شد" if new_status == "off" else "روشن شد")
    txt = "🔴 خاموش" if new_status == "off" else "🟢 روشن"
    note = (
        "\n\n⛔ از این پس فقط شما (مالک) به ربات دسترسی دارید."
        if new_status == "off"
        else "\n\n✅ ربات برای همه در دسترس است."
    )
    await safe_edit(query, f"🛠 <b>پنل مدیریت</b>\nوضعیت ربات: {txt}{note}", reply_markup=admin_kb())

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    total = db_one("SELECT COUNT(*) n FROM complaints")["n"]
    pending = db_one("SELECT COUNT(*) n FROM complaints WHERE status='pending'")["n"]
    replied = db_one("SELECT COUNT(*) n FROM complaints WHERE status='replied'")["n"]
    logs_n = db_one("SELECT COUNT(*) n FROM logs")["n"]
    users = db_one("SELECT COUNT(DISTINCT plaintiff_telegram_id) n FROM complaints")["n"]
    last = db_one("SELECT created_at FROM complaints ORDER BY id DESC LIMIT 1")
    size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0

    txt = (
        "📊 <b>آمار دادگاه کملوت</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"⚖️ کل شکایات: <b>{total}</b>\n"
        f"⏳ در انتظار پاسخ: <b>{pending}</b>\n"
        f"✅ پاسخ داده‌شده: <b>{replied}</b>\n"
        f"👥 تعداد شاکیان یکتا: <b>{users}</b>\n"
        f"📋 تعداد لاگ‌ها: <b>{logs_n}</b>\n"
        f"🕒 آخرین شکایت: <code>{esc(last['created_at']) if last else '—'}</code>\n"
        f"💽 حجم دیتابیس: <b>{size:.1f} KB</b>\n"
        f"🔌 وضعیت ربات: <b>{'🟢 روشن' if bot_is_on() else '🔴 خاموش'}</b>"
    )
    await safe_edit(
        query,
        txt,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]
        ),
    )

async def admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    rows = db_all("SELECT * FROM logs ORDER BY id DESC LIMIT 15")
    total = db_one("SELECT COUNT(*) n FROM logs")["n"]
    if not rows:
        await safe_edit(query, "📭 هیچ لاگی ثبت نشده است.", reply_markup=logs_kb())
        return

    txt = f"📋 <b>۱۵ لاگ آخر</b> (کل: {total})\n━━━━━━━━━━━━━━━\n"
    for r in rows:
        who = f"<code>{r['user_id']}</code>" if r["user_id"] else "سیستم"
        txt += f"🕐 {esc(r['created_at'])} | 👤 {who}\n📌 {esc(r['action'])}\n"
        if r["details"]:
            txt += f"📝 {esc(clip(r['details'], 90))}\n"
        txt += "─────────────\n"
    if len(txt) > MAX_MSG:
        txt = txt[: MAX_MSG - 20] + "\n…"
    await safe_edit(query, txt, reply_markup=logs_kb())

async def admin_logs_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    rows = db_all("SELECT * FROM logs ORDER BY id DESC")
    if not rows:
        try:
            await query.answer("لاگی وجود ندارد.", show_alert=True)
        except TelegramError:
            pass
        return
    lines = [f"لاگ‌های ربات دادگاه کملوت — {now_tehran()}", "=" * 50]
    for r in rows:
        lines.append(
            f"[{r['created_at']}] user={r['user_id'] or 'system'} | {r['action']}"
            + (f" | {r['details']}" if r["details"] else "")
        )
    buf = io.BytesIO("\n".join(lines).encode("utf-8"))
    buf.name = f"logs_{datetime.now(TEHRAN).strftime('%Y%m%d_%H%M%S')}.txt"
    await context.bot.send_document(
        chat_id=OWNER_ID, document=buf, caption=f"📄 فایل کامل لاگ‌ها ({len(rows)} رکورد)"
    )
    try:
        await query.answer("فایل ارسال شد ✅")
    except TelegramError:
        pass

async def admin_logs_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_edit(
        update.callback_query,
        "🗑 <b>پاک کردن همهٔ لاگ‌ها</b>\n\n⚠️ این عمل بازگشت‌پذیر نیست. مطمئنی؟",
        reply_markup=confirm_kb("admin_logs_clear_yes", "admin_back"),
    )

async def admin_logs_clear_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    n = db_one("SELECT COUNT(*) n FROM logs")["n"]
    db_exec("DELETE FROM logs")
    log_action(uid, "logs_cleared", f"{n} رکورد پاک شد")
    await safe_edit(update.callback_query, f"✅ {n} لاگ پاک شد.", reply_markup=admin_kb())

async def admin_backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_edit(
        update.callback_query,
        "💾 <b>پشتیبان‌گیری و بازیابی</b>\n\n"
        "📥 <b>گرفتن پشتیبان:</b> یک فایل JSON از کل دیتابیس (شکایات، لاگ‌ها، تنظیمات) دریافت می‌کنید.\n"
        "📤 <b>بازیابی:</b> با ارسال همان فایل JSON، دیتابیس بازنویسی می‌شود.",
        reply_markup=backup_kb(),
    )

async def admin_backup_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    await safe_edit(query, "📥 در حال تهیهٔ پشتیبان… لطفاً صبر کنید.")
    try:
        data = export_full_backup()
        buf = io.BytesIO(data.encode("utf-8"))
        buf.name = f"court_backup_{datetime.now(TEHRAN).strftime('%Y%m%d_%H%M%S')}.json"
        n = len(json.loads(data).get("complaints", []))
        await context.bot.send_document(
            chat_id=uid,
            document=buf,
            caption=(
                "💾 <b>پشتیبان دادگاه عدالت کملوت</b>\n"
                f"🕐 {esc(now_tehran())}\n"
                f"⚖️ تعداد شکایات: {n}\n\n"
                "این فایل را نگه دارید؛ برای بازیابی از بخش «بازیابی از پشتیبان» ارسال کنید."
            ),
            parse_mode=H,
        )
        log_action(uid, "backup_export", f"{n} شکایت")
        await safe_edit(query, "✅ پشتیبان با موفقیت تهیه و ارسال شد.", reply_markup=backup_kb())
    except Exception as e:
        logger.exception("export failed")
        await safe_edit(query, f"❌ خطا در تهیهٔ پشتیبان: {esc(e)}", reply_markup=backup_kb())

async def admin_backup_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass
    if not owner_only_cb(update):
        await safe_edit(query, "⛔ شما دسترسی ندارید.")
        return ConversationHandler.END

    await safe_edit(
        query,
        "📤 <b>بازیابی از پشتیبان</b>\n\n"
        "⚠️ توجه: تمام اطلاعات فعلی <b>حذف و بازنویسی</b> می‌شود.\n"
        "لطفاً فایل JSON پشتیبان را ارسال کنید.\n(برای لغو /cancel)",
        reply_markup=cancel_kb(),
    )
    return S_BACKUP_FILE

async def admin_backup_import_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if not is_owner(uid):
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".json"):
        await safe_reply(update, "❌ لطفاً یک فایل با پسوند <code>.json</code> بفرستید.", reply_markup=cancel_kb())
        return S_BACKUP_FILE
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await safe_reply(update, "❌ حجم فایل بیش از حد بزرگ است.", reply_markup=cancel_kb())
        return S_BACKUP_FILE

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        content = bytes(await tg_file.download_as_bytearray()).decode("utf-8")
        parsed = json.loads(content)  # اعتبارسنجی اولیه
        context.user_data["backup_json_data"] = content
        n = len(parsed.get("complaints", []))
        await safe_reply(
            update,
            f"⚠️ <b>تأیید نهایی</b>\n\nفایل شامل <b>{n}</b> شکایت است.\n"
            "با ادامه، دیتابیس فعلی کاملاً بازنویسی می‌شود. مطمئنی؟",
            reply_markup=confirm_kb("admin_backup_import_confirm", "cancel_action"),
        )
        return S_BACKUP_CONFIRM
    except json.JSONDecodeError:
        await safe_reply(update, "❌ محتوای فایل JSON معتبر نیست.", reply_markup=cancel_kb())
        return S_BACKUP_FILE
    except Exception as e:
        logger.exception("import file read failed")
        await safe_reply(update, f"❌ خطا در خواندن فایل: {esc(e)}", reply_markup=cancel_kb())
        return S_BACKUP_FILE

async def admin_backup_import_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass
    uid = update.effective_user.id
    if not is_owner(uid):
        return ConversationHandler.END

    data = context.user_data.get("backup_json_data")
    if not data:
        await safe_edit(query, "❌ دادهٔ پشتیبان در حافظه یافت نشد. دوباره تلاش کنید.", reply_markup=admin_kb())
        return ConversationHandler.END

    await safe_edit(query, "🔄 در حال بازیابی… لطفاً صبر کنید.")
    ok, msg = import_full_backup(data)
    if ok:
        log_action(uid, "backup_import", msg)
        await safe_edit(query, f"✅ {esc(msg)}", reply_markup=admin_kb())
    else:
        await safe_edit(query, f"❌ {esc(msg)}", reply_markup=admin_kb())

    clear_flow(context)
    return ConversationHandler.END

# ======================================================================
#  دیسپچر کالبک‌های مدیریتی
# ======================================================================

ADMIN_ROUTES = {
    "admin_panel": admin_panel_cb,
    "admin_back": admin_panel_cb,
    "admin_toggle_bot": admin_toggle_bot,
    "admin_stats": admin_stats,
    "admin_logs": admin_logs,
    "admin_logs_file": admin_logs_file,
    "admin_logs_clear": admin_logs_clear,
    "admin_logs_clear_yes": admin_logs_clear_yes,
    "admin_backup": admin_backup_menu,
    "admin_backup_export": admin_backup_export,
}

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass

    if not owner_only_cb(update):
        await safe_edit(query, "⛔ شما دسترسی ندارید.")
        return

    handler = ADMIN_ROUTES.get(query.data)
    if handler:
        await handler(update, context)
    else:
        # مثلاً admin_backup_import یا admin_reply_x که مکالمه‌شان منقضی شده
        await safe_edit(
            query,
            "ℹ️ این عملیات منقضی شده است. لطفاً از پنل مدیریت مجدداً شروع کنید.",
            reply_markup=admin_kb(),
        )

async def unknown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer("این دکمه منقضی شده است.", show_alert=True)
    except TelegramError:
        pass
    logger.info("unknown callback: %s", query.data)

# ======================================================================
#  پیام‌های عمومی
# ======================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not await check_access(update, context):
        return

    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if text == BTN_ADMIN:
        if is_owner(uid):
            await show_admin_panel_msg(update, context)
        else:
            await safe_reply(update, "⛔ این بخش مخصوص مالک ربات است.", reply_markup=main_menu_kb(uid))
        return

    if text == BTN_START_COMPLAINT:
        # در حالت عادی مکالمه آن را می‌گیرد؛ این فقط تور اطمینان است
        await safe_reply(update, "برای شروع دوباره /start را بزنید.", reply_markup=main_menu_kb(uid))
        return

    await safe_reply(
        update,
        "ℹ️ لطفاً از دکمه‌های منوی زیر استفاده کنید.\nبرای ثبت شکایت روی «📝 ثبت شکایت جدید» بزنید.",
        reply_markup=main_menu_kb(uid),
    )

async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update.effective_user.id):
        await show_admin_panel_msg(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    try:
        log_action(None, "error", str(context.error)[:400])
    except Exception:
        pass

    if isinstance(update, Update):
        try:
            if update.callback_query:
                await update.callback_query.answer("❌ خطایی رخ داد. دوباره تلاش کنید.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("❌ خطایی رخ داد. لطفاً دوباره تلاش کنید یا /start بزنید.")
        except TelegramError:
            pass
    try:
        await context.bot.send_message(
            OWNER_ID, f"⚠️ <b>خطای سیستمی</b>\n<code>{esc(str(context.error)[:1000])}</code>", parse_mode=H
        )
    except TelegramError:
        pass

# ======================================================================
#  مکالمه‌ها
# ======================================================================

CANCEL_FALLBACKS = [
    CommandHandler("cancel", cancel),
    CommandHandler("start", cancel_then_start := None) if False else CommandHandler("cancel", cancel),
    CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
    MessageHandler(filters.Regex(r"^(لغو|❌ لغو|لغو عملیات)$"), cancel),
]

complaint_conv = ConversationHandler(
    entry_points=[
        MessageHandler(filters.Regex(rf"^{re.escape(BTN_START_COMPLAINT)}$"), complaint_start),
        CallbackQueryHandler(complaint_start, pattern=r"^start_complaint$"),
        CommandHandler("complaint", complaint_start),
    ],
    states={
        S_PLAINTIFF: [
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, plaintiff_info_handler),
        ],
        S_DEFENDANT: [
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, defendant_info_handler),
        ],
        S_EVIDENCE: [
            CallbackQueryHandler(evidence_done, pattern=r"^evidence_done$"),
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
            MessageHandler(~filters.COMMAND & ~filters.StatusUpdate.ALL, evidence_handler),
        ],
        S_CONFIRM: [
            CallbackQueryHandler(submit_complaint, pattern=r"^submit_complaint$"),
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
        ],
    },
    fallbacks=CANCEL_FALLBACKS,
    allow_reentry=True,
    name="complaint_conv",
)

admin_reply_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_reply_start, pattern=r"^admin_reply_\d+$")],
    states={
        S_REPLY_TEXT: [
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.REPLY, admin_reply_text_handler),
        ]
    },
    fallbacks=CANCEL_FALLBACKS,
    allow_reentry=True,
    name="admin_reply_conv",
)

admin_backup_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_backup_import_start, pattern=r"^admin_backup_import$")],
    states={
        S_BACKUP_FILE: [
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
            MessageHandler(filters.Document.ALL, admin_backup_import_file),
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_backup_import_file),
        ],
        S_BACKUP_CONFIRM: [
            CallbackQueryHandler(admin_backup_import_confirm, pattern=r"^admin_backup_import_confirm$"),
            CallbackQueryHandler(cancel, pattern=r"^cancel_action$"),
        ],
    },
    fallbacks=CANCEL_FALLBACKS,
    allow_reentry=True,
    name="admin_backup_conv",
)

# ======================================================================
#  main
# ======================================================================

def main() -> None:
    if "توکن" in BOT_TOKEN:
        raise SystemExit("❌ لطفاً BOT_TOKEN را تنظیم کنید (متغیر محیطی یا داخل کد).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 1) مکالمه‌ها (باید قبل از هندلرهای عمومی باشند)
    app.add_handler(complaint_conv)
    app.add_handler(admin_reply_conv)
    app.add_handler(admin_backup_conv)

    # 2) دستورها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("admin", panel_command))

    # 3) کالبک‌ها
    app.add_handler(CallbackQueryHandler(cancel, pattern=r"^cancel_action$"))
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(complaint_start, pattern=r"^start_complaint$"))

    # 4) ریپلای مالک — فقط روی پیام‌های ریپلای‌شده (تا جلوی handle_message را نگیرد)
    app.add_handler(
        MessageHandler(
            filters.REPLY & filters.User(OWNER_ID) & ~filters.COMMAND,
            owner_direct_reply_handler,
        )
    )

    # 5) پیام‌های عمومی
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 6) کالبک‌های ناشناخته (آخرین خط دفاع، تا دکمه‌ها بی‌پاسخ نمانند)
    app.add_handler(CallbackQueryHandler(unknown_callback))

    app.add_error_handler(error_handler)

    logger.info("🏛 ربات دادگاه عدالت کملوت راه‌اندازی شد.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
