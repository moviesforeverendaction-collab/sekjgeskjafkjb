# ============================================================
# Telegram Temp Mail Bot
# Stack: Pyrogram + Motor (MongoDB) + aiohttp
# ============================================================

import os
import asyncio
import logging
import hashlib
import random
import string
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("TempMailBot")

# ============================================================
# Environment Variables
# ============================================================
BOT_TOKEN  = os.environ["BOT_TOKEN"]
API_ID     = int(os.environ["API_ID"])
API_HASH   = os.environ["API_HASH"]
MONGO_URI  = os.environ["MONGO_URI"]

EMAIL_TTL_MINUTES  = int(os.getenv("EMAIL_TTL_MINUTES", "60"))
MAX_EMAILS_PER_USER = int(os.getenv("MAX_EMAILS_PER_USER", "5"))
RATE_LIMIT_COUNT   = int(os.getenv("RATE_LIMIT_COUNT", "3"))
RATE_LIMIT_WINDOW  = int(os.getenv("RATE_LIMIT_WINDOW", "60"))  # seconds

# ============================================================
# Shared aiohttp session (set at startup)
# ============================================================
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=15)
        _session = aiohttp.ClientSession(timeout=timeout)
    return _session

# ============================================================
# Base API class
# ============================================================
class TempMailAPI(ABC):
    name: str = "base"
    # When an API fails it is marked dead until this many seconds pass
    COOLDOWN: int = 300

    def __init__(self):
        self._dead_until: float = 0.0

    def is_alive(self) -> bool:
        return time.monotonic() >= self._dead_until

    def mark_dead(self):
        logger.warning(f"[{self.name}] marked dead for {self.COOLDOWN}s")
        self._dead_until = time.monotonic() + self.COOLDOWN

    def mark_alive(self):
        self._dead_until = 0.0

    @abstractmethod
    async def generate_email(self) -> Optional[str]:
        """Return a fresh temporary email address, or None on failure."""

    @abstractmethod
    async def get_messages(self, email: str) -> list[dict]:
        """Return list of normalised message dicts."""

    # Helper
    @staticmethod
    def _norm(from_: str = "", subject: str = "", body: str = "", date: str = "") -> dict:
        return {"from": from_, "subject": subject, "body": body, "date": date}

# ============================================================
# 1. 1secmail
# ============================================================
class OneSec(TempMailAPI):
    name = "1secmail"
    BASE = "https://www.1secmail.com/api/v1/"
    DOMAINS = ["1secmail.com", "1secmail.org", "1secmail.net",
               "esiix.com", "wwjmp.com"]

    async def generate_email(self) -> Optional[str]:
        try:
            session = await get_session()
            local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            domain = random.choice(self.DOMAINS)
            return f"{local}@{domain}"
        except Exception as e:
            logger.error(f"[{self.name}] generate_email: {e}")
            return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            local, domain = email.split("@")
            session = await get_session()
            url = f"{self.BASE}?action=getMessages&login={local}&domain={domain}"
            async with session.get(url) as r:
                msgs = await r.json(content_type=None)
            result = []
            for m in msgs:
                detail_url = f"{self.BASE}?action=readMessage&login={local}&domain={domain}&id={m['id']}"
                async with session.get(detail_url) as dr:
                    d = await dr.json(content_type=None)
                result.append(self._norm(
                    from_=d.get("from", ""),
                    subject=d.get("subject", ""),
                    body=d.get("textBody", d.get("htmlBody", ""))[:1000],
                    date=d.get("date", ""),
                ))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 2. Guerrilla Mail
