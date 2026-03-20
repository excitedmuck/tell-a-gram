"""
Telegram Manager — Standalone
Just enter your phone number and OTP to get started.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz
import streamlit as st
from langdetect import detect
from openai import AsyncOpenAI
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.custom.message import Message
from telethon.tl.types import MessageMediaEmpty

# ── Constants (from tg3.py) ───────────────────────────────────────────────────
API_ID       = 29332917
API_HASH     = '873eb7df959278fd6f70ec1511121b62'
DEFAULT_PHONE = '+447865962969'
SESSION_FILE = 'tg_session'
CSV_FILE     = 'tg_detailed_ww99.csv'
CONFIG_FILE  = 'config.json'
MESSAGE_DIR  = './messages'
DB_FILE      = 'telegram.db'
MAX_TOKENS   = 500
MAX_RETRIES  = 3
URGENT_KEYWORDS   = {'urgent', 'asap', 'deadline', 'proposal', 'contract', 'deal'}
FOLLOWUP_KEYWORDS = {'follow up', 'next steps', 'meeting', 'call', 'discuss'}

os.makedirs(MESSAGE_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f)

def get_openai_key() -> str:
    return load_config().get('openai_api_key', os.getenv('OPENAI_API_KEY', ''))

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Telegram Manager",
    page_icon="💬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state defaults ────────────────────────────────────────────────────
_defaults = {
    'auth_step': 'phone',     # phone | code | 2fa | fetching | dashboard
    'phone': DEFAULT_PHONE,
    'phone_code_hash': None,
    'fetch_done': False,
    'fetch_error': None,
    'fetch_progress': None,   # (current, total, chat_name)
    'fetch_thread': None,
    'nav_page': '📊 Dashboard',
    'skipped': set(),
    'last_sync': datetime.now(pytz.UTC),
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# Auto-skip auth if session file already exists
if (st.session_state.auth_step == 'phone'
        and os.path.exists(f'{SESSION_FILE}.session')):
    st.session_state.auth_step = 'dashboard' if os.path.exists(CSV_FILE) else 'fetching'

# ── Async helpers (each step uses a fresh event loop) ─────────────────────────
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

async def _send_code(phone: str) -> str:
    c = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await c.connect()
    r = await c.send_code_request(phone)
    await c.disconnect()
    return r.phone_code_hash

async def _sign_in(phone: str, code: str, hash_: str):
    c = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await c.connect()
    await c.sign_in(phone, code, phone_code_hash=hash_)
    await c.disconnect()

async def _sign_in_2fa(password: str):
    c = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await c.connect()
    await c.sign_in(password=password)
    await c.disconnect()

# ── Message utilities ─────────────────────────────────────────────────────────
def classify_msg(msg: Message) -> str:
    if msg.media and not isinstance(msg.media, MessageMediaEmpty): return "media"
    if msg.voice:   return "voice"
    if msg.poll:    return "poll"
    if msg.contact: return "contact"
    if msg.geo:     return "location"
    if msg.text and msg.text.startswith("/"): return "bot_command"
    if msg.text:    return "text"
    return "other"

def detect_lang(text: str) -> str:
    try:    return detect(text)
    except: return "unknown"

def urgency_score(msg: Message, is_group: bool) -> int:
    score = 0
    text  = (msg.text or '').lower()
    if any(k in text for k in URGENT_KEYWORDS): score += 40
    diff  = (datetime.now(msg.date.tzinfo) - msg.date).total_seconds() / 60
    score += max(0, 30 - int(diff / 10))
    if is_group: score += 10
    return min(100, score)

def needs_followup(text: str) -> bool:
    return any(k in (text or '').lower() for k in FOLLOWUP_KEYWORDS)

# ── AI summary ────────────────────────────────────────────────────────────────
async def ai_summary(messages: List[Message], client_ai: Optional[AsyncOpenAI]) -> str:
    if not client_ai:
        return "No OpenAI key set — skipped."
    texts = []
    for msg in messages[:50]:
        try:
            sender = await msg.get_sender()
            uname  = getattr(sender, 'username', None) or f"User_{msg.sender_id}"
        except Exception:
            uname  = f"User_{msg.sender_id}"
        texts.append(f"[{msg.date:%Y-%m-%d %H:%M}] {uname}: {msg.text or '[media]'}")
    prompt = (
        "Summarize this conversation concisely. "
        "Note key dates, follow-ups, and tag each participant.\n\n"
        + "\n".join(texts)
    )
    try:
        r = await client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        return f"Summary error: {e}"

# ── Fetch (background thread) ─────────────────────────────────────────────────
async def _fetch_all():
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Not authorized — please re-authenticate.")

    oai_key   = get_openai_key()
    client_ai = AsyncOpenAI(api_key=oai_key) if oai_key else None
    dialogs   = await client.get_dialogs()
    total     = len(dialogs)

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, name TEXT, is_group BOOLEAN,
            last_message_date DATETIME, urgency_score INTEGER,
            needs_followup BOOLEAN, last_reply_date DATETIME)''')
        conn.commit()

    log = []
    for i, dialog in enumerate(dialogs):
        name     = dialog.name or "Unknown"
        chat_id  = dialog.id
        unread   = dialog.unread_count or 0
        is_group = dialog.is_group or dialog.is_channel
        st.session_state.fetch_progress = (i + 1, total, name)

        messages, retries = [], 0
        while retries < MAX_RETRIES:
            try:
                async for msg in client.iter_messages(chat_id, limit=100):
                    messages.append(msg)
                break
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 30) * (2 ** retries))
                retries += 1
            except Exception:
                break

        last  = messages[0]  if messages else None
        first = messages[-1] if messages else None

        msg_text = (last.text or f"[{classify_msg(last)}]") if last else "No messages"
        msg_type = classify_msg(last) if last else "None"
        lang     = detect_lang(last.text or "") if last else "unknown"
        urg      = urgency_score(last, is_group) if last else 0
        followup = needs_followup(last.text if last else "")
        summary  = await ai_summary(messages, client_ai) if messages else "No messages"

        sender_id, sender_uname, sender_name = "None", "None", "None"
        if last:
            try:
                s = await last.get_sender()
                sender_id    = s.id if s else "Unknown"
                sender_uname = getattr(s, 'username', None) or "None"
                sender_name  = getattr(s, 'first_name', 'Unknown')
            except Exception:
                pass

        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO chats VALUES (?,?,?,?,?,?,?)',
                (chat_id, name, is_group,
                 last.date.isoformat() if last else None,
                 urg, followup, None),
            )
            conn.commit()

        log.append({
            "Chat Name": name, "Chat ID": chat_id, "Is Group": is_group,
            "Unread Count": unread, "Urgency Score": urg, "Needs Followup": followup,
            "First Message Date": first.date if first else None,
            "Last Unread Message Date": last.date if last else None,
            "Last Sender ID": sender_id, "Last Sender Username": sender_uname,
            "Last Sender Name": sender_name, "Last Message Type": msg_type,
            "Language": lang, "Last Message Text": msg_text, "Summary": summary,
        })

    await client.disconnect()
    pd.DataFrame(log).to_csv(CSV_FILE, index=False)

def _fetch_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_fetch_all())
        st.session_state.fetch_done  = True
        st.session_state.fetch_error = None
    except Exception as e:
        st.session_state.fetch_done  = True
        st.session_state.fetch_error = str(e)
    finally:
        loop.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def format_ago(ts) -> str:
    if pd.isna(ts): return "Unknown"
    diff  = pd.Timestamp.now(tz='UTC') - ts
    hours = diff.total_seconds() / 3600
    if hours < 1:  return "Just now"
    if hours < 24: return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"

@st.cache_data
def load_csv():
    try:
        df = pd.read_csv(CSV_FILE)
        for col in ['Last Unread Message Date', 'First Message Date', 'last_reply_date']:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])
                if not df[col].dt.tz:
                    df[col] = df[col].dt.tz_localize('UTC')
        if 'Last Message Text' not in df.columns:
            df['Last Message Text'] = df.get('Summary', '')
        if 'Needs Followup' not in df.columns:
            df['Needs Followup'] = False
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return None

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.setup-box {
    max-width: 460px; margin: 5rem auto; padding: 2.5rem;
    background: #1e2130; border-radius: 16px; border: 1px solid #3a3f55;
}
.setup-box h1 { text-align: center; margin-bottom: 0.5rem; }
.setup-box p  { text-align: center; color: #8892a4; margin-bottom: 1.5rem; }
.message-card {
    background: #1e2130; color: #e0e0e0; padding: 1rem;
    border-radius: 10px; border: 1px solid #3a3f55; margin-bottom: 0.75rem;
}
.message-card strong { color: #fff; }
.message-card p      { color: #c0c8d8; margin: 0.3rem 0; font-size: 0.9rem; }
.message-card small  { color: #8892a4; }
.needs-reply { border-left: 4px solid #ff4b4b; }
.metric-card {
    background: #1e2130; padding: 1.5rem; border-radius: 10px;
    border: 1px solid #3a3f55; text-align: center;
}
.metric-value { font-size: 2rem; font-weight: bold; color: #4d9fff; }
.metric-label { color: #8892a4; font-size: 0.9rem; }
</style>
""", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGES
# ════════════════════════════════════════════════════════════════════════════════

