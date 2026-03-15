# 📚 Canvas Reminder Discord Bot

A Discord bot that privately DMs users about upcoming Canvas assignments by tracking their personal Canvas iCal calendar feed.

## Features

- 🔗 Each user links their own Canvas iCal URL — no shared credentials needed
- 🔔 Automatic DM reminders at **1 week**, **3 days**, **1 day**, and **day-of** due dates
- 🚫 No duplicate reminders — each reminder is sent exactly once
- 💾 Lightweight SQLite database — no external DB required
- 📋 `/assignments` command to see upcoming work on demand

---

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → give it a name
3. Go to **Bot** → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - ✅ Message Content Intent
5. Copy your **Bot Token**
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`
7. Use the generated URL to invite the bot to your server

### 2. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and paste your Discord bot token:

```
DISCORD_TOKEN=your-discord-bot-token-here
```

### 4. Run the Bot

```bash
python bot.py
```

---

## User Guide (for Discord users)

### Linking your Canvas calendar

1. Log in to **Canvas** and open the **Calendar**
2. Scroll to the bottom-right and click **Calendar Feed**
3. Copy the full `.ics` URL
4. In Discord, run:
   ```
   !setup https://canvas.youruniversity.edu/feeds/calendars/user_xxxxx.ics
   ```
   *(The bot will delete your message to keep the URL private)*

### Commands

| Command | Description |
|---|---|
| `!setup <url>` | Link your Canvas iCal calendar |
| `!assignments` | List your upcoming assignments (sent as DM) |
| `!status` | Check your setup and reminder count |
| `!remove` | Unlink your calendar and stop reminders |
| `!canvashelp` | Show all commands |

> **Note:** The bot only sends reminders as **DMs**, never in public channels.

---

## How It Works

```
Every 30 minutes:
  For each registered user:
    1. Fetch their Canvas iCal URL
    2. Parse all VEVENT entries
    3. For each upcoming assignment:
       - If due in ~7 days  → send "1 week" reminder (once)
       - If due in ~3 days  → send "3 days" reminder  (once)
       - If due in ~1 day   → send "1 day" reminder   (once)
       - If due today       → send "today" reminder    (once)
    4. Log sent reminders to DB to prevent duplicates
```

---

## Project Structure

```
canvas-discord-bot/
├── bot.py           # Discord bot, commands, background task
├── canvas.py        # iCal fetching and parsing
├── database.py      # SQLite wrapper (users + sent reminders)
├── reminders.py     # Reminder scheduling and DM dispatch
├── requirements.txt
├── .env.example
└── README.md
```

---

## Running as a Service (optional)

To keep the bot running 24/7, use **systemd** on Linux:

```ini
# /etc/systemd/system/canvas-bot.service
[Unit]
Description=Canvas Reminder Discord Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/canvas-discord-bot
ExecStart=/path/to/canvas-discord-bot/venv/bin/python bot.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable canvas-bot
sudo systemctl start canvas-bot
```

Or with **PM2** (Node-based process manager, works with Python too):

```bash
pm2 start bot.py --name canvas-bot --interpreter python3
pm2 save
pm2 startup
```

---

## Notes

- Canvas iCal URLs are **private** — each user's URL is unique to their account
- The bot checks for reminders every **30 minutes**
- Old reminder records are automatically purged after **30 days** to keep the database small
- The bot requires **DMs to be enabled** for reminders to be delivered
