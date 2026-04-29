import logging
import re
from datetime import datetime, timedelta
from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.filters import Command

logger = logging.getLogger(__name__)
admin_router = Router()

#бот кик базовый
bot_config = {"allow_other_bots": False}

def parse_duration(text: str) -> timedelta | None:
    text = text.strip().lower()
    if text.isdigit():
        return timedelta(minutes=int(text))
    match = re.match(r'^(\d+)([mhdw])$', text)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        if unit == 'm':
            return timedelta(minutes=val)
        elif unit == 'h':
            return timedelta(hours=val)
        elif unit == 'd':
            return timedelta(days=val)
        elif unit == 'w':
            return timedelta(weeks=val)
    return None

def setup_admin(bot: Bot, target_chat_id: int):
    async def is_admin(user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(target_chat_id, user_id)
            return member.status in ("creator", "administrator")
        except Exception as e:
            logger.warning(f"Ошибка проверки админа: {e}")
            return False

    @admin_router.message(Command("help", ignore_mention=True))
    async def cmd_help(message: Message):
        if not await is_admin(message.from_user.id):
            return
        await message.answer(
            "/mute <длительность> (ответом на сообщение) — замутить (по умолчанию 1 час)\n"
            "/unmute (ответом на сообщение) — размутить\n"
            "/toggle_kick_bots — автобан ботов"
        )
        try:
            await message.delete()
        except Exception:
            pass

    @admin_router.message(Command("mute", ignore_mention=True))
    async def cmd_mute(message: Message):
        if not await is_admin(message.from_user.id):
            return

        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer(
                "<b>Ответьте на сообщение нарушителя и введите команду:</b>\n"
                "<code>/mute &lt;длительность&gt;</code> — например, /mute 2h\n",
                parse_mode="HTML"
            )
            try:
                await message.delete()
            except Exception:
                pass
            return

        parts = message.text.split()
        duration_str = parts[1] if len(parts) > 1 else "1h"
        duration = parse_duration(duration_str)
        if not duration:
            await message.answer("Неверный формат длительности. Примеры: 10m, 2h, 1d, 30w")
            try:
                await message.delete()
            except Exception:
                pass
            return

        user_id = message.reply_to_message.from_user.id

        try:
            member = await bot.get_chat_member(target_chat_id, user_id)
            if member.status in ("creator", "administrator"):
                try:
                    await message.delete()
                except Exception:
                    pass
                return
        except Exception:
            pass

        until = datetime.now() + duration
        try:
            await bot.restrict_chat_member(
                chat_id=target_chat_id,
                user_id=user_id,
                permissions={
                    "can_send_messages": False,
                    "can_send_media": False,
                    "can_send_other": False,
                    "can_add_web_page_previews": False
                },
                until_date=until
            )
            user_name = message.reply_to_message.from_user.full_name
            await message.answer(f"🔇 {user_name} замьючен на {duration_str}.")
            logger.info(f"Мут {user_name} ({user_id}) на {duration_str}")
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
            logger.error(f"Ошибка mute: {e}")

        try:
            await message.delete()
        except Exception:
            pass

    @admin_router.message(Command("unmute", ignore_mention=True))
    async def cmd_unmute(message: Message):
        if not await is_admin(message.from_user.id):
            return

        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer(
                "ℹ️ <b>Ответьте на сообщение пользователя и введите /unmute</b>",
                parse_mode="HTML"
            )
            return

        user_id = message.reply_to_message.from_user.id
        try:
            await bot.restrict_chat_member(
                chat_id=target_chat_id,
                user_id=user_id,
                permissions={
                    "can_send_messages": True,
                    "can_send_media": True,
                    "can_send_other": True,
                    "can_add_web_page_previews": True
                }
            )
            user_name = message.reply_to_message.from_user.full_name
            await message.answer(f"🔊 {user_name} размьючен.")
            logger.info(f"Размучен {user_name} ({user_id})")
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
            logger.error(f"Ошибка unmute: {e}")
        try:
            await message.delete()
        except Exception:
            pass

    bot_config = {"allow_other_bots": False}

    @admin_router.message(Command("toggle_kick_bots", ignore_mention=True))
    async def cmd_toggle_kick_bots(message: Message):
        if not await is_admin(message.from_user.id):
            return
        bot_config["allow_other_bots"] = not bot_config["allow_other_bots"]
        state = "разрешены" if bot_config["allow_other_bots"] else "запрещены"
        await message.answer(f"Другие боты теперь {state}.")
        try:
            await message.delete()
        except Exception:
            pass

    return admin_router, bot_config