# ============================================================
class GuerrillaAPI(TempMailAPI):
    name = "guerrilla"
    BASE = "https://api.guerrillamail.com/ajax.php"

    async def generate_email(self) -> Optional[str]:
        try:
            session = await get_session()
            async with session.get(self.BASE, params={"f": "get_email_address"}) as r:
                data = await r.json(content_type=None)
            return data.get("email_addr")
        except Exception as e:
            logger.error(f"[{self.name}] generate_email: {e}")
            return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            local = email.split("@")[0]
            session = await get_session()
            async with session.get(self.BASE, params={"f": "get_email_list", "offset": 0, "alias": local}) as r:
                data = await r.json(content_type=None)
            msgs = data.get("list", [])
            result = []
            for m in msgs:
                result.append(self._norm(
                    from_=m.get("mail_from", ""),
                    subject=m.get("mail_subject", ""),
                    body=m.get("mail_excerpt", ""),
                    date=m.get("mail_date", ""),
                ))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 3. mail.tm
# ============================================================
class MailTM(TempMailAPI):
    name = "mail.tm"
    BASE = "https://api.mail.tm"

    async def _get_domain(self, session) -> Optional[str]:
        async with session.get(f"{self.BASE}/domains") as r:
            data = await r.json(content_type=None)
        domains = data.get("hydra:member", [])
        return domains[0]["domain"] if domains else None

    async def generate_email(self) -> Optional[str]:
        try:
            session = await get_session()
            domain = await self._get_domain(session)
            if not domain:
                return None
            local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            password = "TmpBot#" + "".join(random.choices(string.digits, k=8))
            payload = {"address": f"{local}@{domain}", "password": password}
            async with session.post(f"{self.BASE}/accounts", json=payload) as r:
                acc = await r.json(content_type=None)
            email_addr = acc.get("address")
            if not email_addr:
                return None
            # store credentials encoded in email field — decoded at inbox time
            # Format: address|password  (we store this in mongo under meta)
            return f"{email_addr}|{password}"
        except Exception as e:
            logger.error(f"[{self.name}] generate_email: {e}")
            return None

    async def _token(self, session, address: str, password: str) -> Optional[str]:
        async with session.post(f"{self.BASE}/token", json={"address": address, "password": password}) as r:
            data = await r.json(content_type=None)
        return data.get("token")

    async def get_messages(self, email: str) -> list[dict]:
        try:
            if "|" not in email:
                return []
            address, password = email.split("|", 1)
            session = await get_session()
            token = await self._token(session, address, password)
            if not token:
                return []
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(f"{self.BASE}/messages", headers=headers) as r:
                data = await r.json(content_type=None)
            msgs = data.get("hydra:member", [])
            result = []
            for m in msgs:
                mid = m["id"]
                async with session.get(f"{self.BASE}/messages/{mid}", headers=headers) as dr:
                    d = await dr.json(content_type=None)
                result.append(self._norm(
                    from_=d.get("from", {}).get("address", ""),
                    subject=d.get("subject", ""),
                    body=d.get("text", d.get("html", ""))[:1000],
                    date=d.get("createdAt", ""),
                ))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 4. Dispostable (dispostable.com)
# ============================================================
class Dispostable(TempMailAPI):
    name = "dispostable"
    BASE = "https://www.dispostable.com/api"
    DOMAIN = "dispostable.com"

    async def generate_email(self) -> Optional[str]:
        try:
            local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            return f"{local}@{self.DOMAIN}"
        except Exception as e:
            logger.error(f"[{self.name}] generate_email: {e}")
            return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            local = email.split("@")[0]
            session = await get_session()
            url = f"{self.BASE}/list-messages/?username={local}"
            async with session.get(url) as r:
                data = await r.json(content_type=None)
            msgs = data.get("messages", [])
            result = []
            for m in msgs:
                result.append(self._norm(
                    from_=m.get("sender", ""),
                    subject=m.get("subject", ""),
                    body=m.get("text", "")[:1000],
                    date=m.get("created_at", ""),
                ))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 5. Mailnesia
