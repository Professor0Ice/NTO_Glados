import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from aiogram import Bot, Dispatcher
import os
from dotenv import load_dotenv

from forwarder import setup_forwarder
from chat_reader import setup_chat_reader
from admin import setup_admin
from database import init_db

load_dotenv()

DATA_DIR = os.getenv("DATA_DIR", "./")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")

handler = TimedRotatingFileHandler(
    "bot.log",
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8"
)
handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def load_config_from_env():
    return {
        "BOT_TOKEN": os.getenv("BOT_TOKEN"),
        "SOURCE_CHANNEL": os.getenv("SOURCE_CHANNEL"),
        "TARGET_CHAT": os.getenv("TARGET_CHAT"),
        "TARGET_THREAD_ID": int(os.getenv("TARGET_THREAD_ID")),
    }

async def main():
    init_db()
    cfg = load_config_from_env()
    if not all(cfg.values()):
        logger.error("Одна или несколько переменных окружения не заданы. Проверь .env или настройки хостинга.")
        return

    bot = Bot(token=cfg["BOT_TOKEN"])
    dp = Dispatcher()

    source_chat = await bot.get_chat(cfg["SOURCE_CHANNEL"])
    source_chat_id = source_chat.id
    logger.info(f"Канал: {source_chat.full_name} (ID={source_chat_id})")

    target_chat = await bot.get_chat(cfg["TARGET_CHAT"])
    target_chat_id = target_chat.id
    logger.info(f"Чат: {target_chat.full_name} (ID={target_chat_id})")

    admin_router, bot_config = setup_admin(bot, target_chat_id)
    dp.include_router(admin_router)
    dp.include_router(setup_forwarder(source_chat_id, target_chat_id, cfg["TARGET_THREAD_ID"]))
    dp.include_router(setup_chat_reader(bot, target_chat_id, bot_config))

    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())