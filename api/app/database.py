from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class ControlBase(DeclarativeBase):
    pass


class DataBase(DeclarativeBase):
    pass


class DatabaseRouterError(RuntimeError):
    """Error de configuración al resolver la base de datos de un storage_key."""


class DatabaseRouter:
    def __init__(self, control_url: str, data_urls: dict[str, str]) -> None:
        self._control_engine: AsyncEngine | None = None
        self._control_sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._data_engines: dict[str, AsyncEngine] = {}
        self._data_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}
        self.configure(control_url=control_url, data_urls=data_urls)

    _ENGINE_KWARGS: dict = dict(
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_timeout=30,
    )

    def configure(self, *, control_url: str, data_urls: dict[str, str]) -> None:
        self.control_url = control_url
        self.data_urls = dict(data_urls)
        self.data_urls.setdefault("default", control_url)
        self._control_engine = create_async_engine(control_url, **self._ENGINE_KWARGS)
        self._control_sessionmaker = async_sessionmaker(
            self._control_engine,
            expire_on_commit=False,
        )
        self._data_engines = {}
        self._data_sessionmakers = {}

    def get_control_engine(self) -> AsyncEngine:
        assert self._control_engine is not None
        return self._control_engine

    def get_control_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        assert self._control_sessionmaker is not None
        return self._control_sessionmaker

    def get_data_engine(self, storage_key: str) -> AsyncEngine:
        if storage_key not in self.data_urls:
            available = ", ".join(sorted(self.data_urls))
            raise DatabaseRouterError(
                f"storage_key '{storage_key}' no está configurado. Disponibles: {available}"
            )

        if storage_key not in self._data_engines:
            self._data_engines[storage_key] = create_async_engine(
                self.data_urls[storage_key],
                **self._ENGINE_KWARGS,
            )
        return self._data_engines[storage_key]

    def get_data_sessionmaker(self, storage_key: str) -> async_sessionmaker[AsyncSession]:
        if storage_key not in self._data_sessionmakers:
            self._data_sessionmakers[storage_key] = async_sessionmaker(
                self.get_data_engine(storage_key),
                expire_on_commit=False,
            )
        return self._data_sessionmakers[storage_key]

    async def dispose(self) -> None:
        engines = [self.get_control_engine(), *self._data_engines.values()]
        seen: set[int] = set()
        for engine in engines:
            if id(engine) in seen:
                continue
            seen.add(id(engine))
            await engine.dispose()


router = DatabaseRouter(
    control_url=settings.database_url,
    data_urls=settings.resolved_data_database_urls(),
)


def get_database_router() -> DatabaseRouter:
    return router


async def reconfigure_database_router(
    *,
    control_url: str | None = None,
    data_urls: dict[str, str] | None = None,
) -> None:
    await router.dispose()
    router.configure(
        control_url=control_url or settings.database_url,
        data_urls=data_urls or settings.resolved_data_database_urls(),
    )


def get_control_engine() -> AsyncEngine:
    return router.get_control_engine()


def get_default_data_engine() -> AsyncEngine:
    return router.get_data_engine("default")


async def get_control_session() -> AsyncGenerator[AsyncSession, None]:
    async with router.get_control_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def control_session_context() -> AsyncIterator[AsyncSession]:
    async with router.get_control_sessionmaker()() as session:
        yield session


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Alias histórico: devuelve la sesión de control."""
    async with router.get_control_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def data_session_context(storage_key: str) -> AsyncIterator[AsyncSession]:
    async with router.get_data_sessionmaker(storage_key)() as session:
        yield session


async def dispose_engines() -> None:
    await router.dispose()


# ── Base Maestra ──────────────────────────────────────────────────────────────

_maestra_engine: AsyncEngine | None = None
_maestra_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_maestra_engine() -> AsyncEngine:
    global _maestra_engine
    if _maestra_engine is None:
        if not settings.base_maestra_url:
            raise RuntimeError("BASE_MAESTRA_URL no está configurado en el entorno")
        _maestra_engine = create_async_engine(
            settings.base_maestra_url,
            **DatabaseRouter._ENGINE_KWARGS,
        )
    return _maestra_engine


async def get_maestra_session() -> AsyncGenerator[AsyncSession, None]:
    if _maestra_sessionmaker is None:
        sm = async_sessionmaker(get_maestra_engine(), expire_on_commit=False)
    else:
        sm = _maestra_sessionmaker
    async with sm() as session:
        yield session
