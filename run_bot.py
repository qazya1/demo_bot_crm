"""
Запуск Telegram бота из корневой директории проекта.
Использование: python run_bot.py
"""
import sys
import os

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Запускаем бота как модуль
if __name__ == "__main__":
    from bot import bot_main

    bot_main.main()
