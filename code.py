import telebot
from telebot import types
import requests
import json
import time
import os
import threading
import re
import sys
from flask import Flask, jsonify
from threading import Lock
import pymongo
import certifi
from collections import defaultdict
import signal

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
keys_env = os.getenv("GROQ_KEYS", "").strip()
GROQ_KEYS = [k.strip() for k in keys_env.split(",") if k.strip()]
gem_env = os.getenv("GEMINI_KEYS", "").strip()
GEMINI_KEYS = [k.strip() for k in gem_env.split(",") if k.strip()]
OWNER_NAME = "Yuhan"
OWNER_USERNAME = "lotus_dark"
MONITOR_ID = 7008437465
MONGO_URL = os.getenv("MONGO_URL", "").strip()

# Validate environment
if not TOKEN:
    print("Error: TELEGRAM_TOKEN is missing.")
    sys.exit(1)
if not MONGO_URL:
    print("Error: MONGO_URL is missing.")
    sys.exit(1)

# Initialize bot and app
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)
app = Flask(__name__)
lock = Lock()

# --- CONSTANTS ---
BAD_PATTERNS = r"\b(fuck|bitch|bastard|idiot|scam|porn|sex|asshole|nude|dick|cock|pussy|slut|whore|cunt|shit|madarchod|behenchod|bhenchod|bhosdike|chutiya|laude|gandu|harami|kutta|randi|mc|bc|bsdk|loda|lora)\b"
LINK_PATTERN = r"(http|https|t\.me|www\.|com|net|org|xyz|ly|link|bio)"
TIME_PATTERN = r'(\d+)\s*(m|h|d|w)'
AI_COOLDOWN_SECONDS = 3
MAX_HISTORY_LENGTH = 5
ADMIN_CACHE_DURATION = 300

# --- GLOBAL STATE ---
ai_cooldowns = defaultdict(float)
admin_cache = {}
api_key_failures = defaultdict(int)

# --- UTILS ---
class Database:
    def __init__(self):
        try:
            self.client = pymongo.MongoClient(
                MONGO_URL,
                tlsCAFile=certifi.where(),
                maxPoolSize=50,
                minPoolSize=10,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000
            )
            self.db = self.client["KaiBotDB"]
            self.groups = self.db["groups"]
            self.cache = {}
            self.client.server_info()
            print("✅ Database connected successfully")
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
            sys.exit(1)

    def get_config(self, chat_id):
        if chat_id in self.cache:
            return self.cache[chat_id]
        try:
            data = self.groups.find_one({"_id": chat_id})
            if not data:
                data = {
                    "_id": chat_id,
                    "welcome_msg": "Welcome!",
                    "ai_mode": True,
                    "antilink": False,
                    "badword": False
                }
                self.groups.insert_one(data)
            self.cache[chat_id] = data
            return data
        except Exception:
            return {"_id": chat_id, "welcome_msg": "Welcome!", "ai_mode": True, "antilink": False, "badword": False}

    def update_config(self, chat_id, key, value):
        try:
            self.groups.update_one({"_id": chat_id}, {"$set": {key: value}}, upsert=True)
            if chat_id in self.cache:
                self.cache[chat_id][key] = value
            else:
                self.get_config(chat_id)
        except Exception:
            pass

    def add_history(self, chat_id, role, content):
        try:
            self.groups.update_one(
                {"_id": chat_id},
                {"$push": {"history": {"$each": [{"role": role, "content": content}], "$slice": -MAX_HISTORY_LENGTH}}},
                upsert=True
            )
        except Exception:
            pass

    def get_history(self, chat_id):
        try:
            data = self.groups.find_one({"_id": chat_id}, {"history": 1})
            if data and "history" in data:
                return data["history"]
        except Exception:
            pass
        return []

    def cleanup_dead_groups(self, chat_id):
        try:
            self.groups.delete_one({"_id": chat_id})
            if chat_id in self.cache:
                del self.cache[chat_id]
        except Exception:
            pass

db = Database()

def safe_text(text):
    if not text:
        return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text))

def clean_json(text):
    try:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return match.group(1)
        match_raw = re.search(r"(\{.*\})", text, re.DOTALL)
        if match_raw:
            return match_raw.group(1)
        return text
    except:
        return "{}"

def sanitize_input(text):
    if not text:
        return ""
    dangerous = ['system:', 'assistant:', 'ignore previous', 'role:', 'forget all']
    clean = text
    for pattern in dangerous:
        clean = clean.replace(pattern, '').replace(pattern.upper(), '')
    return clean[:1000]

