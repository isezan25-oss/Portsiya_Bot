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
DB_PATH = Path(os.environ.get("DB_PATH")
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


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def ensure_user(tg_id: int) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (tg_id,))


def save_profile(tg_id: int, p, t) -> None:
    ensure_user(tg_id)
    with connect() as c:
        c.execute("""
            INSERT INTO profiles (telegram_id, sex, age, height_cm, weight_kg, activity,
                                  goal, meals_per_day, target_kcal, protein_g, fat_g,
                                  carb_g, calculated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                sex=excluded.sex, age=excluded.age, height_cm=excluded.height_cm,
                weight_kg=excluded.weight_kg, activity=excluded.activity,
                goal=excluded.goal, meals_per_day=excluded.meals_per_day,
                target_kcal=excluded.target_kcal, protein_g=excluded.protein_g,
                fat_g=excluded.fat_g, carb_g=excluded.carb_g,
                calculated_at=CURRENT_TIMESTAMP
        """, (tg_id, p.sex, p.age, p.height_cm, p.weight_kg, p.activity, p.goal,
              p.meals_per_day, t.kcal, t.protein_g, t.fat_g, t.carb_g))


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


def save_plan(tg_id: int, plan_date: date, recipe_ids: list[str], text: str) -> None:
    payload = json.dumps({"recipe_ids": recipe_ids, "text": text}, ensure_ascii=False)
    with connect() as c:
        c.execute("""INSERT INTO plans (telegram_id, plan_date, payload) VALUES (?,?,?)
                     ON CONFLICT(telegram_id, plan_date) DO UPDATE SET payload=excluded.payload""",
                  (tg_id, plan_date.isoformat(), payload))


def get_plan(tg_id: int, plan_date: date) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT payload FROM plans WHERE telegram_id=? AND plan_date=?",
                        (tg_id, plan_date.isoformat())).fetchone()
    return json.loads(row["payload"]) if row else None


def recent_recipe_ids(tg_id: int, days: int = 5) -> set[str]:
    since = (date.today() - timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute("SELECT payload FROM plans WHERE telegram_id=? AND plan_date>=?",
                         (tg_id, since)).fetchall()
    ids: set[str] = set()
    for r in rows:
        ids.update(json.loads(r["payload"]).get("recipe_ids", []))
    return ids


def delete_user(tg_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM users WHERE telegram_id=?", (tg_id,))
