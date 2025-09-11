import os, asyncio, re, logging, json
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x]
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")  # without @ or with, both ok
NEW_USER_CHANNEL_ID = int(os.getenv("NEW_USER_CHANNEL_ID", "0") or 0)
CHECK_RESULTS_CHANNEL_ID = int(os.getenv("CHECK_RESULTS_CHANNEL_ID", "0") or 0)
FREE_REG_CREDITS = int(os.getenv("FREE_REG_CREDITS", "10") or 10)

DB_PATH = os.getenv("DB_PATH", "bot.db")
BASE_CC_API = "https://hazunamadada.onrender.com/ccngate/"

class Flow(StatesGroup):
  in_commands = State()
  in_gate_ccn = State()
  in_gate_mccn = State()

processing_users: Dict[int, bool] = {}

async def open_db():
  db = await aiosqlite.connect(DB_PATH)
  await db.execute("PRAGMA journal_mode=WAL;")
  await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE, username TEXT, full_name TEXT, credits INTEGER DEFAULT 0, banned_until TEXT, joined_at TEXT, is_admin INTEGER DEFAULT 0);")
  await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);")
  await db.commit()
  return db

async def get_user(db, tg_id: int):
  c = await db.execute("SELECT tg_id, username, full_name, credits, banned_until, joined_at, is_admin FROM users WHERE tg_id=?", (tg_id,))
  r = await c.fetchone()
  if not r: return None
  return {"tg_id": r[0], "username": r[1], "full_name": r[2], "credits": r[3], "banned_until": r[4], "joined_at": r[5], "is_admin": bool(r[6])}

async def ensure_user(db, u: types.User):
  ex = await get_user(db, u.id)
  if ex: return ex
  joined = datetime.utcnow().isoformat()
  is_admin = 1 if u.id in ADMIN_USER_IDS else 0
  await db.execute("INSERT INTO users(tg_id, username, full_name, credits, banned_until, joined_at, is_admin) VALUES(?,?,?,?,?,?,?)", (u.id, u.username or "", u.full_name, FREE_REG_CREDITS, None, joined, is_admin))
  await db.commit()
  return await get_user(db, u.id)

async def add_credits(db, tg_id: int, amount: int):
  await db.execute("UPDATE users SET credits=COALESCE(credits,0)+? WHERE tg_id=?", (amount, tg_id)); await db.commit()

async def deduct_credits(db, tg_id: int, amount: int):
  await db.execute("UPDATE users SET credits=COALESCE(credits,0)-? WHERE tg_id=? AND credits>=?", (amount, tg_id, amount)); await db.commit()

async def set_ban(db, tg_id: int, until: Optional[datetime]):
  await db.execute("UPDATE users SET banned_until=? WHERE tg_id=?", (until.isoformat() if until else None, tg_id)); await db.commit()

async def is_maintenance(db) -> bool:
  c = await db.execute("SELECT value FROM settings WHERE key='maintenance'")
  r = await c.fetchone()
  return (r and r[0] == "1")

