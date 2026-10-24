"""Pydantic request and response schemas used by the NovelCraft API."""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, PrivateAttr, StrictInt


def gen_id() -> str:
    return uuid4().hex[:12]


class CharacterContext(BaseModel):
    name: str
    role: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    appearance: Optional[str] = None
    identity: Optional[str] = None
    personality: Optional[str] = None
    background: Optional[str] = None
    coreTrauma: Optional[str] = None
    surfaceMotivation: Optional[str] = None
    deepDesire: Optional[str] = None
    arc: Optional[str] = None
    catchphrase: Optional[str] = None
    speechStyle: Optional[str] = None
    habits: Optional[str] = None
    values: Optional[str] = None


class WorldRuleContext(BaseModel):
    category: Optional[str] = None
    name: str
    description: str = ""


class ProjectCreate(BaseModel):
    name: str
    subtitle: str = ""
    genre: str = "xuanhuan"
    targetWordCount: int = 300000
    stylePreset: str = "epic"
    premise: str = ""
    targetChapters: int = 30
    protagonistName: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    subtitle: Optional[str] = None
    status: Optional[str] = None
    isStarred: Optional[bool] = None
    currentWordCount: Optional[int] = None
    outline: Optional[dict[str, Any]] = None
    characters: Optional[list[dict[str, Any]]] = None
    locations: Optional[list[dict[str, Any]]] = None
    worldRules: Optional[list[dict[str, Any]]] = None
    importAnalysis: Optional[dict[str, Any]] = None
    inspirationFragments: Optional[list[dict[str, Any]]] = None


class ChapterCreate(BaseModel):
    title: str
    volumeId: str
    order: int = 1


class ChapterSave(BaseModel):
    content: str
    title: Optional[str] = None
    volumeId: Optional[str] = None
    order: Optional[int] = None
    stylePlanId: Optional[str] = None
    stylePlanRevision: Optional[StrictInt] = Field(default=None, ge=0)


class GenerateOutlineRequest(BaseModel):
    projectId: str
    premise: str
    genre: str = "xuanhuan"
    targetWordCount: int = 300000
    targetChapters: int = 30
    structure: Literal["three_act", "kishotenketsu", "hero_journey", "free"] = "three_act"
    additionalNotes: Optional[str] = None


class GenerateCharacterRequest(BaseModel):
    projectId: str
    role: str = "protagonist"
    hints: str = ""
    genre: str = "xuanhuan"


class ContinueWritingRequest(BaseModel):
    projectId: str
    chapterId: str
    cursorPosition: int = 0
    contextBefore: str = ""
    direction: Optional[str] = None
    wordCount: int = 500
    maxAttempts: int = 3
    chapterTitle: Optional[str] = None
    premise: Optional[str] = None
    outlineSummary: Optional[str] = None
    chapterSummary: Optional[str] = None
    styleTone: Optional[str] = None
    stylePlanId: Optional[str] = None
    stylePlanRevision: Optional[StrictInt] = Field(default=None, ge=0)
    _style_contract: Optional[dict[str, Any]] = PrivateAttr(default=None)
    characters: list[CharacterContext] = Field(default_factory=list)
    worldRules: list[WorldRuleContext] = Field(default_factory=list)


class EnhanceRequest(BaseModel):
    projectId: str
    text: str
    mode: str = "description"
    intensity: int = 3


class DeaiRequest(BaseModel):
    projectId: str
    text: str
    # 可选：传了才能比对上一章，让「开头重复 / 场景关键词组」两条跨章规则生效。
    chapterId: str = ""


class ChatRequest(BaseModel):
    _style_contract: Optional[dict[str, Any]] = PrivateAttr(default=None)
    projectId: str
    messages: list[dict[str, Any]]
    conversationId: Optional[str] = None
    requestId: Optional[str] = None
    regenerateMessageId: Optional[str] = None
    action: Optional[Literal[
        "continue", "rewrite", "expand", "condense", "style", "character", "emotion",
        "pacing", "branch", "analyze_scene", "analyze_next", "continuity", "foreshadow",
    ]] = None
    chapterId: Optional[str] = None
    currentText: str = ""
    selectionText: str = ""
    contextProfile: Optional[
        Literal["plot_planning", "scene_writing", "continuation", "consistency_check"]
    ] = None
    collaborationMode: Literal["assist", "collaborate", "autopilot"] = "collaborate"


class OutlineCandidateCreate(BaseModel):
    candidateOutline: dict[str, Any]
    instruction: str = ""
    runId: str = ""


class AnalyzeCharacterProfileRequest(BaseModel):
    """按角色名分析角色档案：AI 通读章节正文与大纲后给出各字段的建议值。"""

    characterName: str = Field(min_length=1, max_length=100)


class ApiResponse(BaseModel):
    success: bool = True
    data: Optional[dict[str, Any] | list[Any]] = None
    error: Optional[str] = None
