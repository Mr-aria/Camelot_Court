# -*- coding: utf-8 -*-
"""ربات دادگاه عدالت کملوت - نسخه نهایی با رفع تمام باگ‌ها"""

from __future__ import annotations

import logging
import os
import json
import sqlite3
import threading
import io
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)

# -----------------------------
# Configuration
# -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "توکن_ربات_را_اینجا_بگذارید_یا_در_متغیرهای_محیطی")
OWNER_ID = 1275490079
TEHRAN = ZoneInfo("Asia/Tehran")
DB_PATH = "court_bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("camelot-court-bot")

# -----------------------------
# Constants / States
# -----------------------------

BTN_START_COMPLAINT = "📝 ثبت شکایت جدید"
BTN_ADMIN = "🛠 پنل مدیریت"

# Conversation states for complaint
S_PLAINTIFF_INFO = 1
S_DEFENDANT_INFO = 2
S_EVIDENCE = 3
S_CONFIRM = 4

# Admin conversation for reply
S_ADMIN_REPLY_TEXT = 10

# Admin backup/restore states
S_ADMIN_BACKUP_IMPORT_FILE = 20
S_ADMIN_BACKUP_CONFIRM = 21

# -----------------------------
# SQLite helpers
# -----------------------------

_db_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row

def db_exec(query: str, params: tuple = ()) -> None:
    with _db_lock:
        cur = _db.execute(query, params)
        _db.commit()
        return cur

def db_one(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with _db_lock:
        cur = _db.execute(query, params)
        return cur.fetchone()

def db_all(query: str, params: tuple = ()) -> List[sqlite3.Row]:
    with _db_lock:
        cur = _db.execute(query, params)
        return cur.fetchall()

def init_db() -> None:
    with _db_lock:
        _db.execute("PRAGMA journal_mode=WAL;")
        _db.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plaintiff_telegram_id INTEGER,
                plaintiff_name TEXT,
                plaintiff_national_id TEXT,
                plaintiff_account TEXT,
                plaintiff_tg_id TEXT,
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
        _db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('bot_status', 'on')")
        _db.commit()

init_db()

# -----------------------------
# Utility helpers
# -----------------------------

def now_tehran() -> str:
    return datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M:%S")

def bot_is_on() -> bool:
    row = db_one("SELECT value FROM settings WHERE key = 'bot_status'")
    return row["value"] == "on" if row else True

def set_bot_status(status: str) -> None:
    db_exec("INSERT INTO settings(key, value) VALUES('bot_status', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (status,))

def log_action(user_id: Optional[int], action: str, details: str = "") -> None:
    """ثبت لاگ با توضیحات قابل فهم"""
    # ترجمه action به فارسی برای نمایش بهتر
    action_map = {
        "complaint_submitted": "ثبت شکایت جدید",
        "admin_reply": "پاسخ به شکایت",
        "toggle_bot": "تغییر وضعیت ربات",
        "backup_export": "گرفتن پشتیبان",
        "backup_import": "بازیابی از پشتیبان",
    }
    persian_action = action_map.get(action, action)
    db_exec(
        "INSERT INTO logs(user_id, action, details, created_at) VALUES(?, ?, ?, ?)",
        (user_id, persian_action, details, now_tehran()),
    )

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def user_state(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    return context.user_data.get("state")

def set_state(context: ContextTypes.DEFAULT_TYPE, state: Optional[int]) -> None:
    if state is None:
        context.user_data.pop("state", None)
    else:
        context.user_data["state"] = state

def clear_temp(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["temp"] = {}

def get_temp(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("temp", {})

def main_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [[BTN_START_COMPLAINT]]
    if is_owner(uid):
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]])

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 خاموش/روشن", callback_data="admin_toggle_bot")],
        [InlineKeyboardButton("📋 لاگ‌ها", callback_data="admin_logs")],
        [InlineKeyboardButton("💾 پشتیبان‌گیری و بازیابی", callback_data="admin_backup")],
        [InlineKeyboardButton("❌ بستن پنل", callback_data="cancel_action")],
    ])