async def set_maintenance(db, on: bool):
  await db.execute("INSERT INTO settings(key,value) VALUES('maintenance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if on else "0",)); await db.commit()

# Keyboards

def kb_start():
  b=InlineKeyboardBuilder(); b.button(text="Register", callback_data="reg"); b.button(text="Commands", callback_data="commands"); b.button(text="Close", callback_data="close"); b.adjust(3); return b.as_markup()

def kb_commands():
  b=InlineKeyboardBuilder(); b.button(text="Gate", callback_data="gate"); b.button(text="Credits", callback_data="credits"); b.button(text="Close", callback_data="close"); b.adjust(3); return b.as_markup()

def kb_gate():
  b=InlineKeyboardBuilder(); b.button(text="CCN", callback_data="ccn"); b.button(text="MASS CCN", callback_data="mccn"); b.button(text="Back", callback_data="back_to_commands"); b.adjust(2,1); return b.as_markup()

def kb_back():
  b=InlineKeyboardBuilder(); b.button(text="Back", callback_data="back_to_commands"); return b.as_markup()

def kb_contact_back():
  b=InlineKeyboardBuilder();
  url = f"https://t.me/{OWNER_USERNAME.lstrip('@')}" if OWNER_USERNAME else "https://t.me/"
  b.button(text="Contact Owner", url=url); b.button(text="Back to Menu", callback_data="back_to_menu"); b.adjust(1,1); return b.as_markup()

# Helpers

def mention(user: types.User) -> str:
  nm = (user.full_name or "User").replace("<","&lt;").replace(">","&gt;")
  return f"<a href=\"tg://user?id={user.id}\">{nm}</a>"

async def fetch_json(url: str) -> any:
  async with aiohttp.ClientSession() as s:
    async with s.get(url, timeout=60) as r:
      return await r.json(content_type=None)

async def fetch_text(url: str) -> str:
  async with aiohttp.ClientSession() as s:
    async with s.get(url, timeout=30) as r:
      return await r.text()

async def bin_details(bin6: str) -> Dict[str,str]:
  try:
    html = await fetch_text(f"https://bincheck.io/details/{bin6}")
    soup = BeautifulSoup(html, "html.parser"); rows = soup.find_all("tr"); res={}
    for row in rows:
      cols=row.find_all("td");
      if len(cols)==2:
        res[cols[0].get_text(strip=True)] = cols[1].get_text(strip=True)
    return res
  except Exception:
    return {}

def format_bin_block(bin6: str, info: Dict[str,str]) -> str:
  brand = info.get("Card Brand","N/A"); ctype = info.get("Card Type","N/A"); lvl = info.get("Card Level","N/A")
  bank = info.get("Issuer Name / Bank","N/A"); country = info.get("Country","N/A")
  return ("\n━━━━━━━━━━━━━\n"
          f"🔗 BIN DETAILS\n• Bin ⌁ ({bin6})\n"
          f"• Info ⌁ {brand} - {ctype} - {lvl}\n"
          f"• Bank ⌁ {bank}\n"
          f"• Country ⌁ {country}\n"
          "━━━━━━━━━━━━━")

cc_re = re.compile(r"^/ccn\s+([0-9]{12,19}\|[0-9]{1,2}\|[0-9]{2,4}\|[0-9]{3,4})$")
mass_re = re.compile(r"^/mccn\s+(.+)$", re.S)

async def ensure_not_banned(db, user) -> Optional[str]:
  if user.get("banned_until"):
    try:
      until = datetime.fromisoformat(user["banned_until"]) if user["banned_until"] else None
      if until and until > datetime.utcnow():
        left = until - datetime.utcnow()
        return f"⛔ You are banned. Time left: {str(left).split('.')[0]}"
    except Exception:
      return "⛔ You are banned."
  return None

async def ensure_not_maintenance(db, user_id: int, is_admin: bool) -> Optional[str]:
  if is_admin: return None
  if await is_maintenance(db):
    return "🛠️ Bot is under maintenance. Please try again later."
  return None

async def start_message_text(u: types.User) -> str:
  return ("<b>LITTLE YAMRAJ | Version - 1.0</b>\n"
          "━━━━━━━━━━━━━\n"
          f"Hello, <b>{u.first_name}</b> How Can I Help You Today?!\n\n"
          f"👤 Your UserID - <code>{u.id}</code>\n"
          "🤖 BOT Status - <b>UP !!</b>\n\n"
          "🔗 Explore - Click the buttons below to discover all the features we offer!")

async def ccn_gate_info() -> str:
  return ("XOXO [AUTH GATES]\n"
          "━━━━━━━━━━━━━\n"
          "[ϟ] Name: CCN Auth\n"
          "[ϟ] Command: /ccn cc|mes|ano|cvv\n"
          "[ϟ] Status: Active ✅\n"
          "━━━━━━━━━━━━━")

async def mccn_gate_info() -> str:
  return ("XOXO [MASS GATES]\n"
          "━━━━━━━━━━━━━\n"
          "[ϟ] Name: Mass CCN\n"
          "[ϟ] Command: /mccn [ccs]\n"
          "[ϟ] Limit: Max 5 CCs\n"
          "[ϟ] Status: Active ✅\n"
          "━━━━━━━━━━━━━")

# Handlers
async def on_start(message: types.Message, state: FSMContext, db, bot: Bot):
  u = message.from_user
  mu = await ensure_user(db, u)
  ban = await ensure_not_banned(db, mu)
  maint = await ensure_not_maintenance(db, u.id, mu["is_admin"]) if not ban else None
  if ban:
    msg = await message.answer(ban)
    await asyncio.sleep(5); await bot.delete_message(message.chat.id, msg.message_id)
    return
  if maint:
    await message.answer(maint)
    return
  await state.clear()
  await message.answer(await start_message_text(u), reply_markup=kb_start(), parse_mode=ParseMode.HTML)

async def cb_close(call: types.CallbackQuery):
  try: await call.message.delete()
  except: pass
  await call.answer()

async def cb_back_menu(call: types.CallbackQuery, state: FSMContext):
  await state.clear()
  await call.message.edit_text(await start_message_text(call.from_user), reply_markup=kb_start(), parse_mode=ParseMode.HTML)
  await call.answer()

async def cb_reg(call: types.CallbackQuery, db, bot: Bot):
  u = call.from_user
  ex = await get_user(db, u.id)
  if ex:
    b=InlineKeyboardBuilder(); b.button(text="Commands", callback_data="commands"); b.button(text="Close", callback_data="close"); b.adjust(2)
    await call.message.edit_text("Already Registered ⚠️\n\nMessage: You are already registered in our bot. No need to register now.\n\nExplore My Various Commands And Abilities By Tapping on Commands Button.", reply_markup=b.as_markup())
    await call.answer()
    return
  nu = await ensure_user(db, u)
  if NEW_USER_CHANNEL_ID:
    try:
      await bot.send_message(NEW_USER_CHANNEL_ID, f"🆕 New user registered: {mention(u)} (ID: {u.id})", parse_mode=ParseMode.HTML)
    except: pass
  await call.message.edit_text(("Registration Successful\n"
    "━━━━━━━━━━━━━━\n"
    f"Name: {u.full_name}\nUser ID: {u.id}\nCredits: {nu['credits']}"), reply_markup=kb_back())
  await call.answer("Registered")

async def cb_commands(call: types.CallbackQuery, state: FSMContext):
  await state.set_state(Flow.in_commands)
  await call.message.edit_text("Choose:", reply_markup=kb_commands())
  await call.answer()

async def cb_gate(call: types.CallbackQuery, state: FSMContext):
  await call.message.edit_text("Choose Your Gate Type:", reply_markup=kb_gate())
  await call.answer()

async def cb_credits(call: types.CallbackQuery, db):
  u = await get_user(db, call.from_user.id)
  joined = u["joined_at"].split("T")[0] if u.get("joined_at") else "N/A"
  credits = "∞" if u.get("is_admin") else u.get("credits",0)
  await call.message.edit_text(f"🟢 Name : {call.from_user.full_name}\n➛ Joined : {joined}\n╰┈➤ Credits 💰 {credits}", reply_markup=kb_back(), parse_mode=ParseMode.HTML)
  await call.answer()

async def cb_ccn(call: types.CallbackQuery, state: FSMContext):
  await state.set_state(Flow.in_gate_ccn)
  await call.message.edit_text(await ccn_gate_info(), reply_markup=kb_back())
  await call.answer()

async def cb_mccn(call: types.CallbackQuery, state: FSMContext):
  await state.set_state(Flow.in_gate_mccn)
  await call.message.edit_text(await mccn_gate_info(), reply_markup=kb_back())
  await call.answer()

async def insufficient(message: types.Message):
  await message.answer("<b>Insufficient Credits!</b>\n━━━━━━━━━━━━━━━━\n😔 Oops! You're out of credits\n🎯 Required: 1 Credit minimum", reply_markup=kb_contact_back(), parse_mode=ParseMode.HTML)

async def parse_cc(cc: str) -> Optional[str]:
  parts = re.split(r"[|]", cc.strip())
  if len(parts)!=4: return None
  c, m, y, cvv = parts
  if not c.isdigit() or not m.isdigit() or not y.isdigit() or not cvv.isdigit(): return None
  if len(c) < 12: return None
  if len(y)==2: y = "20"+y
  return f"{c}|{m}|{y}|{cvv}"

async def animate_processing(bot: Bot, chat_id: int, message_id: int, base: str, stop: asyncio.Event):
  dots = [".", "..", "..."]
  i=0
  while not stop.is_set():
    try:
      await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"{base}\n🔄 Processing{dots[i%3]}")
    except: pass
    i+=1; await asyncio.sleep(0.6)

