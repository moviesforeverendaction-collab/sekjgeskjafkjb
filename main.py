#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════════╗
# ║        ULTRA ADVANCED TELEGRAM TEMP MAIL BOT v3.0              ║
# ║   Pyrogram · Motor/MongoDB · aiohttp · Render-Ready            ║
# ║   10 Providers · Live Notifications · Admin Panel · Cache      ║
# ╚══════════════════════════════════════════════════════════════════╝

import os, asyncio, logging, hashlib, random, string, time, re, html
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# SINGLE-LOOP BOOTSTRAP  (Python 3.10 – 3.14 compatible)
# ══════════════════════════════════════════════════════════════════
# Root cause of "attached to a different loop":
#   asyncio.run() ALWAYS creates a brand-new event loop.
#   If we also pre-created a loop for pyrogram's import-time
#   get_event_loop() call, pyrogram binds its futures/tasks to
#   that old loop while asyncio.run() executes on a new one.
#
# Solution: create ONE loop right here, set it as the current loop,
#   import pyrogram (which captures this loop), and later run
#   main() on this SAME loop via loop.run_until_complete().
#   Never call asyncio.run() — it would replace the loop.
# ══════════════════════════════════════════════════════════════════
_MAIN_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_MAIN_LOOP)

import aiohttp
import aiohttp.web
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pyrogram.errors import (
    FloodWait, UserIsBlocked, InputUserDeactivated,
    MessageNotModified, PeerIdInvalid,
)

# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("TempMail")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("motor").setLevel(logging.WARNING)

# ══════════════════════════════════════════════════════════════════
# ENVIRONMENT CONFIG
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN        = os.environ["BOT_TOKEN"]
API_ID           = int(os.environ["API_ID"])
API_HASH         = os.environ["API_HASH"]
MONGO_URI        = os.environ["MONGO_URI"]
MONGO_NAME       = os.getenv("MONGO_NAME",             "tempmail_bot")
OWNER_ID         = int(os.getenv("OWNER_ID",           "0"))   # optional
PORT             = int(os.getenv("PORT",                "10000"))
RENDER_URL       = os.getenv("RENDER_EXTERNAL_URL",    "").rstrip("/")

EMAIL_TTL        = int(os.getenv("EMAIL_TTL_MINUTES",   "60"))
MAX_EMAILS       = int(os.getenv("MAX_EMAILS_PER_USER", "5"))
RATE_LIMIT       = int(os.getenv("RATE_LIMIT_COUNT",    "3"))
RATE_WINDOW      = int(os.getenv("RATE_LIMIT_WINDOW",   "60"))
NOTIFY_INTERVAL  = int(os.getenv("NOTIFY_INTERVAL_SECS","90"))
INBOX_CACHE_TTL  = int(os.getenv("INBOX_CACHE_TTL",     "25"))
EMAILS_PER_PAGE  = 4
MSGS_PER_PAGE    = 5
BOT_VERSION      = "3.0"

# ══════════════════════════════════════════════════════════════════
# SHARED HTTP SESSION
# ══════════════════════════════════════════════════════════════════
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=50, limit_per_host=10,
            ttl_dns_cache=300, ssl=False,
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=12, connect=5),
            headers={"User-Agent": "Mozilla/5.0 TempMailBot/3.0"},
        )
    return _session

# ══════════════════════════════════════════════════════════════════
# INBOX CACHE
# ══════════════════════════════════════════════════════════════════
_inbox_cache: dict[str, tuple[list, float]] = {}

def cache_get(key: str) -> Optional[list]:
    if key in _inbox_cache:
        data, ts = _inbox_cache[key]
        if time.monotonic() - ts < INBOX_CACHE_TTL:
            return data
    return None

def cache_set(key: str, data: list):
    _inbox_cache[key] = (data, time.monotonic())

def cache_bust(key: str):
    _inbox_cache.pop(key, None)

# ══════════════════════════════════════════════════════════════════
# BASE API CLASS
# ══════════════════════════════════════════════════════════════════
class TempMailAPI(ABC):
    name:     str = "base"
    icon:     str = "📧"
    COOLDOWN: int = 300

    def __init__(self):
        self._dead_until: float = 0.0
        self.total_generated: int = 0
        self.total_failed:    int = 0

    def is_alive(self) -> bool:
        return time.monotonic() >= self._dead_until

    def mark_dead(self):
        self.total_failed += 1
        logger.warning(f"[{self.name}] marked dead for {self.COOLDOWN}s")
        self._dead_until = time.monotonic() + self.COOLDOWN

    def mark_alive(self):
        self._dead_until = 0.0

    @abstractmethod
    async def generate_email(self) -> Optional[str]: ...

    @abstractmethod
    async def get_messages(self, email: str) -> list[dict]: ...

    @staticmethod
    def _norm(from_="", subject="", body="", date="") -> dict:
        return {
            "from":    (_strip_html(from_)    or "unknown").strip()[:120],
            "subject": (_strip_html(subject)  or "(no subject)").strip()[:200],
            "body":    _strip_html(body or "").strip()[:1500],
            "date":    (date or "").strip()[:50],
        }

def _strip_html(text: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text)

