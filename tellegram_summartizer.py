import asyncio
import os
import pandas as pd
from telethon import TelegramClient
from telethon.tl.custom.message import Message
from telethon.errors import FloodWaitError
from datetime import datetime, timedelta
from openai import AsyncOpenAI
from telethon.tl.types import MessageMediaEmpty
from langdetect import detect
from typing import List, Dict, Tuple
import aiofiles
import re
import sqlite3
import logging
import time

# === Configuration ===
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')    
PHONE = os.getenv('phone')
SESSION_NAME = os.getenv('SESSION_NAME', 'session')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'sk-...')
CSV_FILE = 'tg_detailed_ww99.csv'
MESSAGE_DIR = './messages'
DB_FILE = 'telegram.db'
MAX_TOKENS = 500  # Increased for summarization
URGENT_KEYWORDS = {'urgent', 'asap', 'deadline', 'proposal', 'contract', 'deal'}
SERVICE_KEYWORDS = {'blockchain', 'security', 'audit', 'smart contract', 'defi', 'ethereum', 'starknet', 'protocol'}
FOLLOWUP_KEYWORDS = {'follow up', 'next steps', 'meeting', 'call', 'discuss'}
KNOWN_CONTACTS = {123456, 789012}  # Example sender IDs
MAX_RETRIES = 3
BASE_WAIT = 5

