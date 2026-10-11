# config.py
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

def setup_logging():
    """Настройка логирования в файл и консоль"""
    logs_dir = Path(__file__).resolve().parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler = RotatingFileHandler(
        logs_dir / "app.log",
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    # Настраиваем логирование для uvicorn
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_logger.addHandler(file_handler)
    
    # Настраиваем логирование для uvicorn.access (HTTP запросы)
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.setLevel(logging.INFO)
    uvicorn_access_logger.addHandler(file_handler)
    
    return logger

ENV_PATH = Path(__file__).resolve().parent.parent / "inf.env"
load_dotenv(dotenv_path=ENV_PATH, override=False)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_BOT_NAME = os.getenv("MAX_BOT_NAME", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://127.0.0.1:8443").rstrip("/")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = Path(ROOT)
FILES_DIR = PROJECT_ROOT / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB для MVP