def backup_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 گرفتن پشتیبان", callback_data="admin_backup_export")],
        [InlineKeyboardButton("📤 بازیابی از پشتیبان", callback_data="admin_backup_import")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")],
    ])

def confirm_kb(yes_data: str, no_data: str = "cancel_action") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ بله", callback_data=yes_data),
        InlineKeyboardButton("❌ نه", callback_data=no_data),
    ]])

def complaint_notification_kb(complaint_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 پاسخ به شکایت", callback_data=f"admin_reply_{complaint_id}")],
    ])

# ==================== Access control ====================

async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return False
    if not bot_is_on() and not is_owner(uid):
        msg = "⛔ ربات در حال حاضر خاموش است."
        if update.message:
            await update.message.reply_text(msg)
        elif update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        return False
    return True

# -----------------------------
# Backup & Restore Functions
# -----------------------------

def export_full_backup() -> str:
    tables = ['complaints', 'logs', 'settings']
    data = {}
    with _db_lock:
        for table in tables:
            cursor = _db.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()
            data[table] = [dict(row) for row in rows]
    return json.dumps(data, indent=2, ensure_ascii=False)

def import_full_backup(json_data: str) -> tuple:
    try:
        data = json.loads(json_data)
    except json.JSONDecodeError as e:
        return False, f"فایل JSON معتبر نیست: {e}"
    expected = {'complaints', 'logs', 'settings'}
    if not expected.issubset(data.keys()):
        return False, "فایل پشتیبان کامل نیست."
    with _db_lock:
        try:
            for table in expected:
                _db.execute(f"DELETE FROM {table}")
            for table, rows in data.items():
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ','.join(['?' for _ in columns])
                col_names = ','.join(columns)
                for row in rows:
                    values = [row.get(col) for col in columns]
                    _db.execute(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", values)
            _db.commit()
            return True, "بازیابی با موفقیت انجام شد."
        except Exception as e:
            _db.rollback()
            return False, f"خطا: {str(e)}"

# -----------------------------
# Start & Cancel
# -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return
    uid = update.effective_user.id
    welcome = (
        "🏛 **به دادگاه عدالت کملوت خوش آمدید.**\n\n"
        "برای ثبت شکایت خود روی دکمه زیر بزنید و مدارک و شواهد خود را ثبت کنید."
    )
    await update.message.reply_text(
        welcome,
        reply_markup=main_menu_kb(uid),
        parse_mode='Markdown'
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لغو عملیات جاری - هم برای پیام و هم کالبک"""
    uid = update.effective_user.id
    set_state(context, None)
    clear_temp(context)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "❌ عملیات لغو شد. به منوی اصلی بازگشتید.",
            reply_markup=main_menu_kb(uid)
        )
    else:
        await update.message.reply_text(
            "❌ عملیات لغو شد. به منوی اصلی بازگشتید.",
            reply_markup=main_menu_kb(uid)
        )
    return ConversationHandler.END

# -----------------------------
# Complaint Registration Flow
# -----------------------------

async def complaint_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """شروع ثبت شکایت - هم برای کالبک و هم پیام مستقیم"""
    if not await check_access(update, context):
        return ConversationHandler.END

    set_state(context, S_PLAINTIFF_INFO)
    clear_temp(context)

    msg = (
        "📝 **لطفاً اطلاعات خود (شاکی) را به صورت زیر وارد کنید:**\n\n"
        "نام کملوتی: [نام خود]\n"
        "کدملی کملوتی: [۶ رقم]\n"
        "شماره حساب کملوتی: [۶ رقم]\n"
        "آیدی تلگرام: [آیدی عددی یا یوزرنیم]\n\n"
        "مثال:\n"
        "نام کملوتی: علی رضایی\n"
        "کدملی کملوتی: ۱۲۳۴۵۶\n"
        "شماره حساب: ۷۸۹۰۱۲\n"
        "آیدی تلگرام: @alireza یا ۱۲۳۴۵۶۷۸۹"
    )

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=cancel_kb())
    else:
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=cancel_kb())

    return S_PLAINTIFF_INFO

async def plaintiff_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if not text:
        await update.message.reply_text("❌ لطفاً متن را ارسال کنید.", reply_markup=cancel_kb())
        return S_PLAINTIFF_INFO
    get_temp(context)['plaintiff_raw'] = text
    set_state(context, S_DEFENDANT_INFO)
    await update.message.reply_text(
        "👤 **حالا اطلاعات فردی که از او شکایت دارید (متهم) را بنویسید:**\n\n"
        "هر نام، آیدی تلگرام یا اطلاعات دیگری که دارید را وارد کنید.",
        parse_mode='Markdown',
        reply_markup=cancel_kb()
    )
    return S_DEFENDANT_INFO

async def defendant_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if not text:
        await update.message.reply_text("❌ لطفاً متن را ارسال کنید.", reply_markup=cancel_kb())
        return S_DEFENDANT_INFO
    get_temp(context)['defendant_info'] = text
    set_state(context, S_EVIDENCE)
    await update.message.reply_text(
        "📎 **مدارک و شواهد خود را ارسال کنید.**\n\n"
        "می‌توانید عکس، فایل، لینک پیام تلگرامی یا هر مدرک دیگری ارسال کنید.\n"
        "پس از ارسال همه مدارک، روی دکمه «پایان ارسال مدارک» کلیک کنید.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ پایان ارسال مدارک", callback_data="evidence_done")]
        ])
    )
    get_temp(context)['evidence_texts'] = []
    get_temp(context)['evidence_files'] = []
    return S_EVIDENCE

async def evidence_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    temp = get_temp(context)
    caption = update.message.caption or ""

    if update.message.text:
        temp['evidence_texts'].append(update.message.text)
        await update.message.reply_text("✅ متن به عنوان مدرک ذخیره شد. می‌توانید مدارک دیگر ارسال کنید یا روی دکمه پایان کلیک کنید.")
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        temp['evidence_files'].append({
            'type': 'photo',
            'file_id': file_id,
            'caption': caption
        })
        await update.message.reply_text("✅ عکس به عنوان مدرک ذخیره شد.")
    elif update.message.document:
        file_id = update.message.document.file_id
        temp['evidence_files'].append({
            'type': 'document',
            'file_id': file_id,
            'caption': caption,
            'file_name': update.message.document.file_name
        })
        await update.message.reply_text("✅ فایل به عنوان مدرک ذخیره شد.")
    else:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود. لطفاً عکس، فایل یا متن ارسال کنید.")
    return S_EVIDENCE

async def evidence_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    temp = get_temp(context)
    plaintiff_raw = temp.get('plaintiff_raw', '')
    defendant_info = temp.get('defendant_info', '')
    evidence_texts = temp.get('evidence_texts', [])
    evidence_files = temp.get('evidence_files', [])
    summary = (
        "📋 **خلاصه شکایت شما:**\n\n"
        "👤 **شاکی:**\n"
        f"{plaintiff_raw}\n\n"
        "👤 **متهم:**\n"
        f"{defendant_info}\n\n"
        "📎 **مدارک:**\n"
    )
    if evidence_texts:
        summary += "متون:\n" + "\n".join(evidence_texts) + "\n"
    else:
        summary += "متون: (ندارد)\n"
    if evidence_files:
        summary += f"تعداد فایل‌ها: {len(evidence_files)}\n"
        for i, f in enumerate(evidence_files, 1):
            caption = f.get('caption', 'بدون کپشن')
            summary += f"  {i}. {f['type']} - {caption[:30]}{'...' if len(caption) > 30 else ''}\n"
    else:
        summary += "فایل‌ها: (ندارد)\n"
    summary += "\nآیا اطلاعات صحیح است و می‌خواهید شکایت را ثبت کنید؟"
    await query.edit_message_text(
        summary,
        parse_mode='Markdown',
        reply_markup=confirm_kb("submit_complaint", "cancel_action")
    )
    set_state(context, S_CONFIRM)
    return S_CONFIRM

async def submit_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    temp = get_temp(context)
    plaintiff_raw = temp.get('plaintiff_raw', '')
    defendant_info = temp.get('defendant_info', '')
    evidence_texts = temp.get('evidence_texts', [])
    evidence_files = temp.get('evidence_files', [])

    # Parse plaintiff info
    lines = plaintiff_raw.split('\n')
    plaintiff_name = ''
    plaintiff_nid = ''
    plaintiff_account = ''
    plaintiff_tg_id = ''
    for line in lines:
        if 'نام کملوتی' in line:
            plaintiff_name = line.split(':', 1)[-1].strip()
        elif 'کدملی' in line:
            plaintiff_nid = line.split(':', 1)[-1].strip()
        elif 'شماره حساب' in line:
            plaintiff_account = line.split(':', 1)[-1].strip()
        elif 'آیدی تلگرام' in line:
            plaintiff_tg_id = line.split(':', 1)[-1].strip()

    evidence_files_json = json.dumps(evidence_files, ensure_ascii=False) if evidence_files else None
    evidence_text_combined = "\n".join(evidence_texts) if evidence_texts else None
    created_at = now_tehran()
    cursor = db_exec(
        """
        INSERT INTO complaints (
            plaintiff_telegram_id, plaintiff_name, plaintiff_national_id,
            plaintiff_account, plaintiff_tg_id, defendant_info,
            evidence_text, evidence_files, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (uid, plaintiff_name, plaintiff_nid, plaintiff_account, plaintiff_tg_id,
         defendant_info, evidence_text_combined, evidence_files_json, created_at)
    )
    complaint_id = cursor.lastrowid
    log_action(uid, "complaint_submitted", f"شکایت #{complaint_id} - شاکی: {plaintiff_name} - متهم: {defendant_info[:30]}...")

    await query.edit_message_text(
        "✅ **درخواست شما با موفقیت ثبت شد.**\n\n"
        "بزودی پیامی حاوی نام شاکی و متهم و تاریخ برگزاری دادگاه، در بخش دادگاه کملوت ارسال خواهد شد.",
        reply_markup=main_menu_kb(uid),
        parse_mode='Markdown'
    )
    set_state(context, None)
    clear_temp(context)

    await notify_owner(update, context, complaint_id)

async def notify_owner(update: Update, context: ContextTypes.DEFAULT_TYPE, complaint_id: int):
    complaint = db_one("SELECT * FROM complaints WHERE id = ?", (complaint_id,))
    if not complaint:
        return
    msg = (
        f"⚖️ **شکایت جدید #{complaint_id}**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **شاکی:**\n"
        f"نام: {complaint['plaintiff_name'] or 'نامشخص'}\n"
        f"کدملی: {complaint['plaintiff_national_id'] or 'نامشخص'}\n"
        f"حساب: {complaint['plaintiff_account'] or 'نامشخص'}\n"
        f"آیدی تلگرام: {complaint['plaintiff_tg_id'] or 'نامشخص'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **متهم:**\n{complaint['defendant_info'] or 'ذکر نشده'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📎 **مدارک:**\n"
    )
    if complaint['evidence_text']:
        msg += f"متن:\n{complaint['evidence_text']}\n"
    else:
        msg += "متن: (ندارد)\n"
    if complaint['evidence_files']:
        files = json.loads(complaint['evidence_files'])
        msg += f"تعداد فایل‌ها: {len(files)}\n"
        for i, f in enumerate(files, 1):
            caption = f.get('caption', 'بدون کپشن')
            msg += f"  {i}. {f['type']} - {caption[:50]}\n"
    else:
        msg += "فایل‌ها: (ندارد)\n"
    msg += f"\n🕐 زمان ثبت: {complaint['created_at']}"

    try:
        await context.bot.send_message(
            OWNER_ID,
            msg,
            parse_mode='Markdown',
            reply_markup=complaint_notification_kb(complaint_id)
        )
        # ارسال مدارک به صورت جداگانه با کپشن
        if complaint['evidence_files']:
            files = json.loads(complaint['evidence_files'])
            for f in files:
                file_id = f['file_id']
                caption = f.get('caption', '')
                try:
                    if f['type'] == 'photo':
                        await context.bot.send_photo(
                            OWNER_ID,
                            file_id,
                            caption=f"📎 مدرک #{complaint_id}\n{caption}" if caption else f"📎 مدرک #{complaint_id}"
                        )
                    elif f['type'] == 'document':
                        await context.bot.send_document(
                            OWNER_ID,
                            file_id,
                            caption=f"📎 مدرک #{complaint_id}\n{caption}" if caption else f"📎 مدرک #{complaint_id}"
                        )
                except Exception as e:
                    logger.error(f"Error forwarding evidence: {e}")
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")

# -----------------------------
# Admin Reply Flow
# -----------------------------

async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    if not await check_access(update, context):
        return ConversationHandler.END

    complaint_id = int(query.data.split('_')[2])
    context.user_data['reply_complaint_id'] = complaint_id
    await query.edit_message_text(
        f"📩 **پاسخ به شکایت #{complaint_id}**\n\n"
        "لطفاً پاسخ خود را به صورت متن ارسال کنید:\n"
        "(برای لغو /cancel بزنید)",
        parse_mode='Markdown',
        reply_markup=cancel_kb()
    )
    set_state(context, S_ADMIN_REPLY_TEXT)
    return S_ADMIN_REPLY_TEXT

async def admin_reply_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    if not await check_access(update, context):
        return ConversationHandler.END

    reply_text = update.message.text
    complaint_id = context.user_data.get('reply_complaint_id')
    if not complaint_id:
        await update.message.reply_text("❌ خطا: شناسه شکایت یافت نشد.")
        return ConversationHandler.END

    db_exec(
        "UPDATE complaints SET status = 'replied', reply_text = ?, replied_at = ? WHERE id = ?",
        (reply_text, now_tehran(), complaint_id)
    )
    log_action(uid, "admin_reply", f"پاسخ به شکایت #{complaint_id}")

    complaint = db_one("SELECT plaintiff_telegram_id, plaintiff_name FROM complaints WHERE id = ?", (complaint_id,))
    if complaint:
        plaintiff_id = complaint['plaintiff_telegram_id']
        plaintiff_name = complaint['plaintiff_name'] or 'کاربر'
        try:
            await context.bot.send_message(
                plaintiff_id,
                f"📩 **پاسخ به شکایت شما #{complaint_id}**\n\n"
                f"پاسخ دادگاه:\n{reply_text}\n\n"
                f"🕐 {now_tehran()}",
                parse_mode='Markdown'
            )
            await update.message.reply_text(
                f"✅ پاسخ شما به شکایت #{complaint_id} با موفقیت ارسال شد.",
                reply_markup=main_menu_kb(uid)
            )
            log_action(uid, "admin_reply", f"پاسخ به شکایت #{complaint_id} - کاربر: {plaintiff_name}")
        except Exception as e:
            logger.error(f"Error sending reply to user: {e}")
            await update.message.reply_text(
                f"⚠️ پاسخ در دیتابیس ثبت شد اما ارسال به کاربر با خطا مواجه شد.",
                reply_markup=main_menu_kb(uid)
            )
    else:
        await update.message.reply_text("❌ شکایت یافت نشد.")

    context.user_data.pop('reply_complaint_id', None)
    set_state(context, None)
    return ConversationHandler.END

# -----------------------------
# Admin Panel
# -----------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    await query.edit_message_text("🛠 **پنل مدیریت**", reply_markup=admin_kb(), parse_mode='Markdown')

async def admin_toggle_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    new_status = "off" if bot_is_on() else "on"
    set_bot_status(new_status)
    log_action(uid, "toggle_bot", f"وضعیت: {'خاموش' if new_status == 'off' else 'روشن'}")
    status_text = "🟢 روشن" if new_status == "on" else "🔴 خاموش"
    await query.edit_message_text(
        f"✅ وضعیت ربات: {status_text}",
        reply_markup=admin_kb()
    )

async def admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    rows = db_all("SELECT * FROM logs ORDER BY id DESC LIMIT 50")
    if not rows:
        await query.edit_message_text("📭 هیچ لاگی وجود ندارد.", reply_markup=admin_kb())
        return

    text = "📋 **لاگ‌های اخیر**\n━━━━━━━━━━━━━━━━━━━\n"
    for row in rows:
        user_info = f"کاربر: {row['user_id']}" if row['user_id'] else "سیستم"
        text += f"🕐 {row['created_at']}\n"
        text += f"👤 {user_info}\n"
        text += f"📌 {row['action']}\n"
        if row['details']:
            text += f"📝 {row['details']}\n"
        text += "━━━━━━━━━━━━━━━━━━━\n"
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=admin_kb())

async def admin_backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    await query.edit_message_text(
        "💾 **پشتیبان‌گیری و بازیابی**",
        reply_markup=backup_kb(),
        parse_mode='Markdown'
    )

async def admin_backup_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    await query.edit_message_text("📥 در حال تهیه پشتیبان... لطفاً صبر کنید.", parse_mode='Markdown')
    try:
        json_data = export_full_backup()
        file_obj = io.BytesIO(json_data.encode('utf-8'))
        file_obj.name = f"court_backup_{datetime.now(TEHRAN).strftime('%Y%m%d_%H%M%S')}.json"
        await context.bot.send_document(
            chat_id=uid,
            document=file_obj,
            caption="💾 **پشتیبان دادگاه کملوت**\n\n"
                    f"🕐 تاریخ: {now_tehran()}\n"
                    "برای بازیابی، از بخش «بازیابی از پشتیبان» استفاده کنید.",
            parse_mode='Markdown'
        )
        log_action(uid, "backup_export", f"تعداد رکوردها: {len(json.loads(json_data).get('complaints', []))}")
        await query.edit_message_text(
            "✅ پشتیبان با موفقیت تهیه و ارسال شد.",
            reply_markup=backup_kb()
        )
    except Exception as e:
        logger.error(f"Export backup error: {e}")
        await query.edit_message_text(f"❌ خطا: {str(e)}", reply_markup=backup_kb())

# Admin backup import conversation handlers
async def admin_backup_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    if not await check_access(update, context):
        return ConversationHandler.END

    await query.edit_message_text(
        "📤 **بازیابی از پشتیبان**\n\n"
        "⚠️ این عملیات تمام اطلاعات فعلی را بازنویسی می‌کند.\n"
        "لطفاً فایل JSON پشتیبان را ارسال کنید.\n"
        "(برای لغو /cancel بزنید)",
        parse_mode='Markdown',
        reply_markup=cancel_kb()
    )
    return S_ADMIN_BACKUP_IMPORT_FILE

async def admin_backup_import_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    if not await check_access(update, context):
        return ConversationHandler.END

    document = update.message.document
    if not document or not document.file_name.endswith('.json'):
        await update.message.reply_text("❌ لطفاً یک فایل JSON معتبر ارسال کنید.", reply_markup=cancel_kb())
        return S_ADMIN_BACKUP_IMPORT_FILE

    try:
        file = await context.bot.get_file(document.file_id)
        content = await file.download_as_bytearray()
        json_data = content.decode('utf-8')
        context.user_data['backup_json_data'] = json_data
        await update.message.reply_text(
            "⚠️ تأیید نهایی: آیا مطمئن هستید؟",
            reply_markup=confirm_kb("admin_backup_import_confirm", "cancel_action")
        )
        return S_ADMIN_BACKUP_CONFIRM
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)}", reply_markup=cancel_kb())
        return ConversationHandler.END

