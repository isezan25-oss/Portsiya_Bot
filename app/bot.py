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
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from . import db
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

ACTIVITY_RU = {
    "sedentary": "Сидячая работа, без тренировок",
    "light": "1–3 тренировки в неделю",
    "moderate": "3–5 тренировок в неделю",
    "high": "6–7 тренировок в неделю",
    "athlete": "Физический труд + тренировки",
}
GOAL_RU = {"cut": "Снизить процент жира", "maintain": "Удержать вес",
           "bulk": "Набрать мышечную массу"}


BTN_PLAN = "🍽 План на сегодня"
BTN_PROFILE = "👤 Мой профиль"
BTN_HELP = "❓ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_PLAN)],
              [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True,
)


class Onboarding(StatesGroup):
    sex = State()
    age = State()
    height = State()
    weight = State()
    activity = State()
    goal = State()


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
    await m.answer("Насколько вы активны?", reply_markup=kb(
        [[(v, f"act:{k}")] for k, v in ACTIVITY_RU.items()]))


@dp.callback_query(Onboarding.activity, F.data.startswith("act:"))
async def on_activity(c: CallbackQuery, state: FSMContext):
    await state.update_data(activity=c.data.split(":")[1])
    await state.set_state(Onboarding.goal)
    await c.message.edit_text("Какая у вас цель?", reply_markup=kb(
        [[(v, f"goal:{k}")] for k, v in GOAL_RU.items()]))
    await c.answer()


@dp.callback_query(Onboarding.goal, F.data.startswith("goal:"))
async def on_goal(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    p = Profile(sex=d["sex"], age=d["age"], height_cm=d["height"],
                weight_kg=d["weight"], activity=d["activity"],
                goal=c.data.split(":")[1])
    try:
        t = calculate(p)
    except NotEligible as e:
        await c.message.edit_text(f"{e}\n\nЕсли захотите начать заново — /start")
        return await c.answer()

    db.save_profile(c.from_user.id, p, t)
    notes = ("\n\n" + "\n".join(f"_{n}_" for n in t.notes)) if t.notes else ""
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


async def _send_plan(m: Message, tg_id: int, seed: int | None = None):
    row = db.get_profile(tg_id)
    if not row:
        return await m.answer("Сначала пройдите короткий опрос — /start",
                              reply_markup=MAIN_KB)
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
    db.save_plan(tg_id, date.today(), [i.recipe.id for i in plan.items], text)
    await m.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(
        [[("Другой вариант", "replan")]]))


@dp.message(Command("plan"))
async def cmd_plan(m: Message):
    await _send_plan(m, m.from_user.id)


@dp.callback_query(F.data == "replan")
async def cb_replan(c: CallbackQuery):
    import random
    await _send_plan(c.message, c.from_user.id, seed=random.randint(1, 10 ** 6))
    await c.answer()


@dp.message(Command("replace"))
async def cmd_replace(m: Message):
    await _send_plan(m, m.from_user.id, seed=int(date.today().strftime("%j")) + 7)


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
    await m.answer("Все ваши данные удалены. Начать заново — /start")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/plan — план питания на сегодня\n"
        "/replace — собрать другой вариант\n"
        "/profile — ваши параметры и норма\n"
        "/restart — заполнить параметры заново\n"
        "/delete — удалить все данные\n\n"
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
