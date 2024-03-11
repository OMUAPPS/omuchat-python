from __future__ import annotations

from aiohttp import web
from loguru import logger
from omu.network.bytebuffer import ByteReader, ByteWriter
from omu.network.connection import PacketMapper
from omu.network.packet import Packet, PacketData

from omuserver.security import Permission
from omuserver.session.session import SessionConnection


class WebsocketsConnection(SessionConnection):
    def __init__(self, socket: web.WebSocketResponse) -> None:
        self.socket = socket

    @property
    def closed(self) -> bool:
        return self.socket.closed

    @property
    def permissions(self) -> Permission:
        return self.permissions

    async def receive(self, packet_mapper: PacketMapper) -> Packet:
        msg = await self.socket.receive()
        if msg.type in {
            web.WSMsgType.CLOSE,
            web.WSMsgType.ERROR,
            web.WSMsgType.CLOSED,
            web.WSMsgType.CLOSING,
        }:
            raise RuntimeError(f"Socket {msg.type.name.lower()}")

        if msg.data is None:
            raise RuntimeError("Received empty message")
        if msg.type == web.WSMsgType.TEXT:
            raise RuntimeError("Received text message")
        elif msg.type == web.WSMsgType.BINARY:
            with ByteReader(msg.data) as reader:
                event_type = reader.read_string()
                event_data = reader.read_byte_array()
            packet_data = PacketData(event_type, event_data)
            return packet_mapper.deserialize(packet_data)
        else:
            raise RuntimeError(f"Unknown message type {msg.type}")

    async def close(self) -> None:
        try:
            await self.socket.close()
        except Exception as e:
            logger.warning(f"Error closing socket: {e}")
            logger.error(e)

    async def send(self, packet: Packet, packet_mapper: PacketMapper) -> None:
        if self.closed:
            raise ValueError("Socket is closed")
        packet_data = packet_mapper.serialize(packet)
        writer = ByteWriter()
        writer.write_string(packet_data.type)
        writer.write_byte_array(packet_data.data)
        await self.socket.send_bytes(writer.finish())

    def __repr__(self) -> str:
        return f"WebsocketsConnection(socket={self.socket})"
