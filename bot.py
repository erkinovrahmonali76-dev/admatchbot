import asyncio
import html
import logging
import os
import time

import aiosqlite
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
FREE_LIKES = int(os.getenv("FREE_LIKES_PER_DAY", "50"))
DB_PATH = os.getenv("DB_PATH", "matchbot.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
db: aiosqlite.Connection = None  # type: ignore

# ---------- Matnlar ----------
B_SEARCH = "🔍 Qidirish"
B_CHAT = "💬 Suhbat"
B_LIKES = "💌 Menga kim layk bosdi"
B_PROFILE = "👤 Mening profilim"
B_VIP = "💎 VIP PREMIUM"
B_SAVED = "🔖 Saqlanganlar"
B_STOP = "❌ Suhbatni tugatish"
B_NEXT = "⏭ Keyingi suhbatdosh"
B_CANCEL = "❌ Bekor qilish"
B_SKIP = "⏭ O'tkazib yuborish"

DEFAULT_VIP_TEXT = (
    "💎 <b>VIP PREMIUM</b>\n\n"
    "✅ Sizga kim layk bosganini ko'rasiz\n"
    "✅ Shaxsiy chatga (Telegram profilga) o'tish imkoniyati\n"
    "✅ Anketangiz qidiruvda birinchi ko'rsatiladi\n"
    "✅ Kunlik layk limiti yo'q"
)

# reja: (kun, promokod bonus kuni, nomi)
PLANS = {
    "w": (7, 3, "1 haftalik"),
    "m": (30, 4, "1 oylik"),
    "q": (90, 5, "3 oylik"),
}
DEFAULT_PRICES = {"w": 15000, "m": 40000, "q": 100000}
DEFAULT_CARD = "6262 5701 3419 2691"
DEFAULT_HOLDER = "Boratova Zarnigir"
DEFAULT_RECYCLE_DAYS = 3

pairs: dict[int, int] = {}
waiting: list[int] = []
notify_ts: dict[int, float] = {}


def esc(s) -> str:
    return html.escape(str(s or ""))


def money(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_date(ts) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


# ---------- DB ----------
async def q1(sql, args=()):
    cur = await db.execute(sql, args)
    row = await cur.fetchone()
    await cur.close()
    return row


async def qa(sql, args=()):
    cur = await db.execute(sql, args)
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def ex(sql, args=()):
    await db.execute(sql, args)
    await db.commit()


async def ex_cur(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, name TEXT, age INTEGER,
            gender TEXT, looking TEXT DEFAULT 'all', city TEXT, interests TEXT,
            bio TEXT, photo TEXT, active INTEGER DEFAULT 1, banned INTEGER DEFAULT 0,
            vip_until INTEGER DEFAULT 0, created INTEGER, promo TEXT);
        CREATE TABLE IF NOT EXISTS views(
            from_id INTEGER, to_id INTEGER, action TEXT, created INTEGER,
            PRIMARY KEY(from_id, to_id));
        CREATE TABLE IF NOT EXISTS saved(
            user_id INTEGER, target_id INTEGER, PRIMARY KEY(user_id, target_id));
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, target_id INTEGER,
            created INTEGER, handled INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT,
            days INTEGER, bonus INTEGER DEFAULT 0, amount INTEGER, promo TEXT,
            file_id TEXT, status INTEGER DEFAULT 0, created INTEGER, handled_by INTEGER);
        CREATE TABLE IF NOT EXISTS promos(
            code TEXT PRIMARY KEY, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS promo_uses(
            user_id INTEGER, code TEXT, PRIMARY KEY(user_id, code));
        """
    )
    # eski bazalar uchun
    try:
        await db.execute("ALTER TABLE users ADD COLUMN promo TEXT")
    except Exception:
        pass
    await db.commit()


async def get_setting(key, default=None):
    row = await q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


async def set_setting(key, value):
    await ex(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def int_setting(key, default) -> int:
    v = await get_setting(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def price_of(plan) -> int:
    return await int_setting(f"price_{plan}", DEFAULT_PRICES[plan])


async def get_card():
    return (await get_setting("card", DEFAULT_CARD), await get_setting("holder", DEFAULT_HOLDER))


async def get_user(uid):
    return await q1("SELECT * FROM users WHERE id=?", (uid,))


def is_vip(u) -> bool:
    return bool(u) and u["vip_until"] > time.time()


def has_premium(uid, u) -> bool:
    return uid in ADMINS or is_vip(u)


async def grant_vip(uid, days) -> int:
    u = await get_user(uid)
    base = max(u["vip_until"] if u else 0, int(time.time()))
    until = base + days * 86400
    await ex("UPDATE users SET vip_until=? WHERE id=?", (until, uid))
    return until


def profile_text(u) -> str:
    g = "👨" if u["gender"] == "male" else "👩"
    vip = " 💎" if is_vip(u) else ""
    t = f"<b>{esc(u['name'])}</b>, {u['age']}, {esc(u['city'])} {g}{vip}"
    if u["interests"]:
        t += f"\n\n🎯 <b>Qiziqishlar:</b> {esc(u['interests'])}"
    if u["bio"]:
        t += f"\n\n{esc(u['bio'])}"
    return t


def user_link(u) -> str:
    if u["username"]:
        return f"@{u['username']}"
    return f'<a href="tg://user?id={u["id"]}">{esc(u["name"])}</a>'


# ---------- Klaviaturalar ----------
def rk(rows, one_time=False):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=one_time,
    )


def main_kb():
    return rk([[B_SEARCH, B_CHAT], [B_LIKES], [B_PROFILE, B_VIP], [B_SAVED]])


def ik(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def card_kb(tid, src):
    dm = [("💬 Shaxsiy chat 💎", f"dm:{tid}")]
    if src == "s":
        return ik([[("❤️", f"like:{tid}:s"), ("🗑 O'chirish", f"unsave:{tid}:s")], dm])
    rows = [[("❤️", f"like:{tid}:{src}"), ("👎", f"skip:{tid}:{src}")]]
    rows.append(dm)
    rows.append([("🔖 Saqlash", f"save:{tid}:{src}"), ("🚨 Shikoyat", f"rep:{tid}:{src}")])
    rows.append([("⬅️ Orqaga", "back")])
    return ik(rows)


# ---------- Middleware: ban + majburiy obuna ----------
class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user or user.id in ADMINS:
            return await handler(event, data)
        row = await q1("SELECT banned FROM users WHERE id=?", (user.id,))
        if row and row["banned"]:
            return
        ch = await get_setting("channel")
        if ch and not (isinstance(event, CallbackQuery) and event.data == "chk"):
            try:
                m = await bot.get_chat_member(ch, user.id)
                ok = m.status in ("member", "administrator", "creator")
            except Exception:
                ok = True
            if not ok:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📢 Kanalga o'tish", url=f"https://t.me/{ch.lstrip('@')}")],
                        [InlineKeyboardButton(text="✅ Tekshirish", callback_data="chk")],
                    ]
                )
                txt = "Botdan foydalanish uchun kanalimizga obuna bo'ling 👇"
                if isinstance(event, Message):
                    await event.answer(txt, reply_markup=kb)
                else:
                    await event.answer("Avval kanalga obuna bo'ling!", show_alert=True)
                return
        return await handler(event, data)


# ---------- Ro'yxatdan o'tish ----------
class Reg(StatesGroup):
    name = State()
    age = State()
    gender = State()
    looking = State()
    city = State()
    interests = State()
    bio = State()
    photo = State()


class Pay(StatesGroup):
    receipt = State()
    promo = State()


r = Router()


async def begin_reg(msg: Message, state: FSMContext, first_name: str):
    await state.clear()
    await state.set_state(Reg.name)
    await msg.answer("📝 Anketa to'ldiramiz.\n\nIsmingiz nima?", reply_markup=rk([[first_name[:30]]]))


@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in waiting:
        waiting.remove(m.from_user.id)
    u = await get_user(m.from_user.id)
    if u and u["photo"]:
        await ex("UPDATE users SET username=? WHERE id=?", (m.from_user.username, m.from_user.id))
        await m.answer("🎯 <b>Ola Match Bot</b>ga xush kelibsiz!\n\n"
                       "❤️ Layk bosing — agar u ham yoqsa, match bo'ladi\n"
                       "👎 Yoqmasa — o'tkazib yuboring\n\nTanishuvni hoziroq boshlang 👇",
                       reply_markup=main_kb())
    else:
        await m.answer("👋 Salom! Bu yerda yangi insonlar bilan tanishishingiz mumkin.")
        await begin_reg(m, state, m.from_user.first_name or "Ism")


@r.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    if not 2 <= len(m.text) <= 30:
        return await m.answer("Ism 2-30 ta belgi bo'lsin.")
    await state.update_data(name=m.text.strip())
    await state.set_state(Reg.age)
    await m.answer("Yoshingiz nechada?", reply_markup=ReplyKeyboardRemove())


@r.message(Reg.age, F.text)
async def reg_age(m: Message, state: FSMContext):
    if not m.text.isdigit() or not 16 <= int(m.text) <= 70:
        return await m.answer("Yoshni raqam bilan kiriting (16-70).")
    await state.update_data(age=int(m.text))
    await state.set_state(Reg.gender)
    await m.answer("Jinsingiz?", reply_markup=rk([["👨 Yigit", "👩 Qiz"]]))


@r.message(Reg.gender, F.text.in_({"👨 Yigit", "👩 Qiz"}))
async def reg_gender(m: Message, state: FSMContext):
    await state.update_data(gender="male" if "Yigit" in m.text else "female")
    await state.set_state(Reg.looking)
    await m.answer("Kimni qidiryapsiz?", reply_markup=rk([["👨 Yigitlar", "👩 Qizlar"], ["👥 Hammasi"]]))


@r.message(Reg.looking, F.text.in_({"👨 Yigitlar", "👩 Qizlar", "👥 Hammasi"}))
async def reg_looking(m: Message, state: FSMContext):
    v = {"👨 Yigitlar": "male", "👩 Qizlar": "female", "👥 Hammasi": "all"}[m.text]
    await state.update_data(looking=v)
    await state.set_state(Reg.city)
    await m.answer("Qaysi shaharda yashaysiz?", reply_markup=ReplyKeyboardRemove())


@r.message(Reg.city, F.text)
async def reg_city(m: Message, state: FSMContext):
    await state.update_data(city=m.text.strip()[:40])
    await state.set_state(Reg.interests)
    await m.answer("Qiziqishlaringiz? (masalan: Sport, Musiqa, Sayohat)")


@r.message(Reg.interests, F.text)
async def reg_interests(m: Message, state: FSMContext):
    await state.update_data(interests=m.text.strip()[:100])
    await state.set_state(Reg.bio)
    await m.answer("O'zingiz haqingizda qisqacha yozing:", reply_markup=rk([[B_SKIP]]))


@r.message(Reg.bio, F.text)
async def reg_bio(m: Message, state: FSMContext):
    await state.update_data(bio="" if m.text == B_SKIP else m.text.strip()[:300])
    await state.set_state(Reg.photo)
    await m.answer("📸 Endi rasmingizni yuboring:", reply_markup=ReplyKeyboardRemove())


@r.message(Reg.photo, F.photo)
async def reg_photo(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await ex(
        """INSERT INTO users(id,username,name,age,gender,looking,city,interests,bio,photo,created)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,
           age=excluded.age,gender=excluded.gender,looking=excluded.looking,city=excluded.city,
           interests=excluded.interests,bio=excluded.bio,photo=excluded.photo,active=1""",
        (m.from_user.id, m.from_user.username, d["name"], d["age"], d["gender"], d["looking"],
         d["city"], d["interests"], d["bio"], m.photo[-1].file_id, int(time.time())),
    )
    await m.answer("✅ Anketa saqlandi!", reply_markup=main_kb())
    u = await get_user(m.from_user.id)
    await m.answer_photo(u["photo"], caption=profile_text(u))


@r.message(StateFilter(Reg.photo))
async def reg_photo_bad(m: Message):
    await m.answer("Iltimos, rasm yuboring 📸")


# ---------- Qidirish ----------
async def next_candidate(me):
    """Ko'rilgan anketalar N kundan keyin qayta chiqadi (layk bosilganlar ham)."""
    days = await int_setting("recycle_days", DEFAULT_RECYCLE_DAYS)
    now = int(time.time())
    cutoff = now - days * 86400
    sql = """SELECT * FROM users WHERE id!=? AND active=1 AND banned=0 AND photo IS NOT NULL
             AND (?='all' OR gender=?) AND (looking='all' OR looking=?)
             AND id NOT IN (SELECT to_id FROM views WHERE from_id=? AND created>?)
             ORDER BY (vip_until>?) DESC, RANDOM() LIMIT 1"""
    return await q1(sql, (me["id"], me["looking"], me["looking"], me["gender"], me["id"], cutoff, now))


async def next_liker(uid):
    sql = """SELECT u.* FROM views v JOIN users u ON u.id=v.from_id
             WHERE v.to_id=? AND v.action='like' AND u.banned=0 AND u.active=1
             AND v.from_id NOT IN (SELECT to_id FROM views WHERE from_id=?)
             ORDER BY v.created DESC LIMIT 1"""
    return await q1(sql, (uid, uid))


async def show_next(uid, src="b"):
    me = await get_user(uid)
    if not me:
        return await bot.send_message(uid, "Avval /start bosing.")
    cand = await (next_liker(uid) if src == "l" else next_candidate(me))
    if not cand:
        txt = "😔 Hozircha yangi anketalar qolmadi. Keyinroq qaytib keling!"
        return await bot.send_message(uid, txt, reply_markup=main_kb())
    await bot.send_photo(uid, cand["photo"], caption=profile_text(cand), reply_markup=card_kb(cand["id"], src))


@r.message(F.text == B_SEARCH, StateFilter(None))
async def search(m: Message):
    await show_next(m.from_user.id)


async def likes_today(uid) -> int:
    start = int(time.time()) - int(time.time()) % 86400
    row = await q1("SELECT COUNT(*) c FROM views WHERE from_id=? AND action='like' AND created>=?", (uid, start))
    return row["c"]


async def do_match(a, b):
    for x, y in ((a, b), (b, a)):
        me = await get_user(x)
        u = await get_user(y)
        if not u:
            continue
        try:
            if has_premium(x, me):
                await bot.send_photo(
                    x, u["photo"],
                    caption=f"🎉 <b>MATCH!</b>\n{esc(u['name'])} sizni ham yoqtirdi!\n\n💬 Shaxsiy chat: {user_link(u)}",
                )
            else:
                await bot.send_photo(
                    x, u["photo"],
                    caption=(f"🎉 <b>MATCH!</b>\n{esc(u['name'])} sizni ham yoqtirdi!\n\n"
                             "🔒 Shaxsiy chatga o'tish faqat 💎 PREMIUM foydalanuvchilar uchun."),
                    reply_markup=ik([[("💎 Premium olish", "vipinfo")]]),
                )
        except Exception:
            pass


@r.callback_query(F.data.regexp(r"^(like|skip|save|rep|unsave):(\d+):([bls])$"))
async def act(cb: CallbackQuery):
    action, tid, src = cb.data.split(":")
    tid = int(tid)
    uid = cb.from_user.id
    me = await get_user(uid)
    if not me:
        return await cb.answer("Avval /start bosing", show_alert=True)
    now = int(time.time())

    if action == "like":
        limit = await int_setting("free_likes", FREE_LIKES)
        if not has_premium(uid, me) and await likes_today(uid) >= limit:
            return await cb.answer(f"Kunlik limit ({limit}) tugadi. VIP olib limitsiz foydalaning 💎", show_alert=True)
        await ex("INSERT OR REPLACE INTO views VALUES(?,?,?,?)", (uid, tid, "like", now))
        mutual = await q1("SELECT 1 FROM views WHERE from_id=? AND to_id=? AND action='like'", (tid, uid))
        if mutual:
            await do_match(uid, tid)
        elif time.time() - notify_ts.get(tid, 0) > 6 * 3600:
            notify_ts[tid] = time.time()
            try:
                await bot.send_message(tid, "💌 Kimdir sizga layk bosdi! «Menga kim layk bosdi» bo'limini oching.")
            except Exception:
                pass
    elif action == "skip":
        await ex("INSERT OR REPLACE INTO views VALUES(?,?,?,?)", (uid, tid, "skip", now))
    elif action == "save":
        await ex("INSERT OR IGNORE INTO saved VALUES(?,?)", (uid, tid))
        await ex("INSERT OR REPLACE INTO views VALUES(?,?,?,?)", (uid, tid, "save", now))
        await cb.answer("🔖 Saqlandi")
    elif action == "unsave":
        await ex("DELETE FROM saved WHERE user_id=? AND target_id=?", (uid, tid))
        await cb.answer("O'chirildi")
    elif action == "rep":
        # shikoyat qilingan anketa qayta chiqmasligi uchun "kelajak" vaqti bilan yoziladi
        await ex("INSERT INTO reports(from_id,target_id,created) VALUES(?,?,?)", (uid, tid, now))
        await ex("INSERT OR REPLACE INTO views VALUES(?,?,?,?)", (uid, tid, "skip", now + 3650 * 86400))
        await cb.answer("🚨 Shikoyat yuborildi")
        for a in ADMINS:
            try:
                await bot.send_message(a, f"🚨 Yangi shikoyat: <code>{tid}</code> ustidan. /admin → Shikoyatlar")
            except Exception:
                pass

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if action in ("like", "skip", "save", "rep") and src in ("b", "l"):
        await cb.answer()
        await show_next(uid, src)
    elif action == "like":
        await cb.answer("❤️")


@r.callback_query(F.data.regexp(r"^dm:(\d+)$"))
async def dm(cb: CallbackQuery):
    uid = cb.from_user.id
    me = await get_user(uid)
    if not me:
        return await cb.answer("Avval /start bosing", show_alert=True)
    if not has_premium(uid, me):
        await cb.answer("🔒 Shaxsiy chat faqat PREMIUM uchun", show_alert=True)
        await cb.message.answer(
            "🔒 <b>Shaxsiy chatga o'tish</b> faqat 💎 PREMIUM foydalanuvchilarga ochiq.\n\nPremium olib, xohlagan odamga yozing!",
            reply_markup=ik([[("💎 Premium olish", "vipinfo")]]),
        )
        return
    t = await get_user(int(cb.data.split(":")[1]))
    if not t:
        return await cb.answer("Foydalanuvchi topilmadi", show_alert=True)
    await cb.answer()
    await cb.message.answer(f"💬 {esc(t['name'])} bilan shaxsiy chat: {user_link(t)}")


@r.callback_query(F.data == "back")
async def back(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer("Asosiy menyu", reply_markup=main_kb())
    await cb.answer()


@r.callback_query(F.data == "chk")
async def chk(cb: CallbackQuery):
    await cb.answer("✅ Rahmat!")
    await cb.message.answer("Davom etish uchun /start bosing")


# ---------- Kim layk bosdi ----------
@r.message(F.text == B_LIKES, StateFilter(None))
async def who_liked(m: Message):
    uid = m.from_user.id
    me = await get_user(uid)
    if not me:
        return await m.answer("Avval /start bosing.")
    row = await q1(
        """SELECT COUNT(*) c FROM views v WHERE v.to_id=? AND v.action='like'
           AND v.from_id NOT IN (SELECT to_id FROM views WHERE from_id=?)""", (uid, uid))
    if row["c"] == 0:
        return await m.answer("Hozircha yangi laykler yo'q. Anketangiz qidiruvda ko'rsatilmoqda 😉")
    if has_premium(uid, me):
        await m.answer(f"💘 Sizga {row['c']} kishi layk bosgan:")
        return await show_next(uid, "l")
    await m.answer(f"💘 Sizga <b>{row['c']}</b> kishi layk bosdi!\n\nKimligini ko'rish uchun 💎 VIP PREMIUM oling.",
                   reply_markup=ik([[("💎 VIP olish", "vipinfo")]]))


# ---------- Saqlanganlar ----------
@r.message(F.text == B_SAVED, StateFilter(None))
async def saved(m: Message):
    rows = await qa("SELECT u.* FROM saved s JOIN users u ON u.id=s.target_id WHERE s.user_id=? AND u.banned=0 LIMIT 10",
                    (m.from_user.id,))
    if not rows:
        return await m.answer("🔖 Saqlangan anketalar yo'q.")
    for u in rows:
        await m.answer_photo(u["photo"], caption=profile_text(u), reply_markup=card_kb(u["id"], "s"))


# ---------- Profil ----------
@r.message(F.text == B_PROFILE, StateFilter(None))
async def my_profile(m: Message):
    u = await get_user(m.from_user.id)
    if not u:
        return await m.answer("Avval /start bosing.")
    vip = f"\n\n💎 VIP: {fmt_date(u['vip_until'])} gacha" if is_vip(u) else ""
    state = "🟢 Anketa faol" if u["active"] else "🔴 Anketa yashirilgan"
    kb = ik([
        [("✏️ Qayta to'ldirish", "p_edit")],
        [("🙈 Yashirish" if u["active"] else "👁 Faollashtirish", "p_toggle")],
        [("🗑 O'chirish", "p_del")],
    ])
    await m.answer_photo(u["photo"], caption=f"{profile_text(u)}\n\n{state}{vip}", reply_markup=kb)


@r.callback_query(F.data == "p_edit")
async def p_edit(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await begin_reg(cb.message, state, cb.from_user.first_name or "Ism")


@r.callback_query(F.data == "p_toggle")
async def p_toggle(cb: CallbackQuery):
    await ex("UPDATE users SET active=1-active WHERE id=?", (cb.from_user.id,))
    await cb.answer("Bajarildi")
    await cb.message.answer("Holat o'zgartirildi. «Mening profilim» orqali ko'ring.")


@r.callback_query(F.data == "p_del")
async def p_del(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("Anketani butunlay o'chirasizmi?", reply_markup=ik([[("Ha, o'chirish", "p_del_yes"), ("Yo'q", "back")]]))


@r.callback_query(F.data == "p_del_yes")
async def p_del_yes(cb: CallbackQuery):
    uid = cb.from_user.id
    await ex("DELETE FROM users WHERE id=?", (uid,))
    await ex("DELETE FROM views WHERE from_id=? OR to_id=?", (uid, uid))
    await ex("DELETE FROM saved WHERE user_id=? OR target_id=?", (uid, uid))
    await cb.answer()
    await cb.message.answer("🗑 Anketa o'chirildi. Qayta boshlash: /start", reply_markup=ReplyKeyboardRemove())


# ---------- VIP / TO'LOV ----------
async def send_vip(target: Message, uid: int):
    u = await get_user(uid)
    if not u:
        return await target.answer("Avval /start bosing.")
    perks = await get_setting("vip_text", DEFAULT_VIP_TEXT)
    head = ""
    if is_vip(u):
        head = f"✅ Sizda VIP faol: {fmt_date(u['vip_until'])} gacha\n\n"
    promo = await valid_promo(uid, u["promo"])
    rows = []
    for k, (days, bonus, name) in PLANS.items():
        label = f"{name} — {money(await price_of(k))} so'm"
        if promo:
            label += f" (+{bonus} kun 🎁)"
        rows.append([(label, f"buy:{k}")])
    rows.append([("🎟 Promokod kiritish", "promo")])
    txt = head + perks + "\n\n👇 Tarifni tanlang:"
    if promo:
        txt += f"\n🎟 Promokod faol: <code>{esc(promo)}</code>"
    await target.answer(txt, reply_markup=ik(rows))


async def valid_promo(uid, code):
    if not code:
        return None
    p = await q1("SELECT * FROM promos WHERE code=? AND active=1", (code,))
    if not p:
        return None
    if p["max_uses"] > 0 and p["used"] >= p["max_uses"]:
        return None
    if await q1("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code)):
        return None
    return code


@r.message(F.text == B_VIP, StateFilter(None))
async def vip(m: Message):
    await send_vip(m, m.from_user.id)


@r.callback_query(F.data == "vipinfo")
async def vipinfo_cb(cb: CallbackQuery):
    await cb.answer()
    await send_vip(cb.message, cb.from_user.id)


@r.callback_query(F.data == "promo")
async def promo_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Pay.promo)
    await cb.message.answer("🎟 Promokodni yuboring:", reply_markup=rk([[B_CANCEL]]))


@r.message(F.text == B_CANCEL, StateFilter(Pay.promo, Pay.receipt))
async def pay_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=main_kb())


@r.message(Pay.promo, F.text)
async def promo_enter(m: Message, state: FSMContext):
    await state.clear()
    code = m.text.strip().upper()
    ok = await valid_promo(m.from_user.id, code)
    if not ok:
        await m.answer("❌ Promokod noto'g'ri yoki ishlatib bo'lingan.", reply_markup=main_kb())
    else:
        await ex("UPDATE users SET promo=? WHERE id=?", (code, m.from_user.id))
        await m.answer("✅ Promokod qabul qilindi! Endi tarif tanlasangiz bonus kunlar qo'shiladi 🎁",
                       reply_markup=main_kb())
    await send_vip(m, m.from_user.id)


@r.callback_query(F.data.regexp(r"^buy:[wmq]$"))
async def buy(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    u = await get_user(uid)
    if not u:
        return await cb.answer("Avval /start bosing", show_alert=True)
    if await q1("SELECT 1 FROM payments WHERE user_id=? AND status=0", (uid,)):
        return await cb.answer("⏳ Oldingi to'lovingiz tekshirilmoqda. Admin javobini kuting.", show_alert=True)
    plan = cb.data.split(":")[1]
    days, bonus, name = PLANS[plan]
    price = await price_of(plan)
    promo = await valid_promo(uid, u["promo"])
    card, holder = await get_card()
    bonus_line = f"\n🎁 Promokod bonusi: <b>+{bonus} kun</b> (jami {days + bonus} kun)" if promo else ""
    await state.set_state(Pay.receipt)
    await state.update_data(plan=plan)
    await cb.answer()
    await cb.message.answer(
        f"💎 <b>{name} PREMIUM</b> — {days} kun{bonus_line}\n\n"
        f"💰 To'lov summasi: <b>{money(price)} so'm</b>\n"
        f"💳 Karta: <code>{esc(card)}</code>\n"
        f"👤 Qabul qiluvchi: <b>{esc(holder)}</b>\n\n"
        "1️⃣ Yuqoridagi kartaga to'lov qiling\n"
        "2️⃣ To'lov <b>chekining rasmini (skrinshot)</b> shu yerga yuboring\n"
        "3️⃣ Admin tekshirib tasdiqlaydi — premium avtomatik beriladi ✅",
        reply_markup=rk([[B_CANCEL]]),
    )


@r.message(Pay.receipt, F.photo)
async def receipt(m: Message, state: FSMContext):
    uid = m.from_user.id
    d = await state.get_data()
    plan = d.get("plan")
    await state.clear()
    u = await get_user(uid)
    if plan not in PLANS or not u:
        return await m.answer("Xatolik. Qaytadan urinib ko'ring.", reply_markup=main_kb())
    if await q1("SELECT 1 FROM payments WHERE user_id=? AND status=0", (uid,)):
        return await m.answer("⏳ Oldingi to'lovingiz tekshirilmoqda.", reply_markup=main_kb())
    days, bonus_days, name = PLANS[plan]
    price = await price_of(plan)
    promo = await valid_promo(uid, u["promo"])
    bonus = bonus_days if promo else 0
    cur = await ex_cur(
        "INSERT INTO payments(user_id,plan,days,bonus,amount,promo,file_id,created) VALUES(?,?,?,?,?,?,?,?)",
        (uid, plan, days, bonus, price, promo, m.photo[-1].file_id, int(time.time())),
    )
    pid = cur.lastrowid
    await m.answer("✅ Chek qabul qilindi! Admin tekshirib, tez orada premiumni faollashtiradi. ⏳",
                   reply_markup=main_kb())
    p = await q1("SELECT * FROM payments WHERE id=?", (pid,))
    for a in ADMINS:
        try:
            await bot.send_photo(a, p["file_id"], caption=pay_caption(p, u),
                                 reply_markup=ik([[("✅ Tasdiqlash", f"pa:{pid}"), ("❌ Rad etish", f"pr:{pid}")]]))
        except Exception:
            pass


@r.message(StateFilter(Pay.receipt))
async def receipt_bad(m: Message):
    await m.answer("Iltimos, to'lov chekining <b>rasmini</b> yuboring 📸 (yoki «Bekor qilish»).")


def pay_caption(p, u) -> str:
    tot = p["days"] + p["bonus"]
    uname = f"@{u['username']}" if u and u["username"] else "—"
    return (f"💳 <b>To'lov #{p['id']}</b>\n"
            f"👤 {esc(u['name'] if u else '')} ({uname})\nID: <code>{p['user_id']}</code>\n"
            f"📦 Tarif: {PLANS[p['plan']][2]} ({p['days']} kun"
            + (f" + {p['bonus']} bonus = {tot} kun" if p["bonus"] else "") + ")\n"
            f"💰 Summa: <b>{money(p['amount'])} so'm</b>\n"
            f"🎟 Promokod: {esc(p['promo']) if p['promo'] else '—'}")


# ---------- Anonim suhbat ----------
class InChat(Filter):
    async def __call__(self, m: Message) -> bool:
        return m.from_user.id in pairs


chat_r = Router()
chat_r.message.filter(InChat())


def chat_kb():
    return rk([[B_NEXT], [B_STOP]])


async def end_chat(uid, notify_self=True):
    partner = pairs.pop(uid, None)
    if partner:
        pairs.pop(partner, None)
        try:
            await bot.send_message(partner, "😔 Suhbatdosh suhbatni tugatdi.", reply_markup=main_kb())
        except Exception:
            pass
    if notify_self:
        await bot.send_message(uid, "Suhbat tugadi.", reply_markup=main_kb())


async def start_chat(uid):
    if uid in waiting:
        waiting.remove(uid)
    while waiting:
        pid = waiting.pop(0)
        if pid != uid and pid not in pairs:
            pairs[uid], pairs[pid] = pid, uid
            txt = "✅ Suhbatdosh topildi! Yozishingiz mumkin (matn, rasm, ovoz).\n/stop — tugatish"
            await bot.send_message(uid, txt, reply_markup=chat_kb())
            await bot.send_message(pid, txt, reply_markup=chat_kb())
            return
    waiting.append(uid)
    await bot.send_message(uid, "🔎 Suhbatdosh qidirilmoqda...", reply_markup=rk([[B_CANCEL]]))


@chat_r.message()
async def relay(m: Message):
    uid = m.from_user.id
    if m.text in (B_STOP, "/stop"):
        return await end_chat(uid)
    if m.text == B_NEXT:
        await end_chat(uid, notify_self=False)
        return await start_chat(uid)
    try:
        await m.copy_to(pairs[uid])
    except Exception:
        await m.answer("⚠️ Xabarni yuborib bo'lmadi.")


@r.message(F.text == B_CHAT, StateFilter(None))
async def chat_btn(m: Message):
    if not await get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    await start_chat(m.from_user.id)


@r.message(F.text == B_CANCEL, StateFilter(None))
async def cancel_wait(m: Message):
    if m.from_user.id in waiting:
        waiting.remove(m.from_user.id)
    await m.answer("Bekor qilindi.", reply_markup=main_kb())


# ---------- ADMIN PANEL ----------
class Adm(StatesGroup):
    bc = State()
    ban = State()
    unban = State()
    vip = State()
    chan = State()
    find = State()
    viptext = State()
    price = State()
    card = State()
    promo_add = State()
    promo_del = State()


adm = Router()
adm.message.filter(F.from_user.id.in_(ADMINS))
adm.callback_query.filter(F.from_user.id.in_(ADMINS))

ADMIN_KB = ik([
    [("📊 Statistika", "a:stats"), ("📢 Xabar yuborish", "a:bc")],
    [("💳 To'lovlar (kutilmoqda)", "a:pays"), ("💰 Daromad", "a:income")],
    [("💎 VIP berish", "a:vip"), ("👑 VIP ro'yxati", "a:vips")],
    [("🎟 Promokodlar", "a:promos"), ("💵 Narx va sozlamalar", "a:settings")],
    [("💳 Karta ma'lumoti", "a:card"), ("✏️ VIP matni", "a:viptext")],
    [("🚫 Ban", "a:ban"), ("✅ Unban", "a:unban")],
    [("🔎 Foydalanuvchi", "a:find"), ("🚨 Shikoyatlar", "a:reports")],
    [("📌 Majburiy kanal", "a:chan")],
])


@adm.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    n = (await q1("SELECT COUNT(*) c FROM payments WHERE status=0"))["c"]
    extra = f"\n\n⏳ Kutilayotgan to'lovlar: <b>{n}</b>" if n else ""
    await m.answer(f"🛠 <b>Admin panel</b>{extra}", reply_markup=ADMIN_KB)


@adm.callback_query(F.data == "a:stats")
async def a_stats(cb: CallbackQuery):
    now = int(time.time())
    day = now - now % 86400
    c = lambda s, a=(): q1(s, a)
    total = (await c("SELECT COUNT(*) c FROM users"))["c"]
    active = (await c("SELECT COUNT(*) c FROM users WHERE active=1 AND banned=0"))["c"]
    male = (await c("SELECT COUNT(*) c FROM users WHERE gender='male'"))["c"]
    female = (await c("SELECT COUNT(*) c FROM users WHERE gender='female'"))["c"]
    today = (await c("SELECT COUNT(*) c FROM users WHERE created>=?", (day,)))["c"]
    vips = (await c("SELECT COUNT(*) c FROM users WHERE vip_until>?", (now,)))["c"]
    banned = (await c("SELECT COUNT(*) c FROM users WHERE banned=1"))["c"]
    likes = (await c("SELECT COUNT(*) c FROM views WHERE action='like'"))["c"]
    pend = (await c("SELECT COUNT(*) c FROM payments WHERE status=0"))["c"]
    matches = (await c(
        """SELECT COUNT(*) c FROM views a JOIN views b ON a.from_id=b.to_id AND a.to_id=b.from_id
           WHERE a.action='like' AND b.action='like' AND a.from_id<a.to_id"""))["c"]
    await cb.message.answer(
        f"📊 <b>Statistika</b>\n\n👥 Jami: {total}\n🟢 Faol: {active}\n👨 Yigit: {male} | 👩 Qiz: {female}\n"
        f"🆕 Bugun qo'shilgan: {today}\n💎 VIP: {vips}\n🚫 Banlangan: {banned}\n"
        f"❤️ Laykler: {likes}\n🎉 Matchlar: {matches}\n⏳ Kutilayotgan to'lov: {pend}\n"
        f"💬 Suhbatda: {len(pairs)//2} juft | Kutmoqda: {len(waiting)}"
    )
    await cb.answer()


@adm.callback_query(F.data == "a:income")
async def a_income(cb: CallbackQuery):
    now = int(time.time())
    day = now - now % 86400
    tot = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payments WHERE status=1")
    td = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payments WHERE status=1 AND created>=?", (day,))
    wk = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payments WHERE status=1 AND created>=?", (now - 7 * 86400,))
    mo = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payments WHERE status=1 AND created>=?", (now - 30 * 86400,))
    rej = (await q1("SELECT COUNT(*) c FROM payments WHERE status=2"))["c"]
    await cb.message.answer(
        "💰 <b>Daromad</b>\n\n"
        f"Bugun: {money(td['s'])} so'm ({td['c']} ta)\n"
        f"7 kun: {money(wk['s'])} so'm ({wk['c']} ta)\n"
        f"30 kun: {money(mo['s'])} so'm ({mo['c']} ta)\n"
        f"Jami: <b>{money(tot['s'])} so'm</b> ({tot['c']} ta)\n"
        f"❌ Rad etilgan: {rej}"
    )
    await cb.answer()


@adm.callback_query(F.data == "a:vips")
async def a_vips(cb: CallbackQuery):
    rows = await qa("SELECT * FROM users WHERE vip_until>? ORDER BY vip_until DESC LIMIT 30", (int(time.time()),))
    if not rows:
        await cb.message.answer("💎 Hozircha VIP foydalanuvchilar yo'q.")
    else:
        lines = [f"• {esc(u['name'])} (<code>{u['id']}</code>) — {fmt_date(u['vip_until'])} gacha" for u in rows]
        await cb.message.answer("👑 <b>VIP foydalanuvchilar</b>\n\n" + "\n".join(lines))
    await cb.answer()


# --- to'lovlarni tasdiqlash ---
@adm.callback_query(F.data == "a:pays")
async def a_pays(cb: CallbackQuery):
    rows = await qa("SELECT * FROM payments WHERE status=0 ORDER BY id LIMIT 10")
    if not rows:
        await cb.message.answer("💳 Kutilayotgan to'lovlar yo'q.")
    for p in rows:
        u = await get_user(p["user_id"])
        await cb.message.answer_photo(
            p["file_id"], caption=pay_caption(p, u),
            reply_markup=ik([[("✅ Tasdiqlash", f"pa:{p['id']}"), ("❌ Rad etish", f"pr:{p['id']}")]]))
    await cb.answer()


@adm.callback_query(F.data.regexp(r"^(pa|pr):(\d+)$"))
async def a_pay_act(cb: CallbackQuery):
    act_, pid = cb.data.split(":")
    pid = int(pid)
    new = 1 if act_ == "pa" else 2
    cur = await ex_cur("UPDATE payments SET status=?, handled_by=? WHERE id=? AND status=0",
                       (new, cb.from_user.id, pid))
    if cur.rowcount == 0:
        await cb.answer("Bu to'lov allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    p = await q1("SELECT * FROM payments WHERE id=?", (pid,))
    uid = p["user_id"]
    if new == 1:
        total = p["days"] + p["bonus"]
        until = await grant_vip(uid, total)
        if p["promo"]:
            await ex("INSERT OR IGNORE INTO promo_uses VALUES(?,?)", (uid, p["promo"]))
            await ex("UPDATE promos SET used=used+1 WHERE code=?", (p["promo"],))
            await ex("UPDATE users SET promo=NULL WHERE id=? AND promo=?", (uid, p["promo"]))
        try:
            await bot.send_message(
                uid,
                f"🎉 <b>To'lov tasdiqlandi!</b>\n\n💎 Sizga {total} kunlik PREMIUM berildi."
                f"\n📅 Amal qilish muddati: {fmt_date(until)} gacha\n\n"
                "Endi kim layk bosganini ko'rasiz va shaxsiy chatga o'ta olasiz 💬",
                reply_markup=main_kb())
        except Exception:
            pass
        mark = "✅ TASDIQLANDI"
    else:
        try:
            await bot.send_message(
                uid, "❌ <b>To'lovingiz rad etildi.</b>\n\nChek noto'g'ri yoki to'lov topilmadi. "
                     "Qayta urinib ko'ring: «💎 VIP PREMIUM» bo'limi.", reply_markup=main_kb())
        except Exception:
            pass
        mark = "❌ RAD ETILDI"
    await cb.answer("Bajarildi")
    try:
        await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n<b>{mark}</b> ({cb.from_user.first_name})",
                                      reply_markup=None)
    except Exception:
        pass


# --- promokodlar ---
@adm.callback_query(F.data == "a:promos")
async def a_promos(cb: CallbackQuery):
    rows = await qa("SELECT * FROM promos ORDER BY rowid DESC LIMIT 30")
    lines = [f"• <code>{esc(p['code'])}</code> — {p['used']}/{p['max_uses'] or '∞'}" for p in rows] or ["(hali yo'q)"]
    await cb.message.answer(
        "🎟 <b>Promokodlar</b> (ishlatilgan/limit)\n\n" + "\n".join(lines) +
        "\n\n🎁 Bonus: 1 hafta +3 kun, 1 oy +4 kun, 3 oy +5 kun",
        reply_markup=ik([[("➕ Qo'shish", "a:promo_add"), ("🗑 O'chirish", "a:promo_del")]]))
    await cb.answer()


# --- narx va sozlamalar ---
@adm.callback_query(F.data == "a:settings")
async def a_settings(cb: CallbackQuery, state: FSMContext):
    lk = await int_setting("free_likes", FREE_LIKES)
    rc = await int_setting("recycle_days", DEFAULT_RECYCLE_DAYS)
    await state.set_state(Adm.price)
    await cb.message.answer(
        "💵 <b>Narxlar va sozlamalar</b>\n\n"
        f"w — 1 hafta: {money(await price_of('w'))} so'm\n"
        f"m — 1 oy: {money(await price_of('m'))} so'm\n"
        f"q — 3 oy: {money(await price_of('q'))} so'm\n"
        f"likes — kunlik bepul layk: {lk}\n"
        f"recycle — anketa qayta chiqish (kun): {rc}\n\n"
        "O'zgartirish uchun yuboring: <code>KALIT QIYMAT</code>\n"
        "Masalan: <code>m 45000</code> yoki <code>likes 30</code>\n\nBekor qilish: /admin")
    await cb.answer()


@adm.message(Adm.price)
async def s_price(m: Message, state: FSMContext):
    parts = (m.text or "").split()
    keys = {"w": "price_w", "m": "price_m", "q": "price_q", "likes": "free_likes", "recycle": "recycle_days"}
    if len(parts) != 2 or parts[0] not in keys or not parts[1].isdigit():
        return await m.answer("Format: <code>KALIT QIYMAT</code> (w, m, q, likes, recycle)")
    await set_setting(keys[parts[0]], parts[1])
    await m.answer(f"✅ Saqlandi: {parts[0]} = {parts[1]}\nYana o'zgartirish mumkin yoki /admin")


PROMPTS = {
    "bc": ("📢 Yuboriladigan xabarni jo'nating (matn/rasm/video):", Adm.bc),
    "ban": ("🚫 Ban qilinadigan user ID:", Adm.ban),
    "unban": ("✅ Unban qilinadigan user ID:", Adm.unban),
    "vip": ("💎 Format: <code>ID KUN</code> (masalan: <code>123456 30</code>). Olib tashlash uchun KUN=0", Adm.vip),
    "find": ("🔎 User ID yuboring:", Adm.find),
    "chan": ("📌 Kanal username yuboring (masalan <code>@kanal</code>) yoki <code>off</code>. Bot kanalda admin bo'lishi shart.", Adm.chan),
    "viptext": ("✏️ VIP bo'limi uchun yangi matnni yuboring (HTML mumkin):", Adm.viptext),
    "card": ("💳 Format: <code>KARTA RAQAMI | Ism Familiya</code>\nMasalan: <code>6262 5701 3419 2691 | Boratova Zarnigir</code>", Adm.card),
    "promo_add": ("➕ Format: <code>KOD LIMIT</code> (LIMIT=0 — cheksiz). Masalan: <code>YANGI 100</code>", Adm.promo_add),
    "promo_del": ("🗑 O'chiriladigan promokodni yuboring:", Adm.promo_del),
}


@adm.callback_query(F.data.regexp(r"^a:(bc|ban|unban|vip|find|chan|viptext|card|promo_add|promo_del)$"))
async def a_prompt(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    txt, st = PROMPTS[key]
    await state.set_state(st)
    await cb.message.answer(txt + "\n\nBekor qilish: /admin")
    await cb.answer()


@adm.message(Adm.card)
async def s_card(m: Message, state: FSMContext):
    if "|" not in (m.text or ""):
        return await m.answer("Format: <code>KARTA | Ism Familiya</code>")
    num, holder = [x.strip() for x in m.text.split("|", 1)]
    await state.clear()
    await set_setting("card", num)
    await set_setting("holder", holder)
    await m.answer(f"✅ Karta yangilandi:\n<code>{esc(num)}</code>\n{esc(holder)}")


@adm.message(Adm.promo_add)
async def s_promo_add(m: Message, state: FSMContext):
    parts = (m.text or "").split()
    if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
        return await m.answer("Format: <code>KOD LIMIT</code>")
    code = parts[0].upper()
    limit = int(parts[1]) if len(parts) == 2 else 0
    await state.clear()
    await ex("INSERT OR REPLACE INTO promos(code,max_uses,used,active) VALUES(?,?,COALESCE((SELECT used FROM promos WHERE code=?),0),1)",
             (code, limit, code))
    await m.answer(f"✅ Promokod qo'shildi: <code>{esc(code)}</code> (limit: {limit or '∞'})")


@adm.message(Adm.promo_del)
async def s_promo_del(m: Message, state: FSMContext):
    await state.clear()
    code = (m.text or "").strip().upper()
    await ex("DELETE FROM promos WHERE code=?", (code,))
    await m.answer(f"🗑 O'chirildi: <code>{esc(code)}</code>")


@adm.message(Adm.bc)
async def s_bc(m: Message, state: FSMContext):
    await state.clear()
    ids = [x["id"] for x in await qa("SELECT id FROM users WHERE banned=0")]
    await m.answer(f"📤 {len(ids)} ta foydalanuvchiga yuborilmoqda...")
    ok = fail = 0
    for i in ids:
        try:
            await m.copy_to(i)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Xato: {fail}")


async def parse_id(m: Message):
    if not (m.text or "").strip().isdigit():
        await m.answer("ID raqam bo'lishi kerak.")
        return None
    return int(m.text.strip())


@adm.message(Adm.ban)
async def s_ban(m: Message, state: FSMContext):
    uid = await parse_id(m)
    if uid is None:
        return
    await state.clear()
    await ex("UPDATE users SET banned=1 WHERE id=?", (uid,))
    await end_chat(uid, notify_self=False)
    if uid in waiting:
        waiting.remove(uid)
    await m.answer(f"🚫 {uid} banlandi.")


@adm.message(Adm.unban)
async def s_unban(m: Message, state: FSMContext):
    uid = await parse_id(m)
    if uid is None:
        return
    await state.clear()
    await ex("UPDATE users SET banned=0 WHERE id=?", (uid,))
    await m.answer(f"✅ {uid} unban qilindi.")


@adm.message(Adm.vip)
async def s_vip(m: Message, state: FSMContext):
    parts = (m.text or "").split()
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        return await m.answer("Format: ID KUN")
    uid, days = int(parts[0]), int(parts[1])
    u = await get_user(uid)
    if not u:
        return await m.answer("Bunday foydalanuvchi yo'q.")
    await state.clear()
    if days <= 0:
        await ex("UPDATE users SET vip_until=0 WHERE id=?", (uid,))
        return await m.answer("VIP olib tashlandi.")
    await grant_vip(uid, days)
    await m.answer(f"💎 {uid} ga {days} kunlik VIP berildi.")
    try:
        await bot.send_message(uid, f"🎉 Sizga {days} kunlik 💎 VIP berildi!")
    except Exception:
        pass


@adm.message(Adm.find)
async def s_find(m: Message, state: FSMContext):
    uid = await parse_id(m)
    if uid is None:
        return
    await state.clear()
    u = await get_user(uid)
    if not u:
        return await m.answer("Topilmadi.")
    info = (f"ID: <code>{uid}</code>\nUsername: @{u['username']}\nBan: {bool(u['banned'])}\n"
            f"VIP: {is_vip(u)}\nFaol: {bool(u['active'])}")
    await m.answer_photo(u["photo"], caption=profile_text(u) + "\n\n" + info,
                         reply_markup=ik([[("🚫 Ban", f"ab:{uid}"), ("✅ Unban", f"au:{uid}")]]))


@adm.callback_query(F.data.regexp(r"^(ab|au):(\d+)$"))
async def a_quick_ban(cb: CallbackQuery):
    act_, uid = cb.data.split(":")
    await ex("UPDATE users SET banned=? WHERE id=?", (1 if act_ == "ab" else 0, int(uid)))
    await cb.answer("Bajarildi", show_alert=True)


@adm.message(Adm.chan)
async def s_chan(m: Message, state: FSMContext):
    v = (m.text or "").strip()
    await state.clear()
    if v.lower() == "off":
        await set_setting("channel", "")
        return await m.answer("📌 Majburiy obuna o'chirildi.")
    if not v.startswith("@"):
        v = "@" + v
    await set_setting("channel", v)
    await m.answer(f"📌 Majburiy kanal: {v}\nBot shu kanalda admin ekanini tekshiring!")


@adm.message(Adm.viptext)
async def s_viptext(m: Message, state: FSMContext):
    await state.clear()
    await set_setting("vip_text", m.html_text)
    await m.answer("✅ VIP matni yangilandi.")


@adm.callback_query(F.data == "a:reports")
async def a_reports(cb: CallbackQuery):
    rows = await qa("SELECT * FROM reports WHERE handled=0 ORDER BY id DESC LIMIT 5")
    if not rows:
        await cb.message.answer("🚨 Yangi shikoyatlar yo'q.")
    for rp in rows:
        u = await get_user(rp["target_id"])
        txt = f"🚨 Shikoyat #{rp['id']}\nShikoyatchi: <code>{rp['from_id']}</code>\nAyblanuvchi: <code>{rp['target_id']}</code>"
        kb = ik([[("🚫 Ban", f"rb:{rp['id']}:{rp['target_id']}"), ("✅ Yopish", f"rk:{rp['id']}:0")]])
        if u and u["photo"]:
            await cb.message.answer_photo(u["photo"], caption=profile_text(u) + "\n\n" + txt, reply_markup=kb)
        else:
            await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@adm.callback_query(F.data.regexp(r"^(rb|rk):(\d+):(\d+)$"))
async def a_report_act(cb: CallbackQuery):
    act_, rid, tid = cb.data.split(":")
    await ex("UPDATE reports SET handled=1 WHERE id=?", (int(rid),))
    if act_ == "rb":
        await ex("UPDATE users SET banned=1 WHERE id=?", (int(tid),))
    await cb.answer("Bajarildi", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------- Ishga tushirish ----------
async def start_web():
    # Render Web Service port ochishni talab qiladi (health-check)
    app = web.Application()

    async def ok(request):
        return web.Response(text="OK")

    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi (.env faylini tekshiring)")
    await init_db()
    await start_web()
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_router(chat_r)
    dp.include_router(adm)
    dp.include_router(r)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
