# -*- coding: utf-8 -*-
"""
Telegram Manager Bot — GG gateway style, wired to manager.py.

Admin-only. Two jobs:
  1. Customer keys (exactly like the GG file): Create Key / Revoke Key /
     All Keys / Remain Status.
  2. Main API Manager: see every slot (account file), select which ones
     work in the main API (attach on/off), set each slot's limit,
     Next API manual transfer, reset usage.

Config (BOT_TOKEN / ADMIN_ID) is carried over from the GG file.
Set DISABLE_TELEGRAM_BOT=1 to keep the bot off.
"""

import io
import json
import os
import threading

import telebot
from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

BOT_TOKEN = "8954451439:AAF4sPrktTWMl0F84qPCYUKtO6dCGgQnETw"
ADMIN_ID = 7010003376  

_started = False
_start_lock = threading.Lock()
_bot_ref = None


def notify_admin(text):
    """Send an alert to the admin chat. Used by the gateway (slot
    exhausted / all APIs over / key limit). No-op when bot is off."""
    bot = _bot_ref
    if bot is None:
        return False
    try:
        bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        return True
    except Exception:
        return False


def start_bot(manager, discovered_fn, log=print, callbacks=None):
    global _started
    if os.environ.get("DISABLE_TELEGRAM_BOT") == "1":
        log("[TG] telegram bot disabled by DISABLE_TELEGRAM_BOT=1")
        return False
    with _start_lock:
        if _started:
            return True
        _started = True
    if not BOT_TOKEN or BOT_TOKEN.startswith("__"):
        log("[TG] BOT_TOKEN not configured — telegram bot disabled")
        return False

    bot = telebot.TeleBot(BOT_TOKEN)
    global _bot_ref
    _bot_ref = bot
    callbacks = callbacks or {}

    main_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    main_keyboard.add(
        KeyboardButton("➕ Create Key"),
        KeyboardButton("🗑️ Revoke Key"),
        KeyboardButton("📋 All Keys"),
        KeyboardButton("📊 Remain Status"),
        KeyboardButton("🎛️ API Manager"),
        KeyboardButton("⏭️ Next API"),
        KeyboardButton("📈 Daily Report"),
        KeyboardButton("💾 Backup"),
    )

    def admin(message):
        return message.chat.id == ADMIN_ID

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------
    @bot.message_handler(commands=["start", "help"])
    def cmd_start(message):
        if not admin(message):
            return bot.reply_to(message, "⛔ <b>Unauthorized Access.</b>", parse_mode="HTML")
        bot.send_message(
            message.chat.id,
            "💠 <b>FuHx Like API Manager</b> 💠\n\n"
            "⚡ <i>Main API gateway + Telegram controller.</i>\n"
            "👇 Choose an action from the menu below:",
            reply_markup=main_keyboard,
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # 1. Create Key flow (GG style)
    # ------------------------------------------------------------------
    @bot.message_handler(func=lambda m: m.text == "➕ Create Key")
    def create_ask_name(message):
        if not admin(message):
            return
        msg = bot.send_message(message.chat.id, "✍️ <b>Enter a Key Name or Prefix:</b>\n<i>(e.g., VIP_User1)</i>", parse_mode="HTML")
        bot.register_next_step_handler(msg, step_type)

    def step_type(message):
        key_name = message.text.strip()
        msg = bot.send_message(
            message.chat.id,
            "⚙️ <b>Select Key Type:</b>\n\n"
            "🔑 <code>r1</code> = Everyday reset remain (Daily Quota at 4 AM)\n"
            "💎 <code>r2</code> = No reset date, limited remain only",
            parse_mode="HTML",
        )
        bot.register_next_step_handler(msg, step_limit, key_name)

    def step_limit(message, key_name):
        k_type = message.text.strip().lower()
        if k_type not in ["r1", "r2"]:
            return bot.send_message(message.chat.id, "❌ <b>Invalid type.</b> Please tap ➕ Create Key again.", parse_mode="HTML")
        msg = bot.send_message(message.chat.id, "🔢 <b>Enter Key Limit:</b>\n<i>(How many requests can this key send?)</i>", parse_mode="HTML")
        bot.register_next_step_handler(msg, step_duration, key_name, k_type)

    def step_duration(message, key_name, k_type):
        try:
            limit = int(message.text.strip())
        except Exception:
            return bot.send_message(message.chat.id, "❌ <b>Invalid number.</b> Creation canceled.", parse_mode="HTML")
        if k_type == "r1":
            msg = bot.send_message(
                message.chat.id,
                "⏳ <b>How long will this key work?</b>\n"
                "<i>Options:</i> <code>1h</code>, <code>1d</code>, <code>1w</code>, <code>1m</code>, <code>1y</code>, or <code>no</code> (Lifetime)",
                parse_mode="HTML",
            )
            bot.register_next_step_handler(msg, finalize_key, key_name, k_type, limit)
        else:
            finalize_key_logic(message, key_name, k_type, limit, "no")

    def finalize_key(message, key_name, k_type, limit):
        duration = message.text.strip().lower()
        finalize_key_logic(message, key_name, k_type, limit, duration)

    def finalize_key_logic(message, key_name, k_type, limit, duration):
        info, err = manager.create_key(key_name, k_type, limit, duration)
        if err == "exists":
            return bot.send_message(message.chat.id, "❌ <b>এই নামে key আগেই আছে।</b> আগে Revoke করো, অথবা অন্য নাম দাও।", parse_mode="HTML")
        if err == "reserved":
            return bot.send_message(message.chat.id, "❌ <b>এই নামটা reserved (master key)।</b> অন্য নাম দাও।", parse_mode="HTML")
        if err:
            return bot.send_message(message.chat.id, f"❌ <b>Invalid duration format.</b> Canceled.", parse_mode="HTML")
        bot.send_message(
            message.chat.id,
            f"✅ <b>Key Successfully Created!</b>\n\n"
            f"🔑 <b>Key:</b> <code>{key_name}</code>\n"
            f"🔹 <b>Type:</b> {k_type.upper()}\n"
            f"📊 <b>Limit:</b> {limit}\n"
            f"⏳ <b>Expires:</b> {info.get('expiry') if info.get('expiry') else 'Never (Lifetime)'}",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # 2. Revoke Key
    # ------------------------------------------------------------------
    @bot.message_handler(func=lambda m: m.text == "🗑️ Revoke Key")
    def revoke_ask(message):
        if not admin(message):
            return
        msg = bot.send_message(message.chat.id, "🗑️ <b>Enter the exact Key string to revoke:</b>", parse_mode="HTML")
        bot.register_next_step_handler(msg, revoke_process)

    def revoke_process(message):
        key_name = message.text.strip()
        if manager.revoke_key(key_name):
            bot.send_message(message.chat.id, f"✅ Key <code>{key_name}</code> successfully wiped!", parse_mode="HTML")
        else:
            bot.send_message(message.chat.id, "❌ <b>Key not found.</b>", parse_mode="HTML")

    # ------------------------------------------------------------------
    # 3. All Keys
    # ------------------------------------------------------------------
    @bot.message_handler(func=lambda m: m.text == "📋 All Keys")
    def list_keys(message):
        if not admin(message):
            return
        keys = manager.list_keys()
        if not keys:
            return bot.send_message(message.chat.id, "⚠️ <b>No active keys found.</b>", parse_mode="HTML")
        text = "📋 <b>ALL ACTIVE KEYS:</b>\n\n"
        for k, v in keys.items():
            exp = v.get("expiry") or "Lifetime"
            text += f"🔑 <code>{k}</code>\n🔹 Type: {str(v.get('type', '')).upper()} | 📊 {v.get('used', 0)}/{v.get('limit', 0)} | ⏳ {exp}\n\n"
        if len(text) > 4000:
            text = text[:4000] + "\n...[Truncated]"
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    # ------------------------------------------------------------------
    # 4. Remain Status
    # ------------------------------------------------------------------
    @bot.message_handler(func=lambda m: m.text == "📊 Remain Status")
    def global_status(message):
        if not admin(message):
            return
        st = manager.global_status(discovered_fn())
        cur = st["current"]
        text = (
            f"📊 <b>GLOBAL SYSTEM STATUS:</b>\n\n"
            f"🌐 <b>Total Attached Limit:</b> {st['total_limit']}\n"
            f"📉 <b>Global Used:</b> {st['global_used']}\n"
            f"✅ <b>Total Remain (attached):</b> {st['remain']}\n"
            f"🔄 <b>Rotation Pool:</b> {', '.join('/like' + str(x) for x in st.get('pool', [])) if st.get('pool') else '— none available —'}\n"
            f"💾 <b>State:</b> {st['backend']}\n\n"
            f"🔄 <b>Next Reset:</b> 04:00 AM (BD Time)"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    # ------------------------------------------------------------------
    # 5. API Manager (attach select / limit / next / reset)
    # ------------------------------------------------------------------
    def manager_text_kb():
        disc = discovered_fn()
        st = manager.global_status(disc)
        cur = st["current"]
        pool = st.get("pool", [])
        lines = [
            "🎛️ <b>API MANAGER — Main API Slots</b>\n",
            f"🔄 <b>Rotation pool:</b> {', '.join('/like' + str(x) for x in pool) if pool else '— none —'}",
            f"📊 <b>Attached remain:</b> {st['remain']}/{st['total_limit']}\n",
            "<i>✅ = এই API main-এ কাজ করবে • ❌ = main-এ বন্ধ (direct /likeN চলবে)</i>\n",
        ]
        kb = InlineKeyboardMarkup(row_width=2)
        for s in st["slots"]:
            mark = "🔄 " if s.get("in_pool") else ""
            label = f"{mark}/like{s['n']} — {s['used']}/{s['limit']}"
            kb.row(
                InlineKeyboardButton(label, callback_data="noop"),
                InlineKeyboardButton("✅ ON" if s["attached"] else "❌ OFF", callback_data=f"tgl:{s['n']}"),
            )
            kb.row(
                InlineKeyboardButton("⬆️ আগে", callback_data=f"mv:-1:{s['n']}"),
                InlineKeyboardButton("⬇️ পরে", callback_data=f"mv:1:{s['n']}"),
                InlineKeyboardButton("🧪 Test", callback_data=f"tst:{s['n']}"),
            )
        kb.add(
            InlineKeyboardButton("⏭️ Next API", callback_data="next"),
            InlineKeyboardButton("🔢 Set Limit", callback_data="limitmenu"),
        )
        kb.row(
            InlineKeyboardButton(f"🔄 Pool size: {manager.settings().get('pool_size', 3)} (tap = বদলাও)", callback_data="poolcycle"),
            InlineKeyboardButton("🔄 Reset All Usage", callback_data="resetall"),
        )
        return "\n".join(lines), kb

    @bot.message_handler(func=lambda m: m.text == "🎛️ API Manager")
    def api_manager_menu(message):
        if not admin(message):
            return
        text, kb = manager_text_kb()
        bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="HTML")

    @bot.message_handler(func=lambda m: m.text == "⏭️ Next API")
    def next_api_btn(message):
        if not admin(message):
            return
        cur, changed = manager.next_api(discovered_fn())
        if cur is None:
            bot.send_message(message.chat.id, "⚠️ <b>No attached API with remain available.</b>", parse_mode="HTML")
        elif changed:
            bot.send_message(message.chat.id, f"⏭️ <b>সামনের API পেছনে পাঠানো হয়েছে — pool-এর সামনে এখন /like{cur}</b>", parse_mode="HTML")
        else:
            bot.send_message(message.chat.id, f"▶️ <b>Pool-এ একটাই API আছে: /like{cur}</b> — সরানোর কিছু নাই।", parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: True)
    def on_callback(call):
        if call.message.chat.id != ADMIN_ID:
            return bot.answer_callback_query(call.id, "⛔ Unauthorized")
        data = call.data or ""
        try:
            if data == "noop":
                return bot.answer_callback_query(call.id)
            if data.startswith("tgl:"):
                n = int(data.split(":")[1])
                view = {s["n"]: s for s in manager.slot_view(discovered_fn())}
                now = view.get(n, {}).get("attached", True)
                manager.set_attached(n, not now)
                bot.answer_callback_query(call.id, f"/like{n} main API: {'ON ✅' if not now else 'OFF ❌'}")
            elif data == "next":
                cur, changed = manager.next_api(discovered_fn())
                if cur is None:
                    return bot.answer_callback_query(call.id, "No API available", show_alert=True)
                bot.answer_callback_query(call.id, f"Pool-এর সামনে এখন /like{cur}" if changed else f"Pool-এ একটাই API: /like{cur}")
            elif data.startswith("mv:"):
                _p = data.split(":")
                manager.move_slot(int(_p[2]), int(_p[1]), discovered_fn())
                bot.answer_callback_query(call.id, "Order বদলানো হয়েছে ✅")
            elif data.startswith("tst:"):
                n = int(data.split(":")[1])
                test_fn = callbacks.get("test_slot")
                if test_fn is None:
                    return bot.answer_callback_query(call.id, "Test available না", show_alert=True)
                bot.answer_callback_query(call.id, f"/like{n} test চলছে…")
                def _run_test(n=n):
                    try:
                        r = test_fn(n)
                        verdict = "✅ কাজ করছে" if r.get("ok") else "❌ token আসছে না — account/JWT check করো"
                        bot.send_message(
                            call.message.chat.id,
                            f"🧪 <b>/like{n} TEST RESULT</b>\n\n"
                            f"👥 Accounts: {r.get('accounts')} • 🎟️ Cached tokens: {r.get('cached_tokens')}\n"
                            f"🔬 Sample JWT ({r.get('sample_size')}): ✅ {r.get('sample_ok')} ok / ❌ {r.get('sample_failed')} fail\n"
                            f"🏁 Verdict: {verdict}",
                            parse_mode="HTML",
                        )
                    except Exception as _e:
                        bot.send_message(call.message.chat.id, f"🧪 /like{n} test error: {_e}")
                threading.Thread(target=_run_test, daemon=True).start()
                return
            elif data == "poolcycle":
                now = int(manager.settings().get("pool_size", 3))
                new = now % 5 + 1
                manager.update_settings(pool_size=new)
                bot.answer_callback_query(call.id, f"Rotation pool size → {new}")
            elif data == "resetall":
                manager.reset_all_usage()
                bot.answer_callback_query(call.id, "All API usage reset ✅")
            elif data == "limitmenu":
                kb = InlineKeyboardMarkup(row_width=2)
                for s in manager.slot_view(discovered_fn()):
                    kb.add(InlineKeyboardButton(f"/like{s['n']} (limit {s['limit']})", callback_data=f"lim:{s['n']}"))
                kb.add(InlineKeyboardButton("⬅️ Back", callback_data="back"))
                return bot.edit_message_text(
                    "🔢 <b>কোন API-র limit set করবা?</b>",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=kb, parse_mode="HTML",
                )
            elif data.startswith("lim:"):
                n = int(data.split(":")[1])
                bot.answer_callback_query(call.id)
                msg = bot.send_message(call.message.chat.id, f"🔢 <b>/like{n}-এর নতুন limit লিখো:</b>\n<i>(শুধু number, e.g. 30)</i>", parse_mode="HTML")
                bot.register_next_step_handler(msg, step_set_limit, n)
                return
            elif data == "back":
                pass
            text, kb = manager_text_kb()
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            try:
                bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
            except Exception:
                pass

    def step_set_limit(message, n):
        if not admin(message):
            return
        try:
            limit = int(message.text.strip())
            if limit < 1:
                raise ValueError
        except Exception:
            return bot.send_message(message.chat.id, "❌ <b>Invalid number.</b> Set Limit আবার try করো।", parse_mode="HTML")
        manager.set_limit(n, limit)
        bot.send_message(message.chat.id, f"✅ <b>/like{n} limit set → {limit}</b>", parse_mode="HTML")

    # ------------------------------------------------------------------
    # 6. Daily Report
    # ------------------------------------------------------------------
    @bot.message_handler(func=lambda m: m.text == "📈 Daily Report")
    def daily_report(message):
        if not admin(message):
            return
        rep = manager.daily_report(discovered_fn())
        cur = rep["current"]
        lines = [
            f"📈 <b>DAILY REPORT — {rep['date']}</b> <i>(04:00 BD reset)</i>\n",
            f"🔥 <b>আজকের successful like (main API):</b> {rep['global_used']}",
            f"▶️ <b>Current:</b> {('/like' + str(cur)) if cur is not None else '— none —'} • <b>Remain:</b> {rep['remain']}/{rep['total_limit']}\n",
            "<b>Slots:</b>",
        ]
        for s in rep["slots"]:
            lines.append(f"{'▶️' if s['current'] else '•'} /like{s['n']} — {s['used']}/{s['limit']} {'✅' if s['attached'] else '❌ detached'}")
        if rep["keys"]:
            lines.append("\n<b>Top keys:</b>")
            for k in rep["keys"][:10]:
                lines.append(f"🔑 <code>{k['name']}</code> — {k['used']}/{k['limit']} (last: {k['last_used']})")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

    # ------------------------------------------------------------------
    # 7. Backup (state file) + Restore (send the file back to the bot)
    # ------------------------------------------------------------------
    @bot.message_handler(func=lambda m: m.text == "💾 Backup")
    def backup_state(message):
        if not admin(message):
            return
        payload = json.dumps(manager.export_state(), ensure_ascii=False, indent=1).encode("utf-8")
        bio = io.BytesIO(payload)
        bio.name = "manager_state_backup.json"
        bot.send_document(
            message.chat.id, bio,
            caption="💾 <b>Manager state backup</b>\nKeys, limits, attach, order — সব আছে। Restore করতে এই file-টাই bot-এ পাঠিয়ে দাও।",
            parse_mode="HTML",
        )

    @bot.message_handler(content_types=["document"])
    def restore_state(message):
        if not admin(message):
            return
        try:
            fi = bot.get_file(message.document.file_id)
            raw = bot.download_file(fi.file_path)
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception as e:
            return bot.reply_to(message, f"❌ Backup file পড়া গেল না: {e}")
        ok, err = manager.import_state(data)
        if ok:
            bot.reply_to(message, "✅ <b>State restore হয়েছে!</b>\nKeys, limits, attach, order — backup অনুযায়ী হয়ে গেছে।", parse_mode="HTML")
        else:
            bot.reply_to(message, f"❌ Restore হয়নি: {err}")

    # ------------------------------------------------------------------
    def _poll():
        try:
            log("[TG] Telegram bot polling started")
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            log(f"[TG] telegram polling stopped: {e}")

    threading.Thread(target=_poll, daemon=True).start()
    return True
