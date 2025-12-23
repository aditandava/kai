import telebot
from telebot import types
import requests
import json
import time
import os
import threading
import re
import sys
from flask import Flask
from threading import Lock
import pymongo
import certifi


# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_TOKEN").strip() 
keys_env = os.getenv("GROQ_KEYS", "").strip()
GROQ_KEYS = keys_env.split(",") if keys_env else []
gem_env = os.getenv("GEMINI_KEYS", "").strip() 
GEMINI_KEYS = gem_env.split(",") if gem_env else []
OWNER_NAME = "Yuhan"
OWNER_USERNAME = "lotus_dark"
MONITOR_ID = 7008437465
MONGO_URL = os.getenv("MONGO_URL").strip()

if not TOKEN:
    print("Error: TELEGRAM_TOKEN is missing.")
    sys.exit(1)
if not MONGO_URL:
    print("Error: MONGO_URL is missing.")
    sys.exit(1)
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)
lock = Lock()
# --- STORAGE ---
chat_config = {}
chat_history = {}

# --- CONSTANTS ---
BAD_PATTERNS = r"\b(fuck|bitch|bastard|idiot|scam|porn|sex|asshole|nude|dick|cock|pussy|slut|whore|cunt|shit|madarchod|behenchod|bhenchod|bhosdike|chutiya|laude|gandu|harami|kutta|randi|mc|bc|bsdk|loda|lora)\b"
LINK_PATTERN = r"(http|https|t\.me|www\.|com|net|org|xyz|ly|link|bio)"
TIME_PATTERN = r'(\d+)\s*(m|h|d|w)'

# --- UTILS ---
class Database:
    def __init__(self):
        self.client = pymongo.MongoClient(MONGO_URL, tlsCAFile=certifi.where())
        self.db = self.client["KaiBotDB"]
        self.groups = self.db["groups"]
        self.cache = {} 

    # --- PART 1: CONFIG (Cached for Speed) ---
    def get_config(self, chat_id):
        if chat_id in self.cache: return self.cache[chat_id]
        data = self.groups.find_one({"_id": chat_id})
        if not data:
            data = {"_id": chat_id, "welcome_msg": "Welcome!", "ai_mode": True}
            self.groups.insert_one(data)
        self.cache[chat_id] = data
        return data

    def update_config(self, chat_id, key, value):
        self.groups.update_one({"_id": chat_id}, {"$set": {key: value}}, upsert=True)
        if chat_id in self.cache: self.cache[chat_id][key] = value
        else: self.get_config(chat_id)

    # --- PART 2: HISTORY (Direct DB - No Cache needed) ---
    def add_history(self, chat_id, role, content):
        self.groups.update_one(
            {"_id": chat_id},
            {
                "$push": {
                    "history": {
                        "$each": [{"role": role, "content": content}],
                        "$slice": -5 # ✂️ Keeps only the last 5 messages!
                    }
                }
            },
            upsert=True
        )

    def get_history(self, chat_id):
        """Fetches the last 5 messages for Groq"""
        data = self.groups.find_one({"_id": chat_id}, {"history": 1})
        if data and "history" in data:
            return data["history"]
        return []

# Initialize
db = Database()

def safe_text(text):
    if not text: return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))

def clean_json(text):
    try:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match: return match.group(1)
        match_raw = re.search(r"(\{.*\})", text, re.DOTALL)
        if match_raw: return match_raw.group(1)
        return text
    except: return "{}"

def is_admin(m):
    if m.chat.type == 'private': return True
    user_username = m.from_user.username.lower() if m.from_user.username else ""
    if user_username == OWNER_USERNAME.lower():
        return True
    try:
        return bot.get_chat_member(m.chat.id, m.from_user.id).status in ['administrator', 'creator']
    except: 
        return False


def get_settings(cid):
    with lock:
        if cid not in chat_config:
            chat_config[cid] = {"antilink": False, "badword": False}
        return chat_config[cid]

def resolve_target(m, text_arg=None):
    if m.reply_to_message:
        return m.reply_to_message.from_user.id
    text = str(text_arg) if text_arg is not None else (m.text or "")
    numbers = re.findall(r'\b\d{7,20}\b', text)
    if numbers: return int(numbers[0])
    return None

