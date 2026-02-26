""" Configuration for Lark Project Due Date Tracker Bot
All settings loaded from environment variables (GitHub Secrets / Railway)
"""

import os

# =============================================================================
# LARK APP CREDENTIALS
# =============================================================================
LARK_APP_ID     = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_BASE_URL   = os.environ.get("LARK_BASE_URL", "https://open.larksuite.com")

# =============================================================================
# LARK GROUP CHATS
# Hannah and Lucy each get their own warning channel.
# The chatbot replies to whatever channel the question came from.
# =============================================================================
LARK_CHAT_ID_HANNAH = os.environ.get("LARK_CHAT_ID_HANNAH", "")
LARK_CHAT_ID_LUCY   = os.environ.get("LARK_CHAT_ID_LUCY", "")

# =============================================================================
# LARK BASE APP TOKEN
# From your Base URL: https://xxx.larksuite.com/base/<APP_TOKEN>
# All tables/boards inside are discovered automatically — no table IDs needed.
# =============================================================================
LARK_BASE_APP_TOKEN = os.environ.get("LARK_BASE_APP_TOKEN", "")

# =============================================================================
# GEMINI AI
# =============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# =============================================================================
# LARK WEBHOOK VERIFICATION TOKEN
# =============================================================================
LARK_VERIFICATION_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "")

# =============================================================================
# FIELD NAMES (must match exactly as they appear in your Lark Base columns)
# =============================================================================
FIELD_ORDER_NUM   = "Order #"
FIELD_ORDER_DATE  = "Order Date"
FIELD_DUE_DATE    = "Due Date"
FIELD_STATUS      = "Status"
FIELD_DESCRIPTION = "Description"
FIELD_ADDRESS     = "Address"
FIELD_QTY_ORDERED = "Quantity Ordered"

# =============================================================================
# BOT SETTINGS
# =============================================================================
# Status that means a project is fully done — skip it
DONE_STATUS = "Shipped"

# Warning thresholds in days before due date
WARNING_DAYS = [21, 14, 7]  # 3 weeks, 2 weeks, 1 week

# Labels for each threshold (used in the bot message)
WARNING_LABELS = {
    21: "3 weeks",
    14: "2 weeks",
    7:  "1 week",
}

# Mapping of table name keywords to chat IDs for warnings
# If a table name contains "hannah" (case-insensitive) → Hannah's chat
# If a table name contains "lucy"   (case-insensitive) → Lucy's chat
# Otherwise → send to both
CHAT_ROUTING = {
    "hannah": LARK_CHAT_ID_HANNAH,
    "lucy":   LARK_CHAT_ID_LUCY,
}
