import asyncio
import json
import logging
import os
import sys
from typing import Dict, Any, Tuple, Optional, List

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

# ==================================================
# 1. КОНФИГУРАЦИЯ И ДАННЫЕ
# ==================================================
load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8636744905:AAFfWxamEjs7gT4lxdwrFswXP8mCHUa_dEM").strip()
ADMIN_GROUP_ID_RAW: str = os.getenv("ADMIN_GROUP_ID", "-1004402782952").strip()
ADMIN_USER_IDS_RAW: str = os.getenv("ADMIN_USER_IDS", "6179631430").strip()

if not BOT_TOKEN:
    sys.exit("Ошибка: Переменная BOT_TOKEN не задана")

if not ADMIN_GROUP_ID_RAW:
    sys.exit("Ошибка: Переменная ADMIN_GROUP_ID не задана")

try:
    ADMIN_GROUP_ID: int = int(ADMIN_GROUP_ID_RAW)
except ValueError:
    sys.exit("Ошибка: ADMIN_GROUP_ID должен быть целым числом")

ADMIN_USER_IDS: List[int] = []
if ADMIN_USER_IDS_RAW:
    for uid in ADMIN_USER_IDS_RAW.split(","):
        uid_clean = uid.strip()
        if uid_clean:
            try:
                ADMIN_USER_IDS.append(int(uid_clean))
            except ValueError:
                sys.exit(f"Ошибка: Некорректный ID в ADMIN_USER_IDS: {uid_clean}")

DB_PATH = "veritas_bot.db"
FINAL_SUCCESS_TEXT = "Вᴀɯᴀ ᴀнᴋᴇᴛᴀ ᴨᴩиняᴛᴀ! Сᴛᴀᴩᴀᴇʍᴄя ᴏᴛʙᴇᴛиᴛь ʙᴀʍ ᴋᴀᴋ ʍᴏжнᴏ быᴄᴛᴩᴇᴇ ୨ৎ"
QUESTION_INTRO_TEXT = "Еᴄᴛь ʙᴏᴨᴩᴏᴄ? Нᴀᴨиɯиᴛᴇ ᴇᴦᴏ ᴄюдᴀ — ᴨᴏᴄᴛᴀᴩᴀᴇʍᴄя ᴏᴛʙᴇᴛиᴛь ᴋᴀᴋ ʍᴏжнᴏ ᴄᴋᴏᴩᴇᴇ.˚.ׄ𓈒✧"