def parse_time(text):
    if not text: return 0
    m = re.search(TIME_PATTERN, text.lower())
    if not m: return 0
    try:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == 'm': return val * 60
        if unit == 'h': return val * 3600
        if unit == 'd': return val * 86400
        if unit == 'w': return val * 604800
        return 0
    except: return 0

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
                if start > end: start, end = end, start
                msg_ids = [i for i in range(start, end + 1)]
                clean_count = len(msg_ids)
            else:
                try: n = int(count) if count and str(count).isdigit() else 5
                except: n = 5
                if n > 100: n = 100
                msg_ids = [m.message_id - i for i in range(n + 1)]
                clean_count = n
            msg_ids = [mid for mid in msg_ids if mid > 0]
            if msg_ids:
                batches = [msg_ids[i:i + 100] for i in range(0, len(msg_ids), 100)]
                for batch in batches:
                    try:
                        bot.delete_messages(cid, batch)
                        time.sleep(0.1)
                    except: pass
                # FIX: Double backslash for MarkdownV2 escape
                tmp = bot.send_message(cid, f"🧹 *Cleaned {clean_count} messages\\!*", parse_mode="MarkdownV2")
                threading.Timer(3.0, lambda: bot.delete_message(cid, tmp.message_id)).start()
        except Exception: pass

    @staticmethod
    def punish(m, action, target_id, time_sec=0):
        cid = m.chat.id
        try:
            try:
                user_chat = bot.get_chat_member(cid, target_id)
                # Check if target is Admin
                if action not in ['unban', 'unmute'] and user_chat.status in ['administrator', 'creator']:
                    # FIX: Double backslash for MarkdownV2 escape
                    bot.reply_to(m, "😲 *Wait\\! That's an Admin\\!* I cannot punish my seniors\\!", parse_mode="MarkdownV2")
                    return
            except: pass 
            
            if action == "ban":
                bot.ban_chat_member(cid, target_id)
                # FIX: Double backslash for MarkdownV2 escape
                bot.reply_to(m, f"🔨 *Banned\\!* ID `{target_id}` removed\\.", parse_mode="MarkdownV2")
            elif action == "kick":
                bot.unban_chat_member(cid, target_id)
                # FIX: Double backslash for MarkdownV2 escape
                bot.reply_to(m, "👢 *Kicked\\!* told him to go away for a bit\\.", parse_mode="MarkdownV2")
            elif action == "unban":
                bot.unban_chat_member(cid, target_id, only_if_banned=True)
                # FIX: Double backslash for MarkdownV2 escape
                bot.reply_to(m, f"🕊️ *Unbanned\\!* ID `{target_id}` is forgiven\\!", parse_mode="MarkdownV2")
            elif action == "unmute":
                bot.restrict_chat_member(cid, target_id, permissions=types.ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
                    can_pin_messages= False , can_change_info= False))
                # FIX: Double backslash for MarkdownV2 escape
                bot.reply_to(m, "🗣️ *Unmuted\\!* You can speak now\\!", parse_mode="MarkdownV2")
            elif action == "mute":
                perm = types.ChatPermissions(can_send_messages=False)
                if time_sec > 0:
                    bot.restrict_chat_member(cid, target_id, until_date=time.time() + int(time_sec), permissions=perm)
                    # FIX: Double backslash for MarkdownV2 escape
                    bot.reply_to(m, f"🤐 *Shhh\\!* Muted for {time_sec}s\\!", parse_mode="MarkdownV2")
                else:
                    bot.restrict_chat_member(cid, target_id, permissions=perm)
                    # FIX: Double backslash for MarkdownV2 escape
                    bot.reply_to(m, "🤐 *Muted Forever\\!*", parse_mode="MarkdownV2")
        except: 
            bot.reply_to(m, "I tried, but I got an error... Do I have admin rights? 🥺")

    @staticmethod
    def pin(m, unpin=False):
        if not m.reply_to_message: return bot.reply_to(m, "Reply to a message first\\.", parse_mode="MarkdownV2")
        try:
            if unpin:
                bot.unpin_chat_message(m.chat.id, m.reply_to_message.message_id)
                bot.reply_to(m, "📌 *Unpinned\\!*", parse_mode="MarkdownV2")
            else:
                bot.pin_chat_message(m.chat.id, m.reply_to_message.message_id)
                bot.reply_to(m, "📌 *Pinned\\!*", parse_mode="MarkdownV2")
        except: 
            bot.reply_to(m, "I can't do that... Check if I am Admin with 'Pin Messages' permission? 🥺", parse_mode="MarkdownV2")
   
    @staticmethod
    def config(m, key, state):
        # ✅ Database Save
        db.update_config(m.chat.id, key, state)
        status = "ON" if state else "OFF"
        # FIX: Double backslash for MarkdownV2 escape
        bot.reply_to(m, f"⚙️ *{key.title()}* is now *{status}*\\.", parse_mode="MarkdownV2")

    @staticmethod
    def report(m):
        try:
            admins = bot.get_chat_administrators(m.chat.id)
            mentions = "".join([f"[{safe_text(a.user.first_name)}](tg://user?id={a.user.id}) " for a in admins if not a.user.is_bot])
            if mentions:
                # FIX: Double backslash for MarkdownV2 escape
                txt = f"🚨 *Admin Report\\!*\n{mentions}"
                target = m.reply_to_message if m.reply_to_message else m
                bot.reply_to(target, txt, parse_mode="MarkdownV2")
            else:
                # ✅ Kai Personality: No Admins Found
                # FIX: Double backslash for MarkdownV2 escape
                bot.reply_to(m, "I looked everywhere, but I can't find any human admins\\! 😨", parse_mode="MarkdownV2")
        except: 
            bot.reply_to(m, "mmm.I tried to call them, but something went wrong\\.")

