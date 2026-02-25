# 📋 HLT Urgent Project Tracker Bot

Automatically reads project records from your Lark Base, checks due dates,
and sends warning notifications to your team group chat at:

- 🟡 **3 weeks** before due date
- 🟠 **2 weeks** before due date
- 🔴 **1 week** before due date

Runs automatically **twice a day at 8am and 8pm** via GitHub Actions.
Projects marked as **"Shipped"** are automatically skipped.

---

## How It Works

1. GitHub Actions triggers at 8am and 8pm
2. Bot reads all records from your Lark Base (all boards/tables)
3. For each project, checks days remaining until Due Date
4. If a project hits the 3-week, 2-week, or 1-week threshold → sends a card to your group chat
5. Message is color-coded by urgency (🔴🟠🟡) and grouped by time window

---

## Bot Message Example

```
📋 HLT Project Due Date Reminder
Wednesday, February 25 2026

🔴 Due in 1 week (2 projects)
• #HLT6131 — belt samples / send swatches
  Due: Wed, Mar 4 2026 (6 days left) | Qty: 3 | Status: Waiting Art

🟠 Due in 2 weeks (1 project)
• #HLT6089 — custom hoodie order
  Due: Wed, Mar 11 2026 (13 days left) | Qty: 12 | Status: In Production

🟡 Due in 3 weeks (1 project)
• #HLT6044 — embroidered patches
  Due: Wed, Mar 18 2026 (20 days left) | Qty: 50 | Status: In Production
```

---

## Setup Guide

### Step 1: Lark App (reuse existing if you have one)
- Go to [Lark Developer Console](https://open.larksuite.com/)
- Use your existing app or create a new one
- Make sure it has **Bitable** read permissions (`bitable:record:read`)
- Copy your **App ID** and **App Secret**

### Step 2: Get Your Base App Token
From your Lark Base URL:
```
https://xxx.larksuite.com/base/AbCdEfGhIjKlMn
                                 ^^^^^^^^^^^^^^
                                 This is your LARK_BASE_APP_TOKEN
```

### Step 3: Get Your Table IDs
Each board/view in your Base has its own Table ID.
- Open your Base → click on a table/board
- The URL will look like: `/base/AbCdEf?table=tblXXXXX&view=vewYYYYY`
- `tblXXXXX` is your Table ID
- If you have multiple boards, separate them with commas: `tbl111,tbl222,tbl333`

### Step 4: Add GitHub Secrets
Go to your repo → **Settings → Secrets and variables → Actions** → add:

| Secret Name | Value |
|---|---|
| `LARK_APP_ID` | Your Lark App ID |
| `LARK_APP_SECRET` | Your Lark App Secret |
| `LARK_BASE_URL` | `https://open.larksuite.com` (or JP: `https://open.jp.larksuite.com`) |
| `LARK_CHAT_ID` | Your group chat ID |
| `LARK_BASE_APP_TOKEN` | Your Base app token (from the URL) |
| `LARK_BASE_TABLE_IDS` | Comma-separated table IDs (one per board) |

### Step 5: Timezone
The workflow runs at `0 13 * * *` (8am EST) and `0 1 * * *` (8pm EST).
If you are in a different timezone, update the cron schedule in `.github/workflows/project_tracker.yml`.

### Step 6: Test It
- Go to the **Actions** tab in this repo
- Click **Project Due Date Tracker**
- Click **Run workflow** to trigger manually
- Check the logs to confirm it's reading your Base correctly

---

## Lark Base Column Names Expected

| Column | Used For |
|---|---|
| Order # | Project identifier shown in message |
| Due Date | Date checked for warning thresholds |
| Status | "Shipped" = skip this project |
| Description | Shown in the message body |
| Quantity Ordered | Shown in the message body |
| Order Date | Read but not shown in warnings |

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Main logic — reads Base, calculates warnings, sends messages |
| `lark_client.py` | Lark API client — auth, Base reads, messaging |
| `config.py` | All settings loaded from environment variables |
| `.github/workflows/project_tracker.yml` | GitHub Actions schedule (8am + 8pm) |
