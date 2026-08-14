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
import re
import random
import logging
import datetime as dt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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

def today_str() -> str:
    return dt.date.today().isoformat()


def get_due_word_indices(user: dict, limit: int) -> list:
    """Слова, для которых наступил день повторения (или уже просрочен)."""
    stats = user.get("word_stats", {})
    today = today_str()
    due = [int(idx) for idx, s in stats.items() if s.get("next_review", today) <= today]
    due.sort(key=lambda i: stats[str(i)].get("next_review", today))
    return due[:limit]


def update_word_stat(user: dict, word_index: int, correct: bool) -> None:
    """Обновляет 'карточную коробку' слова после ответа пользователя."""
    stats = user.setdefault("word_stats", {})
    key = str(word_index)
    entry = stats.get(key, {"box": 0})
    if correct:
        entry["box"] = min(entry.get("box", 0) + 1, len(config.REVIEW_INTERVALS_DAYS))
    else:
        entry["box"] = 1
    interval = config.REVIEW_INTERVALS_DAYS[entry["box"] - 1]
    entry["next_review"] = (dt.date.today() + dt.timedelta(days=interval)).isoformat()
    stats[key] = entry


def pick_daily_words(user: dict) -> list:
    """Сначала берёт слова, которые пора повторить (по интервальной системе),
    затем добивает урок новыми словами, которые пользователь ещё не видел."""
    limit = config.WORDS_PER_SESSION
    chosen = get_due_word_indices(user, limit)

    if len(chosen) < limit:
        stats = user.get("word_stats", {})
        seen = {int(k) for k in stats.keys()}
        unseen = [i for i in range(len(WORD_BANK)) if i not in seen]
        pointer = user.get("word_pointer", 0)
        needed = limit - len(chosen)

        if unseen:
            picks = [unseen[(pointer + i) % len(unseen)] for i in range(min(needed, len(unseen)))]
            chosen.extend(picks)
            user["word_pointer"] = pointer + len(picks)
            needed -= len(picks)

        if needed > 0:
            candidates = [i for i in range(len(WORD_BANK)) if i not in chosen]
            random.shuffle(candidates)
            chosen.extend(candidates[:needed])

    random.shuffle(chosen)
    return chosen[:limit]


def build_distractors(correct_index: int, count: int = 3) -> list:
    """Подбирает случайные неверные варианты перевода для лёгкого уровня."""
    pool = [i for i in range(len(WORD_BANK)) if i != correct_index]
    picks = random.sample(pool, min(count, len(pool)))
    return [WORD_BANK[i]["ru"] for i in picks]


def normalize(text: str) -> str:
    text = text.strip().lower().strip(".,!?")
    text = text.replace("ё", "е")
    text = re.sub(r"[-–—]", " ", text)  # дефис и пробел считаем эквивалентными
    text = re.sub(r"\s+", " ", text).strip()
    return text


def acceptable_variants(word: dict) -> set:
    """Собирает все допустимые варианты русского перевода: основные значения,
    через запятую/слэш, содержимое в скобках отдельно и вручную заданные
    синонимы (поле 'alt')."""
    variants = set()
    for part in word["ru"].split(","):
        for piece in part.split("/"):
            piece = piece.strip()
            if not piece:
                continue
            variants.add(normalize(piece))
            match = re.match(r"^(.*?)\s*\((.*?)\)\s*$", piece)
            if match:
                base, extra = match.group(1).strip(), match.group(2).strip()
                if base:
                    variants.add(normalize(base))
                if extra:
                    variants.add(normalize(extra))
    for alt in word.get("alt", []):
        variants.add(normalize(alt))
    return variants


LEVEL_NAMES_RU = {"easy": "лёгкий", "medium": "средний", "hard": "сложный"}