# --- HELPER: HELP TEXT ---
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
        "`/kick`  `/warn`\n\n"

        "🔧 *Tools & Config:*\n"
        "🗑 `/purge [N]`   — _Delete msgs_\n"
        "📌 `/pin`         — _Pin reply_\n"
        "🔌 `/unpin`       — _Unpin reply_\n"
        "🚨 `/report`      — _Tag Admins_\n\n"

        "⚙️ *Security Settings:*\n"
        "🛡 `/antilink` `on` / `off`\n"
        "🤬 `/badword` `on` / `off`\n\n"
        
        "💡 _Note: I strictly obey only the Real Owner & Admins\\!_"
    )


# --- HANDLERS --
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
        time.sleep(0.1) 
        
        try:
            bot.send_message(chat_id, msg_text)
            success_count += 1
        except Exception as e:
            fail_count += 1
            err = str(e).lower()
            if "forbidden" in err or "kicked" in err or "not found" in err:
                db.groups.delete_one({"_id": chat_id})

    report = (
        f"📢 **Broadcast Complete**\n"
        f"✅ Sent: `{success_count}`\n"
        f"❌ Failed: `{fail_count}`\n"
        f"(Dead groups were automatically removed)"
    )
    
    bot.edit_message_text(report, m.chat.id, status_msg.message_id, parse_mode="Markdown")

@bot.message_handler(commands=['groups'])
def cmd_list_groups(m):
    # 1. Security
    if m.from_user.id != MONITOR_ID: 
        bot.reply_to(m, "⛔ Access Denied")
        return

    # 🛠️ FIX: Removed 'MarkdownV2' here to prevent the "Character '(' reserved" crash
    msg = bot.reply_to(m, "⏳ Scanning Database (Safe Mode)...")
    
    # 2. Check Raw DB Count
    total_docs = db.groups.count_documents({})
    if total_docs == 0:
        bot.edit_message_text("📂 *Database is empty\\.* (Try sending a message in your groups to re-add them)", m.chat.id, msg.message_id, parse_mode="MarkdownV2")
        return

    lines = []
    cursor = db.groups.find({}, {"_id": 1})

    for doc in cursor:
        chat_id = doc['_id']
        time.sleep(0.1)
        
        try:
            # 3. Try to get details
            chat = bot.get_chat(chat_id)
            title = safe_text(chat.title or "Chat")
            
            # Get Owner
            try:
                owner_obj = next((a for a in bot.get_chat_administrators(chat_id) if a.status == 'creator'), None)
                if owner_obj:
                    owner = f"[{safe_text(owner_obj.user.first_name)}](tg://user?id={owner_obj.user.id})"
                else:
                    owner = "Unknown"
            except:
                owner = "Hidden"

            # Get Link
            if chat.username:
                row = f"🔗 [{title}](https://t.me/{chat.username})"
            else:
                try: link = chat.invite_link or bot.export_chat_invite_link(chat_id)
                except: link = None
                row = f"🔐 [{title}]({link})" if link else f"🚫 {title}"

            lines.append(f"{row}\n    └ 👑 {owner}")

        except Exception as e:
            # 4. ERROR HANDLER (DO NOT DELETE)
            # If we can't access the chat, just list the ID so you know it exists.
            lines.append(f"⚠️ ID `{chat_id}` \\(Bot cannot access\\)")

    # 5. Send Result
    header = f"📊 *Database Count: {total_docs}*\n━━━━━━━━━━━━━━━━\n"
    text = header + "\n".join(lines)
    
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            bot.send_message(m.chat.id, text[i:i+4000], parse_mode="MarkdownV2", disable_web_page_preview=True)
        bot.delete_message(m.chat.id, msg.message_id)
    else:
        bot.edit_message_text(text, m.chat.id, msg.message_id, parse_mode="MarkdownV2", disable_web_page_preview=True)