def _rnd(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #1 — 1secmail
# ══════════════════════════════════════════════════════════════════
class OneSec(TempMailAPI):
    name = "1secmail"; icon = "⚡"
    BASE = "https://www.1secmail.com/api/v1/"
    DOMAINS = ["1secmail.com","1secmail.org","1secmail.net","esiix.com","wwjmp.com"]

    async def generate_email(self) -> Optional[str]:
        try:
            self.total_generated += 1
            return f"{_rnd()}@{random.choice(self.DOMAINS)}"
        except Exception as e:
            logger.error(f"[{self.name}] gen: {e}"); return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            local, domain = email.split("@")
            s = await get_session()
            async with s.get(f"{self.BASE}?action=getMessages&login={local}&domain={domain}") as r:
                msgs = await r.json(content_type=None)
            result = []
            for m in (msgs or [])[:10]:
                async with s.get(f"{self.BASE}?action=readMessage&login={local}&domain={domain}&id={m['id']}") as dr:
                    d = await dr.json(content_type=None)
                result.append(self._norm(d.get("from",""), d.get("subject",""),
                                         d.get("textBody", d.get("htmlBody","")), d.get("date","")))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #2 — Guerrilla Mail
# ══════════════════════════════════════════════════════════════════
class Guerrilla(TempMailAPI):
    name = "guerrilla"; icon = "🥷"
    BASE = "https://api.guerrillamail.com/ajax.php"

    async def generate_email(self) -> Optional[str]:
        try:
            s = await get_session()
            async with s.get(self.BASE, params={"f": "get_email_address"}) as r:
                d = await r.json(content_type=None)
            addr = d.get("email_addr")
            if addr: self.total_generated += 1
            return addr
        except Exception as e:
            logger.error(f"[{self.name}] gen: {e}"); return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get(self.BASE, params={"f":"get_email_list","offset":0,
                                                 "alias":email.split("@")[0]}) as r:
                d = await r.json(content_type=None)
            return [self._norm(m.get("mail_from",""), m.get("mail_subject",""),
                               m.get("mail_excerpt",""), m.get("mail_date",""))
                    for m in d.get("list", [])]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #3 — mail.tm
# ══════════════════════════════════════════════════════════════════
class MailTM(TempMailAPI):
    name = "mail.tm"; icon = "🌐"
    BASE = "https://api.mail.tm"

    async def generate_email(self) -> Optional[str]:
        try:
            s = await get_session()
            async with s.get(f"{self.BASE}/domains") as r:
                dd = await r.json(content_type=None)
            doms = dd.get("hydra:member", [])
            if not doms: return None
            domain = doms[0]["domain"]
            pw = "Tmp#" + _rnd(8)
            addr = f"{_rnd()}@{domain}"
            async with s.post(f"{self.BASE}/accounts", json={"address": addr, "password": pw}) as r:
                acc = await r.json(content_type=None)
            result = acc.get("address")
            if result:
                self.total_generated += 1
                return f"{result}|{pw}"
            return None
        except Exception as e:
            logger.error(f"[{self.name}] gen: {e}"); return None

    async def get_messages(self, email: str) -> list[dict]:
        try:
            if "|" not in email: return []
            addr, pw = email.split("|", 1)
            s = await get_session()
            async with s.post(f"{self.BASE}/token", json={"address": addr, "password": pw}) as r:
                td = await r.json(content_type=None)
            tok = td.get("token")
            if not tok: return []
            hdrs = {"Authorization": f"Bearer {tok}"}
            async with s.get(f"{self.BASE}/messages", headers=hdrs) as r:
                md = await r.json(content_type=None)
            result = []
            for m in md.get("hydra:member", [])[:10]:
                async with s.get(f"{self.BASE}/messages/{m['id']}", headers=hdrs) as dr:
                    d = await dr.json(content_type=None)
                result.append(self._norm(d.get("from", {}).get("address", ""),
                                         d.get("subject",""), d.get("text", d.get("html","")),
                                         d.get("createdAt","")))
            return result
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #4 — TempMail.Plus
# ══════════════════════════════════════════════════════════════════
class TempMailPlus(TempMailAPI):
    name = "tempmail.plus"; icon = "➕"
    BASE = "https://tempmail.plus/api"
    DOMAINS = ["tempmail.plus", "fakemail.net", "mailtemp.info"]

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{random.choice(self.DOMAINS)}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get(f"{self.BASE}/mails",
                             params={"email": email, "limit": 20, "epin": ""}) as r:
                d = await r.json(content_type=None)
            return [self._norm(m.get("from_mail",""), m.get("subject",""),
                               m.get("text",""), m.get("date",""))
                    for m in d.get("mail_list", [])]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #5 — Dispostable
# ══════════════════════════════════════════════════════════════════
class Dispostable(TempMailAPI):
    name = "dispostable"; icon = "🗂"
    DOMAIN = "dispostable.com"

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get("https://www.dispostable.com/api/list-messages/",
                             params={"username": email.split("@")[0]}) as r:
                d = await r.json(content_type=None)
            return [self._norm(m.get("sender",""), m.get("subject",""),
                               m.get("text",""), m.get("created_at",""))
                    for m in d.get("messages", [])]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #6 — Mailnesia
# ══════════════════════════════════════════════════════════════════
class Mailnesia(TempMailAPI):
    name = "mailnesia"; icon = "🗃"
    DOMAIN = "mailnesia.com"

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get(f"https://mailnesia.com/mailbox/{email.split('@')[0]}") as r:
                text = await r.text()
            subjs = re.findall(r'class="subject"[^>]*>(.*?)</td>', text, re.DOTALL)
            frms  = re.findall(r'class="from"[^>]*>(.*?)</td>',    text, re.DOTALL)
            dts   = re.findall(r'class="date"[^>]*>(.*?)</td>',    text, re.DOTALL)
            def cl(s): return re.sub(r'<[^>]+>', '', s).strip()
            return [self._norm(cl(frms[i]) if i < len(frms) else "",
                               cl(subjs[i]), "", cl(dts[i]) if i < len(dts) else "")
                    for i in range(len(subjs))]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #7 — YOPmail
# ══════════════════════════════════════════════════════════════════
class YOPmail(TempMailAPI):
    name = "yopmail"; icon = "🔮"
    DOMAIN = "yopmail.com"

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get(f"https://yopmail.com/en/inbox?login={email.split('@')[0]}&p=1") as r:
                text = await r.text()
            subjs = re.findall(r'class="lms">(.*?)</span>', text)
            frms  = re.findall(r'class="lmf">(.*?)</span>', text)
            def cl(s): return re.sub(r'<[^>]+>', '', s).strip()
            return [self._norm(cl(frms[i]) if i < len(frms) else "",
                               cl(subjs[i]), "", "")
                    for i in range(len(subjs))]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #8 — TrashMail
# ══════════════════════════════════════════════════════════════════
class TrashMail(TempMailAPI):
    name = "trashmail"; icon = "🗑"
    DOMAIN = "trashmail.at"

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get(f"https://trashmail.at/api/json/v1/mailbox/{email.split('@')[0]}") as r:
                data = await r.json(content_type=None)
            msgs = data if isinstance(data, list) else []
            return [self._norm(m.get("from",""), m.get("subject",""),
                               m.get("body",""), m.get("date","")) for m in msgs]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #9 — FakeMail
# ══════════════════════════════════════════════════════════════════
class FakeMail(TempMailAPI):
    name = "fakemail"; icon = "🎭"
    DOMAIN = "fakemail.net"

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{self.DOMAIN}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            s = await get_session()
            async with s.get(f"https://www.fakemail.net/{email.split('@')[0]}") as r:
                text = await r.text()
            rows = re.findall(r'<tr[^>]*data-mailid[^>]*>(.*?)</tr>', text, re.DOTALL)
            def g(pat, blob):
                m = re.search(pat, blob)
                return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ""
            return [self._norm(g(r'class="from"[^>]*>(.*?)<', row),
                               g(r'class="subject"[^>]*>(.*?)<', row), "", "")
                    for row in rows]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API PROVIDER #10 — Tempr.email
# ══════════════════════════════════════════════════════════════════
class TemprEmail(TempMailAPI):
    name = "tempr.email"; icon = "🔥"
    DOMAINS = ["tempr.email", "discard.email", "spamgourmet.com"]

    async def generate_email(self) -> Optional[str]:
        self.total_generated += 1
        return f"{_rnd()}@{random.choice(self.DOMAINS)}"

    async def get_messages(self, email: str) -> list[dict]:
        try:
            md5 = hashlib.md5(email.lower().encode()).hexdigest()
            s = await get_session()
            async with s.get(f"https://tempr.email/api/v1/mailbox/{md5}") as r:
                if r.status != 200: return []
                data = await r.json(content_type=None)
            msgs = data if isinstance(data, list) else []
            return [self._norm(m.get("from",""), m.get("subject",""),
                               m.get("body",""), m.get("date","")) for m in msgs]
        except Exception as e:
            logger.error(f"[{self.name}] inbox: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# API REGISTRY & SMART ROTATION
# ══════════════════════════════════════════════════════════════════
ALL_APIS: list[TempMailAPI] = [
    OneSec(), Guerrilla(), MailTM(), TempMailPlus(), Dispostable(),
    Mailnesia(), YOPmail(), TrashMail(), FakeMail(), TemprEmail(),
]
_API_MAP = {a.name: a for a in ALL_APIS}


async def generate_with_fallback() -> tuple[Optional[str], Optional[str]]:
    alive = [a for a in ALL_APIS if a.is_alive()]
    if not alive:
        for a in ALL_APIS: a.mark_alive()
        alive = list(ALL_APIS)
    random.shuffle(alive)
    for api in alive:
        try:
            email = await asyncio.wait_for(api.generate_email(), timeout=8)
            if email:
                api.mark_alive()
                logger.info(f"✅ [{api.name}] => {email.split('|')[0]}")
                return email, api.name
            api.mark_dead()
        except asyncio.TimeoutError:
            logger.warning(f"[{api.name}] timeout"); api.mark_dead()
        except Exception as e:
            logger.error(f"[{api.name}] error: {e}"); api.mark_dead()
    return None, None


async def fetch_inbox_for(full_email: str, api_name: str) -> list[dict]:
    cached = cache_get(full_email)
    if cached is not None:
        return cached
    api = _API_MAP.get(api_name)
    if not api:
        return []
    try:
        msgs = await asyncio.wait_for(api.get_messages(full_email), timeout=10)
        result = msgs or []
        cache_set(full_email, result)
        return result
    except asyncio.TimeoutError:
        logger.warning(f"[{api_name}] inbox timeout"); return []
    except Exception as e:
        logger.error(f"[{api_name}] inbox error: {e}"); return []

# ══════════════════════════════════════════════════════════════════
# MONGODB LAYER
# ══════════════════════════════════════════════════════════════════
_mongo_client: Optional[AsyncIOMotorClient] = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    return _mongo_client[MONGO_NAME]


async def setup_indexes():
    db = get_db()
    await db.emails.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("created_at", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("email", ASCENDING)], unique=True),
    ])
    await db.users.create_indexes([IndexModel([("user_id", ASCENDING)], unique=True)])
    logger.info("✅ MongoDB indexes ready")

