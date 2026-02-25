"""
Configuration for Lark Project Due Date Tracker Bot
All settings loaded from environment variables (GitHub Secrets)
"""
import os

# =============================================================================
# LARK APP CREDENTIALS
# =============================================================================
LARK_APP_ID     = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_BASE_URL   = os.environ.get("LARK_BASE_URL", "https://open.larksuite.com")

# =============================================================================
# LARK GROUP CHAT
# =============================================================================
LARK_CHAT_ID = os.environ.get("LARK_CHAT_ID", "")

# =============================================================================
# LARK BASE (app token) + TABLE IDs
# Lark Base app token — from the Base URL:
#   https://xxx.larksuite.com/base/<APP_TOKEN>
# Table/board IDs — comma-separated list of table IDs within that Base
# =============================================================================
LARK_BASE_APP_TOKEN = os.environ.get("LARK_BASE_APP_TOKEN", "")
LARK_BASE_TABLE_IDS = [
    t.strip()
    for t in os.environ.get("LARK_BASE_TABLE_IDS", "").split(",")
    if t.strip()
]

# =============================================================================
# FIELD NAMES (as they appear in Lark Base)
# =============================================================================
FIELD_ORDER_NUM        = "Order #"
FIELD_ORDER_DATE       = "Order Date"
FIELD_DUE_DATE         = "Due Date"
FIELD_STATUS           = "Status"
FIELD_DESCRIPTION      = "Description"
FIELD_ADDRESS          = "Address"
FIELD_TRACKING_NUMBER  = "Tracking Number"
FIELD_CARRIER          = "Carrier"
FIELD_QTY_ORDERED      = "Quantity Ordered"
FIELD_QTY_SHIPPED      = "Quantity Shipped"
FIELD_DATE_SHIPPED     = "Date Shipped"
FIELD_LAST_UPDATED     = "Last Updated"

# =============================================================================
# BOT SETTINGS
# =============================================================================

# Status that means a project is fully done — skip it
DONE_STATUS = "Shipped"

# Warning thresholds in days before due date
WARNING_DAYS = [21, 14, 7]   # 3 weeks, 2 weeks, 1 week

# Labels for each threshold (used in the bot message)
WARNING_LABELS = {
    21: "3 weeks",
    14: "2 weeks",
    7:  "1 week",
}
