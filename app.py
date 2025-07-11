import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os
import pytz
import json

# Page config
st.set_page_config(
    page_title="Telegram Manager",
    page_icon="💬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize session state
if 'page' not in st.session_state:
    st.session_state.page = "📊 Dashboard"
if 'selected_chat' not in st.session_state:
    st.session_state.selected_chat = None
if 'last_sync' not in st.session_state:
    st.session_state.last_sync = datetime.now(pytz.UTC)
if 'skipped_suggestions' not in st.session_state:
    st.session_state.skipped_suggestions = set()

# Custom CSS with button styling
st.markdown("""
    <style>
    .main {
        padding: 0rem 1rem;
    }
    .stAlert {
        margin-top: 1rem;
    }
    .message-card {
        background-color: white;
        padding: 1rem;
        border-radius: 10px;
        border: 1px solid #e6e6e6;
        margin-bottom: 1rem;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    .message-card:hover {
        box-shadow: 0 4px 8px rgba(0,0,0,0.1);
    }
    .needs-reply {
        border-left: 4px solid #ff4b4b;
    }
    .metric-card {
        background-color: white;
        padding: 1.5rem;
        border-radius: 10px;
        border: 1px solid #e6e6e6;
        text-align: center;
    }
    .metric-value {
        font-size: 2rem;
        font-weight: bold;
        color: #0066cc;
    }
    .metric-label {
        color: #666;
        font-size: 0.9rem;
    }
    .suggestion-card {
        background-color: #f8f9fa;
        padding: 1rem;
        border-radius: 10px;
        border: 1px solid #e6e6e6;
        margin-bottom: 1rem;
    }
    .confidence-high {
        color: #28a745;
    }
    .confidence-medium {
        color: #ffc107;
    }
    .confidence-low {
        color: #dc3545;
    }
    .status-connected {
        color: #28a745;
        font-weight: bold;
    }
    .view-all {
        text-align: center;
        padding: 1rem;
    }
    .action-buttons {
        display: flex;
        gap: 0.5rem;
    }
    .action-button {
        padding: 0.5rem 1rem;
        border-radius: 5px;
        border: none;
        cursor: pointer;
        font-size: 0.9rem;
    }
    .use-reply {
        background-color: #28a745;
        color: white;
    }
    .edit-reply {
        background-color: #ffc107;
        color: black;
    }
    .skip-reply {
        background-color: #dc3545;
        color: white;
    }
    </style>
""", unsafe_allow_html=True)

# Load data from CSV
@st.cache_data
def load_data():
    try:
        df = pd.read_csv('tg_detailed_ww5905.csv')
        date_columns = ['Last Unread Message Date', 'last_reply_date']
        for col in date_columns:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])
                if not df[col].dt.tz:  # Only localize if not already tz-aware
                    df[col] = df[col].dt.tz_localize('UTC')
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return None

def format_time_ago(timestamp):
    if pd.isna(timestamp):
        return "Unknown"
    now = pd.Timestamp.now(tz='UTC')
    diff = now - timestamp
    hours = diff.total_seconds() / 3600
    if hours < 1:
        return "Just now"
    elif hours < 24:
        return f"{int(hours)}h ago"
    else:
        days = int(hours / 24)
        return f"{days}d ago"

def handle_use_reply(chat_id, reply_text):
    st.toast(f"Reply sent to chat {chat_id}", icon="✅")
    # Here you would integrate with your Telegram sending function
    return True

def handle_edit_reply(chat_id, reply_text):
    st.session_state.editing_chat = chat_id
    st.session_state.editing_text = reply_text

def handle_skip_suggestion(chat_id):
    st.session_state.skipped_suggestions.add(chat_id)
    st.toast(f"Suggestion skipped", icon="ℹ️")

def export_data(df):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"telegram_export_{timestamp}.csv"
    df.to_csv(filename, index=False)
    st.toast(f"Data exported to {filename}", icon="📤")

def sync_data():
    st.session_state.last_sync = datetime.now(pytz.UTC)
    st.cache_data.clear()
    st.toast("Data synchronized", icon="🔄")