# ── User operations ───────────────────────────────────────────────
async def upsert_user(uid: int, username: str, first_name: str):
    await get_db().users.update_one(
        {"user_id": uid},
        {"$set":  {"username": username, "first_name": first_name,
                   "last_seen": datetime.now(timezone.utc)},
         "$setOnInsert": {"joined_at": datetime.now(timezone.utc),
                          "is_banned": False, "notify": True, "total_gen": 0}},
        upsert=True,
    )

async def get_user(uid: int) -> Optional[dict]:
    return await get_db().users.find_one({"user_id": uid})

async def is_banned(uid: int) -> bool:
    u = await get_user(uid)
    return bool(u and u.get("is_banned"))

async def set_ban(uid: int, val: bool):
    await get_db().users.update_one({"user_id": uid}, {"$set": {"is_banned": val}})

async def toggle_notify(uid: int) -> bool:
    u = await get_user(uid)
    new_val = not (u or {}).get("notify", True)
    await get_db().users.update_one({"user_id": uid}, {"$set": {"notify": new_val}}, upsert=True)
    return new_val

async def all_user_ids() -> list[int]:
    cursor = get_db().users.find({"is_banned": {"$ne": True}}, {"user_id": 1})
    return [d["user_id"] async for d in cursor]

async def total_users() -> int:
    return await get_db().users.count_documents({})

async def total_emails_ever() -> int:
    pipeline = [{"$group": {"_id": None, "t": {"$sum": "$total_gen"}}}]
    agg = await get_db().users.aggregate(pipeline).to_list(1)
    return agg[0]["t"] if agg else 0

# ── Email operations ──────────────────────────────────────────────
async def add_email(uid: int, email: str, api_name: str):
    await get_db().emails.insert_one({
        "user_id": uid, "email": email, "api_name": api_name,
        "created_at": datetime.now(timezone.utc),
        "pinned": False, "notify": True, "seen_hashes": [],
    })
    await get_db().users.update_one({"user_id": uid}, {"$inc": {"total_gen": 1}})

async def get_emails(uid: int) -> list[dict]:
    cursor = get_db().emails.find({"user_id": uid}).sort(
        [("pinned", DESCENDING), ("created_at", DESCENDING)]
    )
    return await cursor.to_list(length=MAX_EMAILS + 20)

async def count_emails(uid: int) -> int:
    return await get_db().emails.count_documents({"user_id": uid})

async def find_email_doc(uid: int, display_email: str) -> Optional[dict]:
    docs = await get_emails(uid)
    return next((d for d in docs if d["email"].split("|")[0] == display_email), None)

async def delete_email_doc(uid: int, display_email: str):
    docs = await get_emails(uid)
    target = next((d for d in docs if d["email"].split("|")[0] == display_email), None)
    if target:
        await get_db().emails.delete_one({"_id": target["_id"]})
        cache_bust(target["email"])

async def pin_email_doc(uid: int, display_email: str, val: bool):
    docs = await get_emails(uid)
    target = next((d for d in docs if d["email"].split("|")[0] == display_email), None)
    if target:
        await get_db().emails.update_one({"_id": target["_id"]}, {"$set": {"pinned": val}})

async def toggle_email_notify(uid: int, display_email: str) -> bool:
    docs = await get_emails(uid)
    target = next((d for d in docs if d["email"].split("|")[0] == display_email), None)
    if not target: return False
    new_val = not target.get("notify", True)
    await get_db().emails.update_one({"_id": target["_id"]}, {"$set": {"notify": new_val}})
    return new_val

async def update_seen_hashes(email_id, hashes: list[str]):
    await get_db().emails.update_one({"_id": email_id}, {"$set": {"seen_hashes": hashes}})

async def all_notify_emails() -> list[dict]:
    cursor = get_db().emails.find({"notify": True})
    return await cursor.to_list(length=10000)

async def cleanup_expired():
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EMAIL_TTL)
    r = await get_db().emails.delete_many({"created_at": {"$lt": cutoff}})
    if r.deleted_count:
        logger.info(f"🧹 Removed {r.deleted_count} expired emails")

# ══════════════════════════════════════════════════════════════════
# RATE LIMITER
# ══════════════════════════════════════════════════════════════════
_rate: dict[int, list[float]] = {}

def check_rate(uid: int) -> tuple[bool, int]:
    """Returns (is_limited, seconds_until_reset)."""
    now = time.monotonic()
    bucket = [t for t in _rate.get(uid, []) if now - t < RATE_WINDOW]
    _rate[uid] = bucket
    if len(bucket) >= RATE_LIMIT:
        return True, int(RATE_WINDOW - (now - bucket[0])) + 1
    _rate[uid].append(now)
    return False, 0

# ══════════════════════════════════════════════════════════════════
# CONVERSATION STATES (admin broadcast multi-step)
# ══════════════════════════════════════════════════════════════════
_states: dict[int, str] = {}

# ══════════════════════════════════════════════════════════════════
# UI / KEYBOARD BUILDERS
# ══════════════════════════════════════════════════════════════════
def KB(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows))

BTN = InlineKeyboardButton


def home_kb() -> InlineKeyboardMarkup:
    return KB(
        [BTN("🚀 Generate Email",   callback_data="gen_email")],
        [BTN("📋 My Emails",        callback_data="my_emails:0"),
         BTN("📥 Check Inbox",      callback_data="inbox_select")],
        [BTN("🔔 Notifications",    callback_data="notif_settings"),
         BTN("📊 My Stats",         callback_data="my_stats")],
        [BTN("🌐 API Health",       callback_data="api_health"),
         BTN("❓ Help",             callback_data="help")],
    )

