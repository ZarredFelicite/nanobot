"""Node channel: routes outbound messages back to connected nodes over WebSocket."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.channels.base import BaseChannel

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.nodes.registry import NodeRegistry


class NodeChannel(BaseChannel):
    """Channel that delivers agent responses to remote nanobot nodes.

    The inbound path is handled by :class:`NodeGatewayHandler` which publishes
    ``InboundMessage`` objects directly to the bus.  This channel only handles
    the *outbound* direction — routing ``OutboundMessage`` payloads back to the
    correct node's WebSocket connection.
    """

    name = "node"

    def __init__(self, registry: NodeRegistry, bus: MessageBus):
        # NodeChannel doesn't have a traditional config object — pass None-safe stub.
        super().__init__(config=_NodeChannelConfig(), bus=bus)
        self.registry = registry

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        node_id = msg.chat_id
        if not node_id:
            logger.warning("NodeChannel.send() called without chat_id (node_id)")
            return
        msg_id = msg.metadata.get("msg_id", "")
        await self.registry.send_response(node_id, msg_id, msg.content)

    def is_allowed(self, sender_id: str) -> bool:
        # Auth is validated at the WS layer — always allow here.
        return True


class _NodeChannelConfig:
    """Minimal config stub so BaseChannel.__init__ doesn't break."""

    allow_from: list[str] = ["*"]