async def admin_backup_import_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    json_data = context.user_data.get('backup_json_data')
    if not json_data:
        await query.edit_message_text("❌ داده‌های پشتیبان یافت نشد.")
        return

    await query.edit_message_text("🔄 در حال بازیابی... لطفاً صبر کنید.", parse_mode='Markdown')
    success, msg = import_full_backup(json_data)
    if success:
        log_action(uid, "backup_import", "بازیابی موفقیت‌آمیز")
        await query.edit_message_text(
            "✅ بازیابی با موفقیت انجام شد.\nلطفاً ربات را ری‌استارت کنید.",
            reply_markup=admin_kb()
        )
    else:
        await query.edit_message_text(f"❌ خطا: {msg}", reply_markup=admin_kb())
    context.user_data.pop('backup_json_data', None)

async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if not is_owner(uid):
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return
    if not await check_access(update, context):
        return

    await query.edit_message_text("🛠 پنل مدیریت", reply_markup=admin_kb(), parse_mode='Markdown')

# -----------------------------
# Callback Handler - برای کالبک‌های عمومی
# -----------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مدیریت تمام کالبک‌هایی که توسط کانورسیشن‌ها گرفته نمی‌شوند"""
    query = update.callback_query
    await query.answer()
    if not await check_access(update, context):
        return

    uid = update.effective_user.id
    data = query.data

    # دکمه لغو عملیات
    if data == "cancel_action":
        set_state(context, None)
        clear_temp(context)
        await query.edit_message_text(
            "❌ عملیات لغو شد. به منوی اصلی بازگشتید.",
            reply_markup=main_menu_kb(uid)
        )
        return

    # دکمه‌های پنل مدیریت
    if data == "admin_panel":
        await admin_panel(update, context)
        return
    if data == "admin_toggle_bot":
        await admin_toggle_bot(update, context)
        return
    if data == "admin_logs":
        await admin_logs(update, context)
        return
    if data == "admin_backup":
        await admin_backup_menu(update, context)
        return
    if data == "admin_backup_export":
        await admin_backup_export(update, context)
        return
    if data == "admin_back":
        await admin_back(update, context)
        return

    # اگر کالبک دیگری بود که مدیریت نشد
    await query.edit_message_text("⚠️ این دکمه معتبر نیست.", reply_markup=main_menu_kb(uid))

