"""
Хранилище. SQLite для прототипа, PostgreSQL для продакшена —
DDL совместим, меняется только строка подключения и драйвер.
"""
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# По умолчанию база лежит рядом с рецептами, в data/. На Railway это не годится:
# том монтируется пустым и перекрывает каталог целиком, то есть recipes.csv и
# addons.csv исчезли бы вместе с ним. Поэтому том монтируется в отдельный
# каталог, а путь задаётся переменной DB_PATH — см. docs/03-деплой.md, этап 4.
_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = Path(os.environ.get("DB_PATH")
               or (Path(_VOLUME) / "bot.db" if _VOLUME else None)
               or Path(__file__).resolve().parent.parent / "data" / "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    tz          TEXT DEFAULT 'Europe/Moscow'
);

CREATE TABLE IF NOT EXISTS profiles (
    telegram_id   INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
    sex           TEXT NOT NULL,
    age           INTEGER NOT NULL,
    height_cm     INTEGER NOT NULL,
    weight_kg     REAL NOT NULL,
    activity      TEXT NOT NULL,
    goal          TEXT NOT NULL,
    meals_per_day INTEGER DEFAULT 4,
    exclusions    TEXT DEFAULT '[]',
    prefer_tags   TEXT DEFAULT '[]',
    target_kcal   INTEGER,
    protein_g     INTEGER,
    fat_g         INTEGER,
    carb_g        INTEGER,
    calculated_at TEXT
);

-- Счётчик подборов за сутки. Намеренно без ссылки на users: при /delete строка
-- остаётся, иначе лимит обходится удалением профиля и повторным /start.
-- Профиля тут нет, только идентификатор, дата и число.
CREATE TABLE IF NOT EXISTS daily_usage (
    telegram_id INTEGER NOT NULL,
    usage_date  TEXT NOT NULL,
    plans       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, usage_date)
);

-- Кому какие рецепты уже выданы. Нужен, чтобы видеть, кто собирает базу:
-- обычный пользователь упирается в 70% за месяцы, сборщик доходит быстрее.
-- Тоже без ссылки на users — иначе /delete обнуляет картину.
CREATE TABLE IF NOT EXISTS recipe_log (
    telegram_id INTEGER NOT NULL,
    recipe_id   TEXT NOT NULL,
    first_sent  TEXT DEFAULT CURRENT_TIMESTAMP,
    times       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (telegram_id, recipe_id)
);

-- Список покупок на день: сами строки и какие из них отмечены.
CREATE TABLE IF NOT EXISTS shopping (
    telegram_id INTEGER NOT NULL,
    plan_date   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (telegram_id, plan_date)
);

-- Профиль партнёра для ужина на двоих. Отдельной таблицей: это не второй
-- пользователь бота, а параметры человека, который ест то же самое.
CREATE TABLE IF NOT EXISTS partners (
    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
    sex         TEXT NOT NULL,
    age         INTEGER NOT NULL,
    height_cm   INTEGER NOT NULL,
    weight_kg   REAL NOT NULL,
    activity    TEXT NOT NULL,
    goal        TEXT NOT NULL,
    target_kcal INTEGER,
    protein_g   INTEGER,
    fat_g       INTEGER,
    carb_g      INTEGER
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at    TEXT
);

