# -*- coding: utf-8 -*-
"""
Простое хранилище данных бота в JSON-файле.

ВАЖНО: на бесплатном тарифе Railway диск не постоянный — при каждом новом
деплое (например, если вы измените код и загрузите заново) файл data.json
может обнулиться. Для личного использования это не критично: бот просто
"забудет" вашу историю и начнёт заново с среднего уровня.
"""

import json
import os
import threading

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
_lock = threading.Lock()

DEFAULT_USER = {
    "level": "medium",       # easy / medium / hard
    "history": [],           # список True/False - последние ответы, для подбора уровня
    "today_words": [],       # индексы слов из WORD_BANK на сегодня
    "word_pointer": 0,       # для последовательной ротации слов по кругу
    "streak": 0,             # дней подряд с пройденным утренним уроком
}


def _load_all() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save_all(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(chat_id: int) -> dict:
    with _lock:
        data = _load_all()
        user = data.get(str(chat_id))
        if user is None:
            user = dict(DEFAULT_USER)
            data[str(chat_id)] = user
            _save_all(data)
        return user


def save_user(chat_id: int, user: dict) -> None:
    with _lock:
        data = _load_all()
        data[str(chat_id)] = user
        _save_all(data)


def all_chat_ids() -> list:
    with _lock:
        data = _load_all()
        return [int(cid) for cid in data.keys()]
