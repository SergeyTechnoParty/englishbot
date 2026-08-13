# -*- coding: utf-8 -*-
import os

# Часовой пояс для расписания. Можно поменять на Railway в переменной TIMEZONE,
# например "Europe/Moscow", "Asia/Bangkok", "Europe/Kyiv" и т.д.
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Bangkok")

# Время утреннего урока и вечернего повторения (24-часовой формат)
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", 9))
MORNING_MINUTE = int(os.environ.get("MORNING_MINUTE", 0))

EVENING_HOUR = int(os.environ.get("EVENING_HOUR", 20))
EVENING_MINUTE = int(os.environ.get("EVENING_MINUTE", 0))

WORDS_PER_SESSION = 8  # 4 рабочих + 4 повседневных слова в день

LEVELS = ["easy", "medium", "hard"]
