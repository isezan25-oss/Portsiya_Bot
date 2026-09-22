"""
Расчётное ядро. Чистая арифметика по спецификации, без ИИ.
Все формулы и коэффициенты — из документа «Спецификация ядра», разделы 2-4.
"""
from dataclasses import dataclass
from typing import Literal

Sex = Literal["male", "female"]
Goal = Literal["cut", "maintain", "bulk"]

ACTIVITY = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "high": 1.725,
    "athlete": 1.9,
}

GOAL_ADJ = {"cut": -0.15, "maintain": 0.0, "bulk": 0.12}
PROTEIN_PER_KG = {"cut": 2.0, "maintain": 1.5, "bulk": 1.8}

KCAL_FLOOR = {"male": 1500, "female": 1200}

MEAL_SPLIT = {
    3: {"breakfast": (0.30, 0.30), "lunch": (0.40, 0.35), "dinner": (0.30, 0.35)},
    4: {"breakfast": (0.25, 0.25), "lunch": (0.35, 0.30),
        "dinner": (0.30, 0.30), "snack": (0.10, 0.15)},
    5: {"breakfast": (0.25, 0.25), "lunch": (0.30, 0.27),
        "dinner": (0.25, 0.27), "snack": (0.10, 0.105), "snack2": (0.10, 0.105)},
}


class NotEligible(Exception):
    """Расчёт не производится — нужен специалист. Сообщение показывается пользователю."""


@dataclass
class Profile:
    sex: Sex
    age: int
    height_cm: int
    weight_kg: float
    activity: str
    goal: Goal
    meals_per_day: int = 4
    pregnant: bool = False
    medical_diet: bool = False

    @property
    def bmi(self) -> float:
        return self.weight_kg / (self.height_cm / 100) ** 2


@dataclass
class Targets:
    bmr: int
    tdee: int
    kcal: int
    protein_g: int
    fat_g: int
    carb_g: int
    fiber_g: int
    water_ml: int
    notes: list[str]

    def per_meal(self, meals: int) -> dict[str, tuple[int, int]]:
        """{'breakfast': (ккал, белок_г), ...}"""
        return {
            m: (round(self.kcal * k), round(self.protein_g * p))
            for m, (k, p) in MEAL_SPLIT[meals].items()
        }


def check_eligible(p: Profile) -> None:
    """Стоп-условия из спецификации 1.2. Бросает NotEligible с текстом для пользователя."""
    if p.age < 18:
        raise NotEligible(
            "Персональный расчёт питания для несовершеннолетних должен делать врач "
            "или диетолог — я не могу посчитать норму корректно и безопасно."
        )
    if p.pregnant:
        raise NotEligible(
            "Во время беременности и грудного вскармливания потребности меняются "
            "и рассчитываются индивидуально. Это вопрос к вашему врачу."
        )
    if p.medical_diet:
        raise NotEligible(
            "При состояниях, требующих лечебной диеты, план питания должен "
            "составлять врач."
        )
    if p.bmi < 18.5 and p.goal == "cut":
        raise NotEligible(
            "При текущем весе снижение не рекомендуется. Если есть беспокойство "
            "по поводу веса, стоит обсудить это со специалистом."
        )


def calc_weight(p: Profile) -> float:
    """Скорректированный вес при ИМТ > 30 (спецификация 3.1)."""
    if p.bmi <= 30:
        return p.weight_kg
    ideal = 22 * (p.height_cm / 100) ** 2
    return ideal + 0.25 * (p.weight_kg - ideal)


def bmr_mifflin(p: Profile) -> float:
    base = 10 * p.weight_kg + 6.25 * p.height_cm - 5 * p.age
    return base + (5 if p.sex == "male" else -161)


def calculate(p: Profile) -> Targets:
    check_eligible(p)
    notes: list[str] = []

    bmr = bmr_mifflin(p)
    tdee = bmr * ACTIVITY[p.activity]
    kcal = round(tdee * (1 + GOAL_ADJ[p.goal]), -1)

    # 2.4 — ограничители, порядок важен
    if kcal < bmr:
        kcal = round(bmr, -1)
        notes.append("Норма поднята до уровня базового обмена — ниже опускаться не стоит.")
    floor = KCAL_FLOOR[p.sex]
    if kcal < floor:
        kcal = floor
        notes.append(f"Норма поднята до безопасного минимума {floor} ккал.")
    if p.goal == "cut" and (tdee - kcal) / tdee < 0.08:
        notes.append(
            "Безопасный дефицит при текущих параметрах получается совсем небольшим. "
            "Возможно, стоит нацелиться на удержание веса и работу над составом тела."
        )

    cw = calc_weight(p)
    if p.bmi > 30:
        notes.append("Нутриенты рассчитаны от скорректированного веса.")

    protein_g = round(min(cw * PROTEIN_PER_KG[p.goal], cw * 2.5))
    fat_g = round(cw * 0.9)
    fat_min = max(cw * 0.8, kcal * 0.20 / 9)
    if fat_g < fat_min:
        fat_g = round(fat_min)

    carb_g = round((kcal - protein_g * 4 - fat_g * 9) / 4)

    # 3.4 — пересборка, если углеводы провалились
    if carb_g < 100:
        protein_g = round(cw * 1.6)
        carb_g = round((kcal - protein_g * 4 - fat_g * 9) / 4)
        notes.append("Белок снижен, чтобы осталось место для углеводов.")
    if carb_g < 100:
        fat_g = round(fat_min)
        carb_g = round((kcal - protein_g * 4 - fat_g * 9) / 4)

    return Targets(
        bmr=round(bmr), tdee=round(tdee), kcal=int(kcal),
        protein_g=protein_g, fat_g=fat_g, carb_g=max(carb_g, 0),
        fiber_g=round(kcal / 1000 * 14),
        water_ml=round(p.weight_kg * 30),
        notes=notes,
    )