def is_admin(m):
    if m.chat.type == 'private':
        return True
    user_username = m.from_user.username.lower() if m.from_user.username else ""
    if user_username == OWNER_USERNAME.lower():
        return True
    chat_id = m.chat.id
    user_id = m.from_user.id
    if chat_id in admin_cache and user_id in admin_cache[chat_id]:
        is_adm, timestamp = admin_cache[chat_id][user_id]
        if time.time() - timestamp < ADMIN_CACHE_DURATION:
            return is_adm
    try:
        member = bot.get_chat_member(chat_id, user_id)
        is_adm = member.status in ['administrator', 'creator']
        if chat_id not in admin_cache:
            admin_cache[chat_id] = {}
        admin_cache[chat_id][user_id] = (is_adm, time.time())
        return is_adm
    except:
        return False

def resolve_target(m, text_arg=None):
    if m.reply_to_message:
        return m.reply_to_message.from_user.id
    text = str(text_arg) if text_arg is not None else (m.text or "")
    numbers = re.findall(r'\b\d{7,20}\b', text)
    if numbers:
        return int(numbers[0])
    return None

def parse_time(text):
    if not text:
        return 0
    m = re.search(TIME_PATTERN, text.lower())
    if not m:
        return 0
    try:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == 'm':
            return val * 60
        if unit == 'h':
            return val * 3600
        if unit == 'd':
            return val * 86400
        if unit == 'w':
            return val * 604800
        return 0
    except:
        return 0

def check_cooldown(user_id):
    now = time.time()
    if now - ai_cooldowns[user_id] < AI_COOLDOWN_SECONDS:
        return False
    ai_cooldowns[user_id] = now
    return True

def is_api_key_healthy(key, service):
    failure_key = f"{service}_{key}"
    return api_key_failures[failure_key] < 5

def mark_api_failure(key, service):
    failure_key = f"{service}_{key}"
    api_key_failures[failure_key] += 1

def mark_api_success(key, service):
    failure_key = f"{service}_{key}"
    api_key_failures[failure_key] = 0

def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except:
        pass