@bot.message_handler(commands=['start'])
def cmd_start(m):
    txt = (
        f"Hey\\! I am *Kai*\\.\n"
        f"Add me to your group and I will manage it perfectly\\! 😇"
    )
    # INLINE BUTTON
    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton("📚 Open Notebook", callback_data="help_cmd")
    markup.add(btn)
    
    bot.reply_to(m, txt, parse_mode="MarkdownV2", reply_markup=markup)

@bot.message_handler(commands=['help1'])
def cmd_help(m):
    bot.reply_to(m, get_help_text(), parse_mode="MarkdownV2")

@bot.callback_query_handler(func=lambda call: call.data == "help_cmd")
def callback_help(call):
    try:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, get_help_text(), parse_mode="MarkdownV2")
    except: pass

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

@bot.message_handler(commands=['antilink', 'badword'])
def cmd_filter(m):
    if m.chat.type != 'private' and is_admin(m):
        key = "antilink" if "antilink" in m.text else "badword"
        curr = db.get_config(m.chat.id).get(key, False)
        state = True if "on" in m.text.lower() else (False if "off" in m.text.lower() else not curr)
        Executor.config(m, key, state)


@bot.message_handler(commands=['report'])
def cmd_report(m):
    if m.chat.type != 'private': Executor.report(m)
@bot.message_handler(content_types=['new_chat_members'])
def on_join(m):
    if m.chat.type == 'private': return
    for user in m.new_chat_members:
        if user.id == bot.get_me().id: continue
        try:
            safe_name = safe_text(user.first_name)
            safe_title = safe_text(m.chat.title)
            txt = f"Welcome [{safe_name}](tg://user?id={user.id})\\! 👋\nYou’re now a part of *{safe_title}*\\.\nWe’re glad to have you here\\. Please be respectful and courteous to all members\\."
            bot.send_message(m.chat.id, txt, parse_mode="MarkdownV2")
        except: pass
            