# ============================================================
class Mailnesia(TempMailAPI):
    name = "mailnesia"
    DOMAIN = "mailnesia.com"

    async def generate_email(self) -> Optional[str]:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"{local}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            local = email.split("@")[0]
            session = await get_session()
            url = f"https://mailnesia.com/mailbox/{local}"
            headers = {"Accept": "application/json"}
            async with session.get(url, headers=headers) as r:
                text = await r.text()
            # Mailnesia returns HTML; basic scrape for subject lines
            import re
            subjects = re.findall(r'<td class="subject"[^>]*>(.*?)</td>', text, re.DOTALL)
            froms    = re.findall(r'<td class="from"[^>]*>(.*?)</td>',    text, re.DOTALL)
            dates    = re.findall(r'<td class="date"[^>]*>(.*?)</td>',    text, re.DOTALL)
            def clean(s):
                return re.sub(r'<[^>]+>', '', s).strip()
            return [
                self._norm(from_=clean(froms[i]) if i < len(froms) else "",
                           subject=clean(subjects[i]),
                           body="",
                           date=clean(dates[i]) if i < len(dates) else "")
                for i in range(len(subjects))
            ]
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 6. TempMail.Plus
# ============================================================
class TempMailPlus(TempMailAPI):
    name = "tempmail.plus"
    BASE = "https://tempmail.plus/api"
    DOMAINS = ["tempmail.plus", "fakemail.net", "mailtemp.info"]

    async def generate_email(self) -> Optional[str]:
        try:
            local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            domain = random.choice(self.DOMAINS)
            return f"{local}@{domain}"
        except Exception as e:
            logger.error(f"[{self.name}] generate_email: {e}")
            return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            session = await get_session()
            url = f"{self.BASE}/mails?email={email}&limit=20&epin="
            async with session.get(url) as r:
                data = await r.json(content_type=None)
            mail_list = data.get("mail_list", [])
            result = []
            for m in mail_list:
                result.append(self._norm(
                    from_=m.get("from_mail", ""),
                    subject=m.get("subject", ""),
                    body=m.get("text", "")[:1000],
                    date=m.get("date", ""),
                ))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 7. MailSlurp-style: inboxes via trashmail.at
# ============================================================
class TrashMail(TempMailAPI):
    name = "trashmail"
    DOMAIN = "trashmail.at"

    async def generate_email(self) -> Optional[str]:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"{local}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            local = email.split("@")[0]
            session = await get_session()
            url = f"https://trashmail.at/api/json/v1/mailbox/{local}"
            async with session.get(url) as r:
                data = await r.json(content_type=None)
            msgs = data if isinstance(data, list) else []
            return [
                self._norm(
                    from_=m.get("from", ""),
                    subject=m.get("subject", ""),
                    body=m.get("body", "")[:1000],
                    date=m.get("date", ""),
                )
                for m in msgs
            ]
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 8. Temp-Mail.org (rapidapi fallback style)
# ============================================================
class TempMailOrg(TempMailAPI):
    name = "tempmail.org"
    DOMAINS = ["temp-mail.org", "tempr.email", "discard.email"]

    async def generate_email(self) -> Optional[str]:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        domain = random.choice(self.DOMAINS)
        return f"{local}@{domain}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            md5 = hashlib.md5(email.lower().encode()).hexdigest()
            session = await get_session()
            url = f"https://www.temporary-email.com/api/v1/mailbox/{md5}"
            async with session.get(url) as r:
                data = await r.json(content_type=None)
            msgs = data if isinstance(data, list) else []
            return [
                self._norm(
                    from_=m.get("from", ""),
                    subject=m.get("subject", ""),
                    body=m.get("body", "")[:1000],
                    date=m.get("date", ""),
                )
                for m in msgs
            ]
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 9. YOPmail
# ============================================================
class YOPmail(TempMailAPI):
    name = "yopmail"
    DOMAIN = "yopmail.com"

    async def generate_email(self) -> Optional[str]:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"{local}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            import re
            local = email.split("@")[0]
            session = await get_session()
            url = f"https://yopmail.com/en/inbox?login={local}&p=1&d=&ctrl=&scrl=&spam=true&yf=YN3AuGmZuLmZGRmZuT0yGN3A"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as r:
                text = await r.text()
            subjects = re.findall(r'class="lms">(.*?)</span>', text)
            froms    = re.findall(r'class="lmf">(.*?)</span>', text)
            def clean(s): return re.sub(r'<[^>]+>', '', s).strip()
            return [
                self._norm(
                    from_=clean(froms[i]) if i < len(froms) else "",
                    subject=clean(subjects[i]),
                    body="",
                    date="",
                )
                for i in range(len(subjects))
            ]
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# 10. FakeMail (fakemail.net alternative)
# ============================================================
class FakeMail(TempMailAPI):
    name = "fakemail"
    BASE = "https://www.fakemail.net/index/index"

    async def generate_email(self) -> Optional[str]:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"{local}@fakemail.net"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            import re
            local = email.split("@")[0]
            session = await get_session()
            headers = {"User-Agent": "Mozilla/5.0"}
            url = f"https://www.fakemail.net/{local}"
            async with session.get(url, headers=headers) as r:
                text = await r.text()
            rows = re.findall(r'<tr[^>]*data-mailid[^>]*>(.*?)</tr>', text, re.DOTALL)
            result = []
            for row in rows:
                subj = re.search(r'class="subject"[^>]*>(.*?)<', row)
                frm  = re.search(r'class="from"[^>]*>(.*?)<', row)
                def g(m): return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ""
                result.append(self._norm(from_=g(frm), subject=g(subj), body="", date=""))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] get_messages: {e}")
            return []