# -----------------------------
# Message Handler - برای دکمه‌های منوی اصلی
# -----------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not await check_access(update, context):
        return

    uid = update.effective_user.id
    text = update.message.text

    # دکمه پنل مدیریت (فقط مالک)
    if text == BTN_ADMIN and is_owner(uid):
        await update.message.reply_text("🛠 پنل مدیریت", reply_markup=admin_kb())
        return

    # اگر کاربر پیام دیگری غیر از دکمه‌ها ارسال کرد
    await update.message.reply_text(
        "لطفاً از دکمه‌های منو استفاده کنید یا عملیات جاری را کامل کنید.",
        reply_markup=main_menu_kb(uid)
    )

# ==================== Conversation Handlers ====================

# کانورسیشن ثبت شکایت
complaint_conv = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(complaint_start, pattern="^start_complaint$"),
        MessageHandler(filters.Regex(f"^{BTN_START_COMPLAINT}$"), complaint_start),
    ],
    states={
        S_PLAINTIFF_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, plaintiff_info_handler)],
        S_DEFENDANT_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, defendant_info_handler)],
        S_EVIDENCE: [
            MessageHandler(
                filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                evidence_handler
            ),
            CallbackQueryHandler(evidence_done, pattern="^evidence_done$")
        ],
        S_CONFIRM: [CallbackQueryHandler(submit_complaint, pattern="^submit_complaint$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(cancel, pattern="^cancel_action$"),
        MessageHandler(filters.Regex("^لغو$"), cancel),
    ],
)