# --- CORE LOGIC ---
class Executor:
    @staticmethod
    def purge(m, count=None, from_reply=False):
        cid = m.chat.id
        msg_ids = []
        try:
            if from_reply and m.reply_to_message:
                start = m.reply_to_message.message_id
                end = m.message_id
                if start > end:
                    start, end = end, start
                msg_ids = [i for i in range(start, end + 1)]
                clean_count = len(msg_ids)
            else:
                try:
                    n = int(count) if count and str(count).isdigit() else 5
                except:
                    n = 5
                if n > 100:
                    n = 100
                msg_ids = [m.message_id - i for i in range(n + 1)]
                clean_count = n
            msg_ids = [mid for mid in msg_ids if mid > 0]
            if msg_ids:
                batches = [msg_ids[i:i + 100] for i in range(0, len(msg_ids), 100)]
                for batch in batches:
                    try:
                        bot.delete_messages(cid, batch)
                        time.sleep(0.1)
                    except:
                        pass
                tmp = bot.send_message(cid, f"🧹 *Cleaned {clean_count} messages\\!*", parse_mode="MarkdownV2")
                threading.Timer(3.0, lambda: safe_delete(cid, tmp.message_id)).start()
        except Exception:
            pass

    @staticmethod
    def punish(m, action, target_id, time_sec=0):
        cid = m.chat.id
        try:
            try:
                user_chat = bot.get_chat_member(cid, target_id)
                if action not in ['unban', 'unmute'] and user_chat.status in ['administrator', 'creator']:
                    bot.reply_to(m, "😲 *Wait\\! That's an Admin\\!* I cannot punish my seniors\\!", parse_mode="MarkdownV2")
                    return
            except:
                pass
            if action == "ban":
                bot.ban_chat_member(cid, target_id)
                bot.reply_to(m, f"🔨 *Banned\\!* ID `{target_id}` removed\\.", parse_mode="MarkdownV2")
            elif action == "kick":
                bot.ban_chat_member(cid, target_id)
                bot.unban_chat_member(cid, target_id)
                bot.reply_to(m, "👢 *Kicked\\!* Told them to leave for a bit\\.", parse_mode="MarkdownV2")
            elif action == "unban":
                bot.unban_chat_member(cid, target_id, only_if_banned=True)
                bot.reply_to(m, f"🕊️ *Unbanned\\!* ID `{target_id}` is forgiven\\!", parse_mode="MarkdownV2")
            elif action == "unmute":
                bot.restrict_chat_member(cid, target_id, permissions=types.ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
                    can_pin_messages=False, can_change_info=False))
                bot.reply_to(m, "🗣️ *Unmuted\\!* You can speak now\\!", parse_mode="MarkdownV2")
            elif action == "mute":
                perm = types.ChatPermissions(can_send_messages=False)
                if time_sec > 0:
                    bot.restrict_chat_member(cid, target_id, until_date=int(time.time() + time_sec), permissions=perm)
                    bot.reply_to(m, f"🤐 *Shhh\\!* Muted for {time_sec}s\\!", parse_mode="MarkdownV2")
                else:
                    bot.restrict_chat_member(cid, target_id, permissions=perm)
                    bot.reply_to(m, "🤐 *Muted Forever\\!*", parse_mode="MarkdownV2")
        except Exception:
            bot.reply_to(m, "I tried, but got an error\\.\\.\\. Do I have admin rights? 🥺", parse_mode="MarkdownV2")

    @staticmethod
    def pin(m, unpin=False):
        if not m.reply_to_message:
            return bot.reply_to(m, "Reply to a message first\\.", parse_mode="MarkdownV2")
        try:
            if unpin:
                bot.unpin_chat_message(m.chat.id, m.reply_to_message.message_id)
                bot.reply_to(m, "📌 *Unpinned\\!*", parse_mode="MarkdownV2")
            else:
                bot.pin_chat_message(m.chat.id, m.reply_to_message.message_id)
                bot.reply_to(m, "📌 *Pinned\\!*", parse_mode="MarkdownV2")
        except:
            bot.reply_to(m, "I can't do that\\.\\.\\. Check if I'm Admin with 'Pin Messages' permission? 🥺", parse_mode="MarkdownV2")

    @staticmethod
    def config(m, key, state):
        db.update_config(m.chat.id, key, state)
        status = "ON" if state else "OFF"
        bot.reply_to(m, f"⚙️ *{key.title()}* is now *{status}*\\.", parse_mode="MarkdownV2")

    @staticmethod
    def report(m):
        try:
            admins = bot.get_chat_administrators(m.chat.id)
            mentions = "".join([f"[{safe_text(a.user.first_name)}](tg://user?id={a.user.id}) " for a in admins if not a.user.is_bot])
            if mentions:
                txt = f"🚨 *Admin Report\\!*\n{mentions}"
                target = m.reply_to_message if m.reply_to_message else m
                bot.reply_to(target, txt, parse_mode="MarkdownV2")
            else:
                bot.reply_to(m, "I looked everywhere, but can't find any human admins\\! 😨", parse_mode="MarkdownV2")
        except:
            bot.reply_to(m, "I tried to call them, but something went wrong\\.")

def get_help_text():
    owner = safe_text(OWNER_NAME)
    return (
        f"🌟 *KAI SYSTEM MENU* 🌟\n"
        f"👤 *Owner:* {owner}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        "🧠 *AI COMMANDS \\(Natural Mode\\)*\n"
        "Just order me like a human\\! No need for slash commands\\.\n"
        "🗣 _\"Kai mute this guy for 10m\"_\n"
        "🧹 _\"Kai purge 50 messages\"_\n"
        "📌 _\"Kai pin this message\"_\n\n"
        "🛡 *MANUAL COMMANDS*\n"
        "👮 *Moderation:*\n"
        "`/ban`   `/unban`\n"
        "`/mute`  `/unmute`\n"
        "`/kick` \n\n"
        "🔧 *Tools & Config:*\n"
        "🗑 `/purge [N]`   — _Delete msgs_\n"
        "📌 `/pin`         — _Pin reply_\n"
        "🔌 `/unpin`       — _Unpin reply_\n"
        "🚨 `/report`      — _Tag Admins_\n\n"
        "⚙️ *Security Settings:*\n"
        "🛡 `/antilink` `on` / `off`\n"
        "🤬 `/badword` `on` / `off`\n"
        "🤖 `/aimode` `on` / `off`\n\n"
        "💡 _Note: I strictly obey only the Real Owner & Admins\\!_"
    )

