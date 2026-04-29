import sqlite3
import logging
import os

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