-- Без REFERENCES users: человек может удалить профиль после рассылки, а запись
-- о доставке должна остаться — в ней нет его параметров, только исход.
CREATE TABLE IF NOT EXISTS broadcast_delivery (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    telegram_id  INTEGER NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT DEFAULT '',
    at           TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (broadcast_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    plan_date   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    UNIQUE(telegram_id, plan_date)
);

CREATE TABLE IF NOT EXISTS weight_log (
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    log_date    TEXT NOT NULL,
    weight_kg   REAL NOT NULL,
    PRIMARY KEY (telegram_id, log_date)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(c) -> None:
    """Добавляет колонки, появившиеся после первого запуска. CREATE TABLE IF
    NOT EXISTS существующую таблицу не меняет, поэтому нужен явный ALTER."""
    have = {r["name"] for r in c.execute("PRAGMA table_info(profiles)")}
    if "activity_factor" not in have:
        c.execute("ALTER TABLE profiles ADD COLUMN activity_factor REAL")


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def ensure_user(tg_id: int) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (tg_id,))


def save_profile(tg_id: int, p, t) -> None:
    ensure_user(tg_id)
    with connect() as c:
        c.execute("""
            INSERT INTO profiles (telegram_id, sex, age, height_cm, weight_kg, activity,
                                  goal, meals_per_day, activity_factor,
                                  target_kcal, protein_g, fat_g,
                                  carb_g, calculated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                sex=excluded.sex, age=excluded.age, height_cm=excluded.height_cm,
                weight_kg=excluded.weight_kg, activity=excluded.activity,
                goal=excluded.goal, meals_per_day=excluded.meals_per_day,
                activity_factor=excluded.activity_factor,
                target_kcal=excluded.target_kcal, protein_g=excluded.protein_g,
                fat_g=excluded.fat_g, carb_g=excluded.carb_g,
                calculated_at=CURRENT_TIMESTAMP
        """, (tg_id, p.sex, p.age, p.height_cm, p.weight_kg, p.activity, p.goal,
              p.meals_per_day, p.activity_factor,
              t.kcal, t.protein_g, t.fat_g, t.carb_g))


def get_profile(tg_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM profiles WHERE telegram_id=?", (tg_id,)).fetchone()
    return dict(row) if row else None


def set_exclusions(tg_id: int, items: list[str]) -> None:
    with connect() as c:
        c.execute("UPDATE profiles SET exclusions=? WHERE telegram_id=?",
                  (json.dumps(items, ensure_ascii=False), tg_id))


def set_prefer_tags(tg_id: int, items: list[str]) -> None:
    with connect() as c:
        c.execute("UPDATE profiles SET prefer_tags=? WHERE telegram_id=?",
                  (json.dumps(items, ensure_ascii=False), tg_id))


def save_plan(tg_id: int, plan_date: date, recipe_ids: list[str], text: str,
              quick: bool = False, scales: list[float] | None = None) -> None:
    payload = json.dumps({"recipe_ids": recipe_ids, "text": text, "quick": quick,
                          "scales": scales or [1.0] * len(recipe_ids)},
                         ensure_ascii=False)
    with connect() as c:
        c.execute("""INSERT INTO plans (telegram_id, plan_date, payload) VALUES (?,?,?)
                     ON CONFLICT(telegram_id, plan_date) DO UPDATE SET payload=excluded.payload""",
                  (tg_id, plan_date.isoformat(), payload))


def get_plan(tg_id: int, plan_date: date) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT payload FROM plans WHERE telegram_id=? AND plan_date=?",
                        (tg_id, plan_date.isoformat())).fetchone()
    return json.loads(row["payload"]) if row else None


def plans_today(tg_id: int, day: date) -> int:
    with connect() as c:
        row = c.execute("SELECT plans FROM daily_usage WHERE telegram_id=? AND usage_date=?",
                        (tg_id, day.isoformat())).fetchone()
    return row["plans"] if row else 0


def count_plan(tg_id: int, day: date) -> int:
    """Отмечает один собранный план и возвращает, сколько их стало за сутки."""
    with connect() as c:
        c.execute("""INSERT INTO daily_usage (telegram_id, usage_date, plans)
                     VALUES (?,?,1)
                     ON CONFLICT(telegram_id, usage_date)
                     DO UPDATE SET plans = plans + 1""",
                  (tg_id, day.isoformat()))
        row = c.execute("SELECT plans FROM daily_usage WHERE telegram_id=? AND usage_date=?",
                        (tg_id, day.isoformat())).fetchone()
    return row["plans"]


def save_shopping(tg_id: int, day: date, items: list[str], checked: list[int]) -> None:
    with connect() as c:
        c.execute("""INSERT INTO shopping (telegram_id, plan_date, payload) VALUES (?,?,?)
                     ON CONFLICT(telegram_id, plan_date) DO UPDATE SET payload=excluded.payload""",
                  (tg_id, day.isoformat(),
                   json.dumps({"items": items, "checked": checked}, ensure_ascii=False)))


def get_shopping(tg_id: int, day: date) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT payload FROM shopping WHERE telegram_id=? AND plan_date=?",
                        (tg_id, day.isoformat())).fetchone()
    return json.loads(row["payload"]) if row else None


def save_partner(tg_id: int, p, t) -> None:
    with connect() as c:
        c.execute("""
            INSERT INTO partners (telegram_id, sex, age, height_cm, weight_kg,
                                  activity, goal, target_kcal, protein_g, fat_g, carb_g)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                sex=excluded.sex, age=excluded.age, height_cm=excluded.height_cm,
                weight_kg=excluded.weight_kg, activity=excluded.activity,
                goal=excluded.goal, target_kcal=excluded.target_kcal,
                protein_g=excluded.protein_g, fat_g=excluded.fat_g, carb_g=excluded.carb_g
        """, (tg_id, p.sex, p.age, p.height_cm, p.weight_kg, p.activity, p.goal,
              t.kcal, t.protein_g, t.fat_g, t.carb_g))