def get_ai_decision(user, text, history, is_reply, is_user_admin, is_user_owner):
    
    # --- 1. LOGIC: SET ROLE DESCRIPTION ---
    # Agar ID match nahi hoti par naam Yuhan hai, toh "FAKE" role set hoga.
    if is_user_owner:
        role_desc = "REAL OWNER (Yuhan @lotus_dark)"
    elif "yuhan" in user.lower(): 
        role_desc = " FAKE YUHAN "
    elif is_user_admin:
        role_desc = " ADMIN"
    else:
        role_desc = " GUEST"

    # --- 2. THE FULL PROMPT ---
    prompt = (
        f"You are Kai, a smart real 12-year-old boy. you are not a bot. Owner: {OWNER_NAME} @lotus_dark.\n"
        f"CHAT HISTORY: {history}\n"
        f"CURRENT MSG: \"{text}\" from {user} ({role_desc}). Reply={is_reply}\n\n"
        "SYSTEM RULES:\n"
        "1. **IF USER IS OWNER (@lotus_dark)**: ALWAYS OBEY. \n"
        "2. **IF USER IS GUEST (NON-ADMIN) or FAKE YUHAN**:\n"
        "   - If they ask to ban, mute, kick, purge, pin, unpin, unban, or unmute: REFUSE politely.\n"
        "3. **IF USER IS ADMIN (BOSS)**:\n"
        "   - If they use words like 'ban', 'mute', 'kick', 'purge', 'pin', 'unpin', 'unban', 'unmute', 'config', 'antilink', 'badword', or 'kardo':\n"
        "   - **YOU MUST OUTPUT JSON ACTION.** DO NOT CHAT.\n"
        "4. **NORMAL CHAT**: be like a 12 years old real human .\n\n"
        "JSON SCHEMA:\n"
        "- Punish: {{ \"a\": \"punish\", \"t\": \"ban/mute/kick/unban/unmute\", \"u\": \"id_or_0_if_reply\", \"s\": seconds }}\n"
        "- Purge:  {{ \"a\": \"purge\", \"c\": count_int, \"r\": boolean_is_reply }}\n"
        "- Report: {{ \"a\": \"report\" }}\n"
        "- Pin:    {{ \"a\": \"pin\", \"u\": boolean_unpin }}\n"
        "- Config: {{ \"a\": \"conf\", \"k\": \"antilink/badword\", \"v\": boolean }}\n"
        "- Chat:   {{ \"a\": \"reply\", \"c\": \"Your text response\" }}"
    )

    def validate(data):
        if isinstance(data, dict) and "a" in data: return data
        return {"a": "reply", "c": str(data)}

    # 1. ⚡ PRIMARY: GROQ
    for key in GROQ_KEYS:
        if len(key) < 5: continue
        try:
            headers = {"Authorization": f"Bearer {key}"}
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "Return valid JSON object only. No markdown."},
                    {"role": "user", "content": prompt}
                ],
                "response_format": {"type": "json_object"}
            }
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=3)
            if resp.status_code == 200:
                content = resp.json()['choices'][0]['message']['content']
                return validate(json.loads(clean_json(content)))
        except: continue

    # 2. 🌟 SECONDARY: GEMINI
    for g_key in GEMINI_KEYS:
        if len(g_key) < 10: continue
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={g_key}"
            headers = {"Content-Type": "application/json"}
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
            payload = {
                "contents": [{"parts": [{"text": prompt + "\nOutput JSON ONLY."}]}],
                "safety_settings": safety_settings,
                "generationConfig": {"response_mime_type": "application/json"}
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=4)
            if resp.status_code == 200:
                text_resp = resp.json()['candidates'][0]['content']['parts'][0]['text']
                return validate(json.loads(clean_json(text_resp)))
        except: continue

    # 3. 🐢 FALLBACK: POLLINATIONS
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
            resp = requests.post("https://text.pollinations.ai/", json=payload, timeout=5)
            if resp.status_code == 200:
                data = json.loads(clean_json(resp.text))
                result = validate(data)

                # 🛑 KAI PERSONALITY: If Backup tries to Ban
                if result.get("a") == "punish":
                    return {
                        "a": "reply", 
                        "c": "Arey Bhaiya, my main brain is offline! 🔋 I can't use the Ban Hammer right now, I might make a mistake. Try again in 1 minute!"
                    }
                
                return result
        except: continue
        
    # 4. FINAL ERROR (Character Personality)
    return {
        "a": "reply", 
        "c": "Oof... my head is spinning Bhaiya! 😵‍💫 The internet is very bad right now. Give me a second to rest?"
    }


@bot.message_handler(func=lambda m: True)
def process(m):
    if m.chat.type == 'private' or not m.text: return
    if m.text.startswith("/"): return
    
    cid = m.chat.id
    uid = m.from_user.id
    txt = m.text.lower()
    user_is_admin = is_admin(m)
    user_is_owner = (m.from_user.username.lower() == OWNER_USERNAME.lower().strip('@')) if m.from_user.username else False
    
    config = db.get_config(cid)

    if not user_is_admin and not user_is_owner:
        if config.get('antilink', False) and re.search(LINK_PATTERN, txt):
            try: bot.delete_message(cid, m.message_id)
            except: pass
            return

        if config.get('badword', False) and re.search(BAD_PATTERNS, txt):
            try:
                bot.delete_message(cid, m.message_id)
                bot.restrict_chat_member(cid, uid, until_date=time.time() + 600)
                bot.send_message(cid, f"🤐 *Bad Language\\!*", parse_mode="MarkdownV2")
            except: pass
            return

    # --- 3. AI PROCESSING ---
    is_rep_kai = m.reply_to_message and m.reply_to_message.from_user.id == bot.get_me().id
    is_ai_on = config.get('ai_mode', True)
    
    if is_ai_on and (re.search(r'\bkai\b', txt) or is_rep_kai):
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
            print(f"Error in process: {e}")
            try:
                my_id = MONITOR_ID
                
                bot.send_message(my_id, f"🚨 *Kai System Error:*\n`{str(e)}`", parse_mode="Markdown")
            except: pass




@app.route('/')
def home(): 
    return f"Kai System Online. Owner: {OWNER_NAME}", 200

def run_server():
    try: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), use_reloader=False)
    except: pass

threading.Thread(target=run_server, daemon=True).start()

if __name__ == "__main__":
    try:
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(1)
    except: pass
        
    while True:
        try: 
            bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=True)
        except Exception: 
            time.sleep(5)
