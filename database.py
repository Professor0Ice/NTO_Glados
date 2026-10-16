import sqlite3
import logging
import os
from contextlib import closing

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./")
DB_PATH = os.path.join(DATA_DIR, "users.db")

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            message_count INTEGER DEFAULT 0,
            is_verified INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS moderation_warnings (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id, message_id)
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"База данных готова: {DB_PATH}")

def get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, message_count, is_verified FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return {}
    keys = ["user_id", "username", "message_count", "is_verified"]
    return dict(zip(keys, row))

def add_message(user_id: int, username: str = "") -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    user = get_user(user_id)
    if not user:
        c.execute("INSERT INTO users (user_id, username, message_count, is_verified) VALUES (?, ?, 1, 0)",
                  (user_id, username))
        conn.commit()
        conn.close()
        return False
    if user["is_verified"]:
        conn.close()
        return False
    new_count = user["message_count"] + 1
    c.execute("UPDATE users SET message_count = ?, username = ? WHERE user_id = ?",
              (new_count, username, user_id))
    if new_count >= 3:
        c.execute("UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"Пользователь {username or user_id} проверен")
        return True
    conn.commit()
    conn.close()
    return False

def delete_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_verified(user_id: int) -> bool:
    user = get_user(user_id)
    return user.get("is_verified", 0) == 1


def get_warning_count(chat_id: int, user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM moderation_warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()[0]


def add_warning(chat_id: int, user_id: int, message_id: int) -> int:
    # A repeated delivery/edit of the same message must not count as a new warning.
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO moderation_warnings (chat_id, user_id, message_id) VALUES (?, ?, ?)",
            (chat_id, user_id, message_id),
        )
        return conn.execute(
            "SELECT COUNT(*) FROM moderation_warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()[0]


def clear_warnings(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "DELETE FROM moderation_warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
