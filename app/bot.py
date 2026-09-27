"""
Telegram-бот. aiogram 3.
Команды: /start /plan /replace /profile /restart /delete /help
Постоянная клавиатура: план, профиль, помощь.
ИИ не используется — весь подбор детерминированный.
"""
import asyncio
import logging
import os
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from . import db, recipes
from .core import ACTIVITY, NotEligible, Profile, activity_factor, calculate
from .planner import (format_plan, load_addons, load_recipes,
                      filter_recipes, relax_and_build)

logging.basicConfig(level=logging.INFO)

RECIPES = load_recipes()
ADDONS = load_addons()

DISCLAIMER = (
    "Это не медицинский сервис. Расчёты носят справочный характер и не заменяют "
    "консультацию врача или диетолога."
)

# Варианты в онбординге и коэффициент, к которому каждый ведёт. Список, а не
# словарь: «активная работа» и «3–5 тренировок» дают одну и ту же нагрузку,
# но человеку это разные ответы. Сами коэффициенты — в core.ACTIVITY.
STEPS_OPTIONS = [
    ("Меньше 4 тысяч", 3000),
    ("4–7 тысяч", 5500),
    ("7–10 тысяч", 8500),
    ("10–13 тысяч", 11500),
    ("Больше 13 тысяч", 14000),
]
FEET_OPTIONS = [
    ("Почти всё время сижу", 0),
    ("1–3 часа на ногах", 2),
    ("4–6 часов", 5),
    ("Весь день на ногах", 8),
]
LABOR_OPTIONS = [
    ("Нет", 0),
    ("Иногда, пару раз в неделю", 1),
    ("Да, почти каждый день", 2),
]
WORKOUT_OPTIONS = [
    ("Не тренируюсь", 0),
    ("1–2 в неделю", 2),
    ("3–4 в неделю", 3),
    ("5–6 в неделю", 5),
    ("Каждый день", 7),
]

ACTIVITY_OPTIONS = [
    ("Сидячий образ жизни, без тренировок", "sedentary"),
    ("Сидячий образ жизни + 1–3 тренировки", "light"),
    ("Сидячий образ жизни + 3–5 тренировок", "moderate"),
    ("Активный образ жизни, без тренировок", "light"),
    ("Активный образ жизни + 1–3 тренировки", "moderate"),
    ("Активный образ жизни + 3–5 тренировок", "high"),
    ("Физический труд + тренировки", "athlete"),
]
# Для показа сохранённого профиля. В базе лежит только коэффициент, а ведут
# к нему разные ответы, поэтому подпись описывает уровень, а не выбор человека.
ACTIVITY_RU = {
    "sedentary": "Сидячий образ жизни, без тренировок",
    "light": "Сидячий образ жизни + 1–3 тренировки или активный без них",
    "moderate": "Сидячий + 3–5 тренировок или активный + 1–3",
    "high": "Активный образ жизни + 3–5 тренировок",
    "athlete": "Физический труд + тренировки",
}
GOAL_RU = {"cut": "Снизить процент жира", "maintain": "Поддержать форму",
           "bulk": "Набрать мышечную массу"}

SOURCES = (
    "На чём считаю: базовый обмен — формула Миффлина–Сан Жеора, "
    "коэффициенты активности стандартные. КБЖУ блюд посчитаны по составу, "
    "справочные значения продуктов — USDA FoodData Central, молочные и готовые "
    "изделия по усреднённым данным российского рынка. Рецепты авторские."
)
TRAINING_HINT = (
    "Про активность стоит ответить честно, но иметь в виду: тренировки "
    "поднимают норму, а не опускают. Переход от сидячего режима к 1–3 "
    "тренировкам даёт примерно +200 ккал в день даже на снижении жира, "
    "к 3–5 — около +370. Это не абстракция: чем выше норма, тем больше блюд "
    "помещается в день, и рацион выходит сытнее и разнообразнее."
)


# Сколько раз в сутки можно собрать новый план. Просмотр уже собранного
# не считается. Счётчик переживает /delete — иначе лимит обходится
# удалением профиля и повторным /start.
DAILY_PLAN_LIMIT = 3

