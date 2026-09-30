"""Small, per-user SQLite chat history. Image bytes never enter this module."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from . import storage
from .chat_models import ChatConversation, ChatConversationSummary


MAX_CONVERSATIONS = 200
MAX_MESSAGES = 200


class ConversationNotFound(LookupError):
    pass


class ConversationConflict(RuntimeError):
    pass


def initialize() -> None:
    with storage._connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_conversations (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
            conversation_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS chat_conversations_owner
                        ON chat_conversations(user_id, updated_at)""")


def list_conversations() -> list[ChatConversationSummary]:
    user_id = storage.current_user_id()
    initialize()
    with storage._connection() as conn:
        rows = conn.execute("""SELECT id,title,created_at,updated_at FROM chat_conversations
            WHERE user_id=? ORDER BY updated_at DESC LIMIT ?""", (user_id, MAX_CONVERSATIONS)).fetchall()
    return [ChatConversationSummary(**dict(row)) for row in rows]


def create_conversation() -> ChatConversation:
    user_id = storage.current_user_id()
    initialize()
    now = datetime.now(timezone.utc)
    conversation = ChatConversation(id=str(uuid4()), title="新的求职任务", created_at=now, updated_at=now)
    with storage._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) FROM chat_conversations WHERE user_id=?", (user_id,)).fetchone()[0]
        if count >= MAX_CONVERSATIONS:
            raise ValueError("会话数量已达到上限，请继续使用已有会话")
        conn.execute("""INSERT INTO chat_conversations
            (id,user_id,title,conversation_json,created_at,updated_at) VALUES (?,?,?,?,?,?)""",
            (conversation.id, user_id, conversation.title, conversation.model_dump_json(), now.isoformat(), now.isoformat()))
    return conversation


def get_conversation(conversation_id: str) -> ChatConversation:
    user_id = storage.current_user_id()
    initialize()
    with storage._connection() as conn:
        row = conn.execute("SELECT conversation_json FROM chat_conversations WHERE id=? AND user_id=?",
                           (conversation_id, user_id)).fetchone()
    if row is None:
        raise ConversationNotFound("会话不存在")
    return ChatConversation.model_validate_json(row["conversation_json"])


def save_turn(conversation: ChatConversation, expected_updated_at: datetime) -> ChatConversation:
    user_id = storage.current_user_id()
    initialize()
    if len(conversation.messages) > MAX_MESSAGES:
        raise ValueError("本会话消息较多，请新建会话继续；现有记录会保留")
    conversation.updated_at = datetime.now(timezone.utc)
    with storage._connection() as conn:
        cursor = conn.execute("""UPDATE chat_conversations
            SET title=?,conversation_json=?,updated_at=? WHERE id=? AND user_id=? AND updated_at=?""",
            (conversation.title, conversation.model_dump_json(), conversation.updated_at.isoformat(),
             conversation.id, user_id, expected_updated_at.isoformat()))
        if cursor.rowcount != 1:
            exists = conn.execute("SELECT 1 FROM chat_conversations WHERE id=? AND user_id=?",
                                  (conversation.id, user_id)).fetchone()
            if not exists:
                raise ConversationNotFound("会话不存在")
            raise ConversationConflict("另一个消息正在更新此会话，请刷新后重试")
    return conversation
