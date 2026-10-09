import os
import json
import sqlite3
import secrets
import string
import asyncio
from datetime import datetime, timedelta, timezone

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# ALI VIP BOT - Configuration
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8932533833:AAFdTtLA_lFN2b1n6z9CW1nInvkrW833FFs").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8821125770"))

GH_TOKEN = os.getenv("GH_TOKEN", "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").strip()
GH_OWNER = os.getenv("GH_OWNER", "ali12hchcfxkzy").strip()
GH_REPO = os.getenv("GH_REPO", "ALI-VIP-BOT-ALI").strip()
GH_BRANCH = os.getenv("GH_BRANCH", "main").strip()
GH_PATH = os.getenv("GH_PATH", "keylist.json").strip()

DB = os.getenv("DB_PATH", "ali_vip.db")
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 20

# =========================================================
# Database
# =========================================================

def db():
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def date_string(value=None):
    return (value or now()).date().isoformat()


def parse_date(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return None


def init_db():
    conn = db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                banned INTEGER NOT NULL DEFAULT 0,
                devices INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                key TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                attempted_key TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()


def log_action(action, key=None, user_id=None):
    conn = db()
    try:
        conn.execute(
            """INSERT INTO logs(user_id, action, key, created_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, action, key, now().isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def record_failed_attempt(user_id, attempted_key):
    conn = db()
    try:
        conn.execute(
            """INSERT INTO failed_attempts(user_id, attempted_key, created_at)
               VALUES (?, ?, ?)""",
            (user_id, attempted_key, now().isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def generate_key():
    alphabet = string.ascii_uppercase + string.digits
    parts = [
        "".join(secrets.choice(alphabet) for _ in range(4))
        for _ in range(4)
    ]
    return "-".join(parts)


def create_key(days):
    if days < 1 or days > 3650:
        raise ValueError("المدة لازم تكون من يوم إلى 3650 يوم.")

    conn = db()
    try:
        created = now()
        expires = created + timedelta(days=days)

        for _ in range(20):
            key = generate_key()
            try:
                conn.execute(
                    """INSERT INTO keys
                       (key, created_at, expires_at, banned, devices, last_seen)
                       VALUES (?, ?, ?, 0, 0, NULL)""",
                    (
                        key,
                        created.isoformat(),
                        expires.isoformat(),
                    )
                )
                conn.commit()
                log_action("CREATE", key)
                return key
            except sqlite3.IntegrityError:
                continue

        raise RuntimeError("تعذر توليد مفتاح فريد.")
    finally:
        conn.close()


def get_key(key):
    conn = db()
    try:
        return conn.execute(
            "SELECT * FROM keys WHERE key = ?",
            (key.strip().upper(),)
        ).fetchone()
    finally:
        conn.close()


def all_keys():
    conn = db()
    try:
        return conn.execute(
            "SELECT * FROM keys ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()


def format_status(row):
    if not row:
        return "❌ المفتاح غير موجود."

    expires = parse_date(row["expires_at"])
    expired = expires is None or expires <= now()

    status = "🚫 محظور" if row["banned"] else (
        "⌛ منتهي" if expired else "✅ فعال"
    )

    last_seen = row["last_seen"] or "لم يسجل"

    return (
        f"🔑 المفتاح: `{row['key']}`\n"
        f"📅 الإنشاء: {row['created_at']}\n"
        f"⏳ الانتهاء: {row['expires_at']}\n"
        f"📌 الحالة: {status}\n"
        f"📱 الأجهزة: {row['devices']}\n"
        f"🕒 آخر ظهور: {last_seen}"
    )


def is_admin(update):
    user = update.effective_user
    return bool(user and ADMIN_ID and user.id == ADMIN_ID)


# =========================================================
# GitHub Contents API
# =========================================================

def github_headers():
    if not GH_TOKEN:
        raise RuntimeError(
            "متغير GH_TOKEN غير مضبوط في إعدادات الاستضافة."
        )

    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_contents_url():
    return (
        f"{GITHUB_API}/repos/{GH_OWNER}/{GH_REPO}"
        f"/contents/{GH_PATH}"
    )


def github_read_keylist():
    response = requests.get(
        github_contents_url(),
        headers=github_headers(),
        params={"ref": GH_BRANCH},
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code == 404:
        return {}, None

    response.raise_for_status()
    payload = response.json()

    import base64

    encoded = payload.get("content", "")
    decoded = base64.b64decode(encoded).decode("utf-8")
    data = json.loads(decoded) if decoded.strip() else {}

    if not isinstance(data, dict):
        raise ValueError("ملف keylist.json لازم يحتوي على JSON object.")

    return data, payload.get("sha")


def local_keys_for_github():
    result = {}

    for row in all_keys():
        expiry = parse_date(row["expires_at"])
        active = bool(
            not row["banned"]
            and expiry is not None
            and expiry > now()
        )

        result[row["key"]] = {
            "active": active,
            "expiry": date_string(expiry) if expiry else "",
            "created_at": date_string(
                parse_date(row["created_at"])
            ) if parse_date(row["created_at"]) else "",
            "banned": bool(row["banned"]),
            "devices": int(row["devices"] or 0),
            "last_seen": row["last_seen"],
        }

    return result


def github_write_keylist(data, sha=None):
    import base64

    content = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
    )

    payload = {
        "message": "Sync ALI VIP keys",
        "content": base64.b64encode(
            content.encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        payload["sha"] = sha

    response = requests.put(
        github_contents_url(),
        headers=github_headers(),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def sync_keys_to_github():
    """
    دمج المفاتيح المحلية مع ملف GitHub.
    بيانات المفاتيح الموجودة محليًا هي الأساس عند تعارض المفتاح نفسه.
    """
    local_data = local_keys_for_github()

    for attempt in range(3):
        remote_data, sha = github_read_keylist()

        if not isinstance(remote_data, dict):
            remote_data = {}

        merged = dict(remote_data)
        merged.update(local_data)

        try:
            github_write_keylist(merged, sha)
            return len(merged)
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if exc.response is not None else None
            )

            # تعارض SHA: أعد القراءة وحاول مرة أخرى.
            if status in (409, 422) and attempt < 2:
                continue

            raise

    raise RuntimeError("فشلت المزامنة بعد عدة محاولات.")


async def sync_async():
    return await asyncio.to_thread(sync_keys_to_github)


# =========================================================
# Key operations
# =========================================================

def set_banned(key, value):
    conn = db()
    try:
        cur = conn.execute(
            "UPDATE keys SET banned = ? WHERE key = ?",
            (int(value), key.strip().upper())
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def restart_key(key):
    conn = db()
    try:
        cur = conn.execute(
            """UPDATE keys
               SET last_seen = NULL, devices = 0
               WHERE key = ?""",
            (key.strip().upper(),)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def renew_key(key, days):
    if days < 1 or days > 3650:
        raise ValueError("المدة لازم تكون من يوم إلى 3650 يوم.")

    row = get_key(key)
    if not row:
        return False

    old_expiry = parse_date(row["expires_at"])
    base = old_expiry if old_expiry and old_expiry > now() else now()
    new_expiry = base + timedelta(days=days)

    conn = db()
    try:
        conn.execute(
            "UPDATE keys SET expires_at = ? WHERE key = ?",
            (new_expiry.isoformat(), key.strip().upper())
        )
        conn.commit()
    finally:
        conn.close()

    log_action("RENEW", key.strip().upper())
    return True


# =========================================================
# Main menu
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ إنشاء مفتاح", callback_data="generate"),
            InlineKeyboardButton("🗂 إدارة المفاتيح", callback_data="manage"),
        ],
        [
            InlineKeyboardButton("📊 الإحصائيات", callback_data="stats"),
            InlineKeyboardButton("🟢 المتصلون", callback_data="online"),
        ],
        [
            InlineKeyboardButton("📜 النشاط", callback_data="activity"),
            InlineKeyboardButton("🚫 المحظورون", callback_data="banned"),
        ],
        [
            InlineKeyboardButton("🧾 السجلات", callback_data="logs"),
            InlineKeyboardButton("⚠️ المحاولات الفاشلة", callback_data="failed"),
        ],
        [
            InlineKeyboardButton("☁️ مزامنة GitHub", callback_data="sync"),
            InlineKeyboardButton("⚙️ النظام", callback_data="system"),
        ],
    ])


async def safe_reply(update, text, **kwargs):
    if update.message:
        await update.message.reply_text(text, **kwargs)
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, **kwargs)


# =========================================================
# Commands
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ هذا البوت مخصص للإدارة.")
        return

    await update.message.reply_text(
        "👑 أهلاً بك في ALI VIP BOT\n\n"
        "اختر من القائمة أو استخدم الأوامر:\n"
        "/gen 30d 5\n"
        "/find KEY\n"
        "/ban KEY\n"
        "/unban KEY\n"
        "/restart KEY\n"
        "/renew KEY 30d\n"
        "/sync",
        reply_markup=main_keyboard(),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    await update.message.reply_text(
        "📋 القائمة الرئيسية",
        reply_markup=main_keyboard(),
    )


def parse_duration(value):
    value = value.strip().lower()

    if value.endswith("d"):
        return int(value[:-1])
    if value.endswith("h"):
        hours = int(value[:-1])
        return max(1, (hours + 23) // 24)
    if value.endswith("m"):
        minutes = int(value[:-1])
        return max(1, (minutes + 1439) // 1440)

    return int(value)


async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    if len(context.args) < 1:
        await update.message.reply_text(
            "الاستخدام:\n/gen 30d\n/gen 30d 5"
        )
        return

    try:
        days = parse_duration(context.args[0])
        count = int(context.args[1]) if len(context.args) > 1 else 1

        if not 1 <= count <= 50:
            raise ValueError("عدد المفاتيح لازم يكون من 1 إلى 50.")
        if not 1 <= days <= 3650:
            raise ValueError("المدة لازم تكون من يوم إلى 3650 يوم.")

        keys = [create_key(days) for _ in range(count)]

        sync_message = ""
        try:
            total = await sync_async()
            sync_message = f"\n☁️ تمت المزامنة. إجمالي المفاتيح: {total}"
        except Exception as exc:
            sync_message = (
                "\n⚠️ تم إنشاء المفاتيح محليًا لكن فشلت المزامنة: "
                f"{str(exc)[:250]}"
            )

        await update.message.reply_text(
            "✅ تم إنشاء المفاتيح:\n\n"
            + "\n".join(f"`{key}`" for key in keys)
            + f"\n\n⏳ المدة: {days} يوم"
            + sync_message,
            parse_mode="Markdown",
        )

    except (ValueError, TypeError) as exc:
        await update.message.reply_text(f"❌ خطأ: {exc}")


async def getconfig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    await update.message.reply_text(
        "⚙️ إعدادات البوت\n"
        f"GitHub repo: {GH_OWNER}/{GH_REPO}\n"
        f"Branch: {GH_BRANCH}\n"
        f"File: {GH_PATH}\n"
        f"Database: {DB}\n"
        f"GitHub token configured: {'نعم' if GH_TOKEN else 'لا'}\n"
        f"Bot token configured: {'نعم' if BOT_TOKEN else 'لا'}"
    )


async def find_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /find KEY")
        return

    row = get_key(context.args[0])
    await update.message.reply_text(
        format_status(row),
        parse_mode="Markdown",
    )


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /ban KEY")
        return

    key = context.args[0].strip().upper()

    if not set_banned(key, True):
        await update.message.reply_text("❌ المفتاح غير موجود.")
        return

    log_action("BAN", key, update.effective_user.id)

    try:
        await sync_async()
        result = "\n☁️ تمت المزامنة مع GitHub."
    except Exception as exc:
        result = f"\n⚠️ فشلت المزامنة: {str(exc)[:200]}"

    await update.message.reply_text("🚫 تم حظر المفتاح." + result)


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /unban KEY")
        return

    key = context.args[0].strip().upper()

    if not set_banned(key, False):
        await update.message.reply_text("❌ المفتاح غير موجود.")
        return

    log_action("UNBAN", key, update.effective_user.id)

    try:
        await sync_async()
        result = "\n☁️ تمت المزامنة مع GitHub."
    except Exception as exc:
        result = f"\n⚠️ فشلت المزامنة: {str(exc)[:200]}"

    await update.message.reply_text("✅ تم إلغاء حظر المفتاح." + result)


async def restart_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /restart KEY")
        return

    key = context.args[0].strip().upper()

    if not restart_key_db(key):
        await update.message.reply_text("❌ المفتاح غير موجود.")
        return

    log_action("RESTART", key, update.effective_user.id)

    try:
        await sync_async()
        result = "\n☁️ تمت المزامنة مع GitHub."
    except Exception as exc:
        result = f"\n⚠️ فشلت المزامنة: {str(exc)[:200]}"

    await update.message.reply_text("🔄 تمت إعادة ضبط بيانات المفتاح." + result)


def restart_key_db(key):
    return restart_key(key)


async def renew_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("الاستخدام: /renew KEY 30d")
        return

    key = context.args[0].strip().upper()

    try:
        days = parse_duration(context.args[1])
        if not renew_key(key, days):
            await update.message.reply_text("❌ المفتاح غير موجود.")
            return

        try:
            await sync_async()
            result = "\n☁️ تمت المزامنة مع GitHub."
        except Exception as exc:
            result = f"\n⚠️ فشلت المزامنة: {str(exc)[:200]}"

        await update.message.reply_text(
            f"✅ تم تمديد المفتاح {key} لمدة {days} يوم." + result
        )

    except (ValueError, TypeError) as exc:
        await update.message.reply_text(f"❌ خطأ: {exc}")


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ غير مسموح.")
        return

    await update.message.reply_text("⏳ جاري مزامنة المفاتيح مع GitHub...")

    try:
        total = await sync_async()
        await update.message.reply_text(
            f"✅ تمت المزامنة بنجاح.\n☁️ إجمالي المفاتيح: {total}"
        )
    except Exception as exc:
        await update.message.reply_text(
            f"❌ فشلت المزامنة:\n{str(exc)[:1000]}"
        )


# =========================================================
# Button pages
# =========================================================

async def generate_button(query):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("يوم واحد", callback_data="gen_1"),
            InlineKeyboardButton("7 أيام", callback_data="gen_7"),
        ],
        [
            InlineKeyboardButton("30 يوم", callback_data="gen_30"),
            InlineKeyboardButton("90 يوم", callback_data="gen_90"),
        ],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="home")],
    ])

    await query.edit_message_text(
        "➕ إنشاء مفتاح جديد\nاختر مدة المفتاح:",
        reply_markup=keyboard,
    )


async def stats_button(query):
    rows = all_keys()
    total = len(rows)
    active = 0
    banned_count = 0
    expired_count = 0

    for row in rows:
        expiry = parse_date(row["expires_at"])

        if row["banned"]:
            banned_count += 1
        
            expired_count += 1
        else:
            active += 1

    conn = db()
    try:
        logs_count = conn.execute(
            "SELECT COUNT(*) FROM logs"
        ).fetchone()[0]
    finally:
        conn.close()

    await query.edit_message_text(
        "📊 إحصائيات البوت\n\n"
        f"🔑 إجمالي المفاتيح: {total}\n"
        f"✅ مفاتيح فعالة: {active}\n"
        f"🚫 مفاتيح محظورة: {banned_count}\n"
        f"⌛ مفاتيح منتهية: {expired_count}\n"
        f"🧾 عدد السجلات: {logs_count}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def online_button(query):
    rows = all_keys()
    recent_limit = now() - timedelta(minutes=10)
    online = 0

    for row in rows:
        last = parse_date(row["last_seen"])
        expiry = parse_date(row["expires_at"])

        if (
            last and last >= recent_limit
            and not row["banned"]
            and expiry and expiry > now()
        ):
            online += 1

    await query.edit_message_text(
        f"🟢 المفاتيح التي سجلت ظهورًا آخر 10 دقائق: {online}\n"
        "ملاحظة: يلزم نظام التحقق تحديث last_seen.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def activity_button(query):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    text = "📜 لا يوجد نشاط مسجل." if not rows else (
        "📜 آخر الأنشطة:\n\n" + "\n".join(
            f"• {r['created_at']} | {r['action']} | {r['key'] or '-'}"
            for r in rows
        )
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def banned_button(query):
    rows = [r for r in all_keys() if r["banned"]]
    text = "🚫 لا توجد مفاتيح محظورة." if not rows else (
        "🚫 المفاتيح المحظورة:\n\n" + "\n".join(
            f"• {r['key']}" for r in rows[:30]
        )
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def logs_button(query):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()

    text = "🧾 لا توجد سجلات." if not rows else (
        "🧾 آخر السجلات:\n\n" + "\n".join(
            f"#{r['id']} | {r['action']} | {r['key'] or '-'} | "
            f"{r['created_at']}"
            for r in rows
        )
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def failed_button(query):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM failed_attempts ORDER BY id DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()

    text = "⚠️ لا توجد محاولات فاشلة مسجلة." if not rows else (
        "⚠️ آخر المحاولات الفاشلة:\n\n" + "\n".join(
            f"• {r['created_at']} | user: {r['user_id']} | "
            f"key: {r['attempted_key']}"
            for r in rows
        )
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def system_button(query):
    await query.edit_message_text(
        "⚙️ حالة النظام\n\n"
        f"Bot token: {'موجود' if BOT_TOKEN else 'ناقص'}\n"
        f"Admin ID: {'موجود' if ADMIN_ID else 'ناقص'}\n"
        f"GitHub token: {'موجود' if GH_TOKEN else 'ناقص'}\n"
        f"Repository: {GH_OWNER}/{GH_REPO}\n"
        f"Branch: {GH_BRANCH}\n"
        f"File: {GH_PATH}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def manage_button(query):
    rows = all_keys()[:15]

    text = "🗂 لا توجد مفاتيح." if not rows else (
        "🗂 أحدث المفاتيح:\n\n" + "\n".join(
            f"• {r['key']} — "
            f"{'محظور' if r['banned'] else 'غير محظور'}"
            for r in rows
        )
    )

    text += (
        "\n\nالأوامر:\n/find KEY\n/ban KEY\n/unban KEY\n"
        "/restart KEY\n/renew KEY 30d"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
        ]),
    )


async def button_handler(update, context):
    query = update.callback_query

    if not is_admin(update):
        await query.answer("غير مسموح.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "home":
        await query.edit_message_text(
            "👑 ALI VIP BOT\nاختر العملية:",
            reply_markup=main_keyboard(),
        )

    elif data == "generate":
        await generate_button(query)

    elif data.startswith("gen_"):
        try:
            days = int(data.split("_", 1)[1])
            key = create_key(days)

            try:
                total = await sync_async()
                sync_text = f"\n☁️ تمت المزامنة. الإجمالي: {total}"
            except Exception as exc:
                sync_text = f"\n⚠️ فشلت المزامنة: {str(exc)[:200]}"

            await query.edit_message_text(
                f"✅ المفتاح: `{key}`\n⏳ المدة: {days} يوم{sync_text}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
                ]),
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ خطأ: {str(exc)[:500]}",
                reply_markup=main_keyboard(),
            )

    elif data == "stats":
        await stats_button(query)
    elif data == "online":
        await online_button(query)
    elif data == "activity":
        await activity_button(query)
    elif data == "banned":
        await banned_button(query)
    elif data == "logs":
        await logs_button(query)
    elif data == "failed":
        await failed_button(query)
    elif data == "system":
        await system_button(query)
    elif data == "manage":
        await manage_button(query)

    elif data == "sync":
        await query.edit_message_text("⏳ جاري المزامنة...")
        try:
            total = await sync_async()
            await query.edit_message_text(
                f"✅ تمت المزامنة.\nإجمالي المفاتيح: {total}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ رجوع", callback_data="home")]
                ]),
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ فشلت المزامنة:\n{str(exc)[:800]}",
                reply_markup=main_keyboard(),
            )


async def error_handler(update, context):
    print(f"[ERROR] {context.error}")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("اضبط BOT_TOKEN في متغيرات البيئة.")
    if not ADMIN_ID:
        raise RuntimeError("اضبط ADMIN_ID في متغيرات البيئة.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("gen", gen))
    app.add_handler(CommandHandler("getconfig", getconfig))
    app.add_handler(CommandHandler("find", find_key))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("restart", restart_key))
    app.add_handler(CommandHandler("renew", renew_command))
    app.add_handler(CommandHandler("sync", sync_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    print("ALI VIP BOT is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
