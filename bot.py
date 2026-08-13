# -*- coding: utf-8 -*-
"""
Telegram-бот для практики английского языка.

Команды:
  /start  — регистрация, начать получать ежедневные уроки
  /status — текущий уровень сложности, серия дней, точность
  /word   — получить одно слово вне расписания (потренироваться сейчас)
  /help   — справка

Расписание (настраивается в config.py или переменными окружения на хостинге):
  09:00 — утренний урок из 8 слов (4 рабочих + 4 повседневных)
  20:00 — вечернее повторение тех же 8 слов

Уровни сложности (бот подбирает уровень сам, по точности ваших ответов):
  easy   — выбор правильного перевода из 4 вариантов (кнопки)
  medium — нужно написать перевод на русский текстом
  hard   — нужно дописать пропущенное английское слово в предложении
"""

import os
import random
import logging
import datetime as dt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from words import WORD_BANK
import storage
import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_СВОЙ_ТОКЕН")

# Активные сессии в памяти: chat_id -> состояние текущего урока
SESSIONS = {}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def pick_daily_words(user: dict) -> list:
    """Выбирает WORDS_PER_SESSION слов: половина 'work', половина 'everyday',
    последовательно по кругу, чтобы слова не повторялись слишком часто."""
    work_words = [i for i, w in enumerate(WORD_BANK) if w["category"] == "work"]
    everyday_words = [i for i, w in enumerate(WORD_BANK) if w["category"] == "everyday"]

    half = config.WORDS_PER_SESSION // 2
    pointer = user.get("word_pointer", 0)

    def take(pool, count, start):
        n = len(pool)
        return [pool[(start + i) % n] for i in range(count)]

    chosen = take(work_words, half, pointer) + take(everyday_words, half, pointer)
    random.shuffle(chosen)
    user["word_pointer"] = pointer + half
    return chosen


def build_distractors(correct_index: int, count: int = 3) -> list:
    """Подбирает случайные неверные варианты перевода для лёгкого уровня."""
    pool = [i for i in range(len(WORD_BANK)) if i != correct_index]
    picks = random.sample(pool, min(count, len(pool)))
    return [WORD_BANK[i]["ru"] for i in picks]


def normalize(text: str) -> str:
    return text.strip().lower().strip(".,!?")


def adjust_level(user: dict, session_correct: int, session_total: int) -> None:
    """Обновляет историю ответов и, если нужно, меняет уровень сложности."""
    history = user.get("history", [])
    history.extend([True] * session_correct + [False] * (session_total - session_correct))
    user["history"] = history[-20:]  # храним последние 20 ответов

    recent = user["history"][-10:]
    if len(recent) < 4:
        return
    accuracy = sum(recent) / len(recent)

    level = user.get("level", "medium")
    idx = config.LEVELS.index(level)
    if accuracy >= 0.8 and idx < len(config.LEVELS) - 1:
        user["level"] = config.LEVELS[idx + 1]
    elif accuracy <= 0.4 and idx > 0:
        user["level"] = config.LEVELS[idx - 1]


# ---------------------------------------------------------------------------
# Логика сессии (урока)
# ---------------------------------------------------------------------------

async def start_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    user = storage.get_user(chat_id)

    if mode == "morning":
        word_indices = pick_daily_words(user)
        user["today_words"] = word_indices
        storage.save_user(chat_id, user)
        intro = (
            f"☀️ Доброе утро! Урок на сегодня — {len(word_indices)} слов.\n"
            f"Уровень сложности: {user.get('level', 'medium')}."
        )
    else:  # evening review
        word_indices = user.get("today_words") or pick_daily_words(user)
        intro = "🌙 Вечернее повторение сегодняшних слов:"

    await context.bot.send_message(chat_id=chat_id, text=intro)

    SESSIONS[chat_id] = {
        "queue": list(word_indices),
        "correct": 0,
        "total": 0,
        "mode": mode,
        "current": None,
    }
    await send_next_question(chat_id, context)


async def send_next_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = SESSIONS.get(chat_id)
    if not session:
        return

    if not session["queue"]:
        await finish_session(chat_id, context)
        return

    word_index = session["queue"].pop(0)
    session["current"] = word_index
    word = WORD_BANK[word_index]
    user = storage.get_user(chat_id)
    level = user.get("level", "medium")

    if level == "easy":
        options = [word["ru"]] + build_distractors(word_index, 3)
        random.shuffle(options)
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"{word_index}|{opt}")]
            for opt in options
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Выберите перевод:\n\n*{word['en']}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif level == "medium":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Переведите на русский:\n\n*{word['en']}*",
            parse_mode="Markdown",
        )
    else:  # hard
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Впишите пропущенное английское слово:\n\n"
                f"_{word['sentence']}_\n\n(перевод: {word['ru']})"
            ),
            parse_mode="Markdown",
        )