# Режим «нет сил»: в план идут только блюда, которые готовятся не дольше.
# Пустой cook_min — это продукты вроде яблока, их готовить не надо.
MAX_QUICK_MIN = 20

BTN_PLAN = "🍽 План на сегодня"
BTN_QUICK = "😮‍💨 Нет сил: до 20 минут"
BTN_PROFILE = "👤 Мой профиль"
BTN_HELP = "❓ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_PLAN)],
              [KeyboardButton(text=BTN_QUICK)],
              [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True,
)


# Аллергены: подпись кнопки -> что искать в рецептах. Списком, потому что в
# базе одно и то же встречается в разных формах (яйцо/яйца) и в разных полях
# (аллерген «молоко», тег «молочное»). filter_recipes ищет подстроку, поэтому
# склонять за человека нельзя — проще перечислить формы здесь.
ALLERGENS = [
    ("Молочное", ["молоко", "молочное"]),
    ("Глютен", ["глютен"]),
    ("Орехи", ["орех"]),
    ("Арахис", ["арахис"]),
    ("Яйца", ["яйцо", "яйца"]),
    ("Рыба", ["рыба"]),
    ("Соя", ["соя"]),
    ("Кунжут", ["кунжут"]),
]
PREFERENCES = [
    ("Десерты", "десерт"),
    ("Рыба", "рыба"),
    ("Курица", "курица"),
    ("Быстрые блюда", "быстро"),
    ("Вегетарианское", "вегетарианское"),
]


class Onboarding(StatesGroup):
    sex = State()
    age = State()
    height = State()
    weight = State()
    steps = State()
    feet = State()
    workouts = State()
    labor = State()
    goal = State()
    meals = State()
    allergens = State()
    prefers = State()


# Сколько слотов в дне. Разбивка долей — в core.MEAL_SPLIT, туда не лезем.
MEALS_OPTIONS = [
    ("3 раза в день, без перекусов", 3),
    ("3 раза + перекус", 4),
    ("3 раза + два перекуса", 5),
]


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


dp = Dispatcher(storage=MemoryStorage())


async def _ask_sex(m: Message, state: FSMContext):
    await state.set_state(Onboarding.sex)
    # Приветствие отдельным сообщением — только ради постоянной клавиатуры:
    # к одному сообщению нельзя прицепить и её, и кнопки под вопросом, а если
    # опрос оборвётся, человек останется совсем без кнопок.
    await m.answer(
        "Привет! Я соберу для вас план питания под ваши параметры — "
        "с конкретными блюдами и граммовкой, а не просто цифрой калорий.\n\n"
        "Несколько вопросов, меньше минуты.\n\n"
        f"_{SOURCES}_\n\n"
        f"_{DISCLAIMER}_",
        parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB)
    await m.answer(
        "Ваш пол?",
        reply_markup=kb([[("Женский", "sex:female"), ("Мужской", "sex:male")]]),
    )