def get_partner(tg_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM partners WHERE telegram_id=?", (tg_id,)).fetchone()
    return dict(row) if row else None


def log_recipes(tg_id: int, recipe_ids: list[str]) -> int:
    """Отмечает выданные рецепты, возвращает, сколько разных получил человек."""
    with connect() as c:
        c.executemany("""INSERT INTO recipe_log (telegram_id, recipe_id) VALUES (?,?)
                         ON CONFLICT(telegram_id, recipe_id)
                         DO UPDATE SET times = times + 1""",
                      [(tg_id, rid) for rid in recipe_ids])
        row = c.execute("SELECT COUNT(*) AS n FROM recipe_log WHERE telegram_id=?",
                        (tg_id,)).fetchone()
    return row["n"]


def collectors(limit: int = 15) -> list[dict]:
    """Кто сколько разных рецептов собрал, сверху — самые активные."""
    with connect() as c:
        rows = c.execute("""
            SELECT telegram_id,
                   COUNT(*) AS unique_recipes,
                   SUM(times) AS deliveries,
                   MIN(first_sent) AS started
            FROM recipe_log GROUP BY telegram_id
            ORDER BY unique_recipes DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def accept_plan(tg_id: int, plan_date: date) -> None:
    """План принят: рецепты выданы, замена на сегодня больше не предлагается."""
    with connect() as c:
        row = c.execute("SELECT payload FROM plans WHERE telegram_id=? AND plan_date=?",
                        (tg_id, plan_date.isoformat())).fetchone()
        if not row:
            return
        payload = json.loads(row["payload"])
        payload["accepted"] = True
        c.execute("UPDATE plans SET payload=? WHERE telegram_id=? AND plan_date=?",
                  (json.dumps(payload, ensure_ascii=False), tg_id, plan_date.isoformat()))


def recent_recipe_ids(tg_id: int, days: int = 5) -> set[str]:
    since = (date.today() - timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute("SELECT payload FROM plans WHERE telegram_id=? AND plan_date>=?",
                         (tg_id, since)).fetchall()
    ids: set[str] = set()
    for r in rows:
        ids.update(json.loads(r["payload"]).get("recipe_ids", []))
    return ids


def new_broadcast(text: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO broadcasts(text) VALUES (?)", (text,))
        return cur.lastrowid


def drop_broadcast(bid: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM broadcasts WHERE id=?", (bid,))


def get_broadcast(bid: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM broadcasts WHERE id=?", (bid,)).fetchone()
        return dict(row) if row else None


def last_broadcast() -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM broadcasts ORDER BY id DESC "
                        "LIMIT 1").fetchone()
        return dict(row) if row else None


def mark_broadcast_sent(bid: int) -> None:
    with connect() as c:
        c.execute("UPDATE broadcasts SET sent_at=CURRENT_TIMESTAMP "
                  "WHERE id=? AND sent_at IS NULL", (bid,))


def broadcast_targets(bid: int) -> list[int]:
    """Кому ещё не дошло. Ошибки перебираются заново, заблокировавшие бота —
    нет: их статус не изменится от повторной попытки."""
    with connect() as c:
        rows = c.execute(
            "SELECT u.telegram_id FROM users u "
            "LEFT JOIN broadcast_delivery d "
            "  ON d.broadcast_id=? AND d.telegram_id=u.telegram_id "
            "WHERE d.status IS NULL OR d.status='failed' "
            "ORDER BY u.telegram_id", (bid,)).fetchall()
        return [r["telegram_id"] for r in rows]


def audience() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def mark_delivery(bid: int, tg_id: int, status: str, detail: str = "") -> None:
    with connect() as c:
        c.execute("INSERT INTO broadcast_delivery"
                  "(broadcast_id, telegram_id, status, detail) VALUES (?,?,?,?) "
                  "ON CONFLICT(broadcast_id, telegram_id) DO UPDATE SET "
                  "status=excluded.status, detail=excluded.detail, "
                  "at=CURRENT_TIMESTAMP", (bid, tg_id, status, detail))


def broadcast_stats(bid: int) -> dict[str, int]:
    with connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM broadcast_delivery "
                         "WHERE broadcast_id=? GROUP BY status", (bid,)).fetchall()
        return {r["status"]: r["n"] for r in rows}


def delete_user(tg_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM users WHERE telegram_id=?", (tg_id,))
