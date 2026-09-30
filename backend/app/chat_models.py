"""Chat intake is a draft builder, not an application execution endpoint."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


ChatIntent = Literal["apply", "recommend", "profile", "clarify"]


class ChatLink(BaseModel):
    url: str = Field(max_length=2000)
    source: Literal["text", "qr", "image"]
    verified: bool = False


class ChatTaskDraft(BaseModel):
    company: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    recruitment_cycle: str = Field(default="", max_length=100)
    url: str = Field(default="", max_length=2000)
    intent: ChatIntent = "clarify"
    summary: str = Field(default="", max_length=1000)
    warnings: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)
    links: list[ChatLink] = Field(default_factory=list, max_length=12)
    needs_confirmation: bool = True


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str = Field(max_length=16000)
    created_at: datetime
    image_names: list[Annotated[str, Field(max_length=160)]] = Field(default_factory=list, max_length=1)


class ChatConversationSummary(BaseModel):
    id: str
    title: str = Field(max_length=100)
    created_at: datetime
    updated_at: datetime


class ChatConversation(ChatConversationSummary):
    messages: list[ChatMessage] = Field(default_factory=list, max_length=200)
    draft: ChatTaskDraft | None = None


class ChatDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    recruitment_cycle: str = Field(default="", max_length=100)
    url: str = Field(default="", max_length=2000)
    intent: ChatIntent = "clarify"


class ChatImageLinkProposal(BaseModel):
    """Vision output is an explicitly unverified transcription, never a QR decoder."""
    url: str = Field(default="", max_length=2000)
    visible_text: str = Field(default="", max_length=2000)


class ChatInterpretation(BaseModel):
    company: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    recruitment_cycle: str = Field(default="", max_length=100)
    intent: ChatIntent = "clarify"
    selected_url: str = Field(default="", max_length=2000)
    image_links: list[ChatImageLinkProposal] = Field(default_factory=list, max_length=8)
    candidate_job_titles: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=12)
    needs_clarification: bool = False
    clear_fields: list[Literal["company", "job_title", "city", "recruitment_cycle", "url"]] = Field(default_factory=list, max_length=5)
    warnings: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=8)