def back_home_row() -> list:
    return [BTN("🏠 Home", callback_data="back_home")]

def email_list_kb(docs: list[dict], page: int, action: str) -> InlineKeyboardMarkup:
    total = len(docs)
    start = page * EMAILS_PER_PAGE
    chunk = docs[start : start + EMAILS_PER_PAGE]
    rows  = []
    for d in chunk:
        addr  = d["email"].split("|")[0]
        pin   = "📌 " if d.get("pinned") else ""
        bell  = "🔔" if d.get("notify", True) else "🔕"
        rows.append([BTN(f"{pin}{bell} {addr}", callback_data=f"{action}:{addr}")])
    nav = []
    if page > 0:
        nav.append(BTN("⬅️ Prev", callback_data=f"my_emails:{page-1}"))
    total_pages = max(1, (total - 1) // EMAILS_PER_PAGE + 1)
    nav.append(BTN(f"· {page+1}/{total_pages} ·", callback_data="noop"))
    if start + EMAILS_PER_PAGE < total:
        nav.append(BTN("Next ➡️", callback_data=f"my_emails:{page+1}"))
    if nav: rows.append(nav)
    rows.append(back_home_row())
    return InlineKeyboardMarkup(rows)

def email_actions_kb(addr: str, pinned: bool, notify: bool) -> InlineKeyboardMarkup:
    return KB(
        [BTN("📥 View Inbox",       callback_data=f"view_inbox:{addr}:0:0")],
        [BTN("📌 Unpin" if pinned else "📌 Pin", callback_data=f"pin_email:{addr}"),
         BTN("🔕 Mute" if notify else "🔔 Unmute", callback_data=f"notif_email:{addr}")],
        [BTN("🗑 Delete",            callback_data=f"del_confirm:{addr}")],
        [BTN("⬅️ My Emails",         callback_data="my_emails:0"),
         BTN("🏠 Home",              callback_data="back_home")],
    )

def inbox_kb(addr: str, page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total - 1) // MSGS_PER_PAGE + 1)
    nav = []
    if page > 0:
        nav.append(BTN("⬅️ Prev", callback_data=f"view_inbox:{addr}:{page-1}:0"))
    nav.append(BTN(f"· {page+1}/{total_pages} ·", callback_data="noop"))
    if (page + 1) * MSGS_PER_PAGE < total:
        nav.append(BTN("Next ➡️", callback_data=f"view_inbox:{addr}:{page+1}:0"))
    rows = []
    if nav: rows.append(nav)
    rows.append([BTN("🔄 Refresh",       callback_data=f"view_inbox:{addr}:{page}:1"),
                 BTN("📋 Manage Email",   callback_data=f"manage_email:{addr}")])
    rows.append([BTN("⬅️ My Emails",      callback_data="my_emails:0"),
                 BTN("🏠 Home",           callback_data="back_home")])
    return InlineKeyboardMarkup(rows)

def progress_bar(val: int, max_val: int, length: int = 10) -> str:
    if max_val == 0: return "░" * length
    filled = min(length, int((val / max_val) * length))
    return "█" * filled + "░" * (length - filled)

# ══════════════════════════════════════════════════════════════════
# PYROGRAM CLIENT
# ══════════════════════════════════════════════════════════════════
app = Client(
    "ultra_tempmail_bot",
    api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
)

# ── safe helpers ──────────────────────────────────────────────────
async def safe_edit(msg: Message, text: str,
                    kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await msg.edit_text(text, reply_markup=kb,
                            parse_mode=enums.ParseMode.HTML,
                            disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.debug(f"safe_edit: {e}")

async def safe_answer(cq: CallbackQuery, text: str, alert: bool = False):
    try: await cq.answer(text, show_alert=alert)
    except Exception: pass

# ══════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════
@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, msg: Message):
    u = msg.from_user
    if await is_banned(u.id):
        await msg.reply("⛔ You are banned from this bot."); return
    await upsert_user(u.id, u.username or "", u.first_name or "")
    count = await count_emails(u.id)
    text = (
        f"<b>👋 Hello, {u.first_name}!</b>\n\n"
        f"╔══════════════════════╗\n"
        f"║  🔒 TEMP MAIL BOT v{BOT_VERSION}  ║\n"
        f"╚══════════════════════╝\n\n"
        f"✨ <b>Instant disposable inboxes</b> from 10 providers.\n"
        f"📬 Active inboxes: <code>{count}/{MAX_EMAILS}</code>\n"
        f"⏳ Auto-expire:   <code>{EMAIL_TTL} min</code>\n"
        f"🔔 New mail → instant notification\n\n"
        f"<i>Choose an action below:</i>"
    )
    await msg.reply(text, reply_markup=home_kb(),
                    parse_mode=enums.ParseMode.HTML)

# ══════════════════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════════════════
@app.on_message(filters.command("help") & filters.private)
async def cmd_help(_, msg: Message):
    await msg.reply(build_help_text(),
                    reply_markup=KB(back_home_row()),
                    parse_mode=enums.ParseMode.HTML)

def build_help_text() -> str:
    return (
        "📖 <b>HELP &amp; GUIDE</b>\n\n"
        "🚀 <b>Generate Email</b>\n"
        "   Creates a fresh inbox from one of 10 providers.\n\n"
        "📋 <b>My Emails</b>\n"
        "   Manage, pin, mute or delete your inboxes.\n\n"
        "📥 <b>Check Inbox</b>\n"
        "   Read messages with pagination.\n\n"
        "🔔 <b>Notifications</b>\n"
        "   Toggle global or per-email alerts.\n\n"
        "🌐 <b>API Health</b>\n"
        "   Live provider status dashboard.\n\n"
        "⚙️ <b>Commands</b>\n"
        "   /start — Main menu\n"
        "   /help  — This screen\n"
        "   /admin — Admin panel (owner only)\n\n"
        f"⏳ TTL: <b>{EMAIL_TTL} min</b>  |  "
        f"⚡ Rate: <b>{RATE_LIMIT}/{RATE_WINDOW}s</b>  |  "
        f"📦 Max: <b>{MAX_EMAILS}</b>"
    )

# ══════════════════════════════════════════════════════════════════
# /admin
# ══════════════════════════════════════════════════════════════════
@app.on_message(filters.command("admin") & filters.private)
async def cmd_admin(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        await msg.reply("⛔ Unauthorized."); return
    await msg.reply(await build_admin_text(), reply_markup=admin_kb(),
                    parse_mode=enums.ParseMode.HTML)

def admin_kb() -> InlineKeyboardMarkup:
    return KB(
        [BTN("📊 Full Stats",     callback_data="admin_stats"),
         BTN("🌐 API Health",    callback_data="api_health")],
        [BTN("📢 Broadcast",     callback_data="admin_broadcast"),
         BTN("🧹 Force Cleanup", callback_data="admin_cleanup")],
        [BTN("👥 Users",         callback_data="admin_users:0"),
         BTN("🏠 Home",          callback_data="back_home")],
    )

async def build_admin_text() -> str:
    users        = await total_users()
    active_mails = await get_db().emails.count_documents({})
    gen_total    = await total_emails_ever()
    alive_apis   = sum(1 for a in ALL_APIS if a.is_alive())
    return (
        f"👑 <b>ADMIN PANEL</b>\n"
        f"{'═' * 26}\n"
        f"👤 Users:          <code>{users:,}</code>\n"
        f"📧 Active Emails:  <code>{active_mails:,}</code>\n"
        f"📊 Total Generated:<code>{gen_total:,}</code>\n"
        f"🌐 APIs Alive:     <code>{alive_apis}/{len(ALL_APIS)}</code>\n"
        f"{'─' * 26}"
    )

# ══════════════════════════════════════════════════════════════════
# /broadcast  /ban  /unban
# ══════════════════════════════════════════════════════════════════
@app.on_message(filters.command("broadcast") & filters.private)
async def cmd_broadcast(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        await msg.reply("⛔ Unauthorized."); return
    _states[msg.from_user.id] = "broadcast"
    await msg.reply("📢 <b>Broadcast Mode</b>\nSend your message now:",
                    reply_markup=KB([BTN("❌ Cancel", callback_data="admin_cancel_broadcast")]),
                    parse_mode=enums.ParseMode.HTML)

@app.on_message(filters.command(["ban", "unban"]) & filters.private)
async def cmd_ban(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        await msg.reply("⛔ Unauthorized."); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply("Usage: /ban <user_id>"); return
    target = int(parts[1])
    val = (msg.command[0] == "ban")
    await set_ban(target, val)
    action = "⛔ Banned" if val else "✅ Unbanned"
    await msg.reply(f"{action} user <code>{target}</code>.",
                    parse_mode=enums.ParseMode.HTML)

# ══════════════════════════════════════════════════════════════════
# STATE HANDLER (broadcast multi-step)
# ══════════════════════════════════════════════════════════════════
@app.on_message(filters.private & ~filters.command(
    ["start", "help", "admin", "broadcast", "ban", "unban"]))
async def state_handler(_, msg: Message):
    uid   = msg.from_user.id
    state = _states.pop(uid, None)
    if state == "broadcast":
        await run_broadcast(msg, msg.text or msg.caption or "")

async def run_broadcast(origin: Message, text: str):
    if not text:
        await origin.reply("❌ Empty message. Broadcast cancelled."); return
    uids = await all_user_ids()
    sent = failed = 0
    sm   = await origin.reply(f"📢 Broadcasting to <b>{len(uids)}</b> users…",
                               parse_mode=enums.ParseMode.HTML)
    for uid in uids:
        try:
            await app.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
            failed += 1
        except Exception:
            failed += 1
    await sm.edit_text(
        f"✅ <b>Broadcast Complete</b>\n\n"
        f"📤 Sent: <code>{sent}</code>\n"
        f"❌ Failed: <code>{failed}</code>",
        parse_mode=enums.ParseMode.HTML,
    )

# ══════════════════════════════════════════════════════════════════
# CALLBACK: noop / back_home / help
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^noop$"))
async def cb_noop(_, cq: CallbackQuery):
    await safe_answer(cq, "")

@app.on_callback_query(filters.regex("^back_home$"))
async def cb_back_home(_, cq: CallbackQuery):
    u     = cq.from_user
    count = await count_emails(u.id)
    await safe_edit(cq.message,
        f"<b>🏠 Main Menu</b>\n\n"
        f"👤 <b>{u.first_name}</b>  •  📬 <code>{count}/{MAX_EMAILS}</code>\n"
        f"<i>What would you like to do?</i>",
        home_kb(),
    )

@app.on_callback_query(filters.regex("^help$"))
async def cb_help(_, cq: CallbackQuery):
    await safe_edit(cq.message, build_help_text(), KB(back_home_row()))

# ══════════════════════════════════════════════════════════════════
# CALLBACK: gen_email
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^gen_email$"))
async def cb_gen_email(_, cq: CallbackQuery):
    uid = cq.from_user.id
    if await is_banned(uid):
        await safe_answer(cq, "⛔ You are banned.", True); return

    limited, reset_in = check_rate(uid)
    if limited:
        await safe_answer(cq, f"⏳ Rate limit! Try in {reset_in}s.", True); return

    cnt = await count_emails(uid)
    if cnt >= MAX_EMAILS:
        await safe_answer(cq, f"⚠️ Max {MAX_EMAILS} emails. Delete one first.", True); return

    await safe_edit(cq.message,
        "⏳ <b>Generating email…</b>\n\n"
        "🔍 Scanning available providers…\n"
        "<i>Hold on just a second!</i>",
        KB([BTN("❌ Cancel", callback_data="back_home")]),
    )

    email, api_name = await generate_with_fallback()
    if not email:
        await safe_edit(cq.message,
            "❌ <b>All providers unavailable</b>\n\n"
            "Please try again in a few moments.",
            KB(back_home_row()),
        ); return

    await add_email(uid, email, api_name)
    display = email.split("|")[0]
    api     = _API_MAP.get(api_name)
    icon    = api.icon if api else "📧"

    await safe_edit(cq.message,
        f"✅ <b>Email Generated!</b>\n\n"
        f"╔══════════════════════════════╗\n"
        f"  <code>{display}</code>\n"
        f"╚══════════════════════════════╝\n\n"
        f"{icon} Provider:     <b>{api_name}</b>\n"
        f"⏳ Expires in:  <b>{EMAIL_TTL} min</b>\n"
        f"🔔 Notify:      <b>ON</b>\n\n"
        f"<i>💡 Tap the address above to copy</i>",
        KB(
            [BTN("📥 Check Inbox",     callback_data=f"view_inbox:{display}:0:0")],
            [BTN("📌 Pin",             callback_data=f"pin_email:{display}"),
             BTN("🗑 Delete",          callback_data=f"del_confirm:{display}")],
            [BTN("🚀 Generate Another",callback_data="gen_email")],
            back_home_row(),
        ),
    )

# ══════════════════════════════════════════════════════════════════
# CALLBACK: my_emails
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^my_emails:(\d+)$"))
async def cb_my_emails(_, cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    uid  = cq.from_user.id
    docs = await get_emails(uid)
    if not docs:
        await safe_edit(cq.message,
            "📭 <b>No Active Emails</b>\n\nGenerate your first inbox to get started!",
            KB([BTN("🚀 Generate Email", callback_data="gen_email")], back_home_row()),
        ); return
    pinned = sum(1 for d in docs if d.get("pinned"))
    await safe_edit(cq.message,
        f"📋 <b>My Emails</b>   <code>{len(docs)}/{MAX_EMAILS}</code>\n"
        f"{'─' * 24}\n"
        f"📌 Pinned: <code>{pinned}</code>  ⏳ TTL: <code>{EMAIL_TTL} min</code>\n\n"
        f"<i>Tap to manage an inbox:</i>",
        email_list_kb(docs, page, "manage_email"),
    )

# ══════════════════════════════════════════════════════════════════
# CALLBACK: manage_email
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^manage_email:(.+)$"))
async def cb_manage_email(_, cq: CallbackQuery):
    addr = cq.data.split(":", 1)[1]
    doc  = await find_email_doc(cq.from_user.id, addr)
    if not doc:
        await safe_answer(cq, "Email not found.", True); return

    created = doc["created_at"]
    if isinstance(created, datetime):
        ts       = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
        age_min  = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        ttl_left = max(0, EMAIL_TTL - age_min)
        bar      = progress_bar(ttl_left, EMAIL_TTL)
        time_str = ts.strftime("%d %b %H:%M UTC")
    else:
        ttl_left = EMAIL_TTL; bar = "░" * 10; time_str = "Unknown"

    api  = _API_MAP.get(doc["api_name"])
    icon = api.icon if api else "📧"

    await safe_edit(cq.message,
        f"📧 <b>Email Details</b>\n"
        f"{'─' * 28}\n"
        f"<code>{addr}</code>\n\n"
        f"{icon} Provider:  <b>{doc['api_name']}</b>\n"
        f"📅 Created:   <b>{time_str}</b>\n"
        f"⏳ TTL Left:  <b>{ttl_left} min</b>\n"
        f"   [{bar}] {ttl_left}/{EMAIL_TTL}\n"
        f"📌 Pinned:    <b>{'Yes ✅' if doc.get('pinned') else 'No'}</b>\n"
        f"🔔 Notify:    <b>{'ON 🟢' if doc.get('notify', True) else 'OFF 🔴'}</b>",
        email_actions_kb(addr, doc.get("pinned", False), doc.get("notify", True)),
    )

# ══════════════════════════════════════════════════════════════════
# CALLBACK: pin_email / notif_email
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^pin_email:(.+)$"))
async def cb_pin_email(_, cq: CallbackQuery):
    addr = cq.data.split(":", 1)[1]
    doc  = await find_email_doc(cq.from_user.id, addr)
    if not doc:
        await safe_answer(cq, "Email not found.", True); return
    new_val = not doc.get("pinned", False)
    await pin_email_doc(cq.from_user.id, addr, new_val)
    await safe_answer(cq, "📌 Pinned!" if new_val else "📌 Unpinned!")
    doc["pinned"] = new_val
    # Re-show manage view
    cq.data = f"manage_email:{addr}"
    await cb_manage_email(_, cq)

@app.on_callback_query(filters.regex(r"^notif_email:(.+)$"))
async def cb_notif_email(_, cq: CallbackQuery):
    addr    = cq.data.split(":", 1)[1]
    new_val = await toggle_email_notify(cq.from_user.id, addr)
    await safe_answer(cq, "🔔 Notifications ON" if new_val else "🔕 Notifications OFF")
    cq.data = f"manage_email:{addr}"
    await cb_manage_email(_, cq)

# ══════════════════════════════════════════════════════════════════
# CALLBACK: del_confirm / del_email
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^del_confirm:(.+)$"))
async def cb_del_confirm(_, cq: CallbackQuery):
    addr = cq.data.split(":", 1)[1]
    await safe_edit(cq.message,
        f"⚠️ <b>Confirm Deletion</b>\n\n"
        f"Delete this inbox permanently?\n\n"
        f"<code>{addr}</code>\n\n"
        f"<i>This action cannot be undone.</i>",
        KB(
            [BTN("✅ Yes, Delete", callback_data=f"del_email:{addr}"),
             BTN("❌ Cancel",      callback_data=f"manage_email:{addr}")],
        ),
    )

@app.on_callback_query(filters.regex(r"^del_email:(.+)$"))
async def cb_del_email(_, cq: CallbackQuery):
    addr = cq.data.split(":", 1)[1]
    await delete_email_doc(cq.from_user.id, addr)
    await safe_answer(cq, "🗑 Deleted!")
    docs = await get_emails(cq.from_user.id)
    if not docs:
        await safe_edit(cq.message,
            "✅ <b>Email deleted.</b>\n\n📭 No active emails remaining.",
            KB([BTN("🚀 Generate New", callback_data="gen_email")], back_home_row()),
        )
    else:
        await safe_edit(cq.message, "✅ <b>Email deleted.</b>",
                        email_list_kb(docs, 0, "manage_email"))

# ══════════════════════════════════════════════════════════════════
# CALLBACK: inbox_select / view_inbox_sel / view_inbox
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^inbox_select$"))
async def cb_inbox_select(_, cq: CallbackQuery):
    docs = await get_emails(cq.from_user.id)
    if not docs:
        await safe_edit(cq.message,
            "📭 <b>No emails yet.</b>\nGenerate one first!",
            KB([BTN("🚀 Generate Email", callback_data="gen_email")], back_home_row()),
        ); return
    await safe_edit(cq.message,
        "📥 <b>Select an inbox:</b>\n\n<i>Tap an email to see its messages</i>",
        email_list_kb(docs, 0, "view_inbox_sel"),
    )

@app.on_callback_query(filters.regex(r"^view_inbox_sel:(.+)$"))
async def cb_view_inbox_sel(_, cq: CallbackQuery):
    addr    = cq.data.split(":", 1)[1]
    cq.data = f"view_inbox:{addr}:0:0"
    await cb_view_inbox(_, cq)

@app.on_callback_query(filters.regex(r"^view_inbox:(.+):(\d+):(\d+)$"))
async def cb_view_inbox(_, cq: CallbackQuery):
    parts = cq.data.split(":")
    addr  = parts[1]
    page  = int(parts[2])
    force = parts[3] == "1"
    uid   = cq.from_user.id

    doc = await find_email_doc(uid, addr)
    if not doc:
        await safe_answer(cq, "Email not found.", True); return

    if force:
        cache_bust(doc["email"])
        await safe_edit(cq.message,
            f"🔄 <b>Refreshing…</b>\n<code>{addr}</code>", None)

    msgs  = await fetch_inbox_for(doc["email"], doc["api_name"])
    total = len(msgs)

    if not msgs:
        await safe_edit(cq.message,
            f"📭 <b>Inbox Empty</b>\n\n"
            f"<code>{addr}</code>\n\n"
            f"<i>No messages yet. New mail triggers an automatic notification.</i>",
            KB(
                [BTN("🔄 Refresh", callback_data=f"view_inbox:{addr}:{page}:1")],
                [BTN("📋 Manage",  callback_data=f"manage_email:{addr}"),
                 BTN("🏠 Home",   callback_data="back_home")],
            ),
        ); return

    start = page * MSGS_PER_PAGE
    chunk = msgs[start : start + MSGS_PER_PAGE]
    api   = _API_MAP.get(doc["api_name"])
    icon  = api.icon if api else "📧"

    lines = [
        f"📥 <b>Inbox</b>  <code>{addr}</code>\n"
        f"{icon} <i>{doc['api_name']}</i>  ·  <b>{total}</b> msg{'s' if total != 1 else ''}\n"
        f"{'─' * 28}\n",
    ]
    for i, m in enumerate(chunk, start + 1):
        body = m["body"][:300].strip() or "<i>(empty body)</i>"
        lines += [
            f"<b>✉️ #{i}</b>\n",
            f"👤 <b>From:</b>    <code>{m['from']}</code>\n",
            f"📌 <b>Subject:</b> {m['subject']}\n",
            f"📅 <b>Date:</b>    {m['date'] or 'N/A'}\n",
            f"💬 {body}\n",
            f"{'─' * 28}\n",
        ]

    text = "".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n<i>…truncated</i>"

    await safe_edit(cq.message, text, inbox_kb(addr, page, total))

# ══════════════════════════════════════════════════════════════════
# CALLBACK: notif_settings / toggle_global_notify
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^notif_settings$"))
async def cb_notif_settings(_, cq: CallbackQuery):
    u      = await get_user(cq.from_user.id)
    notify = (u or {}).get("notify", True)
    await safe_edit(cq.message,
        f"🔔 <b>Notification Settings</b>\n"
        f"{'─' * 26}\n\n"
        f"Global Alerts: <b>{'🟢 ON' if notify else '🔴 OFF'}</b>\n\n"
        f"<i>Receive Telegram alerts when new\n"
        f"mail arrives in your inboxes.</i>",
        KB(
            [BTN("🔕 Turn OFF" if notify else "🔔 Turn ON",
                 callback_data="toggle_global_notify")],
            back_home_row(),
        ),
    )

@app.on_callback_query(filters.regex("^toggle_global_notify$"))
async def cb_toggle_notify(_, cq: CallbackQuery):
    new_val = await toggle_notify(cq.from_user.id)
    await safe_answer(cq, "🔔 ON" if new_val else "🔕 OFF")
    await cb_notif_settings(_, cq)

# ══════════════════════════════════════════════════════════════════
# CALLBACK: my_stats
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^my_stats$"))
async def cb_my_stats(_, cq: CallbackQuery):
    uid  = cq.from_user.id
    u    = await get_user(uid)
    docs = await get_emails(uid)
    gen  = (u or {}).get("total_gen", 0)
    active  = len(docs)
    pinned  = sum(1 for d in docs if d.get("pinned"))
    joined  = (u or {}).get("joined_at")
    jstr    = joined.strftime("%d %b %Y") if isinstance(joined, datetime) else "?"
    bar_e   = progress_bar(active, MAX_EMAILS)
    await safe_edit(cq.message,
        f"📊 <b>Your Statistics</b>\n"
        f"{'─' * 26}\n"
        f"📅 Joined:        <b>{jstr}</b>\n"
        f"📧 Total Gen:     <b>{gen:,}</b>\n"
        f"📬 Active:        <b>{active}/{MAX_EMAILS}</b>\n"
        f"   [{bar_e}]\n"
        f"📌 Pinned:        <b>{pinned}</b>\n"
        f"🔔 Notifications: <b>{'ON' if (u or {}).get('notify', True) else 'OFF'}</b>",
        KB(back_home_row()),
    )

# ══════════════════════════════════════════════════════════════════
# CALLBACK: api_health
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^api_health$"))
async def cb_api_health(_, cq: CallbackQuery):
    lines = ["🌐 <b>API Health Dashboard</b>\n" + "─" * 28 + "\n"]
    for a in ALL_APIS:
        if a.is_alive():
            status = "🟢"
            note   = f"gen:{a.total_generated}  fail:{a.total_failed}"
        else:
            secs   = max(0, int(a._dead_until - time.monotonic()))
            status = "🔴"
            note   = f"cooldown {secs}s"
        lines.append(f"{status} {a.icon} <b>{a.name}</b>  <i>{note}</i>\n")
    alive = sum(1 for a in ALL_APIS if a.is_alive())
    lines.append(f"\n<b>Status: {alive}/{len(ALL_APIS)} online</b>")
    await safe_edit(cq.message, "".join(lines),
                    KB([BTN("🔄 Refresh", callback_data="api_health")], back_home_row()))

# ══════════════════════════════════════════════════════════════════
# ADMIN CALLBACKS
# ══════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex("^admin_stats$"))
async def cb_admin_stats(_, cq: CallbackQuery):
    if cq.from_user.id != OWNER_ID:
        await safe_answer(cq, "⛔ Unauthorized", True); return
    users_total  = await total_users()
    active_mails = await get_db().emails.count_documents({})
    gen_total    = await total_emails_ever()
    banned       = await get_db().users.count_documents({"is_banned": True})
    alive_apis   = sum(1 for a in ALL_APIS if a.is_alive())
    plines = "\n".join(
        f"  {a.icon} {a.name:<14} gen={a.total_generated:<4} fail={a.total_failed}"
        for a in ALL_APIS
    )
    await safe_edit(cq.message,
        f"📊 <b>GLOBAL STATISTICS</b>\n"
        f"{'═' * 26}\n"
        f"👤 Total Users:   <code>{users_total:,}</code>\n"
        f"⛔ Banned:        <code>{banned:,}</code>\n"
        f"📧 Active Emails: <code>{active_mails:,}</code>\n"
        f"📊 Total Gen:     <code>{gen_total:,}</code>\n"
        f"🌐 APIs Alive:    <code>{alive_apis}/{len(ALL_APIS)}</code>\n"
        f"{'─' * 26}\n"
        f"<b>Providers:</b>\n<pre>{plines}</pre>",
        KB([BTN("⬅️ Admin Panel", callback_data="admin_panel")]),
    )

@app.on_callback_query(filters.regex("^admin_panel$"))
async def cb_admin_panel(_, cq: CallbackQuery):
    if cq.from_user.id != OWNER_ID:
        await safe_answer(cq, "⛔ Unauthorized", True); return
    await safe_edit(cq.message, await build_admin_text(), admin_kb())

@app.on_callback_query(filters.regex("^admin_broadcast$"))
async def cb_admin_broadcast(_, cq: CallbackQuery):
    if cq.from_user.id != OWNER_ID:
        await safe_answer(cq, "⛔ Unauthorized", True); return
    _states[cq.from_user.id] = "broadcast"
    await safe_edit(cq.message,
        "📢 <b>Broadcast Active</b>\n\nSend your message now. It goes to all users.",
        KB([BTN("❌ Cancel", callback_data="admin_cancel_broadcast")]),
    )

@app.on_callback_query(filters.regex("^admin_cancel_broadcast$"))
async def cb_cancel_broadcast(_, cq: CallbackQuery):
    _states.pop(cq.from_user.id, None)
    await safe_edit(cq.message, await build_admin_text(), admin_kb())

@app.on_callback_query(filters.regex("^admin_cleanup$"))
async def cb_admin_cleanup(_, cq: CallbackQuery):
    if cq.from_user.id != OWNER_ID:
        await safe_answer(cq, "⛔ Unauthorized", True); return
    await safe_edit(cq.message, "🧹 <b>Running cleanup…</b>", None)
    await cleanup_expired()
    await safe_edit(cq.message, "✅ <b>Cleanup complete.</b>",
                    KB([BTN("⬅️ Admin", callback_data="admin_panel")]))

@app.on_callback_query(filters.regex(r"^admin_users:(\d+)$"))
async def cb_admin_users(_, cq: CallbackQuery):
    if cq.from_user.id != OWNER_ID:
        await safe_answer(cq, "⛔ Unauthorized", True); return
    page   = int(cq.data.split(":")[1])
    cursor = get_db().users.find({}).sort("joined_at", DESCENDING).skip(page * 10).limit(10)
    users  = await cursor.to_list(10)
    total  = await total_users()
    lines  = [f"👥 <b>Users</b>  (page {page+1})\n{'─' * 26}\n"]
    for u in users:
        ban = " ⛔" if u.get("is_banned") else ""
        lines.append(
            f"<code>{u['user_id']}</code>  "
            f"<b>{u.get('first_name','?')[:12]}</b>{ban}  "
            f"gen:{u.get('total_gen',0)}\n"
        )
    nav = []
    if page > 0: nav.append(BTN("⬅️ Prev", callback_data=f"admin_users:{page-1}"))
    if (page + 1) * 10 < total: nav.append(BTN("Next ➡️", callback_data=f"admin_users:{page+1}"))
    rows = [nav] if nav else []
    rows.append([BTN("⬅️ Admin", callback_data="admin_panel")])
    await safe_edit(cq.message, "".join(lines), InlineKeyboardMarkup(rows))

# ══════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════════
async def cleanup_task():
    """Every 5 minutes, remove expired emails."""
    while True:
        await asyncio.sleep(300)
        try:
            await cleanup_expired()
        except Exception as e:
            logger.error(f"cleanup_task: {e}")


async def inbox_notify_task():
    """Poll inboxes and push Telegram alerts for new mail."""
    await asyncio.sleep(45)   # warm-up delay
    while True:
        try:
            email_docs = await all_notify_emails()
            # group by user
            by_user: dict[int, list[dict]] = {}
            for d in email_docs:
                by_user.setdefault(d["user_id"], []).append(d)

            for uid, docs in by_user.items():
                u = await get_user(uid)
                if not u or not u.get("notify", True) or u.get("is_banned"):
                    continue
                for doc in docs:
                    try:
                        msgs = await fetch_inbox_for(doc["email"], doc["api_name"])
                        if not msgs:
                            continue
                        # Hash each message by (from, subject, date)
                        new_hashes = [
                            hashlib.md5(
                                f"{m['from']}|{m['subject']}|{m['date']}".encode()
                            ).hexdigest()
                            for m in msgs
                        ]
                        seen_set = set(doc.get("seen_hashes", []))
                        fresh    = [h for h in new_hashes if h not in seen_set]
                        if not fresh:
                            continue
                        # Save updated seen list
                        await update_seen_hashes(doc["_id"], new_hashes)
                        addr  = doc["email"].split("|")[0]
                        # Notify for each new message (max 3 per cycle)
                        fresh_msgs = [msgs[i] for i, h in enumerate(new_hashes)
                                      if h in set(fresh)][:3]
                        for m in fresh_msgs:
                            text = (
                                f"🔔 <b>New Mail!</b>\n\n"
                                f"📧 <code>{addr}</code>\n"
                                f"{'─' * 24}\n"
                                f"👤 <b>From:</b>    <code>{m['from']}</code>\n"
                                f"📌 <b>Subject:</b> {m['subject']}\n"
                                f"📅 <b>Date:</b>    {m['date'] or 'N/A'}\n"
                                f"💬 {m['body'][:250] or '<i>(empty)</i>'}"
                            )
                            kb = KB([BTN("📥 Open Inbox",
                                        callback_data=f"view_inbox:{addr}:0:1")])
                            await app.send_message(uid, text, reply_markup=kb,
                                                   parse_mode=enums.ParseMode.HTML)
                            await asyncio.sleep(0.25)
                    except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
                        pass
                    except Exception as e:
                        logger.debug(f"notify uid={uid}: {e}")
        except Exception as e:
            logger.error(f"inbox_notify_task: {e}")

        await asyncio.sleep(NOTIFY_INTERVAL)


async def self_ping_task():
    """Prevent Render free-tier from sleeping — pings self every 14 min."""
    if not RENDER_URL:
        logger.info("RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    logger.info(f"🏓 Self-ping active → {RENDER_URL}/ping  every 14 min")
    await asyncio.sleep(60)
    s = await get_session()
    while True:
        await asyncio.sleep(14 * 60)
        try:
            async with s.get(f"{RENDER_URL}/ping",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                logger.info(f"🏓 Self-ping: HTTP {r.status}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")

# ══════════════════════════════════════════════════════════════════
# AIOHTTP WEB SERVER  (binds Render's PORT, exposes health + ping)
# ══════════════════════════════════════════════════════════════════
_start_time = time.time()


async def handle_root(_r):
    return aiohttp.web.Response(
        text="🤖 TempMail Bot is running!", content_type="text/plain"
    )

async def handle_ping(_r):
    return aiohttp.web.Response(text="pong", content_type="text/plain")

async def handle_health(_r):
    uptime  = int(time.time() - _start_time)
    alive   = sum(1 for a in ALL_APIS if a.is_alive())
    return aiohttp.web.json_response({
        "status":      "ok",
        "version":     BOT_VERSION,
        "uptime_sec":  uptime,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "apis_alive":  alive,
        "apis_total":  len(ALL_APIS),
        "providers":   {a.name: a.is_alive() for a in ALL_APIS},
    })

async def handle_stats(_r):
    users  = await total_users()
    active = await get_db().emails.count_documents({})
    gen    = await total_emails_ever()
    return aiohttp.web.json_response(
        {"users": users, "active_emails": active, "total_generated": gen}
    )

async def start_web_server():
    web_app = aiohttp.web.Application()
    web_app.router.add_get("/",       handle_root)
    web_app.router.add_get("/ping",   handle_ping)
    web_app.router.add_get("/health", handle_health)
    web_app.router.add_get("/stats",  handle_stats)
    runner = aiohttp.web.AppRunner(web_app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Web server → 0.0.0.0:{PORT}  (/  /ping  /health  /stats)")

# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  ULTRA ADVANCED TEMP MAIL BOT  v3.0         ║")
    logger.info("║  Kurigram · Motor · aiohttp · Render-Ready  ║")
    logger.info("╚══════════════════════════════════════════════╝")

    # Setup DB indexes
    await setup_indexes()

    # Start web server (binds PORT for Render)
    await start_web_server()

    # ── Use `async with app` so kurigram's dispatcher is FULLY active ──
    # This is the only guaranteed way — app.start() alone does NOT wire
    # the update dispatcher on some kurigram/pyrofork builds.
    async with app:
        me = await app.get_me()
        logger.info(f"✅ @{me.username} (id={me.id}) is online!")
        logger.info(f"🗄  MongoDB  : {MONGO_NAME}")
        logger.info(f"🌐 Port     : {PORT}  |  Render: {RENDER_URL or 'not set'}")

        # Notify owner if configured
        if OWNER_ID:
            try:
                await app.send_message(
                    OWNER_ID,
                    f"🚀 <b>Bot started!  v{BOT_VERSION}</b>\n\n"
                    f"🤖 @{me.username}\n"
                    f"🗄 DB: <code>{MONGO_NAME}</code>\n"
                    f"🌐 Port: <code>{PORT}</code>\n"
                    f"🔌 Providers: <code>{len(ALL_APIS)}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass

        # Launch background tasks (inside the async-with so they share the loop)
        asyncio.create_task(cleanup_task(),      name="cleanup")
        asyncio.create_task(inbox_notify_task(), name="inbox_notify")
        asyncio.create_task(self_ping_task(),    name="self_ping")

        logger.info("✅ All systems go — bot is ready and responding!")

        # Keep the coroutine alive until process is killed (SIGTERM from Render)
        stop_event = asyncio.Event()
        await stop_event.wait()


if __name__ == "__main__":
    # Use the SAME loop that pyrogram captured at import time.
    # asyncio.run() would create a NEW loop → "attached to a different loop".
    try:
        _MAIN_LOOP.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        _MAIN_LOOP.close()