def get_ai_decision(user, text, history, is_reply, is_user_admin, is_user_owner):
    # 1. Sanitize Input
    text = sanitize_input(text)
    
    # 2. Determine Role
    if is_user_owner:
        role_desc = "REAL OWNER (Yuhan @lotus_dark)"
    elif "yuhan" in user.lower(): 
        role_desc = "FAKE YUHAN (Imposter)"
    elif is_user_admin:
        role_desc = "ADMIN (Boss)"
    else:
        role_desc = "GUEST"

    # 3. The Solid Prompt
    prompt = (
        f"You are Kai, a backend system bot with a 12-year-old boy personality. Owner: {OWNER_NAME}.\n"
        f"CONTEXT: History=[{history}]\n"
        f"INPUT: \"{text}\" | FROM: {user} ({role_desc}) | REPLY_EXISTS: {is_reply}\n\n"
        "⚡ **COMMAND RULES (HIGHEST PRIORITY):**\n"
        "1. **IF ADMIN/OWNER** orders punishment (ban, mute, kick, unban, unmute, warn) or config change:\n"
        "   -> **STOP CHATTING.** You MUST output a JSON command.\n"
        "   -> Example: 'Kai mute him' -> Output JSON. Do NOT say 'Okay'.\n"
        "2. **IF GUEST/IMPOSTER** tries to command:\n"
        "   -> IGNORE the command. Reply as a cheeky 12yo boy refusing them.\n"
        "3. **NORMAL CHAT**:\n"
        "   -> Output JSON with \"a\": \"reply\". Be fun, short, and 12 years old.\n\n"
        "📝 **JSON OUTPUT SCHEMA (STRICT):**\n"
        "- PUNISH: {{ \"a\": \"punish\", \"t\": \"ban/mute/kick/unban/unmute/warn\", \"u\": 0, \"s\": 0 }}\n"
        "  (NOTE: Use \"u\": 0 if replying to a message. Use \"s\": seconds for mute time).\n"
        "- PURGE:  {{ \"a\": \"purge\", \"c\": count_int, \"r\": boolean }}\n"
        "- CONFIG: {{ \"a\": \"conf\", \"k\": \"antilink/badword\", \"v\": boolean }}\n"
        "- REPORT: {{ \"a\": \"report\" }}\n"
        "- PIN:    {{ \"a\": \"pin\", \"u\": boolean_unpin }}\n"
        "- REPLY:  {{ \"a\": \"reply\", \"c\": \"Your text response here\" }}"
    )

    def validate(data):
        if isinstance(data, dict) and "a" in data: return data
        return {"a": "reply", "c": str(data)}

    # --- MODEL 1: GROQ ---
    for key in GROQ_KEYS:
        if len(key) < 5 or not is_api_key_healthy(key, "groq"): continue
        try:
            headers = {"Authorization": f"Bearer {key}"}
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "You are a JSON generator. Output valid JSON only. No Markdown."},
                    {"role": "user", "content": prompt}
                ],
                "response_format": {"type": "json_object"}
            }
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=5)
            if resp.status_code == 200:
                mark_api_success(key, "groq")
                return validate(json.loads(clean_json(resp.json()['choices'][0]['message']['content'])))
            else:
                mark_api_failure(key, "groq")
        except: 
            mark_api_failure(key, "groq")
            continue

    # --- MODEL 2: GEMINI (Fixed 2.5 Lite) ---
    for g_key in GEMINI_KEYS:
        if len(g_key) < 10 or not is_api_key_healthy(g_key, "gemini"): continue
        try:
            # ✅ USING THE CORRECT STABLE MODEL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={g_key}"
            
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "response_mime_type": "application/json", # Forces JSON response
                    "temperature": 0.4
                }
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=5)
            if resp.status_code == 200:
                mark_api_success(g_key, "gemini")
                return validate(json.loads(clean_json(resp.json()['candidates'][0]['content']['parts'][0]['text'])))
            else:
                mark_api_failure(g_key, "gemini")
        except: 
            mark_api_failure(g_key, "gemini")
            continue

    # --- MODEL 3: POLLINATIONS (Fallback) ---
    pollinations_models = ["mistral", "llama", "openai", "qwen-coder"]
    for model in pollinations_models:
        try:
            payload = {
                "messages": [
                    {"role": "system", "content": "You are a backend system. Output RAW JSON ONLY."}, 
                    {"role": "user", "content": prompt}
                ],
                "model": model,
                "jsonMode": True
            }
            resp = requests.post("https://text.pollinations.ai/", json=payload, timeout=7)
            if resp.status_code == 200:
                data = json.loads(clean_json(resp.text))
                result = validate(data)
                
                # 🛑 SAFETY CHECK: Block Pollinations from Banning (Remove this block if you trust it)
                if result.get("a") == "punish":
                    return {"a": "reply", "c": "My main brain is offline! I can't punish right now. Try again in 1 minute!"}
                
                return result
        except:
            continue

    return {"a": "reply", "c": "My brain is lagging... try again! 😵‍💫"}

