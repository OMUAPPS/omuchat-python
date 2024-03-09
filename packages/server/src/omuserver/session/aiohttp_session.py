from __future__ import annotations

import asyncio
from typing import Dict

from aiohttp import web
from loguru import logger
from omu.network.packet import PACKET_TYPES, PacketData, PacketType
from omu.app import App
from omu.network.bytebuffer import ByteReader, ByteWriter

from omuserver.security import Permission
from omuserver.server import Server
from omuserver.session import Session
from omuserver.session.session import SessionListeners


class AiohttpSession(Session):
    def __init__(
        self, socket: web.WebSocketResponse, app: App, permissions: Permission
    ) -> None:
        self.socket = socket
        self._app = app
        self._permissions = permissions
        self._listeners = SessionListeners()
        self._event_times: Dict[str, float] = {}

    @property
    def app(self) -> App:
        return self._app

    @property
    def closed(self) -> bool:
        return self.socket.closed

    @property
    def permissions(self) -> Permission:
        return self.permissions

    @classmethod
    async def _receive(cls, socket: web.WebSocketResponse) -> PacketData | None:
        msg = await socket.receive()
        if msg.type in {
            web.WSMsgType.CLOSE,
            web.WSMsgType.ERROR,
            web.WSMsgType.CLOSED,
            web.WSMsgType.CLOSING,
        }:
            if msg.type == web.WSMsgType.CLOSE:
                return None
            raise RuntimeError(f"Socket {msg.type.name.lower()}")

        if msg.data is None:
            raise RuntimeError("Received empty message")
        if msg.type == web.WSMsgType.TEXT:
            raise RuntimeError("Received text message")
        elif msg.type == web.WSMsgType.BINARY:
            with ByteReader(msg.data) as reader:
                event_type = reader.read_string()
                event_data = reader.read_byte_array()
            return PacketData(event_type, event_data)
        else:
            raise RuntimeError(f"Unknown message type {msg.type}")

    @classmethod
    async def create(
        cls, server: Server, socket: web.WebSocketResponse
    ) -> AiohttpSession:
        data = await cls._receive(socket)
        if data is None:
            raise RuntimeError("Socket closed before connect")
        if data.type != PACKET_TYPES.Connect.type:
            raise RuntimeError(
                f"Expected {PACKET_TYPES.Connect.type} but got {data.type}"
            )
        event = PACKET_TYPES.Connect.serializer.deserialize(data.data)
        permissions, token = await server.security.authenticate_app(
            event.app, event.token
        )
        session = cls(socket, app=event.app, permissions=permissions)
        await session.send(PACKET_TYPES.Token, token)
        return session

    async def listen(self) -> None:
        try:
            while True:
                event = await self._receive(self.socket)
                if event is None:
                    break
                asyncio.create_task(self._listeners.packet.emit(self, event))
        finally:
            await self.disconnect()

    async def disconnect(self) -> None:
        try:
            await self.socket.close()
        except Exception as e:
            logger.warning(f"Error closing socket: {e}")
            logger.error(e)
        await self._listeners.disconnected.emit(self)

    async def send[T](self, type: PacketType[T], data: T) -> None:
        if self.closed:
            raise ValueError("Socket is closed")
        writer = ByteWriter()
        writer.write_string(type.type)
        writer.write_byte_array(type.serializer.serialize(data))
        await self.socket.send_bytes(writer.finish())

    @property
    def listeners(self) -> SessionListeners:
        return self._listeners

    def __repr__(self) -> str:
        return f"AiohttpSession({self._app})"

    def hash(self) -> int:
        return hash(self._app)
