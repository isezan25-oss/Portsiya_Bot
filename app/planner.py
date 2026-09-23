"""
Подбор дневного плана. Перебор комбинаций + масштабирование порций + добавка.
Без ИИ: результат детерминированный. Спецификация, раздел 5.
"""
from __future__ import annotations
import csv
import itertools
import random
from dataclasses import dataclass, field
from pathlib import Path

from .core import MEAL_SPLIT, Targets

DATA = Path(__file__).resolve().parent.parent / "data"

SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.75, 1.40, 0.05
TOL_KCAL_PCT, TOL_KCAL_ABS = 0.05, 75
TOL_PROTEIN_LO, TOL_PROTEIN_HI = -0.05, 0.15
# Жиры: ±20% по спецификации, раздел 5.3. Верхняя граница в коде отсутствовала —
# отсюда и брался перекос: калории и белок были зажаты допусками, жир оставался
# свободным, углеводы схлопывались ему навстречу. Замер на 130 планах до правки:
# жир 1.40 нормы по медиане, 2.91 в худшем случае, углеводы 0.77 нормы.
# Цена соблюдения: 2 профиля из 130 (атлеты на наборе, 3800+ ккал) остаются без
# плана. Поднять до 1.35 — покрытие 100%, но жир до 1.35 нормы.
TOL_FAT_LO, TOL_FAT_HI = 0.80, 1.20
# Штраф в score за отклонение жиров от нормы. Покрытие не трогает: набор
# допустимых планов тот же, меняется только порядок. 0.5 подобрано как
# наименьшее значение, дающее эффект и не перебивающее штраф за повтор блюда.
FAT_SCORE_PENALTY = 0.5
# Штраф за отклонение долей приёмов от MEAL_SPLIT. Допуски проверяют только итог
# за день, поэтому раскладка разъезжалась: замер на 130 планах до правки —
# обед 30% при заложенных 35%, ужин 34% при 30%, перекус p90 22% при 10%
# и до 26% в худшем случае. Жёсткие коридоры стоили бы покрытия (±50% — 94.6%,
# ±20% — 80.8%), штраф же не трогает набор допустимых планов, только порядок.
# Выше 3 не поднимать: сумма отклонений типично 0.15–0.25, то есть при k=12 штраф
# доходит до 300 очков и перебивает и повтор блюда (15), и близость к норме жиров.
MEAL_SCORE_PENALTY = 1.0

MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack", "snack2"]
MEAL_RU = {"breakfast": "Завтрак", "lunch": "Обед",
           "dinner": "Ужин", "snack": "Перекус", "snack2": "Второй перекус"}


@dataclass
class Recipe:
    id: str
    collection: str
    title: str
    meals: tuple[str, ...]   # приёмы, к которым блюдо подходит; одно блюдо в день — один раз
    kcal: float
    protein: float
    fat: float
    carb: float
    fixed: bool
    tags: str
    allergens: str
    author: str
    kind: str
    portion_g: str = ""
    cook_min: str = ""


@dataclass
class Addon:
    id: str
    title: str
    kcal: float
    protein: float
    fat: float
    carb: float
    meals: str
    tier: str


@dataclass
class PlanItem:
    recipe: Recipe
    meal: str
    scale: float

    @property
    def kcal(self) -> float:
        return self.recipe.kcal * self.scale

    @property
    def protein(self) -> float:
        return self.recipe.protein * self.scale


@dataclass
class Plan:
    items: list[PlanItem]
    addon: Addon | None
    kcal: float
    protein: float
    fat: float
    carb: float
    score: float = 0.0
    notes: list[str] = field(default_factory=list)


def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return d


def _meals(row: dict) -> tuple[str, ...]:
    """Колонка meals — список через запятую. Старая колонка meal тоже понимается."""
    raw = row.get("meals") or row.get("meal") or ""
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def load_recipes(path: Path | None = None) -> list[Recipe]:
    with open(path or DATA / "recipes.csv", encoding="utf-8") as f:
        return [
            Recipe(id=r["id"], collection=r["collection"], title=r["title"],
                   meals=_meals(r), kcal=_f(r["kcal"]), protein=_f(r["protein"]),
                   fat=_f(r["fat"]), carb=_f(r["carb"]), fixed=r["fixed"] == "1",
                   tags=r["tags"], allergens=r["allergens"], author=r["author"],
                   kind=r["kind"], portion_g=r["portion_g"], cook_min=r["cook_min"])
            for r in csv.DictReader(f)
        ]


