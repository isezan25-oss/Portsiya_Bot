"""
Напитки: отдельная подборка, которая НЕ участвует в подборе плана.

Почему отдельно от рецептов. Подбор обязан свести день по калориям, белку и
жирам, и любое блюдо в нём — часть этой арифметики. Напиток человек выбирает
под настроение, а не под норму, поэтому бот его не навязывает и в план не
ставит. КБЖУ при этом указаны у каждого: решение остаётся за человеком, но
принимается он не вслепую. Вписать напиток в день помогает режим «побаловать
себя» — он освобождает в плане место под то, что человек выберет сам.

Источник состава и приготовления — docs/14-сборник-напитков.md, КБЖУ —
data/drinks.csv, как и у рецептов.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
COLLECTION = Path(__file__).resolve().parent.parent / "docs" / "14-сборник-напитков.md"

CATEGORIES = [("чай", "🍵 Чай"), ("кофе", "☕️ Кофе и какао"), ("смузи", "🥤 Смузи")]
CATEGORY_RU = dict(CATEGORIES)


@dataclass
class Drink:
    id: str
    title: str
    category: str
    kcal: float
    protein: float
    fat: float
    carb: float
    volume_ml: str
    minutes: str
    allergens: str

    @property
    def light(self) -> bool:
        """Напиток, который можно не считать вовсе: чай и кофе без добавок."""
        return self.kcal <= 5


def load(path: Path | None = None) -> list[Drink]:
    with open(path or DATA / "drinks.csv", encoding="utf-8") as f:
        return [Drink(id=r["id"], title=r["title"], category=r["category"],
                      kcal=float(r["kcal"]), protein=float(r["protein"]),
                      fat=float(r["fat"]), carb=float(r["carb"]),
                      volume_ml=r["volume_ml"], minutes=r["minutes"],
                      allergens=r["allergens"])
                for r in csv.DictReader(f)]


def allowed(drinks: list[Drink], exclusions: list[str]) -> list[Drink]:
    """Убирает напитки с аллергенами человека — теми же словами, что и в плане."""
    out = []
    for d in drinks:
        hay = f"{d.allergens} {d.title}".lower()
        if any(x.strip().lower() in hay for x in exclusions if x.strip()):
            continue
        out.append(d)
    return out


def _cards() -> dict[str, str]:
    text = COLLECTION.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for block in re.split(r"\n---\n", text):
        m = re.search(r"\*\*id в базе:\*\*\s*`([^`]+)`", block)
        if m:
            out[m.group(1)] = block.strip()
    return out


CARDS = _cards()


def card(drink_id: str) -> str | None:
    """Карточка без разметки: в тексте есть кавычки и звёздочки, на которых
    Telegram падает с can't parse entities. Как и у рецептов — отдаём простым
    текстом, а не экранируем живой текст."""
    raw = CARDS.get(drink_id)
    if not raw:
        return None
    text = re.sub(r"^\*\*id в базе:\*\*.*$", "", raw, flags=re.M)
    text = text.replace("**", "").replace("### ", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
