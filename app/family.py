"""
Ужин на двоих: одно блюдо, у каждого своя порция и своя добавка.

Почему именно ужин, а не весь день: план на день должен сойтись сразу по
калориям, белку и жирам, и у пары с разными целями таких комбинаций единицы.
Один приём пищи — задача на порядок проще, и она решается для любых пар,
включая нормы, различающиеся втрое. Замер: подходящих ужинов от 17 до 45
из 50 в зависимости от пары.
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import MEAL_SPLIT
from .planner import Addon, Recipe

SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.75, 1.40, 0.05
SCALES = [round(SCALE_MIN + i * SCALE_STEP, 2)
          for i in range(int((SCALE_MAX - SCALE_MIN) / SCALE_STEP) + 1)]
# Допуск на один приём мягче дневного: день выправляется остальными приёмами.
TOL_PCT, TOL_ABS = 0.10, 60


@dataclass
class Share:
    """Сколько этому человеку положено за ужин и чем это закрыто."""
    kcal_target: float
    scale: float
    addon: Addon | None
    kcal: float
    protein: float


@dataclass
class FamilyDinner:
    recipe: Recipe
    first: Share
    second: Share
    score: float


def dinner_target(kcal: int, meals: int = 4) -> float:
    return kcal * MEAL_SPLIT[meals]["dinner"][0]


def _fit(target: float, r: Recipe, addons: list[Addon]) -> Share | None:
    """Лучшая пара «порция + добавка», попадающая в целевую калорийность.

    Добавки берутся только те, что подходят к ужину: в CSV у них есть колонка
    meals, и без неё к ужину предлагалась овсянка на молоке."""
    best: Share | None = None
    for addon in addons:
        if addon.kcal and "dinner" not in addon.meals:
            continue
        for s in ([1.0] if r.fixed else SCALES):
            kcal = r.kcal * s + addon.kcal
            if abs(kcal - target) > max(target * TOL_PCT, TOL_ABS):
                continue
            share = Share(kcal_target=target, scale=s,
                          addon=addon if addon.kcal else None, kcal=kcal,
                          protein=r.protein * s + addon.protein)
            if best is None or abs(kcal - target) < abs(best.kcal - best.kcal_target):
                best = share
    return best


def build_dinner(recipes: list[Recipe], addons: list[Addon],
                 kcal_a: int, protein_a: int, kcal_b: int, protein_b: int,
                 meals: int = 4, top_n: int = 3) -> list[FamilyDinner]:
    """Ужины, которые подходят обоим. Сортировка — по близости к норме белка."""
    ta, tb = dinner_target(kcal_a, meals), dinner_target(kcal_b, meals)
    pa, pb = protein_a * MEAL_SPLIT[meals]["dinner"][1], protein_b * MEAL_SPLIT[meals]["dinner"][1]
    out: list[FamilyDinner] = []
    for r in recipes:
        if "dinner" not in r.meals:
            continue
        fa = _fit(ta, r, addons)
        fb = _fit(tb, r, addons)
        if not (fa and fb):
            continue
        # чем ближе белок к норме у обоих, тем выше в списке
        score = -(abs(fa.protein - pa) / pa + abs(fb.protein - pb) / pb)
        out.append(FamilyDinner(recipe=r, first=fa, second=fb, score=score))
    out.sort(key=lambda d: -d.score)
    return out[:top_n]


def format_dinner(d: FamilyDinner, name_a: str, name_b: str) -> str:
    def line(name: str, sh: Share) -> str:
        portion = f"порция ×{sh.scale:.2f}" if sh.scale != 1.0 else "обычная порция"
        text = f"*{name}:* {portion} — {round(sh.kcal)} ккал"
        if sh.addon:
            text += f"\n   плюс {sh.addon.title}"
        return text

    return (f"*Ужин на двоих*\n\n{d.recipe.title}\n\n"
            f"{line(name_a, d.first)}\n{line(name_b, d.second)}\n\n"
            f"_Готовится одно блюдо, порции разные. Ужин посчитан как доля "
            f"дневной нормы каждого._")