def load_addons(path: Path | None = None) -> list[Addon]:
    with open(path or DATA / "addons.csv", encoding="utf-8") as f:
        return [
            Addon(id=r["id"], title=r["title"], kcal=_f(r["kcal"]),
                  protein=_f(r["protein"]), fat=_f(r["fat"]), carb=_f(r["carb"]),
                  meals=r["meals"], tier=r["tier"])
            for r in csv.DictReader(f)
        ]


def filter_recipes(recipes: list[Recipe], exclusions: list[str],
                   collections: list[str] | None = None) -> list[Recipe]:
    """Убирает рецепты с исключёнными продуктами; опционально режет по подборкам."""
    out = []
    for r in recipes:
        hay = f"{r.allergens} {r.tags} {r.title}".lower()
        if any(x.strip().lower() in hay for x in exclusions if x.strip()):
            continue
        if collections and r.collection not in collections and r.kind != "base":
            continue
        out.append(r)
    return out


def _scales(fixed: bool, target_ratio: float) -> list[float]:
    if fixed:
        return [1.0]
    lo = max(SCALE_MIN, target_ratio - 0.25)
    hi = min(SCALE_MAX, target_ratio + 0.25)
    n = int(round((hi - lo) / SCALE_STEP))
    return [round(lo + i * SCALE_STEP, 2) for i in range(max(n, 0) + 1)] or [1.0]


def _snap(fixed: bool, ratio: float) -> float:
    """То же, что ближайшее значение из _scales(), но без построения списка."""
    if fixed:
        return 1.0
    lo = max(SCALE_MIN, ratio - 0.25)
    hi = min(SCALE_MAX, ratio + 0.25)
    n = int(round((hi - lo) / SCALE_STEP))
    if n < 0:
        return 1.0
    k = min(max(int(round((ratio - lo) / SCALE_STEP)), 0), n)
    return round(lo + k * SCALE_STEP, 2)


def _combos(pool_list: list[list[Recipe]], rng: random.Random,
            cap: int = 60_000):
    """Перебор комбинаций. Пока их немного — исчерпывающий, в случайном порядке.
    Когда база вырастает, полный список стоит слишком дорого, и вместо него
    идёт случайная выборка того же размера."""
    size = 1
    for p in pool_list:
        size *= len(p)
    if size <= cap:
        combos = list(itertools.product(*pool_list))
        rng.shuffle(combos)
        return combos

    def sample():
        seen: set[tuple[str, ...]] = set()
        for _ in range(cap):
            combo = tuple(rng.choice(p) for p in pool_list)
            key = tuple(r.id for r in combo)
            if key in seen:
                continue
            seen.add(key)
            yield combo
    return sample()