# --- PART 2: HANDLERS & MAIN EXECUTION ---
# (This continues from Part 1)

# --- HANDLERS ---
@bot.message_handler(commands=['gcast'])
def cmd_broadcast(m):
    if m.from_user.id != MONITOR_ID:
        bot.reply_to(m, "⛔ Access Denied")
        return
    if m.reply_to_message:
        msg_text = m.reply_to_message.text
    elif len(m.text.split()) > 1:
        msg_text = m.text.split(maxsplit=1)[1]
    else:
        bot.reply_to(m, "⚠️ **Usage:**\n1. `/gcast Your Message`\n2. Reply to a message with `/gcast`", parse_mode="Markdown")
        return
    status_msg = bot.reply_to(m, "⏳ *Starting Broadcast...*", parse_mode="Markdown")
    success_count = 0
    fail_count = 0
    cursor = db.groups.find({}, {"_id": 1})
    for doc in cursor:
        chat_id = doc['_id']
        time.sleep(0.05)
        try:
            bot.send_message(chat_id, msg_text)
            success_count += 1
        except Exception as e:
            fail_count += 1
            err = str(e).lower()
            if "forbidden" in err or "kicked" in err or "not found" in err:
                db.cleanup_dead_groups(chat_id)
    report = f"📢 **Broadcast Complete**\n✅ Sent: `{success_count}`\n❌ Failed: `{fail_count}`\n(Dead groups were automatically removed)"
    bot.edit_message_text(report, m.chat.id, status_msg.message_id, parse_mode="Markdown")

@bot.message_handler(commands=['groups'])
def cmd_list_groups(m):
    if m.from_user.id != MONITOR_ID:
        bot.reply_to(m, "⛔ Access Denied")
        return
    msg = bot.reply_to(m, "⏳ Scanning Database...")
    total_docs = db.groups.count_documents({})
    if total_docs == 0:
        bot.edit_message_text("📂 *Database is empty\\.*", m.chat.id, msg.message_id, parse_mode="MarkdownV2")
        return
    page_size = 25
    cursor = db.groups.find({}, {"_id": 1}).limit(page_size)
    lines = []
    for doc in cursor:
        chat_id = doc['_id']
        time.sleep(0.05)
        try:
            chat = bot.get_chat(chat_id)
            title = safe_text(chat.title or f"Chat {chat_id}")
            try:
                admins = bot.get_chat_administrators(chat_id)
                owner_obj = next((a for a in admins if a.status == 'creator'), None)
                owner = f"[{safe_text(owner_obj.user.first_name)}](tg://user?id={owner_obj.user.id})" if owner_obj else "Unknown"
            except:
                owner = "Hidden"
            if chat.username:
                row = f"🔗 [{title}](https://t.me/{chat.username})"
            else:
                row = f"🔐 {title}"
            lines.append(f"{row}\n    └ 👑 {owner}")
        except Exception:
            lines.append(f"⚠️ `{chat_id}` \\(inaccessible\\)")
    header = f"📊 *Total: {total_docs}* \\(showing {min(page_size, len(lines))}\\)\n{'━' * 20}\n"
    text = header + "\n".join(lines)
    if len(text) > 4000:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            bot.send_message(m.chat.id, chunk, parse_mode="MarkdownV2", disable_web_page_preview=True)
        safe_delete(m.chat.id, msg.message_id)
    else:
        bot.edit_message_text(text, m.chat.id, msg.message_id, parse_mode="MarkdownV2", disable_web_page_preview=True)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    txt = f"Hey\\! I am *Kai*\\.\nAdd me to your group and I will manage it perfectly\\! 😇"
    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton("📚 Open Notebook", callback_data="help_cmd")
    markup.add(btn)
    bot.reply_to(m, txt, parse_mode="MarkdownV2", reply_markup=markup)