async def do_ccn(message: types.Message, state: FSMContext, db, bot: Bot):
  if await is_maintenance(db) and message.from_user.id not in ADMIN_USER_IDS:
    await message.answer("🛠️ Bot is under maintenance. Please try again later."); return
  if (await state.get_state()) != Flow.in_gate_ccn.state:
    try: await message.delete()
    except: pass
    return
  m = cc_re.match(message.text or "")
  if not m:
    try: await message.delete()
    except: pass
    return
  user = await ensure_user(db, message.from_user)
  if not user.get("is_admin") and user.get("credits",0) < 1:
    await insufficient(message); return
  if processing_users.get(message.from_user.id):
    try: await message.delete()
    except: pass
    return
  processing_users[message.from_user.id]=True
  full = await parse_cc(m.group(1))
  if not full:
    processing_users.pop(message.from_user.id, None)
    return
  cnum = full.split("|")[0]; bin6 = cnum[:6]
  info = await bin_details(bin6)
  base = f"💳 {full}"
  msg = await message.answer(base)
  stop = asyncio.Event(); task = asyncio.create_task(animate_processing(bot, message.chat.id, msg.message_id, base+format_bin_block(bin6, info), stop))
  try:
    res = await fetch_json(BASE_CC_API + full)
    # API returns list
    if isinstance(res, list) and res:
      r=res[0]; status = r.get("status",""); emsg = r.get("message","Result")
      ok = status in ("succeeded","order_id","requires_action") or (emsg == "Your card's security code is incorrect.")
      head = "✅ Approved" if ok else "❌ Declined"
      text = f"💳 {full} {head}\n╰┈➤ {emsg}" + format_bin_block(bin6, info) + f"\n🆔 Checked by: {mention(message.from_user)}"
      await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=text, parse_mode=ParseMode.HTML)
      if CHECK_RESULTS_CHANNEL_ID:
        try: await bot.send_message(CHECK_RESULTS_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        except: pass
      if not user.get("is_admin"):
        await deduct_credits(db, message.from_user.id, 1)
  finally:
    stop.set(); await asyncio.sleep(0.1)
    processing_users.pop(message.from_user.id, None)
    # show gate card again
    await message.answer(await ccn_gate_info(), reply_markup=kb_back())

async def do_mccn(message: types.Message, state: FSMContext, db, bot: Bot):
  if await is_maintenance(db) and message.from_user.id not in ADMIN_USER_IDS:
    await message.answer("🛠️ Bot is under maintenance. Please try again later."); return
  if (await state.get_state()) != Flow.in_gate_mccn.state:
    try: await message.delete()
    except: pass
    return
  m = mass_re.match(message.text or "")
  if not m:
    try: await message.delete()
    except: pass
    return
  user = await ensure_user(db, message.from_user)
  credits = 9999 if user.get("is_admin") else user.get("credits",0)
  raw = m.group(1).replace("\n"," ").split()
  cards=[]
  for s in raw:
    p = await parse_cc(s)
    if p: cards.append(p)
    if len(cards)>=5: break
  if not cards:
    try: await message.delete()
    except: pass
    return
  can = min(len(cards), credits)
  if can==0:
    await insufficient(message); return
  cards = cards[:can]
  # bins
  uniq_bins = {}
  for c in cards:
    b = c.split('|')[0][:6]
    if b not in uniq_bins:
      uniq_bins[b] = await bin_details(b)
  # prepare message
  base = "\n".join([f"💳 {c}" for c in cards])
  # append bins blocks sorted by order of first appearance
  for b,info in uniq_bins.items():
    base += format_bin_block(b, info)
  msg = await message.answer(base)
  stop = asyncio.Event(); task = asyncio.create_task(animate_processing(bot, message.chat.id, msg.message_id, base, stop))
  try:
    # call API per card to get messages, so credit deduct per card
    out_lines=[]
    for c in cards:
      res = await fetch_json(BASE_CC_API + c)
      if isinstance(res, list) and res:
        r=res[0]; emsg = r.get("message","Result"); status=r.get("status","")
        ok = status in ("succeeded","order_id","requires_action") or (emsg == "Your card's security code is incorrect.")
        head = "✅ Approved" if ok else "❌ Declined"
        out_lines.append(f"{c} {head} | {emsg}")
        if not user.get("is_admin"): await deduct_credits(db, message.from_user.id, 1)
    final = ("\n".join(out_lines))
    final += f"\n🆔 Checked by: {mention(message.from_user)}"
    await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=final, parse_mode=ParseMode.HTML)
    if CHECK_RESULTS_CHANNEL_ID:
      try: await bot.send_message(CHECK_RESULTS_CHANNEL_ID, final, parse_mode=ParseMode.HTML)
      except: pass
  finally:
    stop.set(); await asyncio.sleep(0.1)
    await message.answer(await mccn_gate_info(), reply_markup=kb_back())

