"""Conversation lifecycle only; no provider calls."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from app.core.auth import require_project_owner
from app.dao import chat_conversations as chats
from app.models.schemas import ApiResponse

router = APIRouter(prefix='/projects/{project_id}/chat-conversations', tags=['chat-conversations'])


class ConversationCreate(BaseModel):
    title: str = Field(default='新对话', max_length=100)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    archived: bool | None = None


@router.get('')
async def list_conversations(project_id: str, request: Request, archived: bool = False):
    require_project_owner(request, project_id)
    return ApiResponse(data={'records': chats.list_conversations(project_id, archived)})


@router.post('')
async def create_conversation(project_id: str, request: Request, body: ConversationCreate):
    require_project_owner(request, project_id)
    return ApiResponse(data=chats.create(project_id, body.title))


@router.get('/{conversation_id}')
async def get_conversation(project_id: str, conversation_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=chats.get(project_id, conversation_id))


@router.patch('/{conversation_id}')
async def update_conversation(project_id: str, conversation_id: str, body: ConversationUpdate, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=chats.update(project_id, conversation_id, **body.model_dump()))


@router.delete('/{conversation_id}')
async def delete_conversation(project_id: str, conversation_id: str, request: Request):
    require_project_owner(request, project_id)
    chats.delete(project_id, conversation_id)
    return ApiResponse(data=None)
