from __future__ import annotations

from collections.abc import Callable

from omu.errors import PermissionDenied
from omu.extension.permission import PermissionType
from omu.extension.registry import RegistryType
from omu.extension.registry.packets import RegistryPermissions, RegistryRegisterPacket
from omu.extension.registry.registry_extension import (
    REGISTRY_GET_ENDPOINT,
    REGISTRY_LISTEN_PACKET,
    REGISTRY_PERMISSION_ID,
    REGISTRY_REGISTER_PACKET,
    REGISTRY_UPDATE_PACKET,
    RegistryPacket,
)
from omu.identifier import Identifier

from omuserver.server import Server
from omuserver.session import Session

from .registry import Registry, ServerRegistry

REGISTRY_PERMISSION = PermissionType(
    REGISTRY_PERMISSION_ID,
    {
        "level": "low",
        "name": {
            "en": "Registry Permission",
            "ja": "レジストリ権限",
        },
        "note": {
            "en": "Permission to read and write to a registry",
            "ja": "レジストリに読み書きする権限",
        },
    },
)


class RegistryExtension:
    def __init__(self, server: Server) -> None:
        self._server = server
        self.registries: dict[Identifier, ServerRegistry] = {}
        self._startup_registries: list[ServerRegistry] = []
        server.permissions.register(REGISTRY_PERMISSION)
        server.packet_dispatcher.register(
            REGISTRY_REGISTER_PACKET,
            REGISTRY_LISTEN_PACKET,
            REGISTRY_UPDATE_PACKET,
        )
        server.packet_dispatcher.add_packet_handler(
            REGISTRY_REGISTER_PACKET, self.handle_register
        )
        server.packet_dispatcher.add_packet_handler(
            REGISTRY_LISTEN_PACKET, self.handle_listen
        )
        server.packet_dispatcher.add_packet_handler(
            REGISTRY_UPDATE_PACKET, self.handle_update
        )
        server.endpoints.bind_endpoint(REGISTRY_GET_ENDPOINT, self.handle_get)
        server.listeners.start += self._on_start

    async def _on_start(self) -> None:
        for registry in self._startup_registries:
            await registry.load()
        self._startup_registries.clear()

    async def handle_register(
        self, session: Session, packet: RegistryRegisterPacket
    ) -> None:
        registry = await self.get(packet.id)
        if not registry.id.is_subpart_of(session.app.id):
            msg = f"App {session.app.id=} not allowed to register {packet.id=}"
            raise PermissionDenied(msg)
        registry.permissions = packet.permissions

    async def handle_listen(self, session: Session, identifier: Identifier) -> None:
        registry = await self.get(identifier)
        self.check_permission(
            registry,
            session,
            lambda permissions: [permissions.all, permissions.read],
        )
        await registry.attach_session(session)

    async def handle_update(self, session: Session, packet: RegistryPacket) -> None:
        registry = await self.get(packet.id)
        self.check_permission(
            registry,
            session,
            lambda permissions: [permissions.all, permissions.write],
        )
        await registry.store(packet.value)

    async def handle_get(
        self, session: Session, identifier: Identifier
    ) -> RegistryPacket:
        registry = await self.get(identifier)
        self.check_permission(
            registry,
            session,
            lambda permissions: [permissions.all, permissions.read],
        )
        return RegistryPacket(identifier, registry.data)

    async def get(self, id: Identifier) -> ServerRegistry:
        registry = self.registries.get(id)
        if registry is None:
            registry = ServerRegistry(
                server=self._server,
                id=id,
            )
            self.registries[id] = registry
            await registry.load()
        return registry

    def check_permission(
        self,
        registry: ServerRegistry,
        session: Session,
        get_permissions: Callable[[RegistryPermissions], list[Identifier | None]],
    ) -> None:
        if registry.id.is_subpart_of(session.app.id):
            return
        require_permissions = get_permissions(registry.permissions)
        if not any(
            self._server.permissions.has_permission(session, permission)
            for permission in filter(None, require_permissions)
        ):
            msg = f"App {session.app.id=} not allowed to access {registry.id=}"
            raise PermissionDenied(msg)

    def register[T](
        self,
        registry_type: RegistryType[T],
    ) -> Registry[T]:
        registry = self.registries.get(registry_type.id)
        if registry is None:
            registry = ServerRegistry(
                server=self._server,
                id=registry_type.id,
                permissions=registry_type.permissions,
            )
            self.registries[registry_type.id] = registry
            self._startup_registries.append(registry)
        return Registry(
            registry,
            registry_type.default_value,
            registry_type.serializer,
        )

    async def store(self, identifier: Identifier, value: bytes) -> None:
        registry = await self.get(identifier)
        await registry.store(value)