# ============================================================
# API Registry & Smart Rotation
# ============================================================
ALL_APIS: list[TempMailAPI] = [
    OneSec(),
    GuerrillaAPI(),
    MailTM(),
    Dispostable(),
    Mailnesia(),
    TempMailPlus(),
    TrashMail(),
    TempMailOrg(),
    YOPmail(),
    FakeMail(),
]

_api_index = 0  # round-robin cursor

async def pick_and_generate() -> tuple[Optional[str], Optional[str]]:
    """
    Try APIs in round-robin order, skipping dead ones.
    Returns (email, api_name) or (None, None) if all fail.
    """
    global _api_index
    alive = [a for a in ALL_APIS if a.is_alive()]
    if not alive:
        # Reset all and try again
        for a in ALL_APIS:
            a.mark_alive()
        alive = list(ALL_APIS)

    # rotate starting point
    random.shuffle(alive)
    for api in alive:
        try:
            email = await api.generate_email()
            if email:
                api.mark_alive()
                logger.info(f"Email generated via [{api.name}]: {email.split('|')[0]}")
                return email, api.name
            else:
                api.mark_dead()
        except Exception as e:
            logger.error(f"[{api.name}] unexpected: {e}")
            api.mark_dead()
    return None, None

async def fetch_inbox(email: str, api_name: str) -> list[dict]:
    """Find the correct API by name and fetch its inbox."""
    api_map = {a.name: a for a in ALL_APIS}
    api = api_map.get(api_name)
    if not api:
        return []
    try:
        return await api.get_messages(email)
    except Exception as e:
        logger.error(f"[{api_name}] fetch_inbox: {e}")
        return []

# ============================================================
# MongoDB Database Layer
# ============================================================
_db = None

def get_db():
    global _db
    if _db is None:
        client = AsyncIOMotorClient(MONGO_URI)
        _db = client["tempmail_bot"]
    return _db

async def db_add_email(user_id: int, email: str, api_name: str):
    col = get_db()["emails"]
    await col.insert_one({
        "user_id":    user_id,
        "email":      email,
        "api_name":   api_name,
        "created_at": datetime.now(timezone.utc),
    })

async def db_get_emails(user_id: int) -> list[dict]:
    col = get_db()["emails"]
    cursor = col.find({"user_id": user_id}).sort("created_at", -1)
    return await cursor.to_list(length=MAX_EMAILS_PER_USER + 10)

