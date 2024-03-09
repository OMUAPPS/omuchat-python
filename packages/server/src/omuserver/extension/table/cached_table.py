from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, AsyncIterator, Dict, List

from omu.extension.table.table_extension import TableProxyData, TableProxyEvent
from omu.extension.table import TableConfig
from omu.identifier import Identifier


from .adapters.tableadapter import TableAdapter
from .server_table import ServerTable, ServerTableListener
from .session_table_handler import SessionTableListener

if TYPE_CHECKING:
    from omuserver.server import Server
    from omuserver.session import Session


class CachedTable(ServerTable):
    def __init__(
        self,
        server: Server,
        identifier: Identifier,
    ):
        self._server = server
        self._identifier = identifier
        self._listeners: List[ServerTableListener] = []
        self._sessions: Dict[Session, SessionTableListener] = {}
        self._proxy_sessions: Dict[str, Session] = {}
        self._changed = False
        self._proxy_id = 0
        self._save_task: asyncio.Task | None = None
        self._adapter: TableAdapter | None = None
        self._config: TableConfig = {}

        self._cache: Dict[str, bytes] = {}
        self._cache_size: int | None = None

    @property
    def adapter(self) -> TableAdapter | None:
        return self._adapter

    @property
    def cache(self) -> Dict[str, bytes]:
        return self._cache

    def set_config(self, config: TableConfig) -> None:
        self._config = config
        self._cache_size = config.get("cache_size", None)

    def set_adapter(self, adapter: TableAdapter) -> None:
        self._adapter = adapter

    async def store(self) -> None:
        if self._adapter is None:
            raise Exception("Table not set")
        if not self._changed:
            return
        self._changed = False
        await self._adapter.store()

    def attach_session(self, session: Session) -> None:
        if session in self._sessions:
            return
        handler = SessionTableListener(self._identifier.key(), session)
        self._sessions[session] = handler
        self.add_listener(handler)
        session.listeners.disconnected += self.handle_disconnection

    def detach_session(self, session: Session) -> None:
        if session in self._proxy_sessions:
            del self._proxy_sessions[session.app.key()]
        if session in self._sessions:
            handler = self._sessions.pop(session)
            self.remove_listener(handler)

    async def handle_disconnection(self, session: Session) -> None:
        self.detach_session(session)

    def attach_proxy_session(self, session: Session) -> None:
        self._proxy_sessions[session.app.key()] = session

    async def get(self, key: str) -> bytes | None:
        if self._adapter is None:
            raise Exception("Table not set")
        if key in self._cache:
            return self._cache[key]
        data = await self._adapter.get(key)
        if data is None:
            return None
        await self.update_cache({key: data})
        return data

    async def get_all(self, keys: List[str]) -> Dict[str, bytes]:
        if self._adapter is None:
            raise Exception("Table not set")
        items = {}
        for key in tuple(keys):
            if key in self._cache:
                items[key] = self._cache[key]
                keys.remove(key)
        if len(keys) == 0:
            return items
        data = await self._adapter.get_all(keys)
        for key, value in data.items():
            items[key] = value
        await self.update_cache(items)
        return items

    async def add(self, items: Dict[str, bytes]) -> None:
        if self._adapter is None:
            raise Exception("Table not set")
        if len(self._proxy_sessions) > 0:
            await self.send_proxy_event(items)
            return
        await self._adapter.set_all(items)
        for listener in self._listeners:
            await listener.on_add(items)
        await self.update_cache(items)
        self.mark_changed()

    async def send_proxy_event(self, items: Dict[str, bytes]) -> None:
        session = tuple(self._proxy_sessions.values())[0]
        self._proxy_id += 1
        await session.send(
            TableProxyEvent,
            TableProxyData(
                items=items,
                type=self._identifier.key(),
                key=self._proxy_id,
            ),
        )

    async def proxy(self, session: Session, key: int, items: Dict[str, bytes]) -> int:
        adapter = self._adapter
        if adapter is None:
            raise Exception("Table not set")
        if session.app.key() not in self._proxy_sessions:
            raise ValueError("Session not in proxy sessions")
        session_key = session.app.key()
        index = tuple(self._proxy_sessions.keys()).index(session_key)
        if index == len(self._proxy_sessions) - 1:
            adapter = self._adapter
            if adapter is None:
                raise Exception("Table not set")
            await adapter.set_all(items)
            for listener in self._listeners:
                await listener.on_add(items)
            await self.update_cache(items)
            self.mark_changed()
            return 0
        session = tuple(self._proxy_sessions.values())[index + 1]
        await session.send(
            TableProxyEvent,
            TableProxyData(
                items=items,
                type=self._identifier.key(),
                key=self._proxy_id,
            ),
        )
        return self._proxy_id

    async def update(self, items: Dict[str, bytes]) -> None:
        if self._adapter is None:
            raise Exception("Table not set")
        await self._adapter.set_all(items)
        for listener in self._listeners:
            await listener.on_update(items)
        await self.update_cache(items)
        self.mark_changed()

    async def remove(self, items: list[str]) -> None:
        if self._adapter is None:
            raise Exception("Table not set")
        removed = await self._adapter.get_all(items)
        await self._adapter.remove_all(items)
        for key in items:
            if key in self._cache:
                del self._cache[key]
        for listener in self._listeners:
            await listener.on_remove(removed)
        self.mark_changed()

    async def clear(self) -> None:
        if self._adapter is None:
            raise Exception("Table not set")
        await self._adapter.clear()
        for listener in self._listeners:
            await listener.on_clear()
        self._cache.clear()
        self.mark_changed()

    async def fetch_items(
        self,
        before: int | None = None,
        after: str | None = None,
        cursor: str | None = None,
    ) -> Dict[str, bytes]:
        if self._adapter is None:
            raise Exception("Table not set")
        return await self._adapter.fetch_items(before, after, cursor)

    async def iterate(self) -> AsyncIterator[bytes]:
        cursor: str | None = None
        while True:
            items = await self.fetch_items(self._cache_size, cursor)
            if len(items) == 0:
                break
            for item in items.values():
                yield item
            *_, cursor = items.keys()

    async def size(self) -> int:
        return len(self._cache)

    def add_listener(self, listener: ServerTableListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: ServerTableListener) -> None:
        self._listeners.remove(listener)

    async def save_task(self) -> None:
        while self._changed:
            await self.store()
            await asyncio.sleep(30)

    def mark_changed(self) -> None:
        self._changed = True
        if self._save_task is None:
            self._save_task = asyncio.create_task(self.save_task())

    def set_cache_size(self, size: int) -> None:
        self._cache_size = size

    async def update_cache(self, items: Dict[str, bytes]) -> None:
        if self._cache_size is None or self._cache_size <= 0:
            return
        for key, item in items.items():
            self._cache[key] = item
            if len(self._cache) > self._cache_size:
                del self._cache[next(iter(self._cache))]
        for listener in self._listeners:
            await listener.on_cache_update(self._cache)