# ==================================================
# 2. БАЗА ДАННЫХ (SQLite / aiosqlite)
# ==================================================
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_code TEXT UNIQUE NOT NULL,
            app_type TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            direction TEXT,
            data_json TEXT NOT NULL,
            group_chat_id INTEGER,
            group_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'submitted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_group_msg 
        ON applications (group_chat_id, group_message_id);
        """)
        await db.commit()

async def get_user_submitted_types(user_id: int) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT app_type FROM applications WHERE user_id = ?", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def create_application(
    app_type: str,
    user_id: int,
    username: Optional[str],
    direction: Optional[str],
    data_dict: Dict[str, Any]
) -> Tuple[int, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        if app_type == "volunteer":
            prefix = "V"
        elif app_type == "student":
            prefix = "S"
        else:
            prefix = "Q"

        async with db.execute(
            "SELECT COUNT(*) FROM applications WHERE app_type = ?", (app_type,)
        ) as cursor:
            row = await cursor.fetchone()
            count = (row[0] if row else 0) + 1

        app_code = f"{prefix}-{count:04d}"

        cursor = await db.execute(
            """
            INSERT INTO applications (app_code, app_type, user_id, username, direction, data_json, status)
            VALUES (?, ?, ?, ?, ?, ?, 'submitted')
            """,
            (app_code, app_type, user_id, username, direction, json.dumps(data_dict, ensure_ascii=False))
        )
        app_id = cursor.lastrowid
        await db.commit()
        return app_id, app_code

async def update_app_message_id(app_id: int, group_chat_id: int, group_message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE applications 
            SET group_chat_id = ?, group_message_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (group_chat_id, group_message_id, app_id)
        )
        await db.commit()

async def get_app_by_message(group_chat_id: int, group_message_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM applications 
            WHERE group_chat_id = ? AND group_message_id = ?
            """,
            (group_chat_id, group_message_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

# ==================================================
# 3. FSM СОСТОЯНИЯ
# ==================================================
class VolunteerStates(StatesGroup):
    choosing_direction = State()
    q_fullname = State()
    q_age = State()
    q_location = State()
    q_timezone = State()
    q_username = State()
    q_time_commitment = State()
    q_motivation = State()
    q_experience = State()
    answering_theme = State()
    confirm = State()
    editing_field = State()

class StudentStates(StatesGroup):
    choosing_direction = State()
    q_fullname = State()
    q_age = State()
    q_location = State()
    q_timezone = State()
    q_username = State()
    q_english_level = State()
    q_math_class = State()
    q_math_material = State()
    q_goal = State()
    q_hardest = State()
    choosing_format = State()
    test_q1 = State()
    test_q2 = State()
    test_q3 = State()
    test_q4 = State()
    test_q5 = State()
    q_schedule = State()
    q_extra = State()
    confirm = State()
    editing_field = State()

class QuestionStates(StatesGroup):
    waiting_for_question = State()

# ==================================================
# 4. ТЕКСТЫ И НАПРАВЛЕНИЯ
# ==================================================
DIRECTION_NAMES: Dict[str, str] = {
    "dir_smm": "SMM-специалист (TikTok)",
    "dir_copywriter": "Копирайтер (Telegram-канал)",
    "dir_coord": "Координатор учебной группы",
    "dir_tutor_en": "Тьютор по английскому языку в группе",
    "dir_rep_en": "Индивидуальный репетитор по английскому языку",
    "dir_speaking": "Ведущий Speaking Clubs",
    "dir_tutor_math": "Тьютор по математике в группе",
    "dir_rep_math": "Индивидуальный репетитор по математике (5-11 классы)"
}

VOLUNTEER_THEME_QUESTIONS: Dict[str, List[str]] = {
    "dir_smm": [
        "Умеете ли вы снимать и монтировать короткие видео? Какими приложениями пользуетесь? Есть ли у вас примеры работ? Если да — отправьте ссылку.",
        "Какую идею для TikTok Veritas Academy вы бы предложили?"
    ],
    "dir_copywriter": [
        "Есть ли у вас опыт написания постов или ведения Telegram-каналов? Если есть — отправьте примеры ваших работ.",
        "Какие тексты вам нравится создавать?",
        "Какой пост для Veritas Academy вы бы предложили написать первым?"
    ],
    "dir_coord": [
        "Был ли у вас опыт организации людей, мероприятий или учебного процесса?",
        "Как вы обычно работаете с расписанием и дедлайнами?",
        "Что вы будете делать, если студент регулярно пропускает занятия?",
        "Как поступите, если тьютор не выходит на связь перед занятием?"
    ],
    "dir_tutor_en": [
        "С какой группой вам удобнее работать (уровни от A0 до B2)?",
        "Какие навыки английского вы готовы преподавать?",
        "Есть ли у вас опыт преподавания или помощи другим в изучении английского?",
        "Как бы вы объяснили сложную тему ученику, который никак её не понимает?"
    ],
    "dir_rep_en": [
        "Какой у вас уровень английского и с какими уровнями учеников вы готовы работать?",
        "Есть ли у вас опыт индивидуального преподавания?",
        "Какие цели учеников вы готовы помогать достигать?",
        "Как вы планируете отслеживать прогресс ученика?"
    ],
    "dir_speaking": [
        "Какой у вас уровень английского?",
        "Был ли у вас опыт проведения разговорных клубов или подобных мероприятий?",
        "Какие темы и активности вы хотели бы использовать на Speaking Club?",
        "Как вы вовлечёте в разговор ученика, который стесняется говорить?"
    ],
    "dir_tutor_math": [
        "С какими классами вам удобнее работать (от 5 до 11)?",
        "Что готовы преподавать: алгебру, геометрию или оба направления?",
        "Есть ли у вас опыт объяснения математики другим людям?",
        "Как бы вы объяснили сложную тему ученику, который её не понимает?"
    ],
    "dir_rep_math": [
        "С какими классами вам удобнее работать (от 5 до 11)?",
        "Есть ли у вас опыт индивидуального преподавания?",
        "С какими проблемами учеников вы готовы работать?",
        "Как вы планируете отслеживать прогресс ученика?"
    ]
}

FIELD_QUESTIONS_VOLUNTEER = {
    "q_fullname": "Укажите ваше имя и фамилию.",
    "q_age": "Сколько вам лет?",
    "q_location": "Из какой вы страны?",
    "q_timezone": "Укажите ваш часовой пояс.",
    "q_username": "Укажите ваш Telegram username, если он есть.",
    "q_time_commitment": "Сколько времени вы готовы уделять Veritas Academy в неделю?",
    "q_motivation": "Почему вы хотите стать волонтёром именно в Veritas Academy?",
    "q_experience": "Расскажите об опыте, который может быть полезен для выбранного направления."
}

FIELD_QUESTIONS_STUDENT = {
    "q_fullname": "Укажите ваше имя и фамилию.",
    "q_age": "Сколько вам лет?",
    "q_location": "Из какой вы страны?",
    "q_timezone": "Укажите ваш часовой пояс.",
    "q_username": "Укажите ваш Telegram username, если он есть.",
    "q_english_level": "Какой у вас примерный уровень английского (от A0 до C1)?",
    "q_math_class": "В каком вы сейчас классе?",
    "q_math_material": "Материал за какой класс вам необходимо изучать / подтянуть?",
    "q_goal": "Для чего вы хотите изучать выбранные предметы? Расскажите о вашей главной цели.",
    "q_hardest": "Что сейчас даётся вам сложнее всего?",
    "q_schedule": "В какое время и в какие дни вам обычно удобно заниматься?",
    "q_extra": "Есть ли что-то ещё, что вы хотели бы рассказать нам о себе или своих целях?"
}

START_TEXT = (
    "Здᴩᴀʙᴄᴛʙуйᴛᴇ!\n\n"
    "Мы ᴩᴀды, чᴛᴏ ʙы ʙыбᴩᴀᴧи иʍᴇннᴏ Vᴇriᴛᴀs Aᴄᴀdᴇʍy.\n\n"
    "Чᴛᴏбы нᴀчᴀᴛь, ʙыбᴇᴩиᴛᴇ ᴏᴨц ию н ижᴇ:\n"
    "(Вы можете подать максимум 2 анкеты: 1 волонтёра и 1 ученика)"
)

VOLUNTEER_DIRECTIONS_TEXT = (
    "Волонтёрские направления\n\n"
    "В Veritas Academy сейчас доступны следующие направления:\n\n"
    "•SMM-специалист (TikTok): Занимается съёмкой и монтажом коротких видеороликов.\n\n"
    "•Копирайтер (Telegram-канал): Пишет посты, анонсы и полезные материалы.\n\n"
    "•Координатор учебной группы: Контролирует расписание и посещаемость группы.\n\n"
    "•Тьютор по английскому языку в группе (уровни A0-B2): Проводит 1 занятие в неделю.\n\n"
    "•Индивидуальный репетитор по английскому языку: Проводит от 2 уроков в неделю персонально.\n\n"
    "•Ведущий Speaking Clubs: Проводит разговорные встречи.\n\n"
    "•Тьютор по математике в группе (5-11 классы): Проводит занятия в группах.\n\n"
    "•Индивидуальный репетитор по математике (5-11 классы): Проводит индивидуальные уроки."
)

STUDENT_INTRO_TEXT = (
    "Анкета ученика\n\n"
    "Расскажите немного о себе, вашем уровне и целях обучения.\n\n"
    "Каждый ответ отправляйте отдельным сообщением."
)

def format_volunteer_summary(data: Dict[str, Any]) -> str:
    answers = data.get("answers", {})
    dir_code = data.get("direction_code", "")
    dir_name = DIRECTION_NAMES.get(dir_code, "Не указано")
    questions = VOLUNTEER_THEME_QUESTIONS.get(dir_code, [])
    theme_answers = data.get("theme_answers", [])

    theme_text = ""
    for i, q in enumerate(questions):
        ans = theme_answers[i] if i < len(theme_answers) else "Нет ответа"
        theme_text += f"\n{i+1}. {q}\nОтвет: {ans}\n"

    return (
        "✷ Ваша анкета\n\n"
        f"Имя и фамилия: {answers.get('q_fullname', '')}\n"
        f"Возраст: {answers.get('q_age', '')}\n"
        f"Страна: {answers.get('q_location', '')}\n"
        f"Часовой пояс: {answers.get('q_timezone', '')}\n"
        f"Telegram: {answers.get('q_username', '')}\n"
        f"Направление: {dir_name}\n"
        f"Занятость: {answers.get('q_time_commitment', '')}\n"
        f"Мотивация: {answers.get('q_motivation', '')}\n"
        f"Опыт: {answers.get('q_experience', '')}\n\n"
        f"Ответы на тематические вопросы:{theme_text}\n"
        "Проверьте, всё ли указано верно."
    )

def format_student_summary(data: Dict[str, Any]) -> str:
    answers = data.get("answers", {})
    dir_label = data.get("direction_label", "Не выбрано")
    
    details = f"Направление: {dir_label}\n"
    if answers.get("q_english_level"):
        details += f"Уровень английского: {answers.get('q_english_level')}\n"
    if answers.get("q_math_class"):
        details += f"Текущий класс (математика): {answers.get('q_math_class')}\n"
    if answers.get("q_math_material"):
        details += f"Материал за класс: {answers.get('q_math_material')}\n"

    return (
        "✷ Ваша анкета\n\n"
        f"Имя и фамилия: {answers.get('q_fullname', '')}\n"
        f"Возраст: {answers.get('q_age', '')}\n"
        f"Страна: {answers.get('q_location', '')}\n"
        f"Часовой пояс: {answers.get('q_timezone', '')}\n"
        f"Telegram: {answers.get('q_username', '')}\n"
        f"{details}"
        f"Главная цель: {answers.get('q_goal', '')}\n"
        f"Сложности: {answers.get('q_hardest', '')}\n"
        f"Формат обучения: {answers.get('q_format', '')}\n"
        f"Удобное время: {answers.get('q_schedule', '')}\n"
        f"Дополнительная информация: {answers.get('q_extra', '')}\n\n"
        "Проверьте, всё ли указано верно."
    )

def format_admin_volunteer(app_code: str, data: Dict[str, Any]) -> str:
    answers = data.get("answers", {})
    dir_code = data.get("direction_code", "")
    dir_name = DIRECTION_NAMES.get(dir_code, "Не указано")
    questions = VOLUNTEER_THEME_QUESTIONS.get(dir_code, [])
    theme_answers = data.get("theme_answers", [])

    theme_text = ""
    for i, q in enumerate(questions):
        ans = theme_answers[i] if i < len(theme_answers) else "Нет ответа"
        theme_text += f"\n{i+1}. {q}\nОтвет: {ans}\n"

    return (
        "Нᴏʙᴀя ᴀнᴋᴇᴛᴀ — ʙᴏᴧᴏнᴛёᴩ\n\n"
        f"ID заявки: {app_code}\n\n"
        f"Направление: {dir_name}\n\n"
        f"Имя и фамилия: {answers.get('q_fullname', '')}\n"
        f"Возраст: {answers.get('q_age', '')}\n"
        f"Страна: {answers.get('q_location', '')}\n"
        f"Часовой пояс: {answers.get('q_timezone', '')}\n"
        f"Telegram: {answers.get('q_username', '')}\n"
        f"Занятость: {answers.get('q_time_commitment', '')}\n\n"
        f"Мотивация:\n{answers.get('q_motivation', '')}\n\n"
        f"Опыт:\n{answers.get('q_experience', '')}\n\n"
        f"Тематические вопросы:{theme_text}"
    )

def format_admin_student(app_code: str, data: Dict[str, Any]) -> str:
    answers = data.get("answers", {})
    dir_label = data.get("direction_label", "Не выбрано")

    details = f"Направление: {dir_label}\n"
    if answers.get("q_english_level"):
        details += f"Уровень английского: {answers.get('q_english_level')}\n"
    if answers.get("q_math_class"):
        details += f"Текущий класс (математика): {answers.get('q_math_class')}\n"
    if answers.get("q_math_material"):
        details += f"Материал за класс: {answers.get('q_math_material')}\n"

    return (
        "Нᴏʙᴀя ᴀнᴋᴇᴛᴀ — учᴇниᴋ\n\n"
        f"ID заявки: {app_code}\n\n"
        f"Имя и фамилия: {answers.get('q_fullname', '')}\n"
        f"Возраст: {answers.get('q_age', '')}\n"
        f"Страна: {answers.get('q_location', '')}\n"
        f"Часовой пояс: {answers.get('q_timezone', '')}\n"
        f"Telegram: {answers.get('q_username', '')}\n\n"
        f"{details}\n"
        f"Цель: {answers.get('q_goal', '')}\n"
        f"Сложности: {answers.get('q_hardest', '')}\n"
        f"Формат обучения: {answers.get('q_format', '')}\n"
        f"Удобное время: {answers.get('q_schedule', '')}\n\n"
        "Дополнительная информация:\n"
        f"{answers.get('q_extra', '')}"
    )

# ==================================================
# 5. КЛАВИАТУРЫ
# ==================================================
def get_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Волонтёр", callback_data="role_volunteer"),
                InlineKeyboardButton(text="Ученик", callback_data="role_student")
            ],
            [
                InlineKeyboardButton(text="Задать вопрос", callback_data="role_question")
            ]
        ]
    )

