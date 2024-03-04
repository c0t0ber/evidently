import asyncio
import contextlib
import logging
from typing import AsyncGenerator
from typing import Dict
from typing import List

import pandas as pd
import uvicorn
from litestar import Litestar
from litestar import Request
from litestar import get
from litestar import post
from litestar.concurrency import sync_to_thread
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.handlers import BaseRouteHandler
from litestar.params import Parameter
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from typing_extensions import Annotated

from evidently import ColumnMapping
from evidently.collector.config import CONFIG_PATH
from evidently.collector.config import CollectorConfig
from evidently.collector.config import CollectorServiceConfig
from evidently.collector.storage import CollectorStorage
from evidently.collector.storage import LogEvent
from evidently.telemetry import DO_NOT_TRACK_ENV
from evidently.telemetry import event_logger
from evidently.ui.config import NoSecurityConfig
from evidently.ui.security.no_security import NoSecurityService
from evidently.ui.security.service import SecurityService
from evidently.ui.security.token import TokenSecurity
from evidently.ui.security.token import TokenSecurityConfig

COLLECTOR_INTERFACE = "collector"

logger = logging.getLogger(__name__)


@post("/{id:str}")
async def create_collector(
    id: Annotated[str, Parameter(description="Collector ID")],
    data: CollectorConfig,
    service: CollectorServiceConfig,
    storage: CollectorStorage,
) -> CollectorConfig:
    data.id = id
    service.collectors[id] = data
    storage.init(id)
    service.save(CONFIG_PATH)
    return data


@get("/{id:str}")
async def get_collector(
    id: Annotated[str, Parameter(description="Collector ID")],
    service: CollectorServiceConfig,
) -> CollectorConfig:
    if id not in service.collectors:
        raise HTTPException(status_code=404, detail=f"Collector config with id '{id}' not found")
    return service.collectors[id]


@post("/{id:str}/reference")
async def reference(
    id: Annotated[str, Parameter(description="Collector ID")],
    request: Request,
    service: CollectorServiceConfig,
) -> Dict[str, str]:
    if id not in service.collectors:
        raise HTTPException(status_code=404, detail=f"Collector config with id '{id}' not found")
    collector = service.collectors[id]
    data = pd.DataFrame.from_dict(await request.json())
    path = collector.reference_path or f"{id}_reference.parquet"
    data.to_parquet(path)
    collector.reference_path = path
    service.save(CONFIG_PATH)
    return {}


@post("/{id:str}/data")
async def data(
    id: Annotated[str, Parameter(description="Collector ID")],
    request: Request,
    service: CollectorServiceConfig,
    storage: CollectorStorage,
) -> Dict[str, str]:
    if id not in service.collectors:
        raise HTTPException(status_code=404, detail=f"Collector config with id '{id}' not found")
    async with storage.lock(id):
        storage.append(id, await request.json())
    return {}


@get("/{id:str}/logs")
async def get_logs(
    id: Annotated[str, Parameter(description="Collector ID")],
    service: CollectorServiceConfig,
    storage: CollectorStorage,
) -> List[LogEvent]:
    if id not in service.collectors:
        raise HTTPException(status_code=404, detail=f"Collector config with id '{id}' not found")
    return storage.get_logs(id)


async def check_snapshots_factory(service: CollectorServiceConfig, storage: CollectorStorage) -> None:
    for _, collector in service.collectors.items():
        if not collector.trigger.is_ready(collector, storage):
            continue
        await create_snapshot(collector, storage)


async def create_snapshot(collector: CollectorConfig, storage: CollectorStorage) -> None:
    async with storage.lock(collector.id):
        current = await sync_to_thread(storage.get_and_flush, collector.id)  # FIXME: sync function
        if current is None:
            return
        current.index = current.index.astype(int)
        report_conf = collector.report_config
        report = report_conf.to_report_base()
        try:
            await sync_to_thread(
                report.run, reference_data=collector.reference, current_data=current, column_mapping=ColumnMapping()
            )  # FIXME: sync function
            report._inner_suite.raise_for_error()
        except Exception as e:
            logger.exception(f"Error running report: {e}")
            storage.log(
                collector.id, LogEvent(ok=False, error=f"Error running report: {e.__class__.__name__}: {e.args}")
            )
            return
        try:
            await sync_to_thread(
                collector.workspace.add_snapshot, collector.project_id, report.to_snapshot()
            )  # FIXME: sync function
        except Exception as e:
            logger.exception(f"Error saving snapshot: {e}")
            storage.log(
                collector.id, LogEvent(ok=False, error=f"Error saving snapshot: {e.__class__.__name__}: {e.args}")
            )
            return
        storage.log(collector.id, LogEvent(ok=True))


def run(host: str = "0.0.0.0", port: int = 8001, config_path: str = CONFIG_PATH, secret: str = None):
    service = CollectorServiceConfig.load_or_default(config_path)
    service.storage.init_all(service)

    if event_logger.is_enabled():
        print(f"Anonimous usage reporting is enabled. To disable it, set env variable {DO_NOT_TRACK_ENV} to any value")
    else:
        print("Anonimous usage reporting is disabled")
    event_logger.send_event(COLLECTOR_INTERFACE, "startup")

    security: SecurityService
    if secret is None:
        security = NoSecurityService(NoSecurityConfig())
    else:
        security = TokenSecurity(TokenSecurityConfig(token=secret))

    def auth_middleware_factory(app: ASGIApp) -> ASGIApp:
        async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
            request: Request = Request(scope)
            auth = security.authenticate(request)
            if auth is None:
                scope["auth"] = {
                    "authenticated": False,
                }
            else:
                scope["auth"] = {
                    "user_id": auth.id,
                    "org_id": auth.org_id,
                    "authenticated": True,
                }
            await app(scope, receive, send)

        return middleware

    def is_authenticated(connection: ASGIConnection, _: BaseRouteHandler) -> None:
        if not connection.scope["auth"]["authenticated"]:
            raise NotAuthorizedException()

    @contextlib.asynccontextmanager
    async def check_snapshots_factory_lifespan(app: Litestar) -> AsyncGenerator[None, None]:
        stop_event = asyncio.Event()

        async def check_service_snapshots_periodically():
            while not stop_event.is_set():
                try:
                    await check_snapshots_factory(service, service.storage)
                except Exception as e:
                    logger.exception(f"Check snapshots factory error: {e}")
                await asyncio.sleep(service.check_interval)

        task = asyncio.create_task(check_service_snapshots_periodically())
        try:
            yield
        finally:
            stop_event.set()
            await task

    app = Litestar(
        route_handlers=[
            create_collector,
            get_collector,
            reference,
            data,
            get_logs,
        ],
        dependencies={
            "security": Provide(lambda: security, use_cache=True, sync_to_thread=True),
            "service": Provide(lambda: service, use_cache=True, sync_to_thread=True),
            "storage": Provide(lambda: service.storage, use_cache=True, sync_to_thread=True),
        },
        middleware=[auth_middleware_factory],
        guards=[is_authenticated],
        lifespan=[check_snapshots_factory_lifespan],
    )
    uvicorn.run(app, host=host, port=port)


def main():
    run()


if __name__ == "__main__":
    main()
