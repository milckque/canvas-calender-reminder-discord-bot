import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, timezone, timedelta
import os

import pytz
from dotenv import load_dotenv

from database import Database, ALL_REMINDER_WINDOWS, DEFAULT_TIMEZONE
from canvas import CanvasCalendar
from reminders import ReminderScheduler, get_tz, fmt_due

load_dotenv()

os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True

bot       = commands.Bot(command_prefix="!", intents=intents, help_command=None)
db        = Database("data/data.db")
canvas    = CanvasCalendar()
scheduler = ReminderScheduler(db, canvas)

WINDOW_LABELS = {"7d": "1 week before", "3d": "3 days before", "1d": "1 day before", "0d": "Day of"}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _require_user(ctx) -> dict | None:
    """DM an error and return None if the user hasn't set up their calendar."""
    user = await db.get_user(ctx.author.id)
    if not user:
        await ctx.author.send(
            "❌ You haven't linked your Canvas calendar yet.\n"
            "Run `!setup <ical-url>` to get started, or `!help` for instructions."
        )
    return user


async def _fetch_upcoming(user: dict, days: int = 0) -> list[dict] | None:
    """Return sorted upcoming assignments, optionally filtered to `days` window (0 = all)."""
    assignments = await canvas.fetch_assignments(user["ical_url"])
    if assignments is None:
        return None
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days) if days else None
    return sorted(
        [
            a for a in assignments
            if a["due"] > now and (cutoff is None or a["due"] <= cutoff)
        ],
        key=lambda x: x["due"],
    )


# ── Bot lifecycle ──────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await db.init()
    check_reminders.start()
    weekly_digest.start()
    log.info("Reminder and digest loops started.")


# ── Commands ───────────────────────────────────────────────────────────────────

@bot.command(name="setup")
async def setup(ctx, ical_url: str = None):
    """Register or update your Canvas iCal URL."""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    if not ical_url:
        await ctx.author.send(
            "❌ Please provide your Canvas iCal URL.\n\n"
            "**How to find it:**\n"
            "1. Log in to Canvas and open the **Calendar**\n"
            "2. Scroll to the bottom-right and click **Calendar Feed**\n"
            "3. Copy the URL and run:\n"
            "```\n!setup <paste-url-here>\n```"
        )
        return

    await ctx.author.send("⏳ Validating your Canvas calendar URL…")
    assignments = await canvas.fetch_assignments(ical_url)
    if assignments is None:
        await ctx.author.send(
            "❌ Couldn't fetch your Canvas calendar. Please check the URL and try again."
        )
        return

    await db.upsert_user(ctx.author.id, ical_url)
    upcoming = [a for a in assignments if a["due"] > datetime.now(timezone.utc)]

    await ctx.author.send(
        f"✅ **Calendar linked!**\n\n"
        f"Found **{len(assignments)}** total assignments, **{len(upcoming)}** upcoming.\n\n"
        f"Default reminders: 1 week · 3 days · 1 day · day-of (Melbourne time)\n"
        f"Customise with `!reminders` · Change timezone with `!timezone`\n\n"
        f"Run `!assignments` to see your upcoming work, or `!help` for all commands."
    )
    log.info(f"User {ctx.author.id} registered ({len(assignments)} assignments).")


@bot.command(name="assignments")
async def assignments_cmd(ctx):
    """List upcoming assignments."""
    user = await _require_user(ctx)
    if not user:
        return

    upcoming = await _fetch_upcoming(user)
    if upcoming is None:
        await ctx.author.send("❌ Couldn't fetch your calendar right now. Try again later.")
        return

    tz = get_tz(user)
    await ctx.author.send(await _format_assignment_list(
        ctx.author.id, upcoming[:15], tz,
        title="📚 Your Upcoming Assignments",
        footer="Run `!done` to mark an assignment as completed."
    ))