async def db_get_email_doc(user_id: int, email: str) -> Optional[dict]:
    col = get_db()["emails"]
    return await col.find_one({"user_id": user_id, "email": email})

async def db_delete_email(user_id: int, email: str):
    col = get_db()["emails"]
    await col.delete_one({"user_id": user_id, "email": email})

async def db_count_emails(user_id: int) -> int:
    col = get_db()["emails"]
    return await col.count_documents({"user_id": user_id})

async def db_ensure_user(user_id: int, username: str, first_name: str):
    col = get_db()["users"]
    await col.update_one(
        {"user_id": user_id},
        {"$set": {"username": username, "first_name": first_name, "last_seen": datetime.now(timezone.utc)},
         "$setOnInsert": {"joined_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

async def db_cleanup_expired():
    """Delete emails older than EMAIL_TTL_MINUTES."""
    col = get_db()["emails"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EMAIL_TTL_MINUTES)
    result = await col.delete_many({"created_at": {"$lt": cutoff}})
    if result.deleted_count:
        logger.info(f"Cleanup: removed {result.deleted_count} expired emails.")

# ============================================================
# Rate Limiter (in-memory, per user)
# ============================================================
_rate_store: dict[int, list[float]] = {}

def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    window = _rate_store.setdefault(user_id, [])
    # drop old entries
    _rate_store[user_id] = [t for t in window if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_store[user_id]) >= RATE_LIMIT_COUNT:
        return True
    _rate_store[user_id].append(now)
    return False

# ============================================================
# UI Helpers
# ============================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Generate Email", callback_data="gen_email")],
        [InlineKeyboardButton("📋 My Emails",      callback_data="my_emails"),
         InlineKeyboardButton("📥 Inbox",          callback_data="inbox_select")],
        [InlineKeyboardButton("ℹ️ Help",            callback_data="help")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]])

def email_list_kb(emails: list[dict], action_prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for doc in emails:
        display_email = doc["email"].split("|")[0]  # strip mail.tm password
        rows.append([InlineKeyboardButton(
            f"📧 {display_email}",
            callback_data=f"{action_prefix}:{display_email}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_home")])
    return InlineKeyboardMarkup(rows)

def email_actions_kb(email: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 View Inbox", callback_data=f"view_inbox:{email}")],
        [InlineKeyboardButton("🗑 Delete",      callback_data=f"del_email:{email}")],
        [InlineKeyboardButton("⬅️ Back",        callback_data="my_emails")],
    ])

# ============================================================
# Pyrogram App
# ============================================================
app = Client(
    "tempmail_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ============================================================
# /start
# ============================================================
@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, message: Message):
    user = message.from_user
    await db_ensure_user(user.id, user.username or "", user.first_name or "")
    text = (
        f"👋 **Hello, {user.first_name}!**\n\n"
        "🔒 Welcome to **Temp Mail Bot**.\n"
        "Get free disposable emails instantly — no registration needed.\n\n"
        f"📌 Emails auto-expire after **{EMAIL_TTL_MINUTES} minutes**.\n"
        f"📌 Max **{MAX_EMAILS_PER_USER}** active emails per account.\n\n"
        "Use the buttons below to get started:"
    )
    await message.reply(text, reply_markup=main_menu_kb())

# ============================================================
# Callback: back_home
# ============================================================
@app.on_callback_query(filters.regex("^back_home$"))
async def cb_back_home(_, cq: CallbackQuery):
    await cq.message.edit_text(
        "🏠 **Main Menu** — choose an action:",
        reply_markup=main_menu_kb(),
    )

# ============================================================
# Callback: help
# ============================================================
@app.on_callback_query(filters.regex("^help$"))
async def cb_help(_, cq: CallbackQuery):
    text = (
        "📖 **Help & Commands**\n\n"
        "• **Generate Email** — creates a new disposable inbox\n"
        "• **My Emails** — lists all your active inboxes\n"
        "• **Inbox** — check messages for a chosen email\n\n"
        f"⏳ Emails expire after **{EMAIL_TTL_MINUTES} min**.\n"
        f"⚡ Rate limit: **{RATE_LIMIT_COUNT}** generations per {RATE_LIMIT_WINDOW}s.\n"
        f"📦 Max emails: **{MAX_EMAILS_PER_USER}** per user.\n\n"
        "Powered by 10 temp-mail providers with automatic fallback."
    )
    await cq.message.edit_text(text, reply_markup=back_kb())

# ============================================================
# Callback: gen_email
# ============================================================
@app.on_callback_query(filters.regex("^gen_email$"))
async def cb_gen_email(_, cq: CallbackQuery):
    user_id = cq.from_user.id

    # Rate limit check
    if is_rate_limited(user_id):
        await cq.answer(
            f"⏳ Slow down! Max {RATE_LIMIT_COUNT} emails per {RATE_LIMIT_WINDOW}s.",
            show_alert=True,
        )
        return

    # Max email check
    count = await db_count_emails(user_id)
    if count >= MAX_EMAILS_PER_USER:
        await cq.answer(
            f"⚠️ You have reached the limit of {MAX_EMAILS_PER_USER} emails. Delete one first.",
            show_alert=True,
        )
        return

    await cq.message.edit_text("⏳ Generating a fresh email address…")

    email, api_name = await pick_and_generate()
    if not email:
        await cq.message.edit_text(
            "❌ All providers are currently unavailable. Please try again in a moment.",
            reply_markup=back_kb(),
        )
        return

    await db_add_email(user_id, email, api_name)
    display_email = email.split("|")[0]

    text = (
        "✅ **New Email Generated!**\n\n"
        f"`{display_email}`\n\n"
        f"🔌 Provider: `{api_name}`\n"
        f"⏳ Expires in: **{EMAIL_TTL_MINUTES} minutes**\n\n"
        "Tap the email above to copy it."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Check Inbox", callback_data=f"view_inbox:{display_email}")],
        [InlineKeyboardButton("🗑 Delete",       callback_data=f"del_email:{display_email}")],
        [InlineKeyboardButton("⬅️ Back",         callback_data="back_home")],
    ])
    await cq.message.edit_text(text, reply_markup=kb)