def get_directions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="SMM-специалист", callback_data="dir_smm")],
            [InlineKeyboardButton(text="Копирайтер", callback_data="dir_copywriter")],
            [InlineKeyboardButton(text="Координатор учебной группы", callback_data="dir_coord")],
            [InlineKeyboardButton(text="Тьютор по английскому языку в группе", callback_data="dir_tutor_en")],
            [InlineKeyboardButton(text="Репетитор по английскому языку", callback_data="dir_rep_en")],
            [InlineKeyboardButton(text="Ведущий Speaking Club", callback_data="dir_speaking")],
            [InlineKeyboardButton(text="Тьютор по математике в группе", callback_data="dir_tutor_math")],
            [InlineKeyboardButton(text="Репетитор по математике (5-11 классы)", callback_data="dir_rep_math")]
        ]
    )

def get_student_dir_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Английский язык", callback_data="stu_dir_en")],
            [InlineKeyboardButton(text="Математика", callback_data="stu_dir_math")],
            [InlineKeyboardButton(text="Математика и английский", callback_data="stu_dir_both")]
        ]
    )

def get_student_format_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Групповые", callback_data="fmt_group")],
            [InlineKeyboardButton(text="Индивидуальные", callback_data="fmt_indiv")],
            [InlineKeyboardButton(text="Пройти тест формата (5 вопросов)", callback_data="fmt_test")]
        ]
    )

