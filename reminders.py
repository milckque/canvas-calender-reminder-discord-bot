import logging
from datetime import datetime, timezone, timedelta

import discord
import pytz

from database import Database, ALL_REMINDER_WINDOWS
from canvas import CanvasCalendar

log = logging.getLogger("reminders")

# Full definition of every possible reminder window
# (label, days_before, reminder_type_key)
ALL_WINDOWS = [
    ("1 week",  7, "7d"),
    ("3 days",  3, "3d"),
    ("1 day",   1, "1d"),
    ("today",   0, "0d"),
]

WINDOW_TOLERANCE_HOURS = 12

EMOJI  = {"7d": "📅", "3d": "📆", "1d": "⏰", "0d": "🔴"}
COLOUR = {"7d": 0x5865F2, "3d": 0xFEE75C, "1d": 0xED4245, "0d": 0xEB459E}


def get_tz(user: dict) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(user.get("timezone") or "Australia/Melbourne")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Australia/Melbourne")


def fmt_due(dt: datetime, tz: pytz.BaseTzInfo) -> str:
    """Format a UTC datetime in the user's local timezone."""
    local = dt.astimezone(tz)
    return local.strftime("%a %d %b, %I:%M %p %Z")


class ReminderScheduler:
    def __init__(self, db: Database, canvas: CanvasCalendar):
        self.db = db
        self.canvas = canvas

    # ── Regular reminder loop ──────────────────────────────────────────────────

    async def run(self, bot: discord.Client):
        """Check all users and dispatch any pending reminders."""
        users = await self.db.get_all_users()
        log.info(f"Checking reminders for {len(users)} user(s)…")

        for user in users:
            discord_id = user["discord_id"]
            assignments = await self.canvas.fetch_assignments(user["ical_url"])
            if assignments is None:
                log.warning(f"Skipping user {discord_id} — could not fetch calendar.")
                continue

            active_windows = self.db.parse_reminder_windows(user)
            now = datetime.now(timezone.utc)

            for assignment in assignments:
                if assignment["due"] < now:
                    continue
                if await self.db.is_completed(discord_id, assignment["uid"]):
                    continue
                await self._check_assignment(bot, discord_id, assignment, now, active_windows, user)

        await self.db.purge_old_reminders(days=30)

    async def _check_assignment(
        self,
        bot: discord.Client,
        discord_id: int,
        assignment: dict,
        now: datetime,
        active_windows: list[str],
        user: dict,
    ):
        due   = assignment["due"]
        days  = (due - now).total_seconds() / 86400

        for label, days_before, rtype in ALL_WINDOWS:
            if rtype not in active_windows:
                continue

            lower = days_before - (WINDOW_TOLERANCE_HOURS / 24)
            upper = days_before + (WINDOW_TOLERANCE_HOURS / 24)
            if not (lower <= days <= upper):
                continue

            if await self.db.has_sent_reminder(discord_id, assignment["uid"], rtype):
                continue

            sent = await self._send_reminder(bot, discord_id, assignment, label, rtype, user)
            if sent:
                await self.db.mark_reminder_sent(discord_id, assignment["uid"], rtype)

    async def _send_reminder(
        self,
        bot: discord.Client,
        discord_id: int,
        assignment: dict,
        label: str,
        rtype: str,
        user: dict,
    ) -> bool:
        try:
            discord_user = await bot.fetch_user(discord_id)
        except discord.NotFound:
            log.warning(f"User {discord_id} not found on Discord.")
            return False

        tz    = get_tz(user)
        embed = self._build_reminder_embed(assignment, label, rtype, tz)

        try:
            await discord_user.send(embed=embed)
            log.info(f"Sent {rtype} reminder to {discord_id} for '{assignment['title']}'")
            return True
        except discord.Forbidden:
            log.warning(f"Cannot DM user {discord_id} (DMs disabled?).")
            return False
        except discord.HTTPException as e:
            log.error(f"Failed to DM user {discord_id}: {e}")
            return False

    # ── Weekly digest ──────────────────────────────────────────────────────────

    async def run_weekly_digest(self, bot: discord.Client):
        """Send Monday morning digest to all users who haven't had one this week."""
        users = await self.db.get_all_users()
        log.info(f"Running weekly digest for {len(users)} user(s)…")

        for user in users:
            discord_id = user["discord_id"]
            tz         = get_tz(user)
            now_local  = datetime.now(tz)

            # Only send on Monday (weekday 0), between 07:00–08:00 local time
            if now_local.weekday() != 0 or not (7 <= now_local.hour < 8):
                continue

            # Avoid sending more than once per week
            last = await self.db.get_last_digest(discord_id)
            if last:
                last_dt = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_dt).days < 6:
                    continue

            assignments = await self.canvas.fetch_assignments(user["ical_url"])
            if assignments is None:
                continue

            now_utc   = datetime.now(timezone.utc)
            week_end  = now_utc + timedelta(days=7)
            due_this_week = sorted(
                [
                    a for a in assignments
                    if now_utc < a["due"] <= week_end
                    and not await self.db.is_completed(discord_id, a["uid"])
                ],
                key=lambda x: x["due"],
            )

            sent = await self._send_digest(bot, discord_id, due_this_week, tz)
            if sent:
                await self.db.mark_digest_sent(discord_id)

    async def _send_digest(
        self,
        bot: discord.Client,
        discord_id: int,
        assignments: list[dict],
        tz: pytz.BaseTzInfo,
    ) -> bool:
        try:
            discord_user = await bot.fetch_user(discord_id)
        except discord.NotFound:
            return False

        embed = self._build_digest_embed(assignments, tz)
        try:
            await discord_user.send(embed=embed)
            log.info(f"Sent weekly digest to {discord_id} ({len(assignments)} assignments)")
            return True
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"Could not send digest to {discord_id}: {e}")
            return False

    # ── Test reminder ──────────────────────────────────────────────────────────

    async def send_test(self, discord_user: discord.User, user: dict):
        """Send a fake reminder DM so the user can verify their setup."""
        tz = get_tz(user)
        fake = {
            "uid":   "test-assignment",
            "title": "Example Assignment (Test)",
            "due":   datetime.now(timezone.utc) + timedelta(days=1),
            "url":   None,
        }
        embed = self._build_reminder_embed(fake, "1 day", "1d", tz)
        embed.set_footer(text="✅ This is a test reminder — your bot is working correctly!")
        await discord_user.send(embed=embed)

    # ── Embed builders ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_reminder_embed(
        assignment: dict, label: str, rtype: str, tz: pytz.BaseTzInfo
    ) -> discord.Embed:
        emoji  = EMOJI.get(rtype, "🔔")
        colour = COLOUR.get(rtype, 0x99AAB5)
        due_str = fmt_due(assignment["due"], tz)

        embed = discord.Embed(
            title=f"{emoji} Assignment Due in {label.title()}",
            description=f"**{assignment['title']}**",
            colour=colour,
        )
        embed.add_field(name="Due", value=due_str, inline=False)
        if assignment.get("url"):
            embed.add_field(
                name="Canvas Link",
                value=f"[Open in Canvas]({assignment['url']})",
                inline=False,
            )
        embed.set_footer(text="Canvas Reminder Bot • !uni assignments to see all upcoming work")
        return embed

    @staticmethod
    def _build_digest_embed(
        assignments: list[dict], tz: pytz.BaseTzInfo
    ) -> discord.Embed:
        embed = discord.Embed(
            title="🗓️ Your Week Ahead",
            colour=0x57F287,
        )

        if not assignments:
            embed.description = "🎉 Nothing due this week — enjoy your time off!"
        else:
            lines = []
            for a in assignments:
                due_str = fmt_due(a["due"], tz)
                lines.append(f"• **{a['title']}**\n  {due_str}")
            embed.description = "\n".join(lines)
            embed.set_footer(
                text=f"{len(assignments)} assignment{'s' if len(assignments) != 1 else ''} due this week • "
                     "!uni done <number> to mark complete"
            )

        return embed