# Sidebar Navigation
with st.sidebar:
    st.title("💬 Telegram Manager")
    
    # User status
    st.markdown("---")
    st.markdown("""
        <div style='display: flex; align-items: center;'>
            <span style='font-size: 1.2rem; margin-right: 8px;'>👤</span>
            <div>
                <div style='font-weight: bold;'>Telegram User</div>
                <div class='status-connected'>● Connected</div>
            </div>
        </div>
    """, unsafe_allow_html=True)
    st.markdown("---")
    
    # Navigation Menu
    st.session_state.page = st.radio(
        "Navigation",
        ["📊 Dashboard", "📩 Unreplied Messages", "👥 Groups", 
         "🤖 AI Suggestions", "📈 Database Analysis", "⚙️ Settings"]
    )

# Load the data
df = load_data()

if df is not None:
    unreplied_count = len(df[df['Needs Followup']])
    groups_count = len(df[df['Is Group']])
    ai_suggestions = len(df[df['AI Reply'].notna()])
    
    # Top Action Buttons
    col1, col2, col3 = st.columns([6, 1, 1])
    with col1:
        st.title("Dashboard Overview")
        st.caption(f"Last synced {format_time_ago(st.session_state.last_sync)}")
    with col2:
        if st.button("📤 Export", use_container_width=True):
            export_data(df)
    with col3:
        if st.button("🔄 Sync Now", use_container_width=True):
            sync_data()
    
    # Enhanced metrics
    m1, m2, m3, m4 = st.columns(4)
    
    with m1:
        group_unread = df[df['Is Group']]['Unread Count'].sum()
        private_unread = df[~df['Is Group']]['Unread Count'].sum()
        st.markdown("""
            <div class='metric-card'>
                <div class='metric-value'>%d / %d</div>
                <div class='metric-label'>Unread (Groups/Private)</div>
            </div>
        """ % (group_unread, private_unread), unsafe_allow_html=True)
    
    with m2:
        active_groups = len(df[df['Is Group'] & (df['Last Unread Message Date'] > pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=7))])
        total_groups = len(df[df['Is Group']])
        st.markdown("""
            <div class='metric-card'>
                <div class='metric-value'>%d / %d</div>
                <div class='metric-label'>Active/Total Groups (7d)</div>
            </div>
        """ % (active_groups, total_groups), unsafe_allow_html=True)
    
    with m3:
        urgent_count = len(df[df['Urgency Score'] >= 7])
        ai_suggestions = len(df[df['AI Reply'].notna()])
        st.markdown("""
            <div class='metric-card'>
                <div class='metric-value'>%d</div>
                <div class='metric-label'>High Urgency Chats</div>
            </div>
        """ % urgent_count, unsafe_allow_html=True)
    
    with m4:
        today = pd.Timestamp.now(tz='UTC').date()
        today_count = len(df[df['Last Unread Message Date'].dt.date == today])
        st.markdown("""
            <div class='metric-card'>
                <div class='metric-value'>%d</div>
                <div class='metric-label'>Messages Today</div>
            </div>
        """ % today_count, unsafe_allow_html=True)
    
    # Main Content Area
    st.markdown("---")
    
    if st.session_state.page == "📊 Dashboard":
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("📩 Recent Unreplied Messages")
            unreplied = df[df['Needs Followup']].sort_values('Last Unread Message Date', ascending=False).head(3)
            
            for _, row in unreplied.iterrows():
                time_ago = format_time_ago(row['Last Unread Message Date'])
                st.markdown(f"""
                    <div class='message-card needs-reply'>
                        <strong>{row['Chat Name']}</strong>
                        <p>{row['Last Message Text'][:100]}...</p>
                        <small>Needs Reply • {row['Members'] if 'Members' in row else 'Unknown'} members • {time_ago}</small>
                    </div>
                """, unsafe_allow_html=True)
            
            if len(unreplied) > 0:
                if st.button("View all unreplied messages", key="view_all_unreplied"):
                    st.session_state.page = "📩 Unreplied Messages"
        
        with col2:
            st.subheader("🤖 AI Reply Suggestions")
            suggestions = df[
                (df['AI Reply'].notna()) & 
                (~df['Chat ID'].isin(st.session_state.skipped_suggestions))
            ].sort_values('Urgency Score', ascending=False).head(3)
            
            for _, row in suggestions.iterrows():
                confidence = row['Urgency Score'] * 10
                confidence_class = 'confidence-high' if confidence >= 85 else 'confidence-medium' if confidence >= 70 else 'confidence-low'
                
                with st.container():
                    st.markdown(f"""
                        <div class='suggestion-card'>
                            <strong>{row['Chat Name']}</strong>
                            <p>{row['Last Message Text'][:100]}...</p>
                            <p><em>Suggested Reply:</em><br>{row['AI Reply'][:100]}...</p>
                            <div style='display: flex; justify-content: space-between; align-items: center;'>
                                <span class='{confidence_class}'>{confidence:.0f}% confidence</span>
                            </div>
                        </div>
                    """, unsafe_allow_html=True)
                    
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        if st.button("Use Reply", key=f"use_{row['Chat ID']}"):
                            handle_use_reply(row['Chat ID'], row['AI Reply'])
                    with col2:
                        if st.button("Edit", key=f"edit_{row['Chat ID']}"):
                            handle_edit_reply(row['Chat ID'], row['AI Reply'])
                    with col3:
                        if st.button("Skip", key=f"skip_{row['Chat ID']}"):
                            handle_skip_suggestion(row['Chat ID'])
            
            if len(suggestions) > 0:
                if st.button("View all AI suggestions", key="view_all_suggestions"):
                    st.session_state.page = "🤖 AI Suggestions"
    
    elif st.session_state.page == "📩 Unreplied Messages":
        st.subheader("📩 All Unreplied Messages")
        unreplied = df[df['Needs Followup']].sort_values('Last Unread Message Date', ascending=False)
        for _, row in unreplied.iterrows():
            time_ago = format_time_ago(row['Last Unread Message Date'])
            st.markdown(f"""
                <div class='message-card needs-reply'>
                    <strong>{row['Chat Name']}</strong>
                    <p>{row['Last Message Text']}</p>
                    <small>Needs Reply • {row['Members'] if 'Members' in row else 'Unknown'} members • {time_ago}</small>
                </div>
            """, unsafe_allow_html=True)
    
    elif st.session_state.page == "👥 Groups":
        st.subheader("👥 Group Activity Overview")
        
        # Add group activity filters
        col1, col2 = st.columns(2)
        with col1:
            activity_filter = st.selectbox(
                "Activity Filter",
                ["All Groups", "Active (24h)", "Active (7d)", "Inactive (>7d)"]
            )
        with col2:
            sort_by = st.selectbox(
                "Sort By",
                ["Last Activity", "Unread Count", "Urgency Score"]
            )
        
        # Filter and sort groups
        groups = df[df['Is Group']].copy()
        
        if activity_filter == "Active (24h)":
            groups = groups[groups['Last Unread Message Date'] > pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=1)]
        elif activity_filter == "Active (7d)":
            groups = groups[groups['Last Unread Message Date'] > pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=7)]
        elif activity_filter == "Inactive (>7d)":
            groups = groups[groups['Last Unread Message Date'] <= pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=7)]
        
        if sort_by == "Last Activity":
            groups = groups.sort_values('Last Unread Message Date', ascending=False)
        elif sort_by == "Unread Count":
            groups = groups.sort_values('Unread Count', ascending=False)
        else:
            groups = groups.sort_values('Urgency Score', ascending=False)
        
        # Display groups with enhanced information
        for _, row in groups.iterrows():
            time_ago = format_time_ago(row['Last Unread Message Date'])
            urgency_class = 'confidence-high' if row['Urgency Score'] >= 7 else 'confidence-medium' if row['Urgency Score'] >= 4 else 'confidence-low'
            
            st.markdown(f"""
                <div class='message-card'>
                    <div style='display: flex; justify-content: space-between; align-items: start;'>
                        <div>
                            <strong>{row['Chat Name']}</strong>
                            <p>
                                🔔 {row['Unread Count']} unread messages<br>
                                👤 Last message by: {row['Last Sender Name'] or row['Last Sender Username'] or 'Unknown'}<br>
                                💬 "{row['Last Message Text'][:100]}..."
                            </p>
                        </div>
                        <div style='text-align: right;'>
                            <span class='{urgency_class}'>Priority: {row['Urgency Score']}/10</span>
                        </div>
                    </div>
                    <small>
                        Last activity: {time_ago} • 
                        Message type: {row['Last Message Type']} • 
                        Language: {row['Language']}
                    </small>
                </div>
            """, unsafe_allow_html=True)
        
        if len(groups) == 0:
            st.info(f"No groups found matching the filter: {activity_filter}")
    
    elif st.session_state.page == "🤖 AI Suggestions":
        st.subheader("🤖 All AI Suggestions")
        suggestions = df[
            (df['AI Reply'].notna()) & 
            (~df['Chat ID'].isin(st.session_state.skipped_suggestions))
        ].sort_values('Urgency Score', ascending=False)
        
        for _, row in suggestions.iterrows():
            confidence = row['Urgency Score'] * 10
            confidence_class = 'confidence-high' if confidence >= 85 else 'confidence-medium' if confidence >= 70 else 'confidence-low'
            
            with st.container():
                st.markdown(f"""
                    <div class='suggestion-card'>
                        <strong>{row['Chat Name']}</strong>
                        <p>{row['Last Message Text']}</p>
                        <p><em>Suggested Reply:</em><br>{row['AI Reply']}</p>
                        <div style='display: flex; justify-content: space-between; align-items: center;'>
                            <span class='{confidence_class}'>{confidence:.0f}% confidence</span>
                        </div>
                    </div>
                """, unsafe_allow_html=True)
                
                col1, col2, col3 = st.columns(3)
                with col1:
                    if st.button("Use Reply", key=f"use_{row['Chat ID']}_full"):
                        handle_use_reply(row['Chat ID'], row['AI Reply'])
                with col2:
                    if st.button("Edit", key=f"edit_{row['Chat ID']}_full"):
                        handle_edit_reply(row['Chat ID'], row['AI Reply'])
                with col3:
                    if st.button("Skip", key=f"skip_{row['Chat ID']}_full"):
                        handle_skip_suggestion(row['Chat ID'])
    
    elif st.session_state.page == "📈 Database Analysis":
        st.subheader("📈 Message Analytics")
        
        # Message Type Distribution
        message_types = df['Last Message Type'].value_counts()
        st.bar_chart(message_types)
        
        # Urgency Distribution
        st.subheader("Urgency Score Distribution")
        urgency_hist = pd.DataFrame(df['Urgency Score'].value_counts().sort_index())
        st.bar_chart(urgency_hist)
        
        # Group vs Private
        st.subheader("Group vs Private Chats")
        chat_types = df['Is Group'].value_counts()
        st.bar_chart(pd.DataFrame({
            'Chat Type': ['Group Chats', 'Private Chats'],
            'Count': [chat_types.get(True, 0), chat_types.get(False, 0)]
        }).set_index('Chat Type'))
    
    elif st.session_state.page == "⚙️ Settings":
        st.subheader("⚙️ Settings")
        
        st.write("### Export Options")
        export_format = st.selectbox("Export Format", ["CSV", "Excel"])
        if st.button("Export Data"):
            if export_format == "CSV":
                export_data(df)
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"telegram_export_{timestamp}.xlsx"
                df.to_excel(filename, index=False)
                st.toast(f"Data exported to {filename}", icon="📤")
        
        st.write("### Display Preferences")
        st.number_input("Messages to fetch per chat", min_value=5, max_value=100, value=10)
        st.checkbox("Dark mode", value=False)
        st.checkbox("Show message timestamps", value=True)
        
        if st.button("Clear Cache"):
            st.cache_data.clear()
            st.toast("Cache cleared", icon="🗑️")
            st.rerun()