# Delete stray messages in gate states
async def delete_other(message: types.Message):
  try: await message.delete()
  except: pass

# Admin commands
async def admin_only(message: types.Message) -> bool:
  return message.from_user.id in ADMIN_USER_IDS

async def cmd_add_credits(message: types.Message, db):
  if not await admin_only(message): return
  try:
    _, uid, amt = message.text.split()
    await add_credits(db, int(uid), int(amt))
    txt = f"Credits Added ✅\n━━━━━━━━━━━━━\nUser: <a href=\"tg://user?id={uid}\">{uid}</a>\nCredits Added: {amt}\nDate: {datetime.utcnow().date()}"
    await message.answer(txt, parse_mode=ParseMode.HTML)
  except Exception as e:
    await message.answer("Usage: /addusercredits <user_id> <amount>")

async def cmd_deduct_credits(message: types.Message, db):
  if not await admin_only(message): return
  try:
    _, uid, amt = message.text.split()
    await deduct_credits(db, int(uid), int(amt))
    txt = f"Credits Deducted ✅\n━━━━━━━━━━━━━\nUser: <a href=\"tg://user?id={uid}\">{uid}</a>\nCredits Deducted: {amt}\nDate: {datetime.utcnow().date()}"
    await message.answer(txt, parse_mode=ParseMode.HTML)
  except: await message.answer("Usage: /deductusercredit <user_id> <amount>")