@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    db.ensure_user(m.from_user.id)
    row = db.get_profile(m.from_user.id)
    if row:
        # Профиль уже заполнен — не гонять человека по шести вопросам заново.
        await state.clear()
        return await m.answer(
            f"С возвращением. Ваша норма: *{row['target_kcal']} ккал*, "
            f"Б {row['protein_g']} · Ж {row['fat_g']} · У {row['carb_g']}.\n\n"
            f"Нажмите «{BTN_PLAN}» — соберу меню на сегодня.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=MAIN_KB)
    await _ask_sex(m, state)


@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    """Вернуть клавиатуру: её можно свернуть, и тогда кнопок не видно."""
    await m.answer("Кнопки на месте.", reply_markup=MAIN_KB)


@dp.message(Command("restart"))
async def cmd_restart(m: Message, state: FSMContext):
    db.ensure_user(m.from_user.id)
    await _ask_sex(m, state)


@dp.message(lambda m: bool(m.text) and "план на сегодня" in m.text.lower())
async def btn_plan(m: Message, state: FSMContext):
    await state.clear()
    await _send_plan(m, m.from_user.id)


@dp.message(lambda m: bool(m.text) and "нет сил" in m.text.lower())
async def btn_quick(m: Message, state: FSMContext):
    await state.clear()
    await _send_plan(m, m.from_user.id, regenerate=True, quick=True)


@dp.message(lambda m: bool(m.text) and "мой профиль" in m.text.lower())
async def btn_profile(m: Message, state: FSMContext):
    await state.clear()
    await cmd_profile(m)


@dp.message(lambda m: bool(m.text) and m.text.lower().strip("❓ ") == "помощь")
async def btn_help(m: Message, state: FSMContext):
    await state.clear()
    await cmd_help(m)


@dp.callback_query(Onboarding.sex, F.data.startswith("sex:"))
async def on_sex(c: CallbackQuery, state: FSMContext):
    await state.update_data(sex=c.data.split(":")[1])
    await state.set_state(Onboarding.age)
    await c.message.edit_text("Сколько вам лет?")
    await c.answer()


@dp.message(Onboarding.age)
async def on_age(m: Message, state: FSMContext):
    if not m.text.isdigit() or not (10 <= int(m.text) <= 100):
        return await m.answer("Введите возраст числом, например 32.")
    await state.update_data(age=int(m.text))
    await state.set_state(Onboarding.height)
    await m.answer("Рост в сантиметрах?")


@dp.message(Onboarding.height)
async def on_height(m: Message, state: FSMContext):
    if not m.text.isdigit() or not (130 <= int(m.text) <= 230):
        return await m.answer("Введите рост в сантиметрах, например 172.")
    await state.update_data(height=int(m.text))
    await state.set_state(Onboarding.weight)
    await m.answer("Вес в килограммах?")


@dp.message(Onboarding.weight)
async def on_weight(m: Message, state: FSMContext):
    try:
        w = float(m.text.replace(",", "."))
        assert 35 <= w <= 250
    except (ValueError, AssertionError):
        return await m.answer("Введите вес в килограммах, например 68 или 68.5.")
    await state.update_data(weight=w)
    await state.set_state(Onboarding.steps)
    await m.answer(
        "Теперь про активность — четыре коротких вопроса. Отвечайте как есть: "
        "коэффициент я посчитаю сам, и от него напрямую зависит ваша норма.\n\n"
        "Сколько шагов в день вы проходите в среднем? Посмотрите в телефоне, "
        "он считает это сам.",
        reply_markup=kb([[(label, f"steps:{v}")] for label, v in STEPS_OPTIONS]))


@dp.callback_query(Onboarding.steps, F.data.startswith("steps:"))
async def on_steps(c: CallbackQuery, state: FSMContext):
    await state.update_data(steps=int(c.data.split(":")[1]))
    await state.set_state(Onboarding.feet)
    await c.message.edit_text(
        "Сколько часов в день вы на ногах помимо ходьбы? Это стоячая работа, "
        "уборка, дети, магазины — всё, что делается не сидя.",
        reply_markup=kb([[(label, f"feet:{v}")] for label, v in FEET_OPTIONS]))
    await c.answer()


@dp.callback_query(Onboarding.feet, F.data.startswith("feet:"))
async def on_feet(c: CallbackQuery, state: FSMContext):
    await state.update_data(feet=int(c.data.split(":")[1]))
    await state.set_state(Onboarding.workouts)
    await c.message.edit_text(
        f"Сколько тренировок в неделю?\n\n_{TRAINING_HINT}_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[(label, f"work:{v}")] for label, v in WORKOUT_OPTIONS]))
    await c.answer()


@dp.callback_query(Onboarding.workouts, F.data.startswith("work:"))
async def on_workouts(c: CallbackQuery, state: FSMContext):
    await state.update_data(workouts=int(c.data.split(":")[1]))
    await state.set_state(Onboarding.labor)
    await c.message.edit_text(
        "Последний вопрос про активность. Есть ли у вас тяжёлый физический "
        "труд — носить тяжести, работать руками смену напролёт: стройка, "
        "склад, цех, уход за лежачим?\n\nПросто быть на ногах — это не он, "
        "про ноги я уже спросил.",
        reply_markup=kb([[(label, f"labor:{v}")] for label, v in LABOR_OPTIONS]))
    await c.answer()


@dp.callback_query(Onboarding.labor, F.data.startswith("labor:"))
async def on_labor(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    labor = int(c.data.split(":")[1])
    factor = activity_factor(d["steps"], d["feet"], d["workouts"], labor)
    await state.update_data(labor=labor, factor=factor)
    await state.set_state(Onboarding.goal)
    await c.message.edit_text(
        f"Ваш коэффициент активности: *{factor}*.\n\nКакая у вас цель?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[(v, f"goal:{k}")] for k, v in GOAL_RU.items()]))
    await c.answer()


def _pick_kb(options: list[str], chosen: set[str], prefix: str,
             done_label: str) -> InlineKeyboardMarkup:
    rows = [[(("✓ " if o in chosen else "") + o, f"{prefix}:{o}")] for o in options]
    rows.append([(done_label, f"{prefix}:__done__")])
    return kb(rows)


ALLERGEN_LABELS = [label for label, _ in ALLERGENS]
PREFERENCE_LABELS = [label for label, _ in PREFERENCES]


@dp.callback_query(Onboarding.goal, F.data.startswith("goal:"))
async def on_goal(c: CallbackQuery, state: FSMContext):
    await state.update_data(goal=c.data.split(":")[1])
    await state.set_state(Onboarding.meals)
    await c.message.edit_text(
        "Сколько раз в день вам удобно есть?\n\n"
        "_Чем выше ваша норма, тем важнее разбить её на большее число приёмов: "
        "иначе на один приём приходится порция, которую тяжело съесть._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[(label, f"meals:{n}")] for label, n in MEALS_OPTIONS]))
    await c.answer()


@dp.callback_query(Onboarding.meals, F.data.startswith("meals:"))
async def on_meals(c: CallbackQuery, state: FSMContext):
    await state.update_data(meals=int(c.data.split(":")[1]), allergens=[])
    await state.set_state(Onboarding.allergens)
    await c.message.edit_text(
        "Есть ли у вас аллергия или непереносимость? Отметьте всё, что нельзя "
        "— такие блюда я в план не поставлю.",
        reply_markup=_pick_kb(ALLERGEN_LABELS, set(), "alg", "Готово / нет аллергий"))
    await c.answer()


@dp.callback_query(Onboarding.allergens, F.data.startswith("alg:"))
async def on_allergens(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    chosen = set(d.get("allergens", []))
    value = c.data.split(":", 1)[1]
    if value != "__done__":
        chosen.symmetric_difference_update({value})
        await state.update_data(allergens=sorted(chosen))
        await c.message.edit_reply_markup(
            reply_markup=_pick_kb(ALLERGEN_LABELS, chosen, "alg",
                                  "Готово / нет аллергий"))
        return await c.answer()

    await state.update_data(prefers=[])
    await state.set_state(Onboarding.prefers)
    await c.message.edit_text(
        "Что любите? Отмеченное буду ставить в план чаще — но не всегда: "
        "норма важнее.",
        reply_markup=_pick_kb(PREFERENCE_LABELS, set(), "prf", "Готово / пропустить"))
    await c.answer()


@dp.callback_query(Onboarding.prefers, F.data.startswith("prf:"))
async def on_prefers(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    chosen = set(d.get("prefers", []))
    value = c.data.split(":", 1)[1]
    if value != "__done__":
        chosen.symmetric_difference_update({value})
        await state.update_data(prefers=sorted(chosen))
        await c.message.edit_reply_markup(
            reply_markup=_pick_kb(PREFERENCE_LABELS, chosen, "prf",
                                  "Готово / пропустить"))
        return await c.answer()

    d = await state.get_data()
    await state.clear()
    await _finish_onboarding(c, d)


async def _finish_onboarding(c: CallbackQuery, d: dict):
    factor = d.get("factor")
    # ключ нужен только для подписи в профиле: в расчёт идёт коэффициент
    nearest = min(ACTIVITY, key=lambda k: abs(ACTIVITY[k] - (factor or 1.55)))
    p = Profile(sex=d["sex"], age=d["age"], height_cm=d["height"],
                weight_kg=d["weight"], activity=d.get("activity", nearest),
                goal=d["goal"], meals_per_day=d.get("meals", 4),
                activity_factor=factor)
    try:
        t = calculate(p)
    except NotEligible as e:
        await c.message.edit_text(f"{e}\n\nЕсли захотите начать заново — /start")
        return await c.answer()

    db.save_profile(c.from_user.id, p, t)
    terms = [term for label, terms in ALLERGENS if label in d.get("allergens", [])
             for term in terms]
    db.set_exclusions(c.from_user.id, terms)
    db.set_prefer_tags(c.from_user.id, [tag for label, tag in PREFERENCES
                                        if label in d.get("prefers", [])])
    notes = ("\n\n" + "\n".join(f"_{n}_" for n in t.notes)) if t.notes else ""
    # Сочетание аллергенов может вырезать слишком много: «молоко + глютен»
    # оставляет 24 блюда из 83, и план не собирается. Честнее сказать сразу,
    # чем показывать отказ на каждый запрос плана.
    if not relax_and_build(t, filter_recipes(RECIPES, terms), ADDONS,
                           meals=p.meals_per_day, seed=1)[0]:
        hint = ("С такими ограничениями" if terms else "Под такую норму")
        more_meals = ("\nПопробуйте выбрать больше приёмов пищи — /restart: "
                      "на высокой норме день из трёх приёмов часто не собирается."
                      if p.meals_per_day < 5 else "")
        notes += (f"\n\n_{hint} в базе пока мало блюд, чтобы собрать день. "
                  f"Я буду пробовать, но план может не получаться — база "
                  f"пополняется.{more_meals}_")
    await c.message.edit_text(
        f"Готово. Ваша норма:\n\n"
        f"*{t.kcal} ккал* в день\n"
        f"Белки {t.protein_g} г · Жиры {t.fat_g} г · Углеводы {t.carb_g} г\n"
        f"Клетчатка {t.fiber_g} г · Вода {t.water_ml} мл{notes}",
        parse_mode=ParseMode.MARKDOWN)
    # Клавиатуру нельзя прицепить к отредактированному сообщению — шлём отдельным.
    await c.message.answer(
        f"Профиль сохранён, второй раз заполнять не придётся.\n"
        f"Нажмите «{BTN_PLAN}» — соберу меню на сегодня.",
        reply_markup=MAIN_KB)
    await c.answer()


def _activity_line(row: dict) -> str:
    """Строка активности в профиле: коэффициент, если он посчитан по опросу."""
    factor = row.get("activity_factor")
    if factor:
        return f"Коэффициент активности {factor}"
    return ACTIVITY_RU.get(row["activity"], row["activity"])


def _targets_from_row(row: dict):
    from .core import Targets
    return Targets(bmr=0, tdee=0, kcal=row["target_kcal"], protein_g=row["protein_g"],
                   fat_g=row["fat_g"], carb_g=row["carb_g"],
                   fiber_g=round(row["target_kcal"] / 1000 * 14),
                   water_ml=round(row["weight_kg"] * 30), notes=[])


PLAN_BUTTONS = kb([[("Другой вариант", "replan")], [("📖 Прислать рецепты", "recipes")]])


async def _send_throttled(m: Message, text: str, pause: float = 1.0) -> None:
    """Telegram пропускает примерно одно сообщение в секунду на чат и отвечает
    429 с retry_after, если частить. Рецепты идут пачкой, поэтому ждём."""
    while True:
        try:
            await m.answer(text)
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
    await asyncio.sleep(pause)


def _quick_pool(pool: list) -> list:
    return [r for r in pool
            if not r.cook_min or float(str(r.cook_min).replace(",", ".")) <= MAX_QUICK_MIN]


async def _send_plan(m: Message, tg_id: int, seed: int | None = None,
                     regenerate: bool = False, quick: bool = False):
    row = db.get_profile(tg_id)
    if not row:
        return await m.answer(
            "Не нахожу ваш профиль. Если вы его уже заполняли, значит данные "
            "не сохранились при обновлении бота — извините. Пройдите /start, "
            "это минута.", reply_markup=MAIN_KB)

    today = date.today()
    saved = db.get_plan(tg_id, today)
    if saved and not regenerate:
        # Уже собранный план показываем как есть: просмотр попытку не тратит.
        return await m.answer(saved["text"], parse_mode=ParseMode.MARKDOWN,
                              reply_markup=PLAN_BUTTONS)
    if db.plans_today(tg_id, today) >= DAILY_PLAN_LIMIT:
        return await m.answer(
            f"На сегодня лимит: {DAILY_PLAN_LIMIT} подбора в день. "
            f"Завтра соберу новый план.\n\n"
            f"Показать сегодняшний — «{BTN_PLAN}».", reply_markup=MAIN_KB)
    import json as _json
    t = _targets_from_row(row)
    pool = filter_recipes(RECIPES, _json.loads(row["exclusions"] or "[]"))
    if quick:
        pool = _quick_pool(pool)
    plans, notes = relax_and_build(
        t, pool, ADDONS, meals=row["meals_per_day"],
        recent_ids=db.recent_recipe_ids(tg_id),
        prefer_tags=_json.loads(row["prefer_tags"] or "[]"), seed=seed)
    if not plans:
        return await m.answer("\n".join(notes) or "Не удалось собрать план.")
    plan = plans[0]
    text = format_plan(plan, t)
    if notes:
        text += "\n\n" + "\n".join(f"_{n}_" for n in notes)
    db.save_plan(tg_id, today, [i.recipe.id for i in plan.items], text,
                 quick=quick, scales=[i.scale for i in plan.items])
    if quick:
        text += f"\n\n_Режим «нет сил»: всё готовится за {MAX_QUICK_MIN} минут или быстрее._"
    used = db.count_plan(tg_id, today)
    left = DAILY_PLAN_LIMIT - used
    if left <= 1:
        text += (f"\n\n_Это последний подбор на сегодня._" if left == 1
                 else "")
    await m.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=PLAN_BUTTONS)


@dp.message(Command("quick"))
async def cmd_quick(m: Message):
    await _send_plan(m, m.from_user.id, regenerate=True, quick=True)


@dp.message(Command("plan"))
async def cmd_plan(m: Message):
    await _send_plan(m, m.from_user.id)


@dp.callback_query(F.data == "replan")
async def cb_replan(c: CallbackQuery):
    import random
    saved = db.get_plan(c.from_user.id, date.today())
    if saved and saved.get("accepted"):
        return await c.answer(
            "Рецепты на сегодня уже выданы — менять план можно до этого. "
            "Новый план будет завтра.", show_alert=True)
    await _send_plan(c.message, c.from_user.id, seed=random.randint(1, 10 ** 6),
                     regenerate=True, quick=bool(saved and saved.get("quick")))
    await c.answer()


@dp.callback_query(F.data == "recipes")
async def cb_recipes(c: CallbackQuery):
    saved = db.get_plan(c.from_user.id, date.today())
    if not saved:
        return await c.answer("Сначала соберите план.", show_alert=True)

    # Отвечаем сразу: рецепты идут с паузами, а кнопка ждать не умеет —
    # Telegram гасит её по таймауту, и человек видит зависшую кнопку.
    await c.answer("Собираю рецепты…")
    db.accept_plan(c.from_user.id, date.today())
    titles = {r.id: r.title for r in RECIPES}
    # продукты вроде «Яблоко, 1 шт.» карточки не имеют — готовить нечего
    with_cards = [rid for rid in saved["recipe_ids"] if recipes.card(rid)]
    cards = [recipes.card(rid) for rid in with_cards]
    sent = len(cards)

    # Трассировка: если сборник всплывёт где-то ещё, видно, чей это экземпляр.
    who = f"@{c.from_user.username}" if c.from_user.username else f"id {c.from_user.id}"
    footer = (f"\n\n— — —\nРецепт из авторского сборника проекта «Порция». "
              f"Экземпляр подготовлен для {who}. Личное использование; "
              f"публикация и перепродажа не разрешены.")
    for part in recipes.pack(cards, limit=recipes.TELEGRAM_LIMIT - len(footer) - 8):
        await _send_throttled(c.message, part + footer)

    collected = db.log_recipes(c.from_user.id, with_cards)
    logging.info("рецепты выданы: user=%s разных_всего=%s", c.from_user.id, collected)
    simple = [titles.get(r, r) for r in saved["recipe_ids"] if not recipes.card(r)]
    tail = ("\n\nБез рецепта: " + ", ".join(simple) + " — готовить нечего.") if simple else ""
    await c.message.answer(
        f"Это все рецепты на сегодня ({sent}).{tail}\n\n"
        f"План на сегодня зафиксирован — заменить блюда уже нельзя. "
        f"Новый план соберётся завтра.",
        reply_markup=kb([[("🛒 Список продуктов", "shoplist")]]))


def _shopping_view(items: list[str], checked: set[int]) -> tuple[str, InlineKeyboardMarkup]:
    """Текст со списком и клавиатура из номеров. Номера, а не названия:
    сорок кнопок с текстом не помещаются на экран телефона."""
    from html import escape
    lines = ["<b>Список продуктов на день</b>", ""]
    for n, item in enumerate(items, 1):
        body = escape(item)
        lines.append(f"{n}. <s>{body}</s>" if n - 1 in checked else f"{n}. {body}")
    lines.append("")
    lines.append(f"Отмечено {len(checked)} из {len(items)}. "
                 f"Нажмите номер, чтобы вычеркнуть то, что уже есть.")
    rows, row = [], []
    for n in range(1, len(items) + 1):
        row.append((f"{'·' if n - 1 in checked else ''}{n}", f"shop:{n - 1}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([("Снять все отметки", "shop:reset")])
    return "\n".join(lines), kb(rows)


@dp.callback_query(F.data == "shoplist")
async def cb_shoplist(c: CallbackQuery):
    saved = db.get_plan(c.from_user.id, date.today())
    if not saved:
        return await c.answer("Сначала соберите план.", show_alert=True)
    await c.answer()
    scales = saved.get("scales") or [1.0] * len(saved["recipe_ids"])
    items = recipes.shopping_list(list(zip(saved["recipe_ids"], scales)))
    if not items:
        return await c.message.answer("Для этого плана состав не указан.")
    db.save_shopping(c.from_user.id, date.today(), items, [])
    text, markup = _shopping_view(items, set())
    await c.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@dp.callback_query(F.data.startswith("shop:"))
async def cb_shop_toggle(c: CallbackQuery):
    data = db.get_shopping(c.from_user.id, date.today())
    if not data:
        return await c.answer("Список устарел, соберите заново.", show_alert=True)
    checked = set(data["checked"])
    value = c.data.split(":", 1)[1]
    if value == "reset":
        checked.clear()
    else:
        checked.symmetric_difference_update({int(value)})
    db.save_shopping(c.from_user.id, date.today(), data["items"], sorted(checked))
    text, markup = _shopping_view(data["items"], checked)
    await c.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    await c.answer()


@dp.message(Command("replace"))
async def cmd_replace(m: Message):
    await _send_plan(m, m.from_user.id, seed=int(date.today().strftime("%j")) + 7,
                     regenerate=True)


@dp.message(Command("profile"))
async def cmd_profile(m: Message):
    row = db.get_profile(m.from_user.id)
    if not row:
        return await m.answer("Профиля пока нет — /start")
    await m.answer(
        f"*Ваш профиль*\n"
        f"{'Женский' if row['sex'] == 'female' else 'Мужской'} пол, {row['age']} лет\n"
        f"{row['height_cm']} см, {row['weight_kg']} кг\n"
        f"{_activity_line(row)}\n"
        f"Цель: {GOAL_RU[row['goal']]}\n\n"
        f"*Норма:* {row['target_kcal']} ккал · Б {row['protein_g']} · "
        f"Ж {row['fat_g']} · У {row['carb_g']}\n\n"
        f"Изменить параметры — /restart\nУдалить данные — /delete",
        parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB)


@dp.message(Command("delete"))
async def cmd_delete(m: Message):
    db.delete_user(m.from_user.id)
    await m.answer("Профиль и планы удалены. Начать заново — /start.\n"
                   "Счётчик подборов и журнал выданных рецептов остаются: в них\n"
                   "нет ваших параметров, только числа.")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    """Кто сколько собрал. Видна только владельцу — ADMIN_ID в окружении."""
    admin = os.environ.get("ADMIN_ID")
    if not admin or str(m.from_user.id) != admin:
        return
    total = len([r for r in RECIPES if r.kind != "product"])
    rows = db.collectors()
    if not rows:
        return await m.answer("Рецепты пока никому не выдавались.")
    lines = [f"Собрано рецептов из {total}:"]
    for r in rows:
        share = r["unique_recipes"] / total
        flag = " (!)" if share >= 0.5 else ""
        lines.append(f"{r['telegram_id']}: {r['unique_recipes']} "
                     f"({share:.0%}), выдач {r['deliveries']}, "
                     f"с {r['started'][:10]}{flag}")
    await m.answer("\n".join(lines))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/plan — план питания на сегодня\n"
        "/replace — собрать другой вариант\n"
        "/profile — ваши параметры и норма\n"
        "/restart — заполнить параметры заново\n"
        "/menu — вернуть кнопки, если пропали\n"
        "/delete — удалить все данные\n\n"
        f"_{SOURCES}_\n\n"
        f"_{DISCLAIMER}_", parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB)


# --- запасные обработчики. Регистрируются последними, поэтому срабатывают
# только если ничего выше не подошло. ---

ONBOARDING_PREFIXES = {"sex", "act", "steps", "feet", "work", "labor",
                       "goal", "meals", "alg", "prf"}


@dp.callback_query(lambda c: bool(c.data) and c.data.split(":")[0] in ONBOARDING_PREFIXES)
async def cb_stale_onboarding(c: CallbackQuery, state: FSMContext):
    """Кнопка опроса без состояния. Состояние живёт в памяти процесса, поэтому
    после перезапуска бота оно теряется, и кнопка молча переставала работать."""
    await state.clear()
    await c.answer("Опрос прервался — бот обновился. Начните заново: /start",
                   show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.message()
async def fallback(m: Message):
    """Что угодно, чего бот не понял. Раньше он просто молчал."""
    if db.get_profile(m.from_user.id):
        await m.answer(
            f"Не понял. Нажмите «{BTN_PLAN}» или выберите команду в меню — "
            f"список есть в /help.", reply_markup=MAIN_KB)
    else:
        await m.answer("Чтобы я собрал план, пройдите короткий опрос — /start")


async def main():
    # Локально токен лежит в .env; на Railway он приходит из Variables,
    # и python-dotenv там не нужен — отсюда мягкий импорт.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = os.environ["BOT_TOKEN"]
    db.init()
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    logging.info("База данных: %s", db.DB_PATH)
    logging.info("Том Railway: %s", volume or "НЕ ПОДКЛЮЧЁН — профили сотрутся "
                                             "при следующем обновлении")
    bot = Bot(token=token)
    await bot.set_my_commands([
        BotCommand(command="plan", description="План питания на сегодня"),
        BotCommand(command="quick", description="План без сил: до 20 минут"),
        BotCommand(command="replace", description="Собрать другой вариант"),
        BotCommand(command="profile", description="Параметры и норма"),
        BotCommand(command="restart", description="Заполнить параметры заново"),
        BotCommand(command="menu", description="Вернуть кнопки"),
        BotCommand(command="delete", description="Удалить все данные"),
        BotCommand(command="help", description="Что умеет бот"),
    ])
    # /stats — только владельцу: она и в меню появляется только у него.
    admin = os.environ.get("ADMIN_ID")
    if admin and admin.isdigit():
        from aiogram.types import BotCommandScopeChat
        await bot.set_my_commands(
            [BotCommand(command="stats", description="Кто сколько рецептов собрал"),
             BotCommand(command="plan", description="План питания на сегодня"),
             BotCommand(command="quick", description="План без сил: до 20 минут"),
             BotCommand(command="profile", description="Параметры и норма"),
             BotCommand(command="help", description="Что умеет бот")],
            scope=BotCommandScopeChat(chat_id=int(admin)))
        logging.info("ADMIN_ID=%s, /stats доступна", admin)
    else:
        logging.warning("ADMIN_ID не задан — /stats не будет отвечать никому. "
                        "Добавьте переменную в Railway, значение узнать у @userinfobot")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