def adjust_level(user: dict, session_correct: int, session_total: int):
    """Обновляет историю ответов. Если результаты слабые — тихо понижает
    уровень. Если сильные — НЕ меняет уровень сама, а возвращает уровень,
    который стоит предложить пользователю (спросить явно)."""
    history = user.get("history", [])
    history.extend([True] * session_correct + [False] * (session_total - session_correct))
    user["history"] = history[-20:]  # храним последние 20 ответов

    recent = user["history"][-10:]
    if len(recent) < 4:
        return None
    accuracy = sum(recent) / len(recent)

    level = user.get("level", "medium")
    idx = config.LEVELS.index(level)
    if accuracy <= 0.4 and idx > 0:
        user["level"] = config.LEVELS[idx - 1]
        return None
    if accuracy >= 0.8 and idx < len(config.LEVELS) - 1:
        return config.LEVELS[idx + 1]
    return None


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
    stats = user.get("word_stats", {})

    if str(word_index) not in stats:
        # Слово встречается впервые — сначала показываем карточку с переводом
        # и примером, без проверки. Отвечать по нему бот попросит в следующий раз.
        example = word["sentence"].replace("___", f"**{word['answer']}**")
        keyboard = [[InlineKeyboardButton("Понял, дальше →", callback_data=f"learn|{word_index}")]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🆕 Новое слово:\n\n*{word['en']}* — {word['ru']}\n\n_{example}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    level = user.get("level", "medium")

    if level == "easy":
        options = [word["ru"]] + build_distractors(word_index, 3)
        random.shuffle(options)
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"quiz|{word_index}|{opt}")]
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

    if session["mode"] == "learn_new":
        count = session.get("new_count", 0)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📚 Отлично! Вы познакомились с {count} новыми словами.\n"
                "Начиная с завтра они будут появляться на повторении в обычных уроках."
            ),
        )
        return

    correct, total = session["correct"], session["total"]
    user = storage.get_user(chat_id)

    suggested_level = None
    if session["mode"] == "morning":
        suggested_level = adjust_level(user, correct, total)
        user["streak"] = user.get("streak", 0) + 1
        storage.save_user(chat_id, user)

    percent = round(100 * correct / total) if total else 0
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏁 Урок завершён: {correct} из {total} правильных ({percent}%).",
    )

    if suggested_level:
        keyboard = [[
            InlineKeyboardButton(f"Да, перейти на {LEVEL_NAMES_RU[suggested_level]}", callback_data=f"setlevel|{suggested_level}"),
            InlineKeyboardButton("Оставить как есть", callback_data="setlevel|keep"),
        ]]
        await context.bot.send_message(
            chat_id=chat_id,
            text="Вы отлично справляетесь! Повысить уровень сложности?",
            reply_markup=InlineKeyboardMarkup(keyboard),
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
        "/learn — изучить пачку новых слов, когда есть свободное время\n"
        "/word — потренироваться на 1 слове вне расписания\n"
        "/status — ваш текущий уровень и статистика\n"
        "/level — выбрать уровень сложности вручную\n"
        "/help — справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = storage.get_user(chat_id)
    history = user.get("history", [])
    accuracy = round(100 * sum(history) / len(history)) if history else 0
    due_count = len(get_due_word_indices(user, limit=len(WORD_BANK)))
    learned_count = len(user.get("word_stats", {}))
    await update.message.reply_text(
        f"Уровень сложности: {LEVEL_NAMES_RU.get(user.get('level', 'medium'))}\n"
        f"Дней подряд с утренним уроком: {user.get('streak', 0)}\n"
        f"Точность за последние {len(history)} ответов: {accuracy}%\n"
        f"Слов в изучении: {learned_count} из {len(WORD_BANK)}\n"
        f"Ждут повторения сегодня: {due_count}"
    )


async def level_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[
        InlineKeyboardButton("Лёгкий", callback_data="setlevel|easy"),
        InlineKeyboardButton("Средний", callback_data="setlevel|medium"),
        InlineKeyboardButton("Сложный", callback_data="setlevel|hard"),
    ]]
    await update.message.reply_text(
        "Выберите уровень сложности:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def word_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in SESSIONS:
        await update.message.reply_text("Урок уже идёт — просто ответьте на текущий вопрос.")
        return
    word_index = random.randrange(len(WORD_BANK))
    SESSIONS[chat_id] = {"queue": [word_index], "correct": 0, "total": 0, "mode": "practice", "current": None}
    await send_next_question(chat_id, context)


async def learn_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in SESSIONS:
        await update.message.reply_text("Урок уже идёт — просто ответьте на текущий вопрос.")
        return

    user = storage.get_user(chat_id)
    stats = user.get("word_stats", {})
    seen = {int(k) for k in stats.keys()}
    unseen = [i for i in range(len(WORD_BANK)) if i not in seen]

    if not unseen:
        await update.message.reply_text("🎉 Вы уже познакомились со всеми словами из базы! Загляните в /status.")
        return

    pointer = user.get("word_pointer", 0)
    count = min(config.WORDS_PER_SESSION, len(unseen))
    picks = [unseen[(pointer + i) % len(unseen)] for i in range(count)]
    user["word_pointer"] = pointer + count
    storage.save_user(chat_id, user)

    await update.message.reply_text(f"📚 Новые слова для знакомства: {count}. Поехали!")
    SESSIONS[chat_id] = {
        "queue": picks,
        "correct": 0,
        "total": 0,
        "mode": "learn_new",
        "current": None,
        "new_count": count,
    }
    await send_next_question(chat_id, context)


async def handle_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    if not session or session["current"] is None:
        await update.message.reply_text("Сейчас нет активного вопроса. Пришлите /word, чтобы начать.")
        return

    user = storage.get_user(chat_id)
    if str(session["current"]) not in user.get("word_stats", {}):
        await update.message.reply_text(
            "Это новое слово — сначала нажмите кнопку «Понял, дальше →» под сообщением выше."
        )
        return

    word = WORD_BANK[session["current"]]
    level = user.get("level", "medium")
    user_answer = normalize(update.message.text)

    if level == "medium":
        is_correct = user_answer in acceptable_variants(word)
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

    update_word_stat(user, session["current"], is_correct)
    storage.save_user(chat_id, user)

    session["current"] = None
    await send_next_question(chat_id, context)


async def handle_callback_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    action, *rest = query.data.split("|")

    if action == "setlevel":
        new_level = rest[0]
        if new_level == "keep":
            await query.edit_message_text("Хорошо, оставляем текущий уровень.")
            return
        user = storage.get_user(chat_id)
        user["level"] = new_level
        storage.save_user(chat_id, user)
        await query.edit_message_text(f"Уровень сложности изменён на: {LEVEL_NAMES_RU[new_level]}.")
        return

    session = SESSIONS.get(chat_id)
    if not session or session["current"] is None:
        return

    if action == "learn":
        # Пользователь ознакомился с новым словом — отмечаем его изученным
        # (без учёта в счётчике правильных/неправильных) и переходим дальше.
        word_index = int(rest[0])
        user = storage.get_user(chat_id)
        stats = user.setdefault("word_stats", {})
        stats[str(word_index)] = {
            "box": 1,
            "next_review": (dt.date.today() + dt.timedelta(days=config.REVIEW_INTERVALS_DAYS[0])).isoformat(),
        }
        storage.save_user(chat_id, user)

        await query.edit_message_reply_markup(reply_markup=None)
        session["current"] = None
        await send_next_question(chat_id, context)
        return

    # action == "quiz"
    word_index_str, chosen_ru = rest
    word = WORD_BANK[session["current"]]

    session["total"] += 1
    is_correct = chosen_ru == word["ru"]
    if is_correct:
        session["correct"] += 1
        result_text = "✅ Верно!"
    else:
        result_text = f"❌ Неверно. Правильный ответ: {word['ru']}"

    user = storage.get_user(chat_id)
    update_word_stat(user, session["current"], is_correct)
    storage.save_user(chat_id, user)

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

# ---------------------------------------------------------------------------
# Меню команд Telegram (кнопка со списком рядом с полем ввода)
# ---------------------------------------------------------------------------

async def setup_commands_menu(application: Application) -> None:
    commands = [
        BotCommand("start", "Начать / как пользоваться ботом"),
        BotCommand("learn", "Изучить новые слова (когда есть время)"),
        BotCommand("word", "Потренироваться на 1 слове прямо сейчас"),
        BotCommand("status", "Мой уровень и статистика"),
        BotCommand("level", "Выбрать уровень сложности"),
        BotCommand("help", "Справка"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_СВОЙ_ТОКЕН":
        raise RuntimeError(
            "Не задан токен бота. Задайте переменную окружения TELEGRAM_BOT_TOKEN "
            "или впишите токен в BOT_TOKEN в этом файле."
        )

    application = Application.builder().token(BOT_TOKEN).post_init(setup_commands_menu).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("level", level_command))
    application.add_handler(CommandHandler("word", word_now))
    application.add_handler(CommandHandler("learn", learn_new))
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