from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
import tomllib

REJOIN_SECONDS = 300
HOUR = 3600


@dataclass(frozen=True)
class Config:
    guild_id: int
    database: Path
    dry_run: bool
    auto_enroll_existing: bool
    support: int
    graduate: int
    operators: frozenset[int]
    text_include: frozenset[int]
    text_exclude: frozenset[int]
    voice_include: frozenset[int]
    voice_exclude: frozenset[int]

    @classmethod
    def load(cls, path: Path):
        with path.open("rb") as file:
            data = tomllib.load(file)

        def snowflake(value):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError("設定ファイルの ID を正の整数で指定してください")
            return value

        def ids(table, key):
            return frozenset(snowflake(value) for value in table.get(key, []))

        roles = data["roles"]
        channels = data["channels"]
        support = snowflake(roles["support"])
        graduate = snowflake(roles["graduate"])
        guild_id = snowflake(data["guild_id"])
        operators = ids(roles, "operators")
        if len({support, graduate, guild_id}) != 3 or operators & {support, graduate, guild_id}:
            raise ValueError("管理対象ロールと運営ロールには別々の ID が必要です")
        introductions = ids(channels, "introductions")
        if not introductions:
            raise ValueError("自己紹介チャンネルの ID を設定してください")
        dry_run = data.get("dry_run", True)
        auto_enroll = data.get("auto_enroll_existing", False)
        if type(dry_run) is not bool or type(auto_enroll) is not bool:
            raise ValueError("dry_run と auto_enroll_existing は true / false で指定してください")
        return cls(
            guild_id, path.parent / data.get("database", "data/tabibito.sqlite3"),
            dry_run, auto_enroll, support, graduate, operators,
            ids(channels, "text_include"), ids(channels, "text_exclude") | introductions,
            ids(channels, "voice_include"), ids(channels, "voice_exclude"),
        )


def included(channel_ids, allow, deny):
    return not (channel_ids & deny) and (not allow or bool(channel_ids & allow))


@dataclass(frozen=True)
class Activity:
    posts: int = 0
    visits: int = 0
    seconds: float = 0


def role_action(activity, *, graduate, support, eligible, new=False, requested=False):
    if graduate:
        return "remove_support" if support else None
    if requested or activity.visits >= 30 or activity.seconds >= 72 * HOUR:
        return "graduate"
    if eligible and not support and (
        new or activity.posts == 0 or activity.visits < 5 or activity.seconds < 24 * HOUR
    ):
        return "add_support"
    return None


class Store:
    def __init__(self, path, guild_id):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                user_id INTEGER PRIMARY KEY, posts INTEGER NOT NULL DEFAULT 0,
                visits INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0,
                last_exit REAL
            );
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS arrivals (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS pending (
                user_id INTEGER PRIMARY KEY, actor INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, user_id INTEGER NOT NULL,
                actor INTEGER, action TEXT NOT NULL, detail TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO metadata VALUES (?, ?)", [
                ("guild_id", str(guild_id)), ("installed_at", str(time.time())),
            ])
        if int(self.metadata("guild_id")) != guild_id:
            self.db.close()
            raise ValueError("SQLite ファイルは別の Discord サーバーに属しています")
        self.installed_at = float(self.metadata("installed_at"))

    def metadata(self, key):
        return self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]

    def ensure(self, user):
        self.db.execute("INSERT OR IGNORE INTO activity(user_id) VALUES (?)", (user,))

    def activity(self, user):
        row = self.db.execute(
            "SELECT posts, visits, seconds FROM activity WHERE user_id=?", (user,)
        ).fetchone()
        return Activity(*row) if row else Activity()

    def post(self, user, message):
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO messages VALUES (?)", (message,))
            if inserted.rowcount:
                self.ensure(user)
                self.db.execute("UPDATE activity SET posts=posts+1 WHERE user_id=?", (user,))

    def enter(self, user, now):
        with self.db:
            self.ensure(user)
            last_exit = self.db.execute(
                "SELECT last_exit FROM activity WHERE user_id=?", (user,)
            ).fetchone()[0]
            same_visit = last_exit is not None and 0 <= now - last_exit <= REJOIN_SECONDS
            self.db.execute(
                "UPDATE activity SET visits=visits+?, last_exit=NULL WHERE user_id=?",
                (int(not same_visit), user),
            )

    def checkpoint(self, increments, *, exit_user=None, at=None):
        with self.db:
            self.db.executemany(
                "UPDATE activity SET seconds=seconds+? WHERE user_id=?", increments
            )
            if exit_user is not None:
                self.db.execute(
                    "UPDATE activity SET last_exit=? WHERE user_id=?", (at, exit_user)
                )

    def clear_exit(self, user):
        with self.db:
            self.ensure(user)
            self.db.execute("UPDATE activity SET last_exit=NULL WHERE user_id=?", (user,))

    def arrival(self, user):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO arrivals VALUES (?)", (user,))

    def arrived(self, user):
        return self.db.execute(
            "SELECT 1 FROM arrivals WHERE user_id=?", (user,)
        ).fetchone() is not None

    def clear_arrival(self, user):
        with self.db:
            self.db.execute("DELETE FROM arrivals WHERE user_id=?", (user,))

    def request(self, user, actor):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO pending VALUES (?, ?)", (user, actor))

    def requester(self, user):
        row = self.db.execute("SELECT actor FROM pending WHERE user_id=?", (user,)).fetchone()
        return row[0] if row else None

    def complete(self, user):
        with self.db:
            self.db.execute("DELETE FROM pending WHERE user_id=?", (user,))

    def _audit(self, user, action, detail, actor):
        self.db.execute(
            "INSERT INTO audit(at,user_id,actor,action,detail) VALUES (?,?,?,?,?)",
            (time.time(), user, actor, action, detail),
        )

    def audit(self, user, action, detail, actor=None):
        with self.db:
            self._audit(user, action, detail, actor)

    def history(self, user):
        return self.db.execute(
            "SELECT at, actor, action, detail FROM audit WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (user,),
        ).fetchall()

    def correct(self, user, actor, *, posts=None, visits=None, seconds=None):
        previous = self.activity(user)
        updated = Activity(
            previous.posts if posts is None else posts,
            previous.visits if visits is None else visits,
            previous.seconds if seconds is None else seconds,
        )
        with self.db:
            self.ensure(user)
            self.db.execute(
                "UPDATE activity SET posts=?, visits=?, seconds=? WHERE user_id=?",
                (updated.posts, updated.visits, updated.seconds, user),
            )
            self._audit(user, "記録修正", f"{previous} → {updated}", actor)


class VoiceTracker:
    def __init__(self, store):
        self.store = store
        self.active = {}

    def reset(self, occupants, now):
        self.active.clear()
        for user in occupants:
            self.store.clear_exit(user)
            self.active[user] = now

    def change(self, user, included_before, included_after, now, wall):
        if included_after and user not in self.active:
            if included_before:
                self.store.clear_exit(user)
            else:
                self.store.enter(user, wall)
            self.active[user] = now
        elif not included_after and user in self.active:
            self.store.checkpoint(
                [(max(0, now - self.active[user]), user)], exit_user=user, at=wall
            )
            del self.active[user]

    def tick(self, now, user=None):
        users = list(self.active) if user is None else [user] if user in self.active else []
        self.store.checkpoint([(max(0, now - self.active[key]), key) for key in users])
        for key in users:
            self.active[key] = now
