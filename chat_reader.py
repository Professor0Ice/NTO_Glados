import logging
import re
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio
import string
import os

from aiogram import Router, Bot
from aiogram.types import Message

from database import get_user, add_message, delete_user, add_warning, clear_warnings
from moderation_content import inspect_content, has_untrusted_source
from spam_patterns import find_pattern, find_spam_rule, load_spam_rules

logger = logging.getLogger(__name__)

# Ищу команду/тиммейта: https://t.me/ontigamedev/209088
# В этой теме только удаление по мягким фильтрам, без предупреждений, банов и мутов.
TEAM_SEARCH_THREAD_ID = 209088

#Для темы счёта
DATA_DIR = os.getenv("DATA_DIR", "./")
COUNTER_FILE = os.path.join(DATA_DIR, "counter_state.txt")

def load_counter_state():
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
            if len(lines) >= 2:
                last_number = int(lines[0])
                last_user_id = int(lines[1]) if lines[1].isdigit() else None
                return last_number, last_user_id
    except (FileNotFoundError, ValueError):
        pass
    return 0, None

def save_counter_state(number, user_id):
    os.makedirs(os.path.dirname(COUNTER_FILE), exist_ok=True)
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        f.write(f"{number}\n{user_id}")

# ---------- Настройки антифлуда ----------
FLOOD_MAX_COUNT = 3
FLOOD_INTERVAL_SEC = 60         # за сколько секунд
MUTE_DURATION_HOURS = 2        # длительность мута за флуд
FLOOD_MAX_AGE_SEC = 1200

# Антиспам
NEW_USER_MSG_LIMIT = 2            # Сколько чистых сообщений нужно, чтобы стать проверенным
WARNING_BAN_THRESHOLD = 3        # После скольких предупреждений бан
WARNING_DELETE_AFTER = 15       # Через сколько секунд удалять предупреждения бота

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
            patterns = list(dict.fromkeys(
                line.strip().lower() for line in f if line.strip() and not line.lstrip().startswith("#")))
        logger.info(f"Загружено {path}")
        return patterns
    except FileNotFoundError:
        logger.warning(f"Файл {path} не найден.")
        return []

def tokenize(text: str):
    # Punctuation separates words instead of joining them ("привет,бля").
    return re.findall(r"\w+", text.casefold())


