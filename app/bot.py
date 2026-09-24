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
from .core import ACTIVITY, NotEligible, Profile, calculate
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

BTN_PLAN = "🍽 План на сегодня"
BTN_PROFILE = "👤 Мой профиль"
BTN_HELP = "❓ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_PLAN)],
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
    activity = State()
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
    await m.answer(
        "Привет! Я соберу для вас план питания под ваши параметры — "
        "с конкретными блюдами и граммовкой, а не просто цифрой калорий.\n\n"
        "Шесть вопросов, меньше минуты.\n\n"
        f"_{SOURCES}_\n\n"
        f"_{DISCLAIMER}_\n\nВаш пол?",
        parse_mode=ParseMode.MARKDOWN,
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


@dp.message(Command("restart"))
async def cmd_restart(m: Message, state: FSMContext):
    db.ensure_user(m.from_user.id)
    await _ask_sex(m, state)


@dp.message(F.text == BTN_PLAN)
async def btn_plan(m: Message, state: FSMContext):
    await state.clear()
    await _send_plan(m, m.from_user.id)


@dp.message(F.text == BTN_PROFILE)
async def btn_profile(m: Message, state: FSMContext):
    await state.clear()
    await cmd_profile(m)


@dp.message(F.text == BTN_HELP)
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
    await state.set_state(Onboarding.activity)
    await m.answer(
        f"Насколько вы активны?\n\n_{TRAINING_HINT}_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[(label, f"act:{key}")] for label, key in ACTIVITY_OPTIONS]))


@dp.callback_query(Onboarding.activity, F.data.startswith("act:"))
async def on_activity(c: CallbackQuery, state: FSMContext):
    await state.update_data(activity=c.data.split(":")[1])
    await state.set_state(Onboarding.goal)
    await c.message.edit_text("Какая у вас цель?", reply_markup=kb(
        [[(v, f"goal:{k}")] for k, v in GOAL_RU.items()]))
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
    p = Profile(sex=d["sex"], age=d["age"], height_cm=d["height"],
                weight_kg=d["weight"], activity=d["activity"], goal=d["goal"],
                meals_per_day=d.get("meals", 4))
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


async def _send_plan(m: Message, tg_id: int, seed: int | None = None,
                     regenerate: bool = False):
    row = db.get_profile(tg_id)
    if not row:
        return await m.answer("Сначала пройдите короткий опрос — /start",
                              reply_markup=MAIN_KB)

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
    db.save_plan(tg_id, today, [i.recipe.id for i in plan.items], text)
    used = db.count_plan(tg_id, today)
    left = DAILY_PLAN_LIMIT - used
    if left <= 1:
        text += (f"\n\n_Это последний подбор на сегодня._" if left == 1
                 else "")
    await m.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=PLAN_BUTTONS)


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
                     regenerate=True)
    await c.answer()


@dp.callback_query(F.data == "recipes")
async def cb_recipes(c: CallbackQuery):
    saved = db.get_plan(c.from_user.id, date.today())
    if not saved:
        return await c.answer("Сначала соберите план.", show_alert=True)

    db.accept_plan(c.from_user.id, date.today())
    titles = {r.id: r.title for r in RECIPES}
    # продукты вроде «Яблоко, 1 шт.» карточки не имеют — готовить нечего
    cards = [recipes.card(rid) for rid in saved["recipe_ids"] if recipes.card(rid)]
    sent = len(cards)
    for part in recipes.pack(cards):
        await _send_throttled(c.message, part)
    simple = [titles.get(r, r) for r in saved["recipe_ids"] if not recipes.card(r)]
    tail = ("\n\nБез рецепта: " + ", ".join(simple) + " — готовить нечего.") if simple else ""
    await c.message.answer(
        f"Это все рецепты на сегодня ({sent}).{tail}\n\n"
        f"План на сегодня зафиксирован — заменить блюда уже нельзя. "
        f"Новый план соберётся завтра.")
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
        f"{ACTIVITY_RU[row['activity']]}\n"
        f"Цель: {GOAL_RU[row['goal']]}\n\n"
        f"*Норма:* {row['target_kcal']} ккал · Б {row['protein_g']} · "
        f"Ж {row['fat_g']} · У {row['carb_g']}\n\n"
        f"Изменить параметры — /restart\nУдалить данные — /delete",
        parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB)


@dp.message(Command("delete"))
async def cmd_delete(m: Message):
    db.delete_user(m.from_user.id)
    await m.answer("Профиль и планы удалены. Начать заново — /start.\n"
                   "Счётчик подборов за сегодня сохраняется — он не содержит ваших данных.")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/plan — план питания на сегодня\n"
        "/replace — собрать другой вариант\n"
        "/profile — ваши параметры и норма\n"
        "/restart — заполнить параметры заново\n"
        "/delete — удалить все данные\n\n"
        f"_{SOURCES}_\n\n"
        f"_{DISCLAIMER}_", parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB)


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
    bot = Bot(token=token)
    await bot.set_my_commands([
        BotCommand(command="plan", description="План питания на сегодня"),
        BotCommand(command="replace", description="Собрать другой вариант"),
        BotCommand(command="profile", description="Параметры и норма"),
        BotCommand(command="restart", description="Заполнить параметры заново"),
        BotCommand(command="delete", description="Удалить все данные"),
        BotCommand(command="help", description="Что умеет бот"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
