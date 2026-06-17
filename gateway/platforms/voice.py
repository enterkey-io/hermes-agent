"""Voice platform adapter for the local Vox gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

try:
    import websockets
except ImportError:  # pragma: no cover - exercised through check fn
    websockets = None

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


class VoiceAdapter(BasePlatformAdapter):
    """Bridge Vox WebSocket speech events into the Hermes gateway pipeline."""

    MAX_MESSAGE_LENGTH = 100_000

    def __init__(self, config):
        super().__init__(config, Platform.VOICE)
        self._ws = None
        self._ws_task: asyncio.Task | None = None
        extra = getattr(config, "extra", {}) or {}
        self._vox_url = extra.get("vox_url", "ws://localhost:8600/adapter/hermes")
        self._platform_name = str(extra.get("platform_name") or "hermes").strip() or "hermes"
        self._active_calls: Dict[str, str] = {}

    async def connect(self) -> bool:
        if not websockets:
            logger.error("websockets package not installed")
            return False
        try:
            self._ws = await websockets.connect(self._vox_url)
            logger.info("Connected to Vox at %s", self._vox_url)
            await self._ws.send(json.dumps({"type": "ready", "platform": self._platform_name}))
            self._ws_task = asyncio.create_task(self._listen())
            self._mark_connected()
            return True
        except Exception as exc:
            logger.error("Failed to connect to Vox: %s", exc)
            return False

    async def _listen(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                    await self._handle_vox_message(msg)
                except json.JSONDecodeError:
                    logger.error("Invalid JSON from Vox")
                except Exception as exc:
                    logger.error("Error handling Vox message: %s", exc, exc_info=True)
        except Exception:
            logger.warning("Vox connection closed, will attempt reconnect")
            self._mark_disconnected()
            await self._reconnect()

    async def _reconnect(self) -> None:
        if not websockets:
            return
        while True:
            await asyncio.sleep(5)
            try:
                self._ws = await websockets.connect(self._vox_url)
                await self._ws.send(json.dumps({"type": "ready", "platform": self._platform_name}))
                self._ws_task = asyncio.create_task(self._listen())
                self._mark_connected()
                logger.info("Reconnected to Vox")
                return
            except Exception as exc:
                logger.warning("Reconnect failed: %s, retrying in 5s", exc)

    async def _handle_vox_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")

        if msg_type == "call_start":
            call_id = str(msg["callId"])
            agent = str(msg.get("agent", "default"))
            self._active_calls[call_id] = agent
            logger.info("Voice call started: %s (agent: %s)", call_id, agent)
            return

        if msg_type == "text":
            call_id = str(msg.get("callId", ""))
            content = str(msg.get("content", ""))
            if not content.strip():
                return

            event = MessageEvent(
                text=content,
                message_type=MessageType.TEXT,
                source=SessionSource(
                    platform=Platform.VOICE,
                    user_id="elliott",
                    chat_id=f"voice:{call_id}",
                ),
                message_id=f"voice-{call_id}-{id(msg)}",
            )

            if self._message_handler:
                response = await self._message_handler(event)
                if response:
                    await self._send_to_vox({
                        "type": "text",
                        "content": response,
                        "callId": call_id,
                    })
            return

        if msg_type == "call_end":
            call_id = str(msg.get("callId", ""))
            self._active_calls.pop(call_id, None)
            logger.info("Voice call ended: %s", call_id)

    async def disconnect(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None
        self._mark_disconnected()
        logger.info("Disconnected from Vox")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        call_id = chat_id.replace("voice:", "")
        await self._send_to_vox({"type": "text", "content": content, "callId": call_id})
        return SendResult(success=True, message_id=f"voice-{call_id}-{id(content)}")

    async def edit_message(self, chat_id: str, message_id: str, content: str) -> bool:
        call_id = chat_id.replace("voice:", "")
        await self._send_to_vox({"type": "text", "content": content, "callId": call_id})
        return True

    async def send_typing(self, chat_id: str, is_typing: bool = True) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        call_id = chat_id.replace("voice:", "")
        agent = self._active_calls.get(call_id, "unknown")
        return {
            "name": f"Voice call {call_id}",
            "type": "dm",
            "chat_id": chat_id,
            "agent": agent,
        }

    async def _send_to_vox(self, msg: dict[str, Any]) -> None:
        if self._ws and not getattr(self._ws, "closed", False):
            await self._ws.send(json.dumps(msg))


def check_voice_requirements() -> bool:
    return websockets is not None