@bot.message_handler(commands=['help'])
def cmd_help(m):
    bot.reply_to(m, get_help_text(), parse_mode="MarkdownV2")

@bot.callback_query_handler(func=lambda call: call.data == "help_cmd")
def callback_help(call):
    try:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, get_help_text(), parse_mode="MarkdownV2")
    except:
        pass

@bot.message_handler(commands=['purge'])
def cmd_purge(m):
    if m.chat.type != 'private' and is_admin(m):
        args = m.text.split()
        arg = args[1] if len(args) > 1 else None
        Executor.purge(m, count=arg, from_reply=bool(m.reply_to_message))

@bot.message_handler(commands=['ban', 'kick', 'mute', 'unban', 'unmute'])
def cmd_punish(m):
    if m.chat.type != 'private' and is_admin(m):
        tid = resolve_target(m)
        if not tid:
            return bot.reply_to(m, "I need a User ID or a Reply\\! 🤔", parse_mode="MarkdownV2")
        act = m.text.split()[0].replace("/", "")
        sec = parse_time(m.text)
        Executor.punish(m, act, tid, sec)

@bot.message_handler(commands=['pin', 'unpin'])
def cmd_pin(m):
    if m.chat.type != 'private' and is_admin(m):
        Executor.pin(m, unpin="unpin" in m.text)

@bot.message_handler(commands=['antilink', 'badword', 'aimode'])
def cmd_filter(m):
    if m.chat.type != 'private' and is_admin(m):
        key = None
        if "antilink" in m.text:
            key = "antilink"
        elif "badword" in m.text:
            key = "badword"
        elif "aimode" in m.text:
            key = "ai_mode"
        if key:
            curr = db.get_config(m.chat.id).get(key, False)
            state = True if "on" in m.text.lower() else (False if "off" in m.text.lower() else not curr)
            Executor.config(m, key, state)

@bot.message_handler(commands=['report'])
def cmd_report(m):
    if m.chat.type != 'private':
        Executor.report(m)

@bot.message_handler(commands=['stats'])
def cmd_stats(m):
    if m.from_user.id != MONITOR_ID:
        bot.reply_to(m, "⛔ Access Denied")
        return
    try:
        total_groups = db.groups.count_documents({})
        groq_healthy = sum(1 for k in GROQ_KEYS if is_api_key_healthy(k, "groq"))
        gemini_healthy = sum(1 for k in GEMINI_KEYS if is_api_key_healthy(k, "gemini"))
        txt = (
            f"📊 *Bot Statistics*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏘 *Total Groups:* `{total_groups}`\n"
            f"🔑 *Groq Keys:* {groq_healthy}/{len(GROQ_KEYS)} healthy\n"
            f"💎 *Gemini Keys:* {gemini_healthy}/{len(GEMINI_KEYS)} healthy\n"
            f"👥 *Active Chats:* `{len(admin_cache)}`\n"
            f"⚡ *Cooldowns Active:* `{len(ai_cooldowns)}`"
        )
        bot.reply_to(m, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['resetkeys'])
def cmd_reset_keys(m):
    if m.from_user.id != MONITOR_ID:
        bot.reply_to(m, "⛔ Access Denied")
        return
    api_key_failures.clear()
    bot.reply_to(m, "🔄 *All API key failure counts have been reset!*", parse_mode="Markdown")

@bot.message_handler(content_types=['new_chat_members'])
def on_join(m):
    if m.chat.type == 'private':
        return
    config = db.get_config(m.chat.id)
    welcome_msg = config.get('welcome_msg', 'Welcome!')
    for user in m.new_chat_members:
        if user.id == bot.get_me().id:
            continue
        try:
            safe_name = safe_text(user.first_name)
            safe_title = safe_text(m.chat.title)
            txt = f"Welcome [{safe_name}](tg://user?id={user.id})\\! 👋\nYou're now a part of *{safe_title}*\\.\nWe're glad to have you here\\. Please be respectful and courteous to all members\\."
            bot.send_message(m.chat.id, txt, parse_mode="MarkdownV2")
        except:
            pass