def page_phone():
    st.markdown("<div class='setup-box'>", unsafe_allow_html=True)
    st.markdown("# 💬 Telegram Manager")
    st.markdown("<p>Enter your phone number to get started</p>", unsafe_allow_html=True)

    phone = st.text_input(
        "Phone number (with country code)",
        value=st.session_state.phone,
        placeholder="+447865962969",
    )
    oai_key = st.text_input(
        "OpenAI API key (optional — for AI summaries)",
        type="password",
        placeholder="sk-...",
        help="Saved locally in config.json. Used only to call OpenAI for chat summaries.",
    )

    if st.button("Send verification code", use_container_width=True, type="primary"):
        if not phone.strip().startswith("+"):
            st.error("Phone must include country code, e.g. +447865962969")
        else:
            with st.spinner("Sending code…"):
                try:
                    hash_ = _run(_send_code(phone.strip()))
                    st.session_state.phone           = phone.strip()
                    st.session_state.phone_code_hash = hash_
                    st.session_state.auth_step       = 'code'
                    if oai_key.strip().startswith("sk-"):
                        cfg = load_config()
                        cfg['openai_api_key'] = oai_key.strip()
                        save_config(cfg)
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not send code: {e}")

    st.markdown("</div>", unsafe_allow_html=True)


def page_code():
    st.markdown("<div class='setup-box'>", unsafe_allow_html=True)
    st.markdown("# 💬 Enter your code")
    st.markdown(f"<p>Telegram sent a code to <b>{st.session_state.phone}</b></p>",
                unsafe_allow_html=True)

    code = st.text_input("Verification code", max_chars=10, placeholder="12345")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("← Back", use_container_width=True):
            st.session_state.auth_step = 'phone'
            st.rerun()
    with c2:
        if st.button("Verify →", use_container_width=True, type="primary"):
            with st.spinner("Verifying…"):
                try:
                    _run(_sign_in(
                        st.session_state.phone,
                        code.strip(),
                        st.session_state.phone_code_hash,
                    ))
                    st.session_state.auth_step = 'fetching'
                    st.rerun()
                except SessionPasswordNeededError:
                    st.session_state.auth_step = '2fa'
                    st.rerun()
                except PhoneCodeInvalidError:
                    st.error("Invalid code — please try again.")
                except Exception as e:
                    st.error(f"Error: {e}")

    st.markdown("</div>", unsafe_allow_html=True)