async def cmd_ban(message: types.Message, db):
  if not await admin_only(message): return
  try:
    _, uid, duration = message.text.split()
    until=None
    if duration.endswith("h"): until=datetime.utcnow()+timedelta(hours=int(duration[:-1]))
    elif duration.endswith("d") or duration.endswith("day"): until=datetime.utcnow()+timedelta(days=int(re.sub(r"\D","",duration) or 1))
    else: until = datetime.max
    await set_ban(db, int(uid), until)
    await message.answer(f"User {uid} banned until {until if until!=datetime.max else '∞'}")
  except: await message.answer("Usage: /banuseraccess <user_id> <1h|1d|2day|unlimited>")

async def cmd_unban(message: types.Message, db):
  if not await admin_only(message): return
  try:
    _, uid = message.text.split()
    await set_ban(db, int(uid), None)
    url=f"tg://user?id={uid}"
    await message.answer(f"<a href=\"{url}\">User</a> unbanned.", parse_mode=ParseMode.HTML)
  except: await message.answer("Usage: /unbanuseraccess <user_id>")

async def cmd_show_users(message: types.Message, db):
  if not await admin_only(message): return
  c=await db.execute("SELECT tg_id, username, credits, joined_at FROM users ORDER BY joined_at DESC LIMIT 200")
  rows=await c.fetchall()
  lines=[f"{r[0]} | @{r[1] or '-'} | {r[2]} | {r[3][:10] if r[3] else ''}" for r in rows]
  await message.answer("Users (id|username|credits|joined):\n"+"\n".join(lines)[:4000])