def get_test_q1_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Замыкаюсь / стесняюсь сбиться при других", callback_data="t1_indiv")],
            [InlineKeyboardButton(text="Чужие вопросы и дискуссии меня подбадривают", callback_data="t1_group")]
        ]
    )

def get_test_q2_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Люблю разбирать детали, пока не пойму на 100%", callback_data="t2_indiv")],
            [InlineKeyboardButton(text="Мне важен динамичный темп и общая структура", callback_data="t2_group")]
        ]
    )

def get_test_q3_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Конкуренция и успехи других меня мотивируют", callback_data="t3_group")],
            [InlineKeyboardButton(text="Сравнение с другими вызывает стресс", callback_data="t3_indiv")]
        ]
    )

def get_test_q4_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изучить тему полностью с нуля или закрыть пробелы", callback_data="t4_indiv")],
            [InlineKeyboardButton(text="Систематизировать знания и регулярно практиковаться", callback_data="t4_group")]
        ]
    )

def get_test_q5_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Легко отвлекаюсь, нужен строгий личный контроль", callback_data="t5_indiv")],
            [InlineKeyboardButton(text="Спокойно занимаюсь, когда внимание делят на всех", callback_data="t5_group")]
        ]
    )

def get_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить", callback_data="submit_app"),
                InlineKeyboardButton(text="Изменить", callback_data="edit_app")
            ]
        ]
    )