@bot.command(name="today")
async def today_cmd(ctx):
    """Show assignments due today or tomorrow."""
    user = await _require_user(ctx)
    if not user:
        return

    upcoming = await _fetch_upcoming(user, days=2)
    if upcoming is None:
        await ctx.author.send("❌ Couldn't fetch your calendar right now. Try again later.")
        return

    tz       = get_tz(user)
    now_local = datetime.now(tz)

    # Filter to assignments due before end of tomorrow in the user's timezone
    tomorrow_end = (now_local + timedelta(days=1)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    due_soon = [a for a in upcoming if a["due"].astimezone(tz) <= tomorrow_end]

    if not due_soon:
        await ctx.author.send("✅ Nothing due today or tomorrow — you're clear!")
        return

    await ctx.author.send(await _format_assignment_list(
        ctx.author.id, due_soon, tz,
        title="🔴 Due Today / Tomorrow",
    ))


@bot.command(name="upcoming")
async def upcoming_cmd(ctx, days: int = 7):
    """Show assignments due within the next N days (default: 7).

    Usage:  !upcoming       — next 7 days
            !upcoming 14    — next 14 days
            !upcoming 30    — next 30 days
    """
    if days < 1 or days > 90:
        await ctx.author.send("❌ Please specify between 1 and 90 days. E.g. `!upcoming 14`")
        return

    user = await _require_user(ctx)
    if not user:
        return

    upcoming = await _fetch_upcoming(user, days=days)
    if upcoming is None:
        await ctx.author.send("❌ Couldn't fetch your calendar right now. Try again later.")
        return

    tz = get_tz(user)

    if not upcoming:
        await ctx.author.send(f"🎉 Nothing due in the next {days} days — enjoy!")
        return

    await ctx.author.send(await _format_assignment_list(
        ctx.author.id, upcoming[:20], tz,
        title=f"📅 Due in the Next {days} Days",
        footer="Run `!done` to mark an assignment as completed."
    ))


@bot.command(name="done")
async def done_cmd(ctx, number: int = None):
    """Mark an assignment as completed — stops all reminders for it."""
    user = await _require_user(ctx)
    if not user:
        return

    upcoming = await _fetch_upcoming(user)
    if upcoming is None:
        await ctx.author.send("❌ Couldn't fetch your calendar right now. Try again later.")
        return

    upcoming = upcoming[:15]
    if not upcoming:
        await ctx.author.send("🎉 No upcoming assignments to mark as done!")
        return

    tz = get_tz(user)

    if number is None:
        await ctx.author.send(await _format_assignment_list(
            ctx.author.id, upcoming, tz,
            title="✅ Mark an Assignment as Done",
            footer="Reply with `!done <number>` to silence reminders for that assignment."
        ))
        return

    if not (1 <= number <= len(upcoming)):
        await ctx.author.send(f"❌ Pick a number between 1 and {len(upcoming)}. Run `!done` to see the list.")
        return

    assignment = upcoming[number - 1]
    if await db.is_completed(ctx.author.id, assignment["uid"]):
        await ctx.author.send(
            f"✅ **{assignment['title']}** is already marked as completed.\n"
            f"Run `!uncomplete` to restore reminders."
        )
        return

    await db.mark_completed(ctx.author.id, assignment["uid"], assignment["title"])
    await ctx.author.send(
        f"✅ **{assignment['title']}** marked as completed!\n"
        f"Due: {fmt_due(assignment['due'], tz)}\n\n"
        f"No more reminders for this one. Run `!uncomplete` to undo."
    )
    log.info(f"User {ctx.author.id} completed '{assignment['title']}'")


@bot.command(name="uncomplete")
async def uncomplete_cmd(ctx, number: int = None):
    """Restore reminders for a completed assignment."""
    user = await _require_user(ctx)
    if not user:
        return

    completed = await db.get_completed(ctx.author.id)
    if not completed:
        await ctx.author.send("You have no completed assignments to restore.")
        return

    if number is None:
        lines = ["↩️ **Completed Assignments**\n",
                 "Run `!uncomplete <number>` to restore reminders:\n"]
        for i, c in enumerate(completed, 1):
            lines.append(f"`{i}.` ~~{c['title']}~~")
        await ctx.author.send("\n".join(lines))
        return

    if not (1 <= number <= len(completed)):
        await ctx.author.send(f"❌ Pick a number between 1 and {len(completed)}.")
        return

    entry = completed[number - 1]
    await db.unmark_completed(ctx.author.id, entry["assignment_uid"])
    await ctx.author.send(f"↩️ **{entry['title']}** restored — reminders will resume.")
    log.info(f"User {ctx.author.id} uncompleted '{entry['title']}'")


@bot.command(name="reminders")
async def reminders_cmd(ctx, *args):
    """View or customise which reminder windows you receive.

    Usage:
      !reminders                  — show your current settings
      !reminders 7d 1d 0d         — receive only these windows
      !reminders all              — turn all windows back on
    """
    user = await _require_user(ctx)
    if not user:
        return

    current = db.parse_reminder_windows(user)

    # No args — show current settings
    if not args:
        lines = ["🔔 **Your Reminder Windows**\n"]
        for key in ALL_REMINDER_WINDOWS:
            status = "✅" if key in current else "❌"
            lines.append(f"{status} `{key}` — {WINDOW_LABELS[key]}")
        lines.append("\nTo change: `!reminders 7d 1d 0d` (space-separated)\nTo reset: `!reminders all`")
        await ctx.author.send("\n".join(lines))
        return

    # Special: reset to all
    if len(args) == 1 and args[0].lower() == "all":
        await db.set_reminder_windows(ctx.author.id, ALL_REMINDER_WINDOWS)
        await ctx.author.send("✅ All reminder windows re-enabled: 1 week · 3 days · 1 day · day-of")
        return

    # Parse the supplied keys
    valid   = [a.lower() for a in args if a.lower() in ALL_REMINDER_WINDOWS]
    invalid = [a for a in args if a.lower() not in ALL_REMINDER_WINDOWS]

    if invalid:
        await ctx.author.send(
            f"❌ Unknown window(s): `{'`, `'.join(invalid)}`\n"
            f"Valid options: `7d` `3d` `1d` `0d`"
        )
        return

    if not valid:
        await ctx.author.send("❌ You must keep at least one reminder window enabled.")
        return

    await db.set_reminder_windows(ctx.author.id, valid)
    labels = " · ".join(WINDOW_LABELS[k] for k in ALL_REMINDER_WINDOWS if k in valid)
    await ctx.author.send(f"✅ Reminder windows updated: {labels}")
    log.info(f"User {ctx.author.id} set reminder windows to {valid}")


@bot.command(name="timezone")
async def timezone_cmd(ctx, tz_str: str = None):
    """View or set your timezone for reminder times.

    Usage:
      !timezone                         — show your current timezone
      !timezone Australia/Melbourne     — set to Melbourne
      !timezone America/New_York        — set to New York
      !timezone UTC                     — set to UTC

    Uses IANA timezone names: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
    """
    user = await _require_user(ctx)
    if not user:
        return

    if not tz_str:
        current = user.get("timezone") or DEFAULT_TIMEZONE
        now_local = datetime.now(pytz.timezone(current))
        await ctx.author.send(
            f"🕐 Your timezone is set to **{current}**\n"
            f"Current local time: **{now_local.strftime('%I:%M %p, %a %d %b')}**\n\n"
            f"To change it: `!timezone <tz-name>` (e.g. `!timezone America/New_York`)"
        )
        return

    try:
        tz = pytz.timezone(tz_str)
    except pytz.UnknownTimeZoneError:
        await ctx.author.send(
            f"❌ `{tz_str}` isn't a recognised timezone.\n"
            f"Use an IANA name like `Australia/Melbourne`, `America/New_York`, or `UTC`.\n"
            f"Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>"
        )
        return

    await db.set_timezone(ctx.author.id, tz_str)
    now_local = datetime.now(tz)
    await ctx.author.send(
        f"✅ Timezone updated to **{tz_str}**\n"
        f"Your local time is now: **{now_local.strftime('%I:%M %p, %a %d %b')}**\n"
        f"All reminder times will now display in this timezone."
    )
    log.info(f"User {ctx.author.id} set timezone to {tz_str}")


@bot.command(name="test")
async def test_cmd(ctx):
    """Send yourself a test reminder to verify the bot is set up correctly."""
    user = await _require_user(ctx)
    if not user:
        return

    await ctx.author.send("⏳ Sending test reminder…")
    try:
        await scheduler.send_test(ctx.author, user)
    except discord.Forbidden:
        await ctx.author.send("❌ Couldn't send a DM. Make sure your DMs are open.")


@bot.command(name="remove")
async def remove(ctx):
    """Unlink your Canvas calendar and stop all reminders."""
    await db.delete_user(ctx.author.id)
    await ctx.author.send(
        "🗑️ Calendar unlinked and all reminders stopped.\n"
        "Run `!setup <ical-url>` any time to reconnect."
    )
    log.info(f"User {ctx.author.id} removed their calendar.")


@bot.command(name="status")
async def status(ctx):
    """Check your current setup."""
    user = await _require_user(ctx)
    if not user:
        return

    tz_name          = user.get("timezone") or DEFAULT_TIMEZONE
    active_windows   = db.parse_reminder_windows(user)
    reminders_sent   = await db.count_reminders(ctx.author.id)
    completed        = await db.get_completed(ctx.author.id)
    window_str       = " · ".join(WINDOW_LABELS[k] for k in ALL_REMINDER_WINDOWS if k in active_windows)

    await ctx.author.send(
        f"✅ **Your Canvas Bot Status**\n\n"
        f"🕐 Timezone: **{tz_name}**\n"
        f"🔔 Active reminders: **{window_str}**\n"
        f"📬 Reminders sent: **{reminders_sent}**\n"
        f"✅ Assignments marked done: **{len(completed)}**\n\n"
        f"`!reminders` to change windows · `!timezone` to change timezone"
    )


@bot.command(name="help")
async def help_cmd(ctx):
    """Show all bot commands."""
    embed = discord.Embed(
        title="📚 Canvas Reminder Bot",
        description=(
            "Tracks your Canvas calendar and DMs you before assignments are due.\n"
            "All responses are sent as **DMs** to keep your channels tidy."
        ),
        colour=0x5865F2,
    )

    embed.add_field(
        name="⚙️ Setup",
        value=(
            "`!setup <url>` — Link your Canvas iCal calendar\n"
            "`!timezone` — View or set your timezone (default: Melbourne)\n"
            "`!reminders` — Choose which reminder windows you receive\n"
            "`!status` — View your current settings\n"
            "`!remove` — Unlink calendar and stop all reminders"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Viewing Assignments",
        value=(
            "`!assignments` — All upcoming assignments\n"
            "`!today` — Due today or tomorrow\n"
            "`!upcoming <days>` — Due within N days (e.g. `!upcoming 14`)"
        ),
        inline=False,
    )
    embed.add_field(
        name="✅ Completing Assignments",
        value=(
            "`!done` — Show list to mark assignments complete\n"
            "`!done <number>` — Mark done (stops reminders)\n"
            "`!uncomplete` — Show completed assignments\n"
            "`!uncomplete <number>` — Restore reminders"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔔 Automatic Reminders",
        value=(
            "Reminders are sent automatically via DM at:\n"
            "📅 1 week before · 📆 3 days before · ⏰ 1 day before · 🔴 Day of\n"
            "🗓️ **Weekly digest** every Monday morning with your week's assignments\n"
            "Customise with `!reminders`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Testing",
        value="`!test` — Send a test reminder to confirm your setup is working",
        inline=False,
    )
    embed.add_field(
        name="🔗 Finding your Canvas iCal URL",
        value=(
            "1. Open Canvas → **Calendar**\n"
            "2. Click **Calendar Feed** (bottom-right)\n"
            "3. Copy the `.ics` URL and run `!setup <url>`\n"
            "*(Your URL is deleted from chat immediately)*"
        ),
        inline=False,
    )

    embed.set_footer(text="Canvas Reminder Bot • !help to show this again")
    await ctx.author.send(embed=embed)
    if ctx.guild:
        try:
            await ctx.message.add_reaction("📬")
        except discord.Forbidden:
            pass


# ── Background tasks ───────────────────────────────────────────────────────────

@tasks.loop(minutes=30)
async def check_reminders():
    log.info("Running reminder check…")
    try:
        await scheduler.run(bot)
    except Exception as e:
        log.error(f"Error in reminder loop: {e}", exc_info=True)


@tasks.loop(minutes=30)
async def weekly_digest():
    try:
        await scheduler.run_weekly_digest(bot)
    except Exception as e:
        log.error(f"Error in weekly digest loop: {e}", exc_info=True)


@check_reminders.before_loop
@weekly_digest.before_loop
async def before_loops():
    await bot.wait_until_ready()


# ── Shared formatting helper ───────────────────────────────────────────────────

async def _format_assignment_list(
    discord_id: int,
    assignments: list[dict],
    tz,
    title: str,
    footer: str = "",
) -> str:
    now = datetime.now(timezone.utc)
    lines = [f"**{title}**\n"]
    for i, a in enumerate(assignments, 1):
        done  = await db.is_completed(discord_id, a["uid"])
        delta = a["due"] - now
        days  = delta.days

        if days == 0:
            when = "**due TODAY** 🔴"
        elif days == 1:
            when = "due **tomorrow** 🟠"
        else:
            when = f"due in **{days} days**"

        due_str = fmt_due(a["due"], tz)
        strike_open  = "~~" if done else ""
        strike_close = "~~" if done else ""
        tick         = " ✅" if done else ""
        lines.append(f"`{i}.`{tick} {strike_open}**{a['title']}**{strike_close}\n    {when} — {due_str}")

    if footer:
        lines.append(f"\n{footer}")
    return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    bot.run(token)