"""
Карточки рецептов для показа в боте. Источник — docs/10-сборник-рецептов.md,
он же источник истины по составу и приготовлению (КБЖУ — в data/recipes.csv).

Текст отдаётся без разметки: в карточках есть `*`, `_` и кавычки, на которых
Telegram падает с can't parse entities, а экранировать живой текст рецепта
надёжнее всего отказом от parse_mode.
"""
from __future__ import annotations

import re
from pathlib import Path

COLLECTION = Path(__file__).resolve().parent.parent / "docs" / "10-сборник-рецептов.md"

TELEGRAM_LIMIT = 4096


def _plain(card: str) -> str:
    """Markdown -> обычный текст: жирное, курсив, цитаты и разделители убираем."""
    text = card
    text = re.sub(r"^>\s?", "", text, flags=re.M)      # цитаты-врезки
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)  # горизонтальные линии
    # служебное — человеку не нужно
    text = re.sub(r"^\*\*Заведено в базу.*$", "", text, flags=re.M)
    text = re.sub(r"\s*·\s*\*\*id в базе:\*\*\s*`[^`]+`", "", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load() -> dict[str, str]:
    if not COLLECTION.exists():
        return {}
    cards = re.split(r"^### ", COLLECTION.read_text(encoding="utf-8"), flags=re.M)[1:]
    out: dict[str, str] = {}
    for card in cards:
        m = re.search(r"\*\*id в базе:\*\* `([^`]+)`", card)
        if m:
            out[m.group(1)] = _plain(card)
    return out


CARDS: dict[str, str] = _load()


def card(recipe_id: str) -> str | None:
    return CARDS.get(recipe_id)


def chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет длинную карточку по абзацам, чтобы влезть в сообщение Telegram."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > limit and current:
            parts.append(current.strip())
            current = ""
        current += para + "\n\n"
    if current.strip():
        parts.append(current.strip())
    return parts
