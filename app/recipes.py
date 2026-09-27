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


GRAMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:–|-)?\s*(\d+(?:[.,]\d+)?)?\s*(г|мл)\b")
PREFIX_RE = re.compile(r"^Для [^:]{0,40}:\s*")
# «1 шт.» — это яйцо, а не специя: в список покупок такое попасть обязано
PIECES_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*шт")


def _parse_item(raw: str) -> dict | None:
    """«Куриное филе — 150 г» -> {'name': ..., 'grams': 150.0, 'note': '150 г'}."""
    text = PREFIX_RE.sub("", raw.strip(" -•"))
    if not text:
        return None
    if "—" not in text:
        # специи и «по желанию»: веса нет, но в списке покупок они нужны
        return {"name": text.rstrip("."), "grams": None, "pieces": None,
                "note": "по вкусу"}
    name, _, amount = text.partition("—")
    name = name.strip().rstrip(",").strip()
    amount = amount.strip()
    m = GRAMS_RE.search(amount)
    grams = None
    if m:
        lo = float(m.group(1).replace(",", "."))
        hi = float(m.group(2).replace(",", ".")) if m.group(2) else lo
        grams = (lo + hi) / 2
    pieces = None
    if grams is None:
        p = PIECES_RE.search(amount)
        if p:
            pieces = float(p.group(1).replace(",", "."))
    return {"name": name, "grams": grams, "pieces": pieces, "note": amount}


def ingredients(recipe_id: str) -> list[dict]:
    """Состав блюда из карточки сборника."""
    text = CARDS.get(recipe_id)
    if not text:
        return []
    block = re.search(r"^Ингредиенты\s*$(.+?)^Приготовление по шагам",
                      text, re.S | re.M)
    if not block:
        return []
    out = []
    for line in block.group(1).strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in line.split(";"):
            item = _parse_item(part)
            if item and item["name"]:
                out.append(item)
    return out


def shopping_list(plan_items: list[tuple[str, float]]) -> list[str]:
    """Список покупок по плану: [(id блюда, множитель порции)] -> строки.

    Одинаковые продукты складываются, граммовка умножается на множитель —
    иначе список не совпадёт с тем, что человек реально положит в тарелку."""
    totals: dict[str, float] = {}
    pieces: dict[str, float] = {}
    loose: dict[str, None] = {}
    for rid, scale in plan_items:
        for item in ingredients(rid):
            key = item["name"][0].upper() + item["name"][1:]
            if item["grams"] is not None:
                totals[key] = totals.get(key, 0) + item["grams"] * scale
            elif item["pieces"] is not None:
                pieces[key] = pieces.get(key, 0) + item["pieces"] * scale
            else:
                loose.setdefault(key, None)
    lines = [f"{name} — {round(g / 5) * 5 if g >= 20 else round(g)} г"
             for name, g in sorted(totals.items())]
    # Специи и «по вкусу» — одной строкой: в списке покупок это обычно то,
    # что уже есть дома, и сорок отдельных пунктов только мешают.
    lines += [f"{name} — {round(n) if round(n) >= 1 else 1} шт."
              for name, n in sorted(pieces.items()) if name not in totals]
    spices = sorted(n.lower() for n in loose if n not in totals and n not in pieces)
    if spices:
        lines.append("Специи и по вкусу: " + ", ".join(spices))
    return lines


def pack(cards: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Складывает карточки в наименьшее число сообщений: Telegram пропускает
    около одного сообщения в секунду на чат, и восемь подряд ловят 429."""
    out: list[str] = []
    current = ""
    for card_text in cards:
        for part in chunks(card_text, limit):
            if current and len(current) + len(part) + 4 > limit:
                out.append(current)
                current = part
            else:
                current = f"{current}\n\n\n{part}" if current else part
    if current:
        out.append(current)
    return out


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
