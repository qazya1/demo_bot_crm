"""
Запуск MAX бота из корневой директории проекта.
Использование: python run_max_bot.py
"""
import sys
import os

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    from bot import max_bot_main

    max_bot_main.main()