async def cmd_freeze(message: types.Message, db):
  if not await admin_only(message): return
  await set_maintenance(db, True); await message.answer("🛠️ Bot usage frozen for maintenance.")

async def cmd_unfreeze(message: types.Message, db):
  if not await admin_only(message): return
  await set_maintenance(db, False); await message.answer("✅ Bot is live again.")

async def cmd_broadcast(message: types.Message, db, bot: Bot):
  if not await admin_only(message): return
  try:
    content = message.text.split(" ",1)[1]
  except: await message.answer("Usage: /broadcastmessage <text> (use \\n for new lines)"); return
  content = content.replace("\\n","\n")
  c=await db.execute("SELECT tg_id FROM users"); rows=await c.fetchall()
  sent=0
  for (uid,) in rows:
    try: await bot.send_message(uid, content)
    except: pass
    sent+=1; await asyncio.sleep(0.03)
  await message.answer(f"Broadcast sent to {sent} users")

# App wiring
async def main():
  if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")

  # ✅ use DefaultBotProperties for parse_mode
  from aiogram.client.default import DefaultBotProperties
  bot = Bot(
      BOT_TOKEN,
      default=DefaultBotProperties(parse_mode=ParseMode.HTML)
  )

  dp = Dispatcher()
  db = await open_db()

  # ✅ aiogram injects FSMContext automatically, no need for s.fsm
  dp.message.register(lambda m, d=db, b=bot: on_start(m, FSMContext(), d, b), CommandStart())
  dp.callback_query.register(cb_close, F.data == "close")
  dp.callback_query.register(cb_back_menu, F.data == "back_to_menu")
  dp.callback_query.register(lambda c, d=db, b=bot: cb_reg(c, d, b), F.data == "reg")
  dp.callback_query.register(cb_commands, F.data == "commands")
  dp.callback_query.register(cb_gate, F.data == "gate")
  dp.callback_query.register(lambda c, d=db: cb_credits(c, d), F.data == "credits")
  dp.callback_query.register(cb_ccn, F.data == "ccn")
  dp.callback_query.register(cb_mccn, F.data == "mccn")
  dp.callback_query.register(cb_commands, F.data == "back_to_commands")

  dp.message.register(lambda m, d=db, b=bot: do_ccn(m, d, b), Flow.in_gate_ccn, F.text.startswith("/ccn"))
  dp.message.register(lambda m, d=db, b=bot: do_mccn(m, d, b), Flow.in_gate_mccn, F.text.startswith("/mccn"))
  dp.message.register(delete_other, Flow.in_gate_ccn)
  dp.message.register(delete_other, Flow.in_gate_mccn)

  dp.message.register(lambda m, d=db: cmd_add_credits(m, d), F.text.startswith("/addusercredits"))
  dp.message.register(lambda m, d=db: cmd_deduct_credits(m, d), F.text.startswith("/deductusercredit"))
  dp.message.register(lambda m, d=db: cmd_ban(m, d), F.text.startswith("/banuseraccess"))
  dp.message.register(lambda m, d=db: cmd_unban(m, d), F.text.startswith("/unbanuseraccess"))
  dp.message.register(lambda m, d=db: cmd_show_users(m, d), F.text.startswith("/showuserlist"))
  dp.message.register(lambda m, d=db: cmd_freeze(m, d), F.text.startswith("/freezebotusage"))
  dp.message.register(lambda m, d=db: cmd_unfreeze(m, d), F.text.startswith("/unfreezebotusage"))
  dp.message.register(lambda m, d=db, b=bot: cmd_broadcast(m, d, b), F.text.startswith("/broadcastmessage"))

  await dp.start_polling(bot)