# === Setup ===
os.makedirs(MESSAGE_DIR, exist_ok=True)
client_ai = AsyncOpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# === Database ===
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            is_group BOOLEAN,
            last_message_date DATETIME,
            urgency_score INTEGER,
            needs_followup BOOLEAN,
            last_reply_date DATETIME
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS opportunities (
            chat_id INTEGER,
            message_id INTEGER,
            service TEXT,
            timestamp DATETIME,
            PRIMARY KEY (chat_id, message_id)
        )''')
        conn.commit()

# === Utilities ===
def detect_language(text: str) -> str:
    try:
        return detect(text)
    except:
        return "unknown"

def classify_message_type(msg: Message) -> str:
    if msg.media and not isinstance(msg.media, MessageMediaEmpty):
        return "media"
    if msg.voice:
        return "voice"
    if msg.poll:
        return "poll"
    if msg.contact:
        return "contact"
    if msg.geo:
        return "location"
    if msg.text and msg.text.startswith("/"):
        return "bot_command"
    if msg.text:
        return "text"
    return "other"

def calculate_urgency(message: Message, is_group: bool) -> int:
    score = 0
    text = message.text.lower() if message.text else ''
    if any(kw in text for kw in URGENT_KEYWORDS):
        score += 40
    if message.sender_id in KNOWN_CONTACTS:
        score += 20
    time_diff = (datetime.now(message.date.tzinfo) - message.date).total_seconds() / 60
    score += max(0, 30 - int(time_diff / 10))
    score += 10 if is_group else 0
    return min(100, score)

def detect_service_opportunities(text: str) -> List[str]:
    text = text.lower() if text else ''
    services = []
    if any(kw in text for kw in {'security', 'audit'}):
        services.append('Security Audits')
    if any(kw in text for kw in {'smart contract', 'solidity', 'cairo'}):
        services.append('Smart Contract Development')
    if any(kw in text for kw in {'defi', 'finance'}):
        services.append('DeFi Solutions')
    if any(kw in text for kw in {'blockchain', 'protocol', 'ethereum', 'starknet'}):
        services.append('Protocol Engineering')
    return services

def needs_followup(text: str, last_reply_date: datetime) -> bool:
    text = text.lower() if text else ''
    if any(kw in text for kw in FOLLOWUP_KEYWORDS):
        return True
    if last_reply_date and (datetime.now() - last_reply_date).days > 2:
        return True
    return False

# === AI Summarization ===
async def generate_ai_summary(messages: List[Message], services: List[str]) -> str:
    service_context = f"Nethermind offers: {', '.join(services)}" if services else "Nethermind offers blockchain solutions."
    message_texts = []
    user_map = {}
    for msg in messages:
        sender = await msg.get_sender()
        sender_id = sender.id if sender else "Unknown"
        sender_username = getattr(sender, 'username', None) or f"User_{sender_id}"
        user_map[sender_id] = sender_username
        date_str = msg.date.strftime('%Y-%m-%d %H:%M:%S')
        text = msg.text or f"[{classify_message_type(msg)} message]"
        message_texts.append(f"[{date_str}] {sender_username}: {text}")
    
    prompt = (
        f"Summarize the following conversation, including important dates, places, reminders, and follow-up items. "
        f"Tag each user who participated. Context: {service_context}\n\n"
        f"Messages:\n" + "\n".join(message_texts)
    )
    
    try:
        response = await client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a professional summarizer for Nethermind's Business Development team. Provide a concise summary of the conversation, highlighting key dates, places, reminders, and follow-up items. Tag each participating user by their username."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=MAX_TOKENS
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error: {e}"

# === File Writer ===
async def write_messages_to_file(filename: str, messages: List[Message], unread_count: int, urgency_score: int):
    async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
        await f.write(f"Unread: {unread_count}, Urgency: {urgency_score}\n\n")
        for msg in messages:
            date_str = msg.date.strftime('%Y-%m-%d %H:%M:%S')
            sender_id = msg.sender_id or "Unknown"
            text = msg.text or f"[{classify_message_type(msg)} message]"
            await f.write(f"[{date_str}] {sender_id}: {text}\n")

# === Main Fetching Function ===
async def fetch_data() -> Tuple[int, int, List[Dict]]:
    client = TelegramClient('session', API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("Session not authorized. Please run Authentication.py first to set up the session.")
            return 0, 0, []
        dialogs = await client.get_dialogs()
    except Exception as e:
        logging.error(f"Failed to start client or fetch dialogs: {e}")
        return 0, 0, []

    private_unread = 0
    group_unread = 0

    init_db()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()

        async def process_dialog(dialog):
            nonlocal private_unread, group_unread
            name = dialog.name or "Unknown"
            chat_id = dialog.id
            unread_count = dialog.unread_count or 0
            is_group = dialog.is_group or dialog.is_channel

            logging.info(f"Processing dialog: {name}, Is Group: {is_group}, Unread: {unread_count}")

            c.execute('SELECT last_reply_date FROM chats WHERE chat_id = ?', (chat_id,))
            result = c.fetchone()
            last_reply_date = None
            if result and result[0]:
                try:
                    last_reply_date = datetime.fromisoformat(result[0])
                except (ValueError, TypeError):
                    logging.warning(f"Invalid last_reply_date format for {name}: {result[0]}")
                    last_reply_date = None

            safe_name = re.sub(r'[^\w]', '_', name)
            filepath = os.path.join(MESSAGE_DIR, f"{chat_id}_{safe_name}.txt")

            messages = []
            retries = 0
            message_limit = 100  # Retrieve past 100 messages
            while retries < MAX_RETRIES:
                try:
                    logging.info(f"Fetching {message_limit} messages from {name}")
                    async for message in client.iter_messages(dialog.id, limit=message_limit):
                        messages.append(message)
                        logging.info(f"Message in {name}: Type={classify_message_type(message)}, Text={message.text or 'None'}")
                    logging.info(f"Fetched {len(messages)} messages from {name}")
                    break
                except FloodWaitError as e:
                    wait_time = min(e.seconds, 30) * (2 ** retries)
                    logging.info(f"Flood wait for {wait_time}s on {name}")
                    await asyncio.sleep(wait_time)
                    retries += 1
                except Exception as e:
                    logging.error(f"Error fetching messages for {name}: {e}")
                    break

            if is_group:
                group_unread += unread_count
            else:
                private_unread += unread_count

            urgency_score = 0
            services = []
            needs_followup_flag = False
            last_message_text = "No messages"
            last_message_type = "None"
            language = "unknown"
            ai_summary = "N/A"
            first_message_date = None
            last_unread_date = None
            sender_id = "None"
            sender_username = "None"
            sender_name = "None"

            if messages:
                first_message = messages[-1]
                last_message = messages[0]
                first_message_date = first_message.date
                last_unread_date = last_message.date
                last_message_text = last_message.text or f"[{classify_message_type(last_message)} message]"
                last_message_type = classify_message_type(last_message)
                language = detect_language(last_message.text or "")
                urgency_score = calculate_urgency(last_message, is_group)
                services = detect_service_opportunities(last_message.text)
                needs_followup_flag = needs_followup(last_message.text, last_reply_date)
                ai_summary = await generate_ai_summary(messages, services)

                sender = await last_message.get_sender()
                sender_id = sender.id if sender else "Unknown"
                sender_username = getattr(sender, 'username', None) or "None"
                sender_name = getattr(sender, 'first_name', 'Unknown')

                if services:
                    for service in services:
                        c.execute('INSERT OR IGNORE INTO opportunities (chat_id, message_id, service, timestamp) VALUES (?, ?, ?, ?)',
                                  (chat_id, last_message.id, service, last_unread_date))

                await write_messages_to_file(filepath, messages, unread_count, urgency_score)
            else:
                logging.warning(f"No messages fetched for {name}, unread count: {unread_count}")

            c.execute('INSERT OR REPLACE INTO chats (chat_id, name, is_group, last_message_date, urgency_score, needs_followup) VALUES (?, ?, ?, ?, ?, ?)',
                      (chat_id, name, is_group, last_unread_date, urgency_score, needs_followup_flag))
            conn.commit()

            return {
                "Chat Name": name,
                "Chat ID": chat_id,
                "Is Group": is_group,
                "Unread Count": unread_count,
                "Urgency Score": urgency_score,
                "Needs Followup": needs_followup_flag,
                "Service Opportunities": ", ".join(services) if services else "None",
                "First Message Date": first_message_date,
                "Last Unread Message Date": last_unread_date,
                "Duration Unread (min)": (last_unread_date - first_message_date).total_seconds() / 60 if messages else 0,
                "Last Sender ID": sender_id,
                "Last Sender Username": sender_username,
                "Last Sender Name": sender_name,
                "Last Message Type": last_message_type,
                "Language": language,
                "Summary": ai_summary
            }

        log = []
        for dialog in dialogs:
            try:
                result = await process_dialog(dialog)
                if result:
                    log.append(result)
                    logging.info(f"Successfully processed dialog: {dialog.name}")
            except Exception as e:
                logging.error(f"Failed to process dialog {dialog.name}: {e}")
                continue

        try:
            await client.disconnect()
        except Exception as e:
            logging.error(f"Error disconnecting client: {e}")
        return private_unread, group_unread, log

# === CSV Export ===
def export_to_csv(log: List[Dict], filename: str):
    if not log:
        logging.warning("No data to export")
        return
    df = pd.DataFrame(log)
    df.to_csv(filename, index=False)
    logging.info(f"Exported {len(df)} records to {filename}")

# === Entry Point ===
if __name__ == '__main__':
    try:
        private_unread, group_unread, log = asyncio.run(fetch_data())
        export_to_csv(log, CSV_FILE)
        print(f"Exported {len(log)} chats to {CSV_FILE}")
        print(f"Private unread: {private_unread}, Group unread: {group_unread}")
    except Exception as e:
        logging.error(f"Script failed: {e}")
