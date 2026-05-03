import logging
import re
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio
import string

from aiogram import Router, Bot
from aiogram.types import Message

from database import get_user, add_message, delete_user, is_verified

logger = logging.getLogger(__name__)
chat_reader_router = Router()

# ---------- Настройки антифлуда ----------
FLOOD_MAX_COUNT = 3
FLOOD_INTERVAL_SEC = 60         # за сколько секунд
MUTE_DURATION_HOURS = 2        # длительность мута за флуд
FLOOD_MAX_AGE_SEC = 1200

# Антиспам
NEW_USER_MSG_LIMIT = 2            # Сколько чистых сообщений нужно, чтобы стать проверенным
WARNING_BAN_THRESHOLD = 2        # После скольких предупреждений жёсткий бан
WARNING_DELETE_AFTER = 30       # Через сколько секунд удалять предупреждения бота
warnings = defaultdict(int)      # предупреждения в памяти

# Хранилище состояний для антифлуда
flood_tracker = defaultdict(lambda: {"text": "", "count": 0, "first_seen": datetime.now()})

# ---------- Загрузка списков ----------
def load_sticker_whitelist(path="whitelist_stickers.txt"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            packs = [line.strip().lower() for line in f if line.strip()]
        return packs
    except FileNotFoundError:
        logger.warning(f"Файл {path} не найден")
        return []

def load_bad_words(path="bad_words.txt"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            words = {line.strip().lower() for line in f if line.strip()}
        logger.info(f"Загружено {len(words)} запрещённых слов")
        return words
    except FileNotFoundError:
        logger.warning(f"Файл {path} не найден")
        return set()

def load_patterns(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            patterns = [line.strip().lower() for line in f if line.strip()]
        logger.info(f"Загружено {path}")
        return patterns
    except FileNotFoundError:
        logger.warning(f"Файл {path} не найден.")
        return []

def tokenize(text: str):
    clean = re.sub(r'[^\w\s]', '', text.lower())
    return clean.split()

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.error(f"Ошибка проверки админа: {e}")
        return False

# ---------- Старые фильтры ----------
async def filter_stickers(message: Message, bot: Bot, allowed_packs: list):
    if not message.sticker:
        return True

    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    sticker_set = message.sticker.set_name
    if not sticker_set:
        await message.delete()
        logger.info(f"Удалён стикер от {message.from_user.full_name}")
        return False

    if sticker_set.lower() not in allowed_packs:
        await message.delete()
        logger.info(
            f"Удалён стикер из пака '{sticker_set}' "
            f"(не в белом списке) от {message.from_user.full_name}"
        )
        return False
    return True

async def filter_bad_words(message: Message, bot: Bot, bad_words: set):
    if not message.text and not message.caption:
        return True

    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    text = message.text or message.caption
    words = tokenize(text)

    for word in words:
        for bad in bad_words:
            if bad in word:
                await message.delete()
                logger.info(
                    f"Удалено сообщение с запрещённым словом от {message.from_user.full_name}: "
                    f"'{word}' содержит '{bad}'"
                )
                return False
    return True

async def filter_flood(message: Message, bot: Bot):
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    user_id = message.from_user.id
    now = datetime.now()
    text = message.text or message.caption or ""
    if not text:
        return True

    # Очистка устаревших записей
    expired = [uid for uid, data in flood_tracker.items()
               if (now - data["first_seen"]).total_seconds() > FLOOD_MAX_AGE_SEC]
    for uid in expired:
        del flood_tracker[uid]

    tracker = flood_tracker[user_id]
    if tracker["text"] != text:
        tracker["text"] = text
        tracker["count"] = 1
        tracker["first_seen"] = now
        return True

    elapsed = (now - tracker["first_seen"]).total_seconds()
    if elapsed <= FLOOD_INTERVAL_SEC:
        tracker["count"] += 1
    else:
        tracker["text"] = text
        tracker["count"] = 1
        tracker["first_seen"] = now
        return True

    if tracker["count"] >= FLOOD_MAX_COUNT:
        until = now + timedelta(hours=MUTE_DURATION_HOURS)
        try:
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user_id,
                permissions={
                    "can_send_messages": False,
                    "can_send_media": False,
                    "can_send_other": False,
                    "can_add_web_page_previews": False
                },
                until_date=until
            )
            await message.answer(f"🔇 {message.from_user.full_name} замучен на {MUTE_DURATION_HOURS} час(а). Причина: флуд")
            logger.info(f"🔇 {message.from_user.full_name} замучен на {MUTE_DURATION_HOURS} час(а). Причина: флуд")
            del flood_tracker[user_id]
            return False
        except Exception as e:
            logger.error(f"Ошибка мута за флуд: {e}")
            await message.delete()
            return False

    return True

def is_emoji_only(text: str) -> bool:
    if not text:
        return False

    if re.search(r'[a-zA-Zа-яёЁ0-9]', text):
        return False

    chars_to_remove = string.whitespace + string.punctuation + '«»—…'
    cleaned = text.translate(str.maketrans('', '', chars_to_remove))

    return bool(cleaned)

# ---------- Предупреждение и бан новичков ----------
async def give_warning(bot: Bot, message: Message, target_chat_id: int, reason: str) -> bool:
    user_id = message.from_user.id
    warnings[user_id] += 1
    current_warnings = warnings[user_id]

    if current_warnings >= WARNING_BAN_THRESHOLD:
        try:
            await bot.ban_chat_member(target_chat_id, user_id)
            logger.info(f"Забанен {message.from_user.full_name}")
        except Exception as e:
            logger.error(f"Ошибка бана: {e}")
        delete_user(user_id)
        warnings.pop(user_id, None)
        return False

    warning_msg = await message.answer(
        f"{message.from_user.full_name}, {reason}\n"
        f"Пока вы новичок в этой группе, вам разрешён только текст"
    )

    async def del_warning():
        await asyncio.sleep(WARNING_DELETE_AFTER)
        try:
            await warning_msg.delete()
        except Exception:
            pass
    asyncio.create_task(del_warning())

    return False

# ---------- Жёсткий фильтр для новичков ----------
async def filter_new_user(message: Message, bot: Bot, target_chat_id: int, hard_patterns: list):
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    user_id = message.from_user.id
    user = get_user(user_id)

    if user.get("is_verified", 0) == 1:
        return True

    text = message.text or message.caption or ""

    # Эмодзи → бан
    if text and is_emoji_only(text):
        await message.delete()
        try:
            await bot.ban_chat_member(target_chat_id, user_id)
            logger.info(f"Забанен {message.from_user.full_name} за эмодзи")
        except Exception as e:
            logger.error(f"Ошибка бана за эмодзи: {e}")
        delete_user(user_id)
        warnings.pop(user_id, None)
        return False

    # Жёсткие паттерны → бан
    if text:
        lower_text = text.lower()
        for pattern in hard_patterns:
            if pattern in lower_text:
                await message.delete()
                try:
                    await bot.ban_chat_member(target_chat_id, user_id)
                    logger.info(f"Забанен {message.from_user.full_name} за спам-паттерн '{pattern}'")
                except Exception as e:
                    logger.error(f"Ошибка бана за спам: {e}")
                delete_user(user_id)
                warnings.pop(user_id, None)
                return False

    # Медиа (кроме стикеров)
    if message.photo or message.video or message.document or message.animation or message.voice or message.video_note:
        await message.delete()
        return await give_warning(bot, message, target_chat_id, "медиа запрещены")

    # Ссылки
    has_link = False
    if text and ('http://' in text or 'https://' in text or 't.me/' in text):
        has_link = True
    if message.entities:
        for entity in message.entities:
            if entity.type in ("url", "text_link"):
                has_link = True
                break
    if has_link:
        await message.delete()
        return await give_warning(bot, message, target_chat_id, "ссылки запрещены")

    # Упоминания
    if text and '@' in text:
        if message.entities:
            for entity in message.entities:
                if entity.type in ("mention", "text_mention"):
                    await message.delete()
                    return await give_warning(bot, message, target_chat_id, "упоминания запрещены")
        else:
            await message.delete()
            return await give_warning(bot, message, target_chat_id, "упоминания запрещены")

    # Чистое сообщение
    became_verified = add_message(user_id, message.from_user.username or message.from_user.full_name)
    if became_verified:
        logger.info(f"Пользователь {message.from_user.full_name} теперь проверенный")
    return True

# ---------- Лёгкий постоянный фильтр ----------
async def filter_soft_spam(message: Message, bot: Bot, soft_patterns: list):
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    text = message.text or message.caption or ""
    if not text:
        return True

    lower_text = text.lower()
    for pattern in soft_patterns:
        if pattern in lower_text:
            await message.delete()
            logger.info(f"Удалено сообщение от {message.from_user.full_name} по спам паттерну '{pattern}'")
            return False
    return True

# ---------- Главный обработчик (всё в одном) ----------
def setup_chat_reader(bot: Bot, target_chat_id: int, bot_config: dict = None):
    if bot_config is None:
        bot_config = {"allow_other_bots": False}
    allowed_packs = load_sticker_whitelist()
    bad_words = load_bad_words()
    hard_patterns = load_patterns("patterns_hard.txt")
    soft_patterns = load_patterns("patterns_soft.txt")

    @chat_reader_router.message()
    async def handle_all(message: Message):
        if message.new_chat_members and message.chat.id == target_chat_id:
            if not bot_config.get("allow_other_bots", False):
                for new_member in message.new_chat_members:
                    if new_member.is_bot and new_member.id != bot.id:
                        try:
                            await bot.ban_chat_member(target_chat_id, new_member.id)
                            await bot.unban_chat_member(target_chat_id, new_member.id)
                            logger.info(f"Удалён бот {new_member.full_name} (ID: {new_member.id})")
                        except Exception as e:
                            logger.error(f"Ошибка при удалении бота: {e}")
            return

        user = message.from_user.full_name if message.from_user else "Unknown"
        text = message.text or message.caption or ""

        media_info = ""
        if message.photo:
            media_info = "Фото"
        elif message.video:
            media_info = "Видео"
        elif message.document:
            media_info = f"Документ ({message.document.mime_type})"
        elif message.animation:
            media_info = "GIF"
        elif message.voice:
            media_info = "Голосовое"
        elif message.video_note:
            media_info = "Кружок"
        else:
            media_info = message.content_type

        quote_info = ""
        if message.reply_to_message:
            reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
            if reply_text:
                quote_info = f" | ↩ Ответ на: '{reply_text[:50]}{'...' if len(reply_text) > 50 else ''}'"
            else:
                quote_info = " | ↩ Ответ (без текста)"
        elif message.forward_origin:
            fwd_text = text or "<медиа>"
            from_user = None
            if hasattr(message.forward_origin, 'sender_user') and message.forward_origin.sender_user:
                from_user = message.forward_origin.sender_user.full_name
            elif hasattr(message.forward_origin, 'sender_chat') and message.forward_origin.sender_chat:
                from_user = message.forward_origin.sender_chat.full_name
            elif hasattr(message.forward_origin, 'chat') and message.forward_origin.chat:
                from_user = message.forward_origin.chat.full_name
            from_str = f" от {from_user}" if from_user else ""
            quote_info = f" | Переслано{from_str}: '{fwd_text[:50]}{'...' if len(fwd_text) > 50 else ''}'"

        logger.info(f"Сообщение: user={user} | {media_info} | текст='{text[:100]}'{quote_info}")

        if message.chat.id != target_chat_id:
            return

        # Фильтры
        if not await filter_flood(message, bot):
            return
        if not await filter_new_user(message, bot, target_chat_id, hard_patterns):
            return
        if not await filter_soft_spam(message, bot, soft_patterns):
            return
        if not await filter_stickers(message, bot, allowed_packs):
            return
        if not await filter_bad_words(message, bot, bad_words):
            return

    return chat_reader_router