# کانورسیشن پاسخ به شکایت توسط مالک
admin_reply_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_reply_start, pattern="^admin_reply_")],
    states={
        S_ADMIN_REPLY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_text_handler)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(cancel, pattern="^cancel_action$"),
        MessageHandler(filters.Regex("^لغو$"), cancel),
    ],
)

# کانورسیشن بازیابی پشتیبان
admin_backup_import_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_backup_import_start, pattern="^admin_backup_import$")],
    states={
        S_ADMIN_BACKUP_IMPORT_FILE: [MessageHandler(filters.Document.ALL, admin_backup_import_file)],
        S_ADMIN_BACKUP_CONFIRM: [CallbackQueryHandler(admin_backup_import_confirm, pattern="^admin_backup_import_confirm$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(cancel, pattern="^cancel_action$"),
        MessageHandler(filters.Regex("^لغو$"), cancel),
    ],
)

# ===================== Main =====================

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    # Conversation handlers
    app.add_handler(complaint_conv)
    app.add_handler(admin_reply_conv)
    app.add_handler(admin_backup_import_conv)

    # Callback handler برای کالبک‌های عمومی
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Message handler برای دکمه‌های منو و پیام‌های دیگر
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("ربات دادگاه عدالت کملوت با موفقیت راه‌اندازی شد.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()