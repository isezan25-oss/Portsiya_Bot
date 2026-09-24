#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка данных перед тем, как заливать новые рецепты.

Ловит то, что иначе всплывёт уже в боте: разошедшуюся арифметику КБЖУ,
неверный тег плотности белка, аллерген не из словаря, символы, на которых
падает Telegram, карточку без строки в базе и наоборот.

    python3 scripts/check_data.py

Выход 0 — всё чисто, 1 — есть ошибки. Предупреждения выходу не мешают.
"""
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "recipes.csv"
COLLECTION = ROOT / "docs" / "10-сборник-рецептов.md"

ALLERGENS = {"молоко", "глютен", "яйцо", "орехи", "рыба", "соя", "арахис", "кунжут"}
MEALS = {"breakfast", "lunch", "dinner", "snack"}
DENSITY_TAGS = {"высокобелковое": (0.070, 9.9),
                "среднебелковое": (0.037, 0.0699),
                "низкобелковое": (0.0, 0.0369)}
BAD_CHARS = set("_*[]`")

errors: list[str] = []
warnings: list[str] = []


def num(v: str) -> float:
    return float(str(v).replace(",", "."))


def check_rows(rows: list[dict]) -> None:
    seen: set[str] = set()
    for r in rows:
        rid, title = r["id"], r["title"]
        if rid in seen:
            errors.append(f"{rid}: id повторяется")
        seen.add(rid)

        kcal, prot = num(r["kcal"]), num(r["protein"])
        fat, carb = num(r["fat"]), num(r["carb"])

        # арифметика: Б×4 + Ж×9 + У×4 против заявленной калорийности
        calc = prot * 4 + fat * 9 + carb * 4
        dev = abs(calc - kcal) / kcal if kcal else 1
        # Партия 1 и продукты — оценки и справочные значения, у них расхождение
        # до 6% ожидаемо и описано в docs/06. Считаные по составу партии
        # (b2 и дальше) обязаны укладываться в 5%, новые блюда тоже.
        # Продукты — целые фрукты и орехи. Углеводы у них записаны усвояемые,
        # без клетчатки, а справочная калорийность её вклад учитывает, поэтому
        # сумма 4/9/4 у них не сходится по определению. Это не ошибка данных.
        if r["kind"] == "product":
            if dev > 0.08:
                warnings.append(f"{rid}: расхождение КБЖУ {dev:.1%} — велико даже "
                                f"с поправкой на клетчатку, стоит перепроверить")
        elif rid.startswith("p1-"):
            # Партия 1 — оценки автора, расхождение до 6% описано в docs/06.
            if dev > 0.06:
                errors.append(f"{rid}: расхождение КБЖУ {dev:.1%} — велико даже "
                              f"для оценочных значений партии 1 (до 6%)")
            elif dev > 0.032:
                warnings.append(f"{rid}: расхождение КБЖУ {dev:.1%} — ожидаемо для "
                                f"партии 1, но стоит сверить с авторской карточкой")
        elif dev > 0.05:
            errors.append(f"{rid}: КБЖУ не сходятся — сумма {calc:.0f} против "
                          f"{kcal:.0f} ккал, расхождение {dev:.1%} (норма до 5%)")
        elif dev > 0.032:
            warnings.append(f"{rid}: расхождение КБЖУ {dev:.1%} — больше, чем "
                            f"в считаных по составу партиях (до 3.2%)")

        # тег плотности белка обязателен и должен соответствовать числам
        tags = [t.strip() for t in r["tags"].split(",") if t.strip()]
        dens = prot / kcal if kcal else 0
        found = [t for t in tags if t in DENSITY_TAGS]
        if r["kind"] != "product":
            if not found:
                errors.append(f"{rid}: нет тега плотности белка "
                              f"(плотность {dens:.3f} — нужен "
                              f"{_density_tag(dens)})")
            elif len(found) > 1:
                errors.append(f"{rid}: сразу несколько тегов плотности: {found}")
            elif found[0] != _density_tag(dens):
                errors.append(f"{rid}: тег «{found[0]}» не соответствует "
                              f"плотности {dens:.3f} — нужен {_density_tag(dens)}")

        # словари
        for a in (x.strip() for x in r["allergens"].split(",") if x.strip()):
            if a not in ALLERGENS:
                errors.append(f"{rid}: аллерген «{a}» не из словаря {sorted(ALLERGENS)}")
        for m in (x.strip() for x in r["meals"].replace("\n", ",").split(",") if x.strip()):
            if m not in MEALS:
                errors.append(f"{rid}: приём «{m}» не из словаря {sorted(MEALS)}")
        if not r["meals"].strip():
            errors.append(f"{rid}: не указан ни один приём пищи")

        # Telegram ломается на разметке в названии
        bad = BAD_CHARS & set(title)
        if bad:
            errors.append(f"{rid}: в названии символы {sorted(bad)} — Telegram "
                          f"разберёт их как разметку и упадёт")

        if r["fixed"] not in ("0", "1"):
            errors.append(f"{rid}: fixed должен быть 0 или 1, а не «{r['fixed']}»")
        if r["cook_min"] and not r["cook_min"].isdigit():
            errors.append(f"{rid}: cook_min должен быть целым числом минут")
        if r["cook_min"] and r["kind"] != "product":
            fast = int(r["cook_min"]) <= 20
            if fast and "быстро" not in tags:
                warnings.append(f"{rid}: готовится {r['cook_min']} мин — просится тег «быстро»")
            if not fast and "быстро" in tags:
                errors.append(f"{rid}: тег «быстро» при {r['cook_min']} мин готовки")

        if r["portion_g"]:
            warnings.append(f"{rid}: portion_g заполнен, а вес готовой порции "
                            f"нигде не замерен — проверьте, откуда цифра")


def _density_tag(dens: float) -> str:
    for tag, (lo, hi) in DENSITY_TAGS.items():
        if lo <= dens <= hi:
            return tag
    return "?"


def check_collection(rows: list[dict]) -> None:
    if not COLLECTION.exists():
        warnings.append("сборник рецептов не найден, сверка пропущена")
        return
    text = COLLECTION.read_text(encoding="utf-8")
    cards = re.split(r"^### ", text, flags=re.M)[1:]
    by_id: dict[str, tuple[float, float, float, float]] = {}
    for card in cards:
        m_id = re.search(r"\*\*id в базе:\*\* `([^`]+)`", card)
        m_kcal = re.search(r"\*\*([\d\s]+) ккал\*\*", card)
        m_macro = re.search(r"Б ([\d,\.]+) г · Ж ([\d,\.]+) г · У ([\d,\.]+) г", card)
        if not m_id:
            title = card.split("\n", 1)[0].strip()
            warnings.append(f"карточка «{title[:50]}» без id в базе — "
                            f"бот её не покажет")
            continue
        if not (m_kcal and m_macro):
            errors.append(f"{m_id.group(1)}: в карточке не разобрать КБЖУ")
            continue
        by_id[m_id.group(1)] = (num(m_kcal.group(1).replace(" ", "")),
                                *(num(x) for x in m_macro.groups()))

    authored = {r["id"]: r for r in rows if r["kind"] != "product"}
    for rid, r in authored.items():
        if rid not in by_id:
            errors.append(f"{rid}: есть в базе, но карточки в сборнике нет — "
                          f"бот не сможет прислать рецепт")
            continue
        ref = (num(r["kcal"]), num(r["protein"]), num(r["fat"]), num(r["carb"]))
        got = by_id[rid]
        if abs(got[0] - ref[0]) > 1 or any(abs(a - b) > 0.5 for a, b in zip(got[1:], ref[1:])):
            errors.append(f"{rid}: КБЖУ в карточке {got} не совпадают с базой {ref}")
    for rid in set(by_id) - set(authored):
        errors.append(f"{rid}: карточка есть, а строки в data/recipes.csv нет")


def main() -> int:
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    print(f"Позиций в базе: {len(rows)}, из них авторских рецептов: "
          f"{sum(1 for r in rows if r['kind'] != 'product')}\n")
    check_rows(rows)
    check_collection(rows)

    for w in warnings:
        print(f"  ! {w}")
    if warnings:
        print()
    for e in errors:
        print(f"  ОШИБКА  {e}")

    if errors:
        print(f"\nОшибок: {len(errors)}. Заливать нельзя, пока не исправлены.")
        return 1
    print(f"Ошибок нет{', предупреждений: ' + str(len(warnings)) if warnings else ''}.")
    print("Дальше: python3 scripts/coverage.py --n 130")
    return 0


if __name__ == "__main__":
    sys.exit(main())