def page_2fa():
    st.markdown("<div class='setup-box'>", unsafe_allow_html=True)
    st.markdown("# 🔐 Two-factor authentication")
    st.markdown("<p>This account has 2FA enabled. Enter your cloud password.</p>",
                unsafe_allow_html=True)

    pw = st.text_input("Cloud password", type="password")
    if st.button("Continue →", use_container_width=True, type="primary"):
        with st.spinner("Signing in…"):
            try:
                _run(_sign_in_2fa(pw))
                st.session_state.auth_step = 'fetching'
                st.rerun()
            except Exception as e:
                st.error(f"Wrong password: {e}")

    st.markdown("</div>", unsafe_allow_html=True)


def page_fetching():
    st.title("💬 Fetching your chats…")
    st.caption("This may take a few minutes depending on how many chats you have.")

    # Start thread once
    if (st.session_state.fetch_thread is None
            or not st.session_state.fetch_thread.is_alive()):
        if not st.session_state.fetch_done:
            t = threading.Thread(target=_fetch_thread, daemon=True)
            t.start()
            st.session_state.fetch_thread = t

    if st.session_state.fetch_done:
        if st.session_state.fetch_error:
            st.error(f"Fetch failed: {st.session_state.fetch_error}")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Retry"):
                    st.session_state.fetch_done   = False
                    st.session_state.fetch_error  = None
                    st.session_state.fetch_thread = None
                    st.rerun()
            with col2:
                if st.button("Sign out"):
                    if os.path.exists(f'{SESSION_FILE}.session'):
                        os.remove(f'{SESSION_FILE}.session')
                    st.session_state.auth_step = 'phone'
                    st.rerun()
        else:
            st.success("Done! Loading your dashboard…")
            st.session_state.auth_step  = 'dashboard'
            st.session_state.fetch_done = False
            st.cache_data.clear()
            st.rerun()
    else:
        prog = st.session_state.fetch_progress
        if prog:
            current, total, name = prog
            pct = current / total
            st.progress(pct, text=f"Processing **{current}/{total}**: {name}")
            st.caption(f"{int(pct * 100)}% complete")
        else:
            st.info("Starting up…")
        time.sleep(2)
        st.rerun()