@bot.message_handler(func=lambda m: True)
def process(m):
    if m.chat.type == 'private' or not m.text:
        return
    if m.text.startswith("/"):
        return
    cid = m.chat.id
    uid = m.from_user.id
    txt = m.text.lower()
    user_is_admin = is_admin(m)
    user_is_owner = (m.from_user.username.lower() == OWNER_USERNAME.lower().strip('@')) if m.from_user.username else False
    config = db.get_config(cid)
    if not user_is_admin and not user_is_owner:
        if config.get('antilink', False) and re.search(LINK_PATTERN, txt):
            try:
                bot.delete_message(cid, m.message_id)
            except:
                pass
            return
        if config.get('badword', False) and re.search(BAD_PATTERNS, txt):
            try:
                bot.delete_message(cid, m.message_id)
                bot.restrict_chat_member(cid, uid, until_date=int(time.time() + 600), permissions=types.ChatPermissions(can_send_messages=False))
                bot.send_message(cid, f"🤐 *Bad Language\\!*", parse_mode="MarkdownV2")
            except:
                pass
            return
    is_rep_kai = m.reply_to_message and m.reply_to_message.from_user.id == bot.get_me().id
    is_ai_on = config.get('ai_mode', True)
    if is_ai_on and (re.search(r'\bkai\b', txt) or is_rep_kai):
        if not check_cooldown(uid):
            bot.reply_to(m, "Slow down! Wait a moment before asking again. 🐢")
            return
        bot.send_chat_action(cid, 'typing')
        try:
            history = db.get_history(cid)
            decision = get_ai_decision(m.from_user.first_name, m.text, history, bool(m.reply_to_message), user_is_admin, user_is_owner)
            act = decision.get("a", "reply")
            if act == "reply":
                ai_text = decision.get("c", "...")
                bot.reply_to(m, ai_text)
                user_msg = f"{m.from_user.first_name}: {m.text}"
                db.add_history(cid, "user", user_msg)
                db.add_history(cid, "assistant", ai_text)
            elif act == "report":
                Executor.report(m)
            elif user_is_admin or user_is_owner:
                if act == "purge":
                    is_rep = decision.get("r", False) or (m.reply_to_message and "this" in txt)
                    Executor.purge(m, count=decision.get("c", 5), from_reply=is_rep)
                elif act == "punish":
                    tid = resolve_target(m, decision.get("u"))
                    if tid:
                        Executor.punish(m, decision.get("t"), tid, int(decision.get("s", 0)))
                    else:
                        bot.reply_to(m, "Bhaiya, tell me WHO to punish\\! Reply to them? 🥺", parse_mode="MarkdownV2")
                elif act == "pin":
                    Executor.pin(m, unpin=decision.get("u", False))
                elif act == "conf":
                    Executor.config(m, decision.get("k"), decision.get("v"))
            else:
                if act != "reply":
                    bot.reply_to(m, "You are not my real Owner! 😝")
        except Exception as e:
            try:
                bot.send_message(MONITOR_ID, f"🚨 *Kai System Error:*\n`{str(e)}`", parse_mode="Markdown")
            except:
                pass

# --- FLASK ROUTES ---
@app.route('/')
def home():
    return f"Kai System Online. Owner: {OWNER_NAME}", 200

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "groups": db.groups.count_documents({}),
        "groq_keys": len(GROQ_KEYS),
        "gemini_keys": len(GEMINI_KEYS)
    }), 200

@app.route('/ping')
def ping():
    return "pong", 200

# --- GRACEFUL SHUTDOWN ---
def signal_handler(sig, frame):
    print('\n🛑 Shutting down gracefully...')
    try:
        bot.stop_polling()
    except:
        pass
    try:
        db.client.close()
    except:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- SERVER THREAD ---
def run_server():
    try:
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), use_reloader=False)
    except:
        pass

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("=" * 50)
    print("🤖 KAI BOT STARTING")
    print("=" * 50)
    print(f"✅ Token: {'*' * 10}{TOKEN[-5:]}")
    print(f"✅ Groq Keys: {len(GROQ_KEYS)}")
    print(f"✅ Gemini Keys: {len(GEMINI_KEYS)}")
    print(f"✅ Owner: {OWNER_NAME} (@{OWNER_USERNAME})")
    print(f"✅ Monitor ID: {MONITOR_ID}")
    print("=" * 50)
    
    # Start Flask server in background
    threading.Thread(target=run_server, daemon=True).start()
    print("🌐 Flask server started")
    
    # Clear webhook
    try:
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(1)
        print("🧹 Webhook cleared")
    except:
        pass
    
    # Start polling with auto-restart
    print("🚀 Starting bot polling...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=True)
        except Exception as e:
            print(f"⚠️ Polling error: {e}")
            print("🔄 Restarting in 5 seconds...")
            time.sleep(5)