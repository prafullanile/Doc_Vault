"""Worker entry point: ``python -m app.worker``.

Uses the same settings as the API. In deployment DATABASE_URL points at the worker role.
"""

import asyncio
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.session import create_engine, create_sessionmaker
from app.documents.storage import create_storage
from app.worker.worker import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    engine = create_engine(settings)
    worker = Worker(settings, create_sessionmaker(engine), await create_storage(settings))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:  # Windows: no add_signal_handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(worker.request_stop))
    try:
        await worker.run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