def page_dashboard():
    df = load_csv()

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("💬 Telegram Manager")
        st.markdown("---")
        st.markdown(f"""
            <div style='display:flex;align-items:center;gap:10px;margin-bottom:0.5rem;'>
                <span style='font-size:1.4rem;'>👤</span>
                <div>
                    <div style='font-weight:bold;'>{st.session_state.phone}</div>
                    <div style='color:#28a745;font-weight:bold;font-size:0.85rem;'>● Connected</div>
                </div>
            </div>
        """, unsafe_allow_html=True)
        st.markdown("---")
        st.session_state.nav_page = st.radio("Navigation", [
            "📊 Dashboard",
            "📩 Unreplied Messages",
            "👥 Groups",
            "📈 Analytics",
            "⚙️ Settings",
        ])

    if df is None:
        st.error("No data loaded. Go to Settings → Re-fetch.")
        return

    # ── Top bar ───────────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([6, 1, 1])
    with c1:
        st.title(st.session_state.nav_page)
        st.caption(f"Last synced {format_ago(st.session_state.last_sync)}")
    with c2:
        if st.button("📤 Export", use_container_width=True):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"telegram_export_{ts}.csv"
            df.to_csv(fname, index=False)
            st.toast(f"Exported to {fname}", icon="📤")
    with c3:
        if st.button("🔄 Re-fetch", use_container_width=True):
            st.session_state.fetch_done   = False
            st.session_state.fetch_error  = None
            st.session_state.fetch_thread = None
            st.session_state.auth_step    = 'fetching'
            st.session_state.last_sync    = datetime.now(pytz.UTC)
            st.cache_data.clear()
            st.rerun()

    # ── Metrics ───────────────────────────────────────────────────────────────
    now        = pd.Timestamp.now(tz='UTC')
    grp_unread = int(df[df['Is Group']]['Unread Count'].sum())
    prv_unread = int(df[~df['Is Group']]['Unread Count'].sum())
    active_grp = len(df[
        df['Is Group'] &
        (df['Last Unread Message Date'] > now - pd.Timedelta(days=7))
    ])
    total_grp  = len(df[df['Is Group']])
    urgent_n   = len(df[df['Urgency Score'] >= 50])
    today_n    = len(df[df['Last Unread Message Date'].dt.date == now.date()])

    m1, m2, m3, m4 = st.columns(4)
    for col, val, label in [
        (m1, f"{grp_unread:,} / {prv_unread:,}", "Unread (Groups / Private)"),
        (m2, f"{active_grp} / {total_grp}",       "Active / Total Groups (7d)"),
        (m3, str(urgent_n),                        "High Urgency Chats"),
        (m4, str(today_n),                         "Messages Today"),
    ]:
        col.markdown(f"""
            <div class='metric-card'>
                <div class='metric-value'>{val}</div>
                <div class='metric-label'>{label}</div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("---")
    page = st.session_state.nav_page

    # ══ Dashboard ══════════════════════════════════════════════════════════════
    if page == "📊 Dashboard":
        c1, c2 = st.columns(2)

        with c1:
            st.subheader("📩 Needs Follow-up")
            needs_reply = df[df['Needs Followup']].sort_values(
                'Last Unread Message Date', ascending=False).head(5)
            if len(needs_reply) == 0:
                st.info("No chats need follow-up.")
            for _, r in needs_reply.iterrows():
                st.markdown(f"""
                    <div class='message-card needs-reply'>
                        <strong>{r['Chat Name']}</strong>
                        <p>{str(r['Last Message Text'])[:180]}…</p>
                        <small>🔔 {r['Unread Count']} unread · {format_ago(r['Last Unread Message Date'])}</small>
                    </div>
                """, unsafe_allow_html=True)

        with c2:
            st.subheader("🔥 Highest Urgency")
            for _, r in df.sort_values('Urgency Score', ascending=False).head(5).iterrows():
                urg_color = "#ff4b4b" if r['Urgency Score'] >= 70 else "#ffc107"
                st.markdown(f"""
                    <div class='message-card'>
                        <strong>{r['Chat Name']}</strong>
                        <span style='float:right;color:{urg_color};font-weight:bold;'>{r['Urgency Score']}/100</span>
                        <p>{str(r['Last Message Text'])[:180]}…</p>
                        <small>👤 {r.get('Last Sender Name','?')} · {format_ago(r['Last Unread Message Date'])}</small>
                    </div>
                """, unsafe_allow_html=True)

    # ══ Unreplied Messages ════════════════════════════════════════════════════
    elif page == "📩 Unreplied Messages":
        unreplied = df[df['Needs Followup']].sort_values(
            'Last Unread Message Date', ascending=False)
        st.caption(f"{len(unreplied)} chats need follow-up")
        if len(unreplied) == 0:
            st.success("You're all caught up! 🎉")
        for _, r in unreplied.iterrows():
            with st.expander(
                f"💬 {r['Chat Name']} — {r['Unread Count']} unread · {format_ago(r['Last Unread Message Date'])}"
            ):
                st.markdown(f"**Last message:** {r['Last Message Text']}")
                summary = r.get('Summary', '')
                if pd.notna(summary) and summary and summary != r['Last Message Text']:
                    st.markdown("**AI Summary:**")
                    st.markdown(summary)

    # ══ Groups ════════════════════════════════════════════════════════════════
    elif page == "👥 Groups":
        f1, f2 = st.columns(2)
        with f1:
            filt = st.selectbox("Filter", [
                "All Groups", "Active (24h)", "Active (7d)", "Inactive (>7d)"])
        with f2:
            sort = st.selectbox("Sort By", [
                "Last Activity", "Unread Count", "Urgency Score"])

        groups = df[df['Is Group']].copy()
        now    = pd.Timestamp.now(tz='UTC')
        if filt == "Active (24h)":
            groups = groups[groups['Last Unread Message Date'] > now - pd.Timedelta(days=1)]
        elif filt == "Active (7d)":
            groups = groups[groups['Last Unread Message Date'] > now - pd.Timedelta(days=7)]
        elif filt == "Inactive (>7d)":
            groups = groups[groups['Last Unread Message Date'] <= now - pd.Timedelta(days=7)]

        sort_map = {
            "Last Activity": "Last Unread Message Date",
            "Unread Count":  "Unread Count",
            "Urgency Score": "Urgency Score",
        }
        groups = groups.sort_values(sort_map[sort], ascending=False)
        st.caption(f"{len(groups)} groups")

        for _, r in groups.iterrows():
            urg_color = "#ff4b4b" if r['Urgency Score'] >= 70 else \
                        "#ffc107" if r['Urgency Score'] >= 40 else "#8892a4"
            with st.expander(
                f"👥 {r['Chat Name']} — 🔔 {r['Unread Count']:,} unread"
            ):
                col_a, col_b = st.columns([4, 1])
                with col_a:
                    st.markdown(
                        f"**Last message by:** {r.get('Last Sender Name','Unknown')} "
                        f"(@{r.get('Last Sender Username','?')})")
                    st.markdown(
                        f"**Type:** {r['Last Message Type']} · "
                        f"**Language:** {r['Language']} · "
                        f"**Last activity:** {format_ago(r['Last Unread Message Date'])}")
                    st.markdown(f"**Last message:** {str(r['Last Message Text'])[:400]}")
                    summary = r.get('Summary', '')
                    if pd.notna(summary) and summary and summary != r['Last Message Text']:
                        st.markdown("**AI Summary:**")
                        st.markdown(summary)
                with col_b:
                    st.markdown(
                        f"<div style='color:{urg_color};font-size:2rem;"
                        f"text-align:center;font-weight:bold;'>{r['Urgency Score']}"
                        f"<br><small style='font-size:0.7rem;color:#8892a4;'>urgency</small>"
                        f"</div>",
                        unsafe_allow_html=True)

    # ══ Analytics ═════════════════════════════════════════════════════════════
    elif page == "📈 Analytics":
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Message Types")
            st.bar_chart(df['Last Message Type'].value_counts())
        with c2:
            st.subheader("Language Distribution")
            st.bar_chart(df['Language'].value_counts().head(10))

        st.subheader("Urgency Score Distribution")
        st.bar_chart(df['Urgency Score'].value_counts().sort_index())

        c3, c4 = st.columns(2)
        with c3:
            st.subheader("Groups vs Private Chats")
            st.bar_chart(pd.DataFrame({
                'Count': [len(df[df['Is Group']]), len(df[~df['Is Group']])]
            }, index=['Groups', 'Private']))
        with c4:
            st.subheader("Top 10 by Unread Count")
            st.dataframe(
                df.nlargest(10, 'Unread Count')[
                    ['Chat Name', 'Unread Count', 'Urgency Score', 'Language']
                ].reset_index(drop=True),
                use_container_width=True,
            )

    # ══ Settings ══════════════════════════════════════════════════════════════
    elif page == "⚙️ Settings":
        st.subheader("🔑 OpenAI API Key")
        cfg = load_config()
        cur = cfg.get('openai_api_key', '')
        if cur:
            st.caption(f"Current key: `sk-...{cur[-4:]}`")
        new_key = st.text_input(
            "New OpenAI API key", type="password", placeholder="sk-...",
            help="Saved locally in config.json — only sent to OpenAI for summaries.")
        if st.button("Save API Key"):
            if new_key.strip().startswith("sk-"):
                cfg['openai_api_key'] = new_key.strip()
                save_config(cfg)
                st.toast("API key saved!", icon="✅")
                st.rerun()
            else:
                st.error("Key must start with 'sk-'")

        st.markdown("---")
        st.subheader("📂 Data")
        st.caption(f"CSV: `{CSV_FILE}`  ·  Session: `{SESSION_FILE}.session`")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 Re-fetch all chats", use_container_width=True):
                st.session_state.fetch_done   = False
                st.session_state.fetch_error  = None
                st.session_state.fetch_thread = None
                st.session_state.auth_step    = 'fetching'
                st.cache_data.clear()
                st.rerun()
        with col2:
            if st.button("🚪 Sign out", use_container_width=True):
                if os.path.exists(f'{SESSION_FILE}.session'):
                    os.remove(f'{SESSION_FILE}.session')
                st.session_state.auth_step = 'phone'
                st.cache_data.clear()
                st.rerun()


# ── Router ────────────────────────────────────────────────────────────────────
_step = st.session_state.auth_step
if   _step == 'phone':     page_phone()
elif _step == 'code':      page_code()
elif _step == '2fa':       page_2fa()
elif _step == 'fetching':  page_fetching()
else:                      page_dashboard()
