import logging
from aiogram import Router
from aiogram.types import Message

logger = logging.getLogger(__name__)
forwarder_router = Router()

def setup_forwarder(source_chat_id: int, target_chat_id: int, thread_id: int):
    @forwarder_router.channel_post()
    async def forward_channel_post(post: Message):
        if post.chat.id == source_chat_id:
            try:
                await post.bot.forward_message(
                    chat_id=target_chat_id,
                    from_chat_id=source_chat_id,
                    message_id=post.message_id,
                    message_thread_id=thread_id
                )
                logger.info(f"Переслан пост из канала: {post.message_id}")
            except Exception as e:
                logger.error(f"Ошибка пересылки: {e}")

    return forwarder_router