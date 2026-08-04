from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str = "新建演示文稿"


class MessageCreate(BaseModel):
    prompt: str = Field(min_length=1)
    attachments: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class RevisionCreate(BaseModel):
    scope: Literal["whole_deck", "selected_pages"]
    instruction: str = Field(min_length=1)
    pages: list[int] = Field(default_factory=list)


class StyleCaseCreate(BaseModel):
    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class StyleImportCreate(BaseModel):
    source: str = Field(min_length=1)
    name: str = ""
    use_model: bool = True