async def finish_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = SESSIONS.pop(chat_id, None)
    if not session:
        return

    correct, total = session["correct"], session["total"]
    user = storage.get_user(chat_id)

    if session["mode"] == "morning":
        adjust_level(user, correct, total)
        user["streak"] = user.get("streak", 0) + 1
        storage.save_user(chat_id, user)

    percent = round(100 * correct / total) if total else 0
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Урок завершён: {correct} из {total} правильных ({percent}%).",
    )


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    storage.get_user(chat_id)  # создаёт запись, если её ещё нет
    await update.message.reply_text(
        "Привет! Я помогу подтянуть английский — тема стройки, закупок и "
        "повседневного общения.\n\n"
        f"Каждый день в {config.MORNING_HOUR:02d}:{config.MORNING_MINUTE:02d} я буду присылать "
        f"урок из {config.WORDS_PER_SESSION} слов, а в "
        f"{config.EVENING_HOUR:02d}:{config.EVENING_MINUTE:02d} — повторение.\n\n"
        "Уровень сложности (лёгкий/средний/сложный) я буду подбирать сам, "
        "по вашим результатам.\n\n"
        "Команды:\n"
        "/word — получить слово вне расписания\n"
        "/status — ваш текущий уровень и статистика\n"
        "/help — справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = storage.get_user(chat_id)
    history = user.get("history", [])
    accuracy = round(100 * sum(history) / len(history)) if history else 0
    await update.message.reply_text(
        f"Уровень сложности: {user.get('level', 'medium')}\n"
        f"Дней подряд с утренним уроком: {user.get('streak', 0)}\n"
        f"Точность за последние {len(history)} ответов: {accuracy}%"
    )


async def word_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in SESSIONS:
        await update.message.reply_text("Урок уже идёт — просто ответьте на текущий вопрос.")
        return
    word_index = random.randrange(len(WORD_BANK))
    SESSIONS[chat_id] = {"queue": [word_index], "correct": 0, "total": 0, "mode": "practice", "current": None}
    await send_next_question(chat_id, context)


async def handle_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    if not session or session["current"] is None:
        await update.message.reply_text("Сейчас нет активного вопроса. Пришлите /word, чтобы начать.")
        return

    word = WORD_BANK[session["current"]]
    user = storage.get_user(chat_id)
    level = user.get("level", "medium")
    user_answer = normalize(update.message.text)

    if level == "medium":
        variants = [normalize(v) for part in word["ru"].split(",") for v in part.split("/")]
        is_correct = user_answer in variants
        correct_text = word["ru"]
    else:  # hard (free text fallback)
        is_correct = user_answer == normalize(word["answer"])
        correct_text = word["answer"]

    session["total"] += 1
    if is_correct:
        session["correct"] += 1
        await update.message.reply_text("✅ Верно!")
    else:
        await update.message.reply_text(f"❌ Неверно. Правильный ответ: *{correct_text}*", parse_mode="Markdown")

    session["current"] = None
    await send_next_question(chat_id, context)


async def handle_callback_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    session = SESSIONS.get(chat_id)
    if not session or session["current"] is None:
        return

    word_index_str, chosen_ru = query.data.split("|", 1)
    word = WORD_BANK[session["current"]]

    session["total"] += 1
    if chosen_ru == word["ru"]:
        session["correct"] += 1
        result_text = "✅ Верно!"
    else:
        result_text = f"❌ Неверно. Правильный ответ: {word['ru']}"

    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(chat_id=chat_id, text=result_text)

    session["current"] = None
    await send_next_question(chat_id, context)


# ---------------------------------------------------------------------------
# Плановые задачи (расписание)
# ---------------------------------------------------------------------------

async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id in storage.all_chat_ids():
        try:
            await start_session(chat_id, context, mode="morning")
        except Exception:
            logger.exception("Не удалось отправить утренний урок chat_id=%s", chat_id)


async def evening_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id in storage.all_chat_ids():
        try:
            await start_session(chat_id, context, mode="evening")
        except Exception:
            logger.exception("Не удалось отправить вечернее повторение chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_СВОЙ_ТОКЕН":
        raise RuntimeError(
            "Не задан токен бота. Задайте переменную окружения TELEGRAM_BOT_TOKEN "
            "или впишите токен в BOT_TOKEN в этом файле."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("word", word_now))
    application.add_handler(CallbackQueryHandler(handle_callback_answer))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_answer))

    try:
        import pytz
        tz = pytz.timezone(config.TIMEZONE)
    except Exception:
        tz = None
        logger.warning("Не удалось загрузить часовой пояс %s, использую UTC", config.TIMEZONE)

    job_queue = application.job_queue
    job_queue.run_daily(
        morning_job,
        time=dt.time(hour=config.MORNING_HOUR, minute=config.MORNING_MINUTE, tzinfo=tz),
        name="morning_lesson",
    )
    job_queue.run_daily(
        evening_job,
        time=dt.time(hour=config.EVENING_HOUR, minute=config.EVENING_MINUTE, tzinfo=tz),
        name="evening_review",
    )

    logger.info("Бот запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()