def get_edit_fields_kb(app_type: str) -> InlineKeyboardMarkup:
    buttons = []
    if app_type == "volunteer":
        fields = [
            ("Имя и фамилия", "edit_field_q_fullname"),
            ("Возраст", "edit_field_q_age"),
            ("Страна", "edit_field_q_location"),
            ("Часовой пояс", "edit_field_q_timezone"),
            ("Telegram username", "edit_field_q_username"),
            ("Занятость", "edit_field_q_time_commitment"),
            ("Мотивация", "edit_field_q_motivation"),
            ("Опыт", "edit_field_q_experience")
        ]
    else:
        fields = [
            ("Имя и фамилия", "edit_field_q_fullname"),
            ("Возраст", "edit_field_q_age"),
            ("Страна", "edit_field_q_location"),
            ("Часовой пояс", "edit_field_q_timezone"),
            ("Telegram username", "edit_field_q_username"),
            ("Главная цель", "edit_field_q_goal"),
            ("Сложности", "edit_field_q_hardest"),
            ("Удобное время", "edit_field_q_schedule"),
            ("Дополнительно", "edit_field_q_extra"),
        ]
    for label, cb_data in fields:
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb_data)])
    
    buttons.append([InlineKeyboardButton(text="🔄 Начать заново / Изменить направление", callback_data="reset_app")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================================================
# 6. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ВЕБ-СЕРВЕР
# ==================================================
async def check_text_message(message: Message) -> bool:
    if not message.text:
        await message.answer("Пожалуйста, отправьте ответ обычным текстом.")
        return False
    return True

async def send_long_message(bot: Bot, chat_id: int, text: str) -> Message:
    MAX_LENGTH = 4000
    if len(text) <= MAX_LENGTH:
        return await bot.send_message(chat_id=chat_id, text=text)

    chunks = [text[i:i + MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
    primary_message = await bot.send_message(chat_id=chat_id, text=chunks[0])
    for chunk in chunks[1:]:
        await bot.send_message(chat_id=chat_id, text=chunk)
    return primary_message

async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)

async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Мини веб-сервер успешно запущен на порту {port}")

# ==================================================
# 7. ОБРАБОТЧИКИ СОБЫТИЙ (HANDLERS)
# ==================================================
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=get_start_kb())

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Заполнение отменено.")
    await message.answer(START_TEXT, reply_markup=get_start_kb())

@router.callback_query(F.data == "reset_app")
async def process_reset_app(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Заполнение анкеты сброшено.")
    await callback.message.answer(START_TEXT, reply_markup=get_start_kb())
    await callback.answer()

@router.callback_query(F.data == "role_volunteer")
async def process_role_volunteer(callback: CallbackQuery, state: FSMContext) -> None:
    submitted_types = await get_user_submitted_types(callback.from_user.id)
    if "volunteer" in submitted_types:
        await callback.answer(
            "Вы уже отправили анкету волонтёра! Можно отправить только 1 анкету волонтёра и 1 анкету ученика.",
            show_alert=True
        )
        return

    await state.clear()
    await state.update_data(app_type="volunteer", answers={})
    await state.set_state(VolunteerStates.choosing_direction)
    await callback.message.edit_text(VOLUNTEER_DIRECTIONS_TEXT, reply_markup=get_directions_kb())
    await callback.answer()

@router.callback_query(F.data == "role_student")
async def process_role_student(callback: CallbackQuery, state: FSMContext) -> None:
    submitted_types = await get_user_submitted_types(callback.from_user.id)
    if "student" in submitted_types:
        await callback.answer(
            "Вы уже отправили анкету ученика! Можно отправить только 1 анкету волонтёра и 1 анкету ученика.",
            show_alert=True
        )
        return

    await state.clear()
    await state.update_data(app_type="student", answers={})
    await state.set_state(StudentStates.choosing_direction)
    await callback.message.answer(STUDENT_INTRO_TEXT)
    await callback.message.answer("Выберите направление:", reply_markup=get_student_dir_kb())
    await callback.answer()

@router.callback_query(F.data == "role_question")
async def process_role_question(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(QuestionStates.waiting_for_question)
    await callback.message.edit_text(QUESTION_INTRO_TEXT)
    await callback.answer()

@router.message(QuestionStates.waiting_for_question)
async def process_user_question(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await check_text_message(message):
        return

    user_id = message.from_user.id
    username = message.from_user.username
    question_text = message.text.strip()

    app_id, app_code = await create_application(
        app_type="question",
        user_id=user_id,
        username=username,
        direction="Вопрос администрации",
        data_dict={"question": question_text}
    )

    admin_msg_text = (
        "Нᴏʙый ʙᴏᴨᴩᴏᴄ ᴏᴛ ᴨᴏᴧьɜᴏʙᴀᴛᴇᴧя\n\n"
        f"ID обращения: {app_code}\n"
        f"Telegram: @{username if username else 'скрыт'}\n"
        f"ID пользователя: {user_id}\n\n"
        f"Вопрос:\n{question_text}"
    )

    try:
        sent_message = await send_long_message(bot, ADMIN_GROUP_ID, admin_msg_text)
        await update_app_message_id(app_id, ADMIN_GROUP_ID, sent_message.message_id)
        await message.answer("Ваш вопрос отправлен! Мы ответим вам в ближайшее время ୨ৎ")
    except Exception as e:
        logging.error(f"Ошибка отправки вопроса {app_code} в группу: {e}")
        await message.answer("Не удалось отправить вопрос. Попробуйте позже.")

    await state.clear()

@router.callback_query(StudentStates.choosing_direction, F.data.startswith("stu_dir_"))
async def process_student_dir_choice(callback: CallbackQuery, state: FSMContext) -> None:
    dir_code = callback.data
    labels = {
        "stu_dir_en": "Английский язык",
        "stu_dir_math": "Математика",
        "stu_dir_both": "Математика и английский"
    }
    await state.update_data(direction_code=dir_code, direction_label=labels.get(dir_code, "Не выбрано"))
    await state.set_state(StudentStates.q_fullname)
    await callback.message.answer("Укажите ваше имя и фамилию.")
    await callback.answer()

@router.message(StudentStates.q_fullname)
async def process_stu_fullname(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_fullname"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_age)
    await message.answer("Сколько вам лет?")

@router.message(StudentStates.q_age)
async def process_stu_age(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_age"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_location)
    await message.answer("Из какой вы страны?")

@router.message(StudentStates.q_location)
async def process_stu_location(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_location"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_timezone)
    await message.answer("Укажите ваш часовой пояс.")

@router.message(StudentStates.q_timezone)
async def process_stu_timezone(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_timezone"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_username)
    await message.answer("Укажите ваш Telegram username, если он есть.")

@router.message(StudentStates.q_username)
async def process_stu_username(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_username"] = message.text.strip()
    await state.update_data(answers=answers)

    dir_code = data.get("direction_code")
    if dir_code in ("stu_dir_en", "stu_dir_both"):
        await state.set_state(StudentStates.q_english_level)
        await message.answer("Какой у вас примерный уровень английского (от A0 до C1)? Если не знаете — напишите «не знаю».")
    else:
        await state.set_state(StudentStates.q_math_class)
        await message.answer("В каком вы сейчас классе?")

@router.message(StudentStates.q_english_level)
async def process_stu_eng_level(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_english_level"] = message.text.strip()
    await state.update_data(answers=answers)

    dir_code = data.get("direction_code")
    if dir_code == "stu_dir_both":
        await state.set_state(StudentStates.q_math_class)
        await message.answer("В каком вы сейчас классе?")
    else:
        await state.set_state(StudentStates.q_goal)
        await message.answer("Для чего вы хотите изучать предмет? Расскажите о вашей главной цели.")

@router.message(StudentStates.q_math_class)
async def process_stu_math_class(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_math_class"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_math_material)
    await message.answer("Материал за какой класс вам необходимо изучать / подтянуть?")

@router.message(StudentStates.q_math_material)
async def process_stu_math_material(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_math_material"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_goal)
    await message.answer("Для чего вы хотите изучать выбранные предметы? Расскажите о вашей главной цели.")

@router.message(StudentStates.q_goal)
async def process_stu_goal(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_goal"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_hardest)
    await message.answer("Что сейчас даётся вам сложнее всего?")

@router.message(StudentStates.q_hardest)
async def process_stu_hardest(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_hardest"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.choosing_format)
    await message.answer("Какой формат обучения вам больше подходит: групповые или индивидуальные занятия?", reply_markup=get_student_format_kb())

@router.callback_query(StudentStates.choosing_format, F.data.in_({"fmt_group", "fmt_indiv", "fmt_test"}))
async def process_stu_format_choice(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    answers = data.get("answers", {})

    if callback.data == "fmt_group":
        answers["q_format"] = "Групповые занятия"
        await state.update_data(answers=answers)
        await state.set_state(StudentStates.q_schedule)
        await callback.message.answer("В какое время и в какие дни вам обычно удобно заниматься?")
    elif callback.data == "fmt_indiv":
        answers["q_format"] = "Индивидуальные занятия"
        await state.update_data(answers=answers)
        await state.set_state(StudentStates.q_schedule)
        await callback.message.answer("В какое время и в какие дни вам обычно удобно заниматься?")
    else:
        await state.update_data(test_score=0)
        await state.set_state(StudentStates.test_q1)
        await callback.message.answer(
            "Тест формата обучения (1/5)\n\nКак вы обычно себя чувствуете, если нужно отвечать или задавать вопросы при незнакомых людях?",
            reply_markup=get_test_q1_kb()
        )
    await callback.answer()

@router.callback_query(StudentStates.test_q1, F.data.startswith("t1_"))
async def process_test_q1(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    score = data.get("test_score", 0) + (1 if callback.data == "t1_indiv" else 0)
    await state.update_data(test_score=score)
    await state.set_state(StudentStates.test_q2)
    await callback.message.answer(
        "Тест формата обучения (2/5)\n\nКакой темп работы вам обычно комфортен при прохождении сложных тем?",
        reply_markup=get_test_q2_kb()
    )
    await callback.answer()

@router.callback_query(StudentStates.test_q2, F.data.startswith("t2_"))
async def process_test_q2(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    score = data.get("test_score", 0) + (1 if callback.data == "t2_indiv" else 0)
    await state.update_data(test_score=score)
    await state.set_state(StudentStates.test_q3)
    await callback.message.answer(
        "Тест формата обучения (3/5)\n\nКак на вас влияет присутствие других людей в процессе обучения?",
        reply_markup=get_test_q3_kb()
    )
    await callback.answer()

@router.callback_query(StudentStates.test_q3, F.data.startswith("t3_"))
async def process_test_q3(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    score = data.get("test_score", 0) + (1 if callback.data == "t3_indiv" else 0)
    await state.update_data(test_score=score)
    await state.set_state(StudentStates.test_q4)
    await callback.message.answer(
        "Тест формата обучения (4/5)\n\nКакая у вас первоочередная цель на занятиях?",
        reply_markup=get_test_q4_kb()
    )
    await callback.answer()

@router.callback_query(StudentStates.test_q4, F.data.startswith("t4_"))
async def process_test_q4(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    score = data.get("test_score", 0) + (1 if callback.data == "t4_indiv" else 0)
    await state.update_data(test_score=score)
    await state.set_state(StudentStates.test_q5)
    await callback.message.answer(
        "Тест формата обучения (5/5)\n\nНасколько вам важна 100% фокусировка преподавателя именно на вас?",
        reply_markup=get_test_q5_kb()
    )
    await callback.answer()

@router.callback_query(StudentStates.test_q5, F.data.startswith("t5_"))
async def process_test_q5(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    score = data.get("test_score", 0) + (1 if callback.data == "t5_indiv" else 0)

    if score >= 3:
        rec_format = "Индивидуальные занятия"
        explanation = (
            "✦ **Вам идеально подойдут Индивидуальные занятия.**\n"
            "Вы цените гибкий темп, персональный подход и комфортную атмосферу без давления. "
            "Преподаватель сможет выстроить программу строго под ваши задачи и подтягивать именно ваши пробелы."
        )
    else:
        rec_format = "Групповые занятия"
        explanation = (
            "✦ **Вам отлично подойдут Групповые занятия.**\n"
            "Вам комфортно работать в команде, обмениваться идеями и учиться в динамичной среде. "
            "Групповая атмосфера поможет поддерживать высокую мотивацию и регулярно практиковаться."
        )

    answers = data.get("answers", {})
    answers["q_format"] = f"{rec_format} (рекомендовано тестом)"
    await state.update_data(answers=answers)

    await callback.message.answer(
        f"˚.ׄ𓈒✧ **Анализ ваших ответов завершён!**\n\n{explanation}\n\n"
        f"Ваш выбор записан как: **{rec_format}**."
    )
    
    await state.set_state(StudentStates.q_schedule)
    await callback.message.answer("В какое время и в какие дни вам обычно удобно заниматься?")
    await callback.answer()

@router.message(StudentStates.q_schedule)
async def process_stu_schedule(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_schedule"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.q_extra)
    await message.answer("Есть ли что-то ещё, что вы хотели бы рассказать нам о себе или своих целях?")

@router.message(StudentStates.q_extra)
async def process_stu_extra(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_extra"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(StudentStates.confirm)
    updated_data = await state.get_data()
    await message.answer(format_student_summary(updated_data), reply_markup=get_confirm_kb())

@router.callback_query(VolunteerStates.choosing_direction, F.data.startswith("dir_"))
async def process_direction(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(direction_code=callback.data)
    await state.set_state(VolunteerStates.q_fullname)
    await callback.message.answer("Укажите ваше имя и фамилию.")
    await callback.answer()

@router.message(VolunteerStates.q_fullname)
async def process_vol_fullname(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_fullname"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_age)
    await message.answer("Сколько вам лет?")

@router.message(VolunteerStates.q_age)
async def process_vol_age(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_age"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_location)
    await message.answer("Из какой вы страны?")

@router.message(VolunteerStates.q_location)
async def process_vol_location(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_location"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_timezone)
    await message.answer("Укажите ваш часовой пояс.")

@router.message(VolunteerStates.q_timezone)
async def process_vol_timezone(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_timezone"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_username)
    await message.answer("Укажите ваш Telegram username, если он есть.")

@router.message(VolunteerStates.q_username)
async def process_vol_username(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_username"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_time_commitment)
    await message.answer("Сколько времени вы готовы уделять Veritas Academy в неделю?")

@router.message(VolunteerStates.q_time_commitment)
async def process_vol_time_commitment(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_time_commitment"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_motivation)
    await message.answer("Почему вы хотите стать волонтёром именно в Veritas Academy?")

@router.message(VolunteerStates.q_motivation)
async def process_vol_motivation(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_motivation"] = message.text.strip()
    await state.update_data(answers=answers)
    await state.set_state(VolunteerStates.q_experience)
    await message.answer(
        "Расскажите об опыте, который может быть полезен для выбранного направления. "
        "Если подобного опыта нет, просто напишите об этом."
    )

@router.message(VolunteerStates.q_experience)
async def process_vol_experience(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q_experience"] = message.text.strip()

    dir_code = data.get("direction_code", "")
    questions = VOLUNTEER_THEME_QUESTIONS.get(dir_code, [])

    await state.update_data(answers=answers, theme_answers=[], theme_q_index=0)
    await state.set_state(VolunteerStates.answering_theme)
    await message.answer(questions[0])

@router.message(VolunteerStates.answering_theme)
async def process_vol_theme_answer(message: Message, state: FSMContext) -> None:
    if not await check_text_message(message): return
    data = await state.get_data()
    theme_answers = data.get("theme_answers", [])
    theme_answers.append(message.text.strip())

    idx = data.get("theme_q_index", 0) + 1
    dir_code = data.get("direction_code", "")
    questions = VOLUNTEER_THEME_QUESTIONS.get(dir_code, [])

    if idx < len(questions):
        await state.update_data(theme_answers=theme_answers, theme_q_index=idx)
        await message.answer(questions[idx])
    else:
        await state.update_data(theme_answers=theme_answers)
        await state.set_state(VolunteerStates.confirm)
        updated_data = await state.get_data()
        await message.answer(format_volunteer_summary(updated_data), reply_markup=get_confirm_kb())

@router.callback_query(VolunteerStates.confirm, F.data == "edit_app")
@router.callback_query(StudentStates.confirm, F.data == "edit_app")
async def process_edit_click(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    app_type = data.get("app_type", "volunteer")
    await callback.message.edit_text(
        "Выберите поле, которое хотите изменить:",
        reply_markup=get_edit_fields_kb(app_type)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("edit_field_"))
async def process_field_selection(callback: CallbackQuery, state: FSMContext) -> None:
    field_key = callback.data.replace("edit_field_", "")
    data = await state.get_data()
    app_type = data.get("app_type", "volunteer")
    await state.update_data(editing_field_name=field_key)

    if app_type == "volunteer":
        await state.set_state(VolunteerStates.editing_field)
        question_text = FIELD_QUESTIONS_VOLUNTEER.get(field_key, "Введите новый ответ:")
    else:
        await state.set_state(StudentStates.editing_field)
        question_text = FIELD_QUESTIONS_STUDENT.get(field_key, "Введите новый ответ:")

    await callback.message.answer(f"Введите новое значение:\n\n{question_text}")
    await callback.answer()

@router.message(VolunteerStates.editing_field)
@router.message(StudentStates.editing_field)
async def process_new_field_value(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, отправьте новый ответ обычным текстом.")
        return

    data = await state.get_data()
    field_key = data.get("editing_field_name")
    app_type = data.get("app_type", "volunteer")
    answers = data.get("answers", {})

    if field_key:
        answers[field_key] = message.text.strip()

    await state.update_data(answers=answers, editing_field_name=None)

    if app_type == "volunteer":
        await state.set_state(VolunteerStates.confirm)
        updated_data = await state.get_data()
        summary_text = format_volunteer_summary(updated_data)
    else:
        await state.set_state(StudentStates.confirm)
        updated_data = await state.get_data()
        summary_text = format_student_summary(updated_data)

    await message.answer(summary_text, reply_markup=get_confirm_kb())

@router.callback_query(VolunteerStates.confirm, F.data == "submit_app")
@router.callback_query(StudentStates.confirm, F.data == "submit_app")
async def process_submit(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    app_type = data.get("app_type", "volunteer")
    user_id = callback.from_user.id
    username = callback.from_user.username
    
    submitted_types = await get_user_submitted_types(user_id)
    if app_type in submitted_types:
        await callback.message.answer("Вы уже отправляли анкету этого типа.")
        await state.clear()
        await callback.answer()
        return

    if app_type == "volunteer":
        dir_code = data.get("direction_code")
        direction = DIRECTION_NAMES.get(dir_code)
    else:
        direction = data.get("direction_label")

    app_id, app_code = await create_application(
        app_type=app_type,
        user_id=user_id,
        username=username,
        direction=direction,
        data_dict=data.get("answers", {})
    )

    if app_type == "volunteer":
        admin_text = format_admin_volunteer(app_code, data)
    else:
        admin_text = format_admin_student(app_code, data)

    try:
        sent_message = await send_long_message(bot, ADMIN_GROUP_ID, admin_text)
        await update_app_message_id(app_id, ADMIN_GROUP_ID, sent_message.message_id)
    except Exception as e:
        logging.error(f"Ошибка отправки анкеты {app_code} в группу: {e}")
        await callback.message.answer(
            "Произошла ошибка при отправке анкеты администраторам. "
            "Попробуйте заново через /start."
        )
        await callback.answer()
        return

    await state.clear()
    await callback.message.answer(FINAL_SUCCESS_TEXT)
    await callback.answer()

@router.message(F.chat.id == ADMIN_GROUP_ID, F.reply_to_message)
async def process_admin_reply(message: Message, bot: Bot) -> None:
    sender_id = message.from_user.id

    is_allowed = False
    if ADMIN_USER_IDS and sender_id in ADMIN_USER_IDS:
        is_allowed = True
    else:
        try:
            member = await bot.get_chat_member(chat_id=ADMIN_GROUP_ID, user_id=sender_id)
            if member.status in ("administrator", "creator"):
                is_allowed = True
        except Exception as e:
            logging.warning(f"Ошибка проверки статуса участника {sender_id}: {e}")

    if not is_allowed:
        return

    reply_to_msg_id = message.reply_to_message.message_id
    app_record = await get_app_by_message(group_chat_id=ADMIN_GROUP_ID, group_message_id=reply_to_msg_id)

    if not app_record:
        return

    target_user_id = app_record["user_id"]
    app_code = app_record["app_code"]
    reply_text = message.text or message.caption or "[Вложение без текста]"

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"Сообщение от администрации Veritas Academy ({app_code}):\n\n{reply_text}"
        )
        await message.reply(f"Ответ успешно доставлен пользователю (Заявка/Вопрос {app_code}).")
    except TelegramForbiddenError:
        await message.reply(f"Ошибка: Пользователь ({app_code}) заблокировал бота.")
    except TelegramBadRequest as e:
        await message.reply(f"Не удалось отправить сообщение пользователю ({app_code}): {e.message}")
    except Exception as e:
        logging.error(f"Ошибка отправки ответа пользователю: {e}")
        await message.reply(f"Произошла ошибка при отправке ответа по заявке {app_code}.")

# ==================================================
# 8. ЗАПУСК БОТА И СЕРВЕРА
# ==================================================
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )
    logging.info("Инициализация базы данных SQLite...")
    await init_db()

    # Запускаем мини-вебсервер для предотвращения остановки сервером Render
    await start_web_server()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logging.info("Очистка старых обновлений и запуск polling...")
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
