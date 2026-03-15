import aiosqlite
import logging

log = logging.getLogger("database")

DEFAULT_TIMEZONE = "Australia/Melbourne"
ALL_REMINDER_WINDOWS = ["7d", "3d", "1d", "0d"]
DEFAULT_REMINDER_WINDOWS = ",".join(ALL_REMINDER_WINDOWS)


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        """Create tables and migrate existing ones if needed."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id        INTEGER PRIMARY KEY,
                    ical_url          TEXT    NOT NULL,
                    timezone          TEXT    NOT NULL DEFAULT 'Australia/Melbourne',
                    reminder_windows  TEXT    NOT NULL DEFAULT '7d,3d,1d,0d',
                    created_at        TEXT    DEFAULT (datetime('now')),
                    updated_at        TEXT    DEFAULT (datetime('now'))
                )
            """)
            # Migrate existing DBs — safe no-op if columns already exist
            for col, definition in [
                ("timezone",         f"TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'"),
                ("reminder_windows", f"TEXT NOT NULL DEFAULT '{DEFAULT_REMINDER_WINDOWS}'"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                except Exception:
                    pass

            await db.execute("""
                CREATE TABLE IF NOT EXISTS sent_reminders (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id      INTEGER NOT NULL,
                    assignment_uid  TEXT    NOT NULL,
                    reminder_type   TEXT    NOT NULL,
                    sent_at         TEXT    DEFAULT (datetime('now')),
                    UNIQUE(discord_id, assignment_uid, reminder_type)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS completed_assignments (
                    discord_id      INTEGER NOT NULL,
                    assignment_uid  TEXT    NOT NULL,
                    title           TEXT    NOT NULL,
                    completed_at    TEXT    DEFAULT (datetime('now')),
                    PRIMARY KEY (discord_id, assignment_uid)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS digest_log (
                    discord_id  INTEGER PRIMARY KEY,
                    last_sent   TEXT    DEFAULT (datetime('now'))
                )
            """)
            await db.commit()
        log.info("Database initialised.")

    # ── Users ──────────────────────────────────────────────────────────────────

    async def upsert_user(self, discord_id: int, ical_url: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO users (discord_id, ical_url, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(discord_id) DO UPDATE SET
                    ical_url   = excluded.ical_url,
                    updated_at = excluded.updated_at
            """, (discord_id, ical_url))
            await db.commit()

    async def get_user(self, discord_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_all_users(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def delete_user(self, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM users                  WHERE discord_id = ?", (discord_id,))
            await db.execute("DELETE FROM sent_reminders         WHERE discord_id = ?", (discord_id,))
            await db.execute("DELETE FROM completed_assignments  WHERE discord_id = ?", (discord_id,))
            await db.execute("DELETE FROM digest_log             WHERE discord_id = ?", (discord_id,))
            await db.commit()

    # ── User preferences ───────────────────────────────────────────────────────

    async def set_timezone(self, discord_id: int, tz: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET timezone = ?, updated_at = datetime('now') WHERE discord_id = ?",
                (tz, discord_id)
            )
            await db.commit()

    async def set_reminder_windows(self, discord_id: int, windows: list[str]):
        value = ",".join(windows)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET reminder_windows = ?, updated_at = datetime('now') WHERE discord_id = ?",
                (value, discord_id)
            )
            await db.commit()

    def parse_reminder_windows(self, user: dict) -> list[str]:
        """Return list of active window keys e.g. ['7d','1d','0d']."""
        raw = user.get("reminder_windows") or DEFAULT_REMINDER_WINDOWS
        return [w.strip() for w in raw.split(",") if w.strip() in ALL_REMINDER_WINDOWS]

    # ── Sent reminders ─────────────────────────────────────────────────────────

    async def has_sent_reminder(
        self, discord_id: int, assignment_uid: str, reminder_type: str
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("""
                SELECT 1 FROM sent_reminders
                WHERE discord_id = ? AND assignment_uid = ? AND reminder_type = ?
            """, (discord_id, assignment_uid, reminder_type)) as cur:
                return await cur.fetchone() is not None

    async def mark_reminder_sent(
        self, discord_id: int, assignment_uid: str, reminder_type: str
    ):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT OR IGNORE INTO sent_reminders
                    (discord_id, assignment_uid, reminder_type)
                VALUES (?, ?, ?)
            """, (discord_id, assignment_uid, reminder_type))
            await db.commit()

    # ── Completed assignments ──────────────────────────────────────────────────

    async def mark_completed(self, discord_id: int, assignment_uid: str, title: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT OR IGNORE INTO completed_assignments
                    (discord_id, assignment_uid, title)
                VALUES (?, ?, ?)
            """, (discord_id, assignment_uid, title))
            await db.commit()

    async def unmark_completed(self, discord_id: int, assignment_uid: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                DELETE FROM completed_assignments
                WHERE discord_id = ? AND assignment_uid = ?
            """, (discord_id, assignment_uid))
            await db.commit()

    async def is_completed(self, discord_id: int, assignment_uid: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("""
                SELECT 1 FROM completed_assignments
                WHERE discord_id = ? AND assignment_uid = ?
            """, (discord_id, assignment_uid)) as cur:
                return await cur.fetchone() is not None

    async def get_completed(self, discord_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM completed_assignments
                WHERE discord_id = ?
                ORDER BY completed_at DESC
            """, (discord_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # ── Weekly digest ──────────────────────────────────────────────────────────

    async def get_last_digest(self, discord_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT last_sent FROM digest_log WHERE discord_id = ?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def mark_digest_sent(self, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO digest_log (discord_id, last_sent)
                VALUES (?, datetime('now'))
                ON CONFLICT(discord_id) DO UPDATE SET last_sent = datetime('now')
            """, (discord_id,))
            await db.commit()

    # ── Misc ───────────────────────────────────────────────────────────────────

    async def count_reminders(self, discord_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM sent_reminders WHERE discord_id = ?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def purge_old_reminders(self, days: int = 30):
        """Remove reminder records older than `days` days to keep the DB lean."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                DELETE FROM sent_reminders
                WHERE sent_at < datetime('now', ? || ' days')
            """, (f"-{days}",))
            await db.commit()
