"""Asyncio MLLP receiver. MLLP has no authentication or encryption: bind to localhost or a private network and put TLS
(for example stunnel) or a VPN in front of anything that carries real data."""
from __future__ import annotations

import asyncio
import json
import logging

from . import hl7v2

log = logging.getLogger("hl7bridge")


class MLLPServer:
    def __init__(self, pipeline, host: str = "127.0.0.1", port: int = 2575, max_connections: int = 20, idle_timeout: float = 60.0):
        self.pipeline, self.host, self.port = pipeline, host, port
        self.idle_timeout = idle_timeout
        self._slots = asyncio.Semaphore(max_connections)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._slots.locked():
            log.warning(json.dumps({"event": "connection_refused", "reason": "too many connections"}))
            writer.close()
            return
        async with self._slots:
            unframer = hl7v2.Unframer()
            loop = asyncio.get_running_loop()
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(reader.read(65536), self.idle_timeout)
                    except asyncio.TimeoutError:
                        break
                    if not data:
                        break
                    try:
                        messages = unframer.feed(data)
                    except hl7v2.HL7Error as exc:
                        writer.write(hl7v2.frame(hl7v2.build_ack(None, "AR", str(exc))))
                        await writer.drain()
                        break
                    for raw in messages:
                        # the pipeline does blocking network I/O, so keep it off the event loop; per-connection order is preserved
                        _, ack = await loop.run_in_executor(None, self.pipeline.process, raw)
                        writer.write(hl7v2.frame(ack))
                        await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