def build_plan(targets: Targets, recipes: list[Recipe], addons: list[Addon],
               meals: int = 4, recent_ids: set[str] | None = None,
               prefer_tags: list[str] | None = None,
               max_cook_min: int | None = None,
               top_n: int = 5, seed: int | None = None) -> list[Plan]:
    """Возвращает до top_n планов, отсортированных по score. Пустой список — решения нет."""
    recent = recent_ids or set()
    prefer = [t.lower() for t in (prefer_tags or [])]
    slots = MEAL_ORDER[:meals] if meals != 5 else MEAL_ORDER[:5]
    split = MEAL_SPLIT[len(slots)]

    pools = {}
    for slot in slots:
        base_meal = "snack" if slot == "snack2" else slot
        pool = [r for r in recipes if base_meal in r.meals]
        if not pool:
            return []
        pools[slot] = pool

    tol_kcal = max(targets.kcal * TOL_KCAL_PCT, TOL_KCAL_ABS)
    rng = random.Random(seed)
    results: list[Plan] = []

    # границы, чтобы отбрасывать комбинацию одним сравнением, не перебирая добавки
    add_kcal_min = min(a.kcal for a in addons)
    add_kcal_max = max(a.kcal for a in addons)
    kcal_lo = targets.kcal / (SCALE_MAX + 0.05)
    kcal_hi = targets.kcal / (SCALE_MIN - 0.05)

    for combo in _combos([pools[s] for s in slots], rng):
        # одно блюдо подходит к нескольким приёмам, но в один день идёт только раз
        if len({r.id for r in combo}) < len(combo):
            continue
        base_kcal = sum(r.kcal for r in combo)
        if base_kcal + add_kcal_max < kcal_lo or base_kcal + add_kcal_min > kcal_hi:
            continue
        for addon in addons:
            total0 = base_kcal + addon.kcal
            if total0 <= 0:
                continue
            ratio = targets.kcal / total0
            if not (SCALE_MIN - 0.05 <= ratio <= SCALE_MAX + 0.05):
                continue

            # сначала считаем числа, объекты строим только для прошедших допуски
            scales = [_snap(r.fixed, ratio) for r in combo]
            kcal = sum(r.kcal * s for r, s in zip(combo, scales)) + addon.kcal
            if abs(kcal - targets.kcal) > tol_kcal:
                continue
            protein = sum(r.protein * s for r, s in zip(combo, scales)) + addon.protein
            dev = (protein - targets.protein_g) / targets.protein_g
            if not (TOL_PROTEIN_LO <= dev <= TOL_PROTEIN_HI):
                continue
            fat = sum(r.fat * s for r, s in zip(combo, scales)) + addon.fat
            if not (targets.fat_g * TOL_FAT_LO <= fat <= targets.fat_g * TOL_FAT_HI):
                continue

            scaled = [PlanItem(recipe=r, meal=slot, scale=s)
                      for slot, r, s in zip(slots, combo, scales)]

            score = 100.0
            score -= 15 * sum(1 for i in scaled if i.recipe.id in recent)
            score -= 5 * sum(abs(i.scale - 1.0) * 100 for i in scaled) / len(scaled)
            score += 8 * sum(1 for i in scaled
                             if any(t in i.recipe.tags.lower() for t in prefer))
            if max_cook_min is not None:
                over = sum(1 for i in scaled
                           if i.recipe.cook_min not in ("", None)
                           and _f(i.recipe.cook_min) > max_cook_min)
                score -= 10 * over
            score -= FAT_SCORE_PENALTY * abs(fat / targets.fat_g - 1.0) * 100
            # доли считаются от суммы приёмов: добавка не привязана к приёму
            meal_kcal = kcal - addon.kcal
            score -= MEAL_SCORE_PENALTY * 100 * sum(
                abs(r.kcal * s / meal_kcal - split[slot][0])
                for slot, r, s in zip(slots, combo, scales))
            mains = [i.recipe.title.split()[0].lower() for i in scaled]
            score -= 10 * (len(mains) - len(set(mains)))

            results.append(Plan(items=scaled, addon=addon if addon.kcal else None,
                                kcal=kcal, protein=protein, fat=fat,
                                carb=sum(i.recipe.carb * i.scale for i in scaled) + addon.carb,
                                score=score))
            if len(results) >= 400:
                break
        if len(results) >= 400:
            break

    results.sort(key=lambda p: -p.score)
    return results[:top_n]


def relax_and_build(targets: Targets, recipes: list[Recipe], addons: list[Addon],
                    meals: int = 4, recent_ids: set[str] | None = None,
                    **kw) -> tuple[list[Plan], list[str]]:
    """Каскад из 5.5: обычный подбор -> без учёта повторов -> 5 приёмов."""
    notes: list[str] = []
    plans = build_plan(targets, recipes, addons, meals, recent_ids, **kw)
    if plans:
        return plans, notes

    plans = build_plan(targets, recipes, addons, meals, set(), **kw)
    if plans:
        notes.append("В плане есть блюда из последних дней — база пока небольшая.")
        return plans, notes

    if meals == 4:
        plans = build_plan(targets, recipes, addons, 5, set(), **kw)
        if plans:
            notes.append("Добавила второй перекус — так проще попасть в вашу норму.")
            return plans, notes

    return [], ["Не удалось собрать план под эту норму: база рецептов пока мала."]


def format_plan(plan: Plan, targets: Targets) -> str:
    lines = []
    for item in plan.items:
        r = item.recipe
        portion = ""
        if r.portion_g:
            portion = f", {round(_f(r.portion_g) * item.scale)} г"
        elif item.scale != 1.0:
            portion = f", порция ×{item.scale:.2f}"
        lines.append(f"*{MEAL_RU[item.meal]}*\n{r.title}{portion} — {round(item.kcal)} ккал")
    if plan.addon:
        lines.append(f"*К этому*\n{plan.addon.title} — {round(plan.addon.kcal)} ккал")
    total = (f"\n*Итого за день*\n{round(plan.kcal)} ккал  ·  Б {round(plan.protein)} г  ·  "
             f"Ж {round(plan.fat)} г  ·  У {round(plan.carb)} г\n"
             f"_Ваша норма: {targets.kcal} ккал, белок {targets.protein_g} г_")
    return "\n\n".join(lines) + "\n" + total
