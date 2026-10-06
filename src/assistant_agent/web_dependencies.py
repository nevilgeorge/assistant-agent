"""Typed FastAPI providers for lifespan-owned application services."""

from fastapi import Request

from assistant_agent.async_workers import AsyncWorker
from assistant_agent.chat import ConversationManager
from assistant_agent.config import Settings
from assistant_agent.gmail_service import GmailService
from assistant_agent.sandbox import Sandbox
from assistant_agent.web_store import WebStore


async def get_application_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_web_store(request: Request) -> WebStore:
    return request.app.state.web_store


async def get_google_worker(request: Request) -> AsyncWorker:
    return request.app.state.google_worker


async def get_conversation_manager(request: Request) -> ConversationManager:
    return request.app.state.conversation_manager


async def get_sandbox_service(request: Request) -> Sandbox:
    return request.app.state.sandbox_service


async def get_gmail_service(request: Request) -> GmailService:
    return request.app.state.gmail_service
