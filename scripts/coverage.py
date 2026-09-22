#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон покрытия: сколько профилей вообще получают план.

Запускать из корня репозитория, использует настоящий код бота —
app.core и app.planner, ничего не дублирует.

    python coverage.py                          # текущий data/recipes.csv
    python coverage.py --recipes data/recipes.csv.bak   # состав до слияния

Чтобы сравнить «до» и «после», прогони оба файла: сетка профилей
детерминированная (seed=20260922), поэтому числа сопоставимы.

Важно: сетка построена этим скриптом, а не та, на которой раньше
получилось 82%. Сравнивать нужно два прогона этого скрипта между собой,
а не с историческим числом.
"""
import argparse
import itertools
import random
import sys
from collections import Counter
from pathlib import Path

# скрипт лежит в scripts/, а пакет app — на уровень выше
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import ACTIVITY, NotEligible, Profile, calculate
from app.planner import load_recipes, load_addons, relax_and_build

SEED = 20260922


def grid(n: int) -> list[Profile]:
    """Детерминированная сетка профилей по всем сочетаниям пол/цель/активность."""
    rng = random.Random(SEED)
    combos = list(itertools.product(
        ("female", "male"),
        ("cut", "maintain", "bulk"),
        tuple(ACTIVITY.keys()),
    ))
    out: list[Profile] = []
    while len(out) < n:
        for sex, goal, act in combos:
            if len(out) >= n:
                break
            height = rng.randint(152, 196) if sex == "male" else rng.randint(148, 182)
            # вес выводим из ИМТ, а не берём независимо от роста:
            # иначе в сетку попадают несуществующие тела вроде 176 см / 50 кг
            bmi = rng.uniform(19.0, 34.0) if goal == "cut" else rng.uniform(18.6, 30.0)
            weight = round(bmi * (height / 100) ** 2, 1)
            p = Profile(sex=sex, age=rng.randint(18, 65), height_cm=height,
                        weight_kg=weight, activity=act, goal=goal)
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipes", default="data/recipes.csv")
    ap.add_argument("--addons", default="data/addons.csv")
    ap.add_argument("--n", type=int, default=130)
    a = ap.parse_args()

    recipes = load_recipes(Path(a.recipes))
    addons = load_addons(Path(a.addons))
    print(f"Рецептов: {len(recipes)}   добавок: {len(addons)}   файл: {a.recipes}\n")

    profiles = grid(a.n)
    ok = 0
    failed = []
    densities_ok, densities_bad = [], []

    for p in profiles:
        try:
            t = calculate(p)
        except NotEligible:
            continue
        plans, _ = relax_and_build(t, recipes, addons, meals=p.meals_per_day, seed=SEED)
        dens = t.protein_g / t.kcal
        if plans:
            ok += 1
            densities_ok.append(dens)
        else:
            failed.append((p, t, dens))
            densities_bad.append(dens)

    total = ok + len(failed)
    print(f"Профилей проверено: {total}")
    print(f"План собрался:      {ok}  ({ok / total * 100:.1f}%)")
    print(f"Плана нет:          {len(failed)}  ({len(failed) / total * 100:.1f}%)\n")

    if failed:
        print("Непокрытые профили:")
        print(f"  норма калорий:     {min(t.kcal for _, t, _ in failed)}–"
              f"{max(t.kcal for _, t, _ in failed)} ккал")
        print(f"  плотность белка:   {min(densities_bad):.3f}–{max(densities_bad):.3f} г/ккал")
        by = Counter((p.sex, p.goal) for p, _, _ in failed)
        for (sex, goal), c in by.most_common():
            print(f"  {sex}/{goal}: {c}")
        print("\n  первые десять:")
        for p, t, d in sorted(failed, key=lambda x: -x[1].kcal)[:10]:
            print(f"    {p.sex[:1]} {p.age}л {p.height_cm}см {p.weight_kg}кг "
                  f"{p.activity:<9} {p.goal:<8} -> {t.kcal} ккал, белок {t.protein_g} г, "
                  f"плотность {d:.3f}")
    else:
        print("Непокрытых профилей нет.")

    if densities_ok:
        print(f"\nПокрытые профили: плотность белка "
              f"{min(densities_ok):.3f}–{max(densities_ok):.3f} г/ккал")


if __name__ == "__main__":
    main()