# ============================================================
# Callback: my_emails
# ============================================================
@app.on_callback_query(filters.regex("^my_emails$"))
async def cb_my_emails(_, cq: CallbackQuery):
    user_id = cq.from_user.id
    emails = await db_get_emails(user_id)
    if not emails:
        await cq.message.edit_text(
            "📭 You have no active emails.\nTap **Generate Email** to create one.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✉️ Generate Email", callback_data="gen_email"),
                InlineKeyboardButton("⬅️ Back",           callback_data="back_home"),
            ]]),
        )
        return
    text = f"📋 **Your Active Emails** ({len(emails)}/{MAX_EMAILS_PER_USER})\n\nSelect an email to manage it:"
    await cq.message.edit_text(text, reply_markup=email_list_kb(emails, "manage_email"))

# ============================================================
# Callback: manage_email:<addr>
# ============================================================
@app.on_callback_query(filters.regex(r"^manage_email:(.+)$"))
async def cb_manage_email(_, cq: CallbackQuery):
    email = cq.data.split(":", 1)[1]
    doc   = await db_get_email_doc(cq.from_user.id, email)
    if not doc:
        # try with mail.tm pipe
        docs = await db_get_emails(cq.from_user.id)
        doc  = next((d for d in docs if d["email"].split("|")[0] == email), None)

    if not doc:
        await cq.answer("Email not found.", show_alert=True)
        return

    display = doc["email"].split("|")[0]
    created = doc["created_at"].strftime("%Y-%m-%d %H:%M UTC") if isinstance(doc["created_at"], datetime) else "Unknown"
    text = (
        f"📧 **Email Details**\n\n"
        f"`{display}`\n\n"
        f"🔌 Provider: `{doc['api_name']}`\n"
        f"📅 Created: `{created}`\n\n"
        "Choose an action:"
    )
    await cq.message.edit_text(text, reply_markup=email_actions_kb(display))