def find_bad_word(text: str, bad_words: set[str]) -> str | None:
    words = tokenize(text)
    for word in words:
        if word in bad_words:
            return word
    # Preserve dictionary phrases and hyphenated entries with whole-word bounds.
    for bad in sorted(bad_words):
        if " " not in bad and "-" not in bad:
            continue
        phrase = tokenize(bad)
        if phrase and any(words[index:index + len(phrase)] == phrase
                          for index in range(len(words) - len(phrase) + 1)):
            return bad
    return None

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
    text = inspect_content(message).text
    if not text:
        return True

    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    bad = find_bad_word(text, bad_words)
    if bad:
        await message.delete()
        logger.info(
            "Удалено сообщение с запрещённым словом от %s: '%s'",
            message.from_user.full_name, bad,
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

    if len(text) < 6:
        return False

    if any(c.isalpha() or c.isdigit() for c in text):
        return False

    chars_to_remove = string.whitespace + string.punctuation + '«»—…'
    cleaned = text.translate(str.maketrans('', '', chars_to_remove))

    return bool(cleaned)

# ---------- Предупреждение и бан новичков ----------
async def give_warning(bot: Bot, message: Message, target_chat_id: int, reason: str) -> bool:
    user_id = message.from_user.id
    current_warnings = add_warning(target_chat_id, user_id, message.message_id)

    if current_warnings >= WARNING_BAN_THRESHOLD:
        try:
            await bot.ban_chat_member(target_chat_id, user_id)
            logger.info(f"Забанен {message.from_user.full_name}")
            delete_user(user_id)
            clear_warnings(target_chat_id, user_id)
        except Exception as e:
            logger.error(f"Ошибка бана: {e}")
        return False

    warning_msg = await message.answer(
        f"{message.from_user.full_name}, {reason}. "
        f"Предупреждение {current_warnings}/{WARNING_BAN_THRESHOLD}.\n"
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
async def filter_new_user(message: Message, bot: Bot, target_chat_id: int, hard_patterns: list,
                          trusted_chat_ids: set[int] | None = None, *, is_edit: bool = False,
                          spam_rules=()):
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    user_id = message.from_user.id
    user = get_user(user_id)

    if user.get("is_verified", 0) == 1:
        return True

    content = inspect_content(message)
    text = content.text

    # Жёсткие паттерны → бан
    pattern = find_pattern(text, hard_patterns)
    if pattern:
        await message.delete()
        try:
            await bot.ban_chat_member(target_chat_id, user_id)
            logger.info(f"Забанен {message.from_user.full_name} за спам-паттерн '{pattern}'")
            delete_user(user_id)
            clear_warnings(target_chat_id, user_id)
        except Exception as e:
            logger.error(f"Ошибка бана: {e}")
        return False

    # Сочетания признаков рекламы → предупреждение вместо бана по одному слову.
    rule = find_spam_rule(text, spam_rules)
    if rule:
        await message.delete()
        logger.info("Удалена реклама от %s по правилу %s", message.from_user.full_name, rule)
        return await give_warning(bot, message, target_chat_id, "рекламные предложения запрещены")

    # Эмодзи → удаление и предупреждение, бан только за повторные нарушения.
    if text and is_emoji_only(text):
        await message.delete()
        return await give_warning(bot, message, target_chat_id, "сообщения только из эмодзи запрещены")

    if has_untrusted_source(message, trusted_chat_ids or {target_chat_id}):
        await message.delete()
        return await give_warning(bot, message, target_chat_id,
                                  "пересылки и цитаты из неизвестных источников запрещены")

    # Медиа (кроме стикеров)
    if content.has_media:
        await message.delete()
        return await give_warning(bot, message, target_chat_id, "медиа запрещены")

    # Ссылки
    if content.has_link:
        await message.delete()
        return await give_warning(bot, message, target_chat_id, "ссылки запрещены")

    # Упоминания
    if content.has_mention:
        await message.delete()
        return await give_warning(bot, message, target_chat_id, "упоминания запрещены")

    # Чистое сообщение
    became_verified = (not is_edit and add_message(
        user_id, message.from_user.username or message.from_user.full_name))
    if became_verified:
        logger.info(f"Пользователь {message.from_user.full_name} теперь проверенный")
    return True

# ---------- Лёгкий постоянный фильтр ----------
async def filter_soft_spam(message: Message, bot: Bot, soft_patterns: list, spam_rules=()):
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True

    text = inspect_content(message).text
    if not text:
        return True

    reason = find_pattern(text, soft_patterns) or find_spam_rule(text, spam_rules)
    if reason:
        await message.delete()
        logger.info("Удалено сообщение от %s по спам-правилу '%s'", message.from_user.full_name, reason)
        return False
    return True

async def handle_counter_message(message: Message, bot: Bot, target_chat_id: int):
    global last_counter_number, last_counter_user

    if await is_admin(bot, target_chat_id, message.from_user.id):
        text = (message.text or message.caption or "").strip()
        if text.isdigit():
            num = int(text)
            if num == last_counter_number + 1 and message.from_user.id != last_counter_user:
                last_counter_number = num
                last_counter_user = message.from_user.id
                save_counter_state(last_counter_number, last_counter_user)
                logger.info(f"Счётчик: {num} от {message.from_user.full_name} (админ)")
        return

    text = (message.text or message.caption or "").strip()
    if not text.isdigit():
        await message.delete()
        return

    num = int(text)
    if num != last_counter_number + 1:
        await message.delete()
        return

    if message.from_user.id == last_counter_user:
        await message.delete()
        return

    last_counter_number = num
    last_counter_user = message.from_user.id
    save_counter_state(last_counter_number, last_counter_user)
    logger.info(f"Счётчик: {num} от {message.from_user.full_name}")

# ---------- Главный обработчик (всё в одном) ----------
def setup_chat_reader(bot: Bot, target_chat_id: int, bot_config: dict = None,
                      source_chat_id: int | None = None):
    chat_reader_router = Router()
    trusted_chat_ids = {target_chat_id}
    if source_chat_id is not None:
        trusted_chat_ids.add(source_chat_id)
    if bot_config is None:
        bot_config = {"allow_other_bots": False}
    allowed_packs = load_sticker_whitelist()
    bad_words = load_bad_words()
    hard_patterns = load_patterns("patterns_hard.txt")
    soft_patterns = load_patterns("patterns_soft.txt")
    spam_rules = load_spam_rules()

    global last_counter_number, last_counter_user
    counter_thread_id = os.getenv("COUNTER_THREAD_ID")
    if counter_thread_id:
        counter_thread_id = int(counter_thread_id)
        last_counter_number, last_counter_user = load_counter_state()
        logger.info(f"Счётчик активен в теме {counter_thread_id}, последнее число: {last_counter_number}")
    else:
        last_counter_number = None

    async def handle_all(message: Message, *, is_edit: bool = False):
        if message.chat.id != target_chat_id:
            return
        is_team_topic = message.message_thread_id == TEAM_SEARCH_THREAD_ID

        if (not is_team_topic and not is_edit and message.from_user and last_counter_number is not None
                and message.message_thread_id == counter_thread_id):
            await handle_counter_message(message, bot, target_chat_id)
            return

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

        if message.from_user and message.from_user.first_name == "Telegram":
            return
        if not message.from_user:
            return

        user = message.from_user.full_name if message.from_user else "Unknown"
        text = inspect_content(message).text

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
        if not is_team_topic:
            if not is_edit and not await filter_flood(message, bot):
                return
            if not await filter_new_user(message, bot, target_chat_id, hard_patterns,
                                         trusted_chat_ids, is_edit=is_edit, spam_rules=spam_rules):
                return
        if not await filter_soft_spam(message, bot, soft_patterns, spam_rules):
            return
        if not await filter_stickers(message, bot, allowed_packs):
            return
        if not await filter_bad_words(message, bot, bad_words):
            return

    @chat_reader_router.message()
    async def handle_message(message: Message):
        await handle_all(message)

    @chat_reader_router.edited_message()
    async def handle_edited_message(message: Message):
        await handle_all(message, is_edit=True)

    return chat_reader_router