# ============================================================
# Callback: del_email:<addr>
# ============================================================
@app.on_callback_query(filters.regex(r"^del_email:(.+)$"))
async def cb_del_email(_, cq: CallbackQuery):
    email   = cq.data.split(":", 1)[1]
    user_id = cq.from_user.id
    # Also match mail.tm composite emails
    docs = await db_get_emails(user_id)
    target = next((d for d in docs if d["email"].split("|")[0] == email), None)
    if target:
        await db_delete_email(user_id, target["email"])
        await cq.answer("🗑 Email deleted.", show_alert=False)
        await cq.message.edit_text("✅ Email deleted successfully.", reply_markup=back_kb())
    else:
        await cq.answer("Email not found.", show_alert=True)

# ============================================================
# Callback: inbox_select — pick email from list
# ============================================================
@app.on_callback_query(filters.regex("^inbox_select$"))
async def cb_inbox_select(_, cq: CallbackQuery):
    user_id = cq.from_user.id
    emails  = await db_get_emails(user_id)
    if not emails:
        await cq.message.edit_text(
            "📭 No emails yet. Generate one first!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✉️ Generate Email", callback_data="gen_email"),
                InlineKeyboardButton("⬅️ Back",           callback_data="back_home"),
            ]]),
        )
        return
    await cq.message.edit_text(
        "📥 **Select an inbox to check:**",
        reply_markup=email_list_kb(emails, "view_inbox"),
    )

# ============================================================
# Callback: view_inbox:<addr>
# ============================================================
@app.on_callback_query(filters.regex(r"^view_inbox:(.+)$"))
async def cb_view_inbox(_, cq: CallbackQuery):
    email   = cq.data.split(":", 1)[1]
    user_id = cq.from_user.id

    # Resolve full email record (may include mail.tm password)
    docs   = await db_get_emails(user_id)
    target = next((d for d in docs if d["email"].split("|")[0] == email), None)
    if not target:
        await cq.answer("Email not found.", show_alert=True)
        return

    await cq.message.edit_text(f"📬 Checking inbox for `{email}`…")

    messages = await fetch_inbox(target["email"], target["api_name"])

    if not messages:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"view_inbox:{email}")],
            [InlineKeyboardButton("⬅️ Back",    callback_data="my_emails")],
        ])
        await cq.message.edit_text(
            f"📭 **Inbox empty** for:\n`{email}`\n\nNo messages received yet.",
            reply_markup=kb,
        )
        return

    lines = [f"📥 **Inbox** — `{email}`\n{len(messages)} message(s) found:\n"]
    for i, m in enumerate(messages[:10], 1):
        lines.append(
            f"──────────────\n"
            f"**#{i}**\n"
            f"👤 From: `{m['from'] or 'Unknown'}`\n"
            f"📌 Subject: {m['subject'] or '(no subject)'}\n"
            f"📅 Date: {m['date'] or 'N/A'}\n"
            f"💬 {(m['body'] or '').strip()[:300] or '(no body)'}"
        )

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"view_inbox:{email}")],
        [InlineKeyboardButton("⬅️ Back",    callback_data="my_emails")],
    ])
    # Telegram message limit safety
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    await cq.message.edit_text(text, reply_markup=kb)

# ============================================================
# Background: periodic cleanup task
# ============================================================
async def cleanup_loop():
    while True:
        try:
            await db_cleanup_expired()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(300)  # every 5 minutes

# ============================================================
# Entry point
# ============================================================
async def main():
    logger.info("Starting Temp Mail Bot…")
    asyncio.create_task(cleanup_loop())
    await app.start()
    me = await app.get_me()
    logger.info(f"Bot running as @{me.username}")
    await asyncio.Event().wait()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
