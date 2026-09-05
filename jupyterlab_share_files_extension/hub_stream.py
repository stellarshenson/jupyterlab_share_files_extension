"""The hub change stream, relayed to the panel.

The hub tells a lab when any of its shares or requests changed (galaxahub
``GET /hub/api/fileshare/stream``, Server-Sent Events, no payload) so the
panel fetches its lists on a ring instead of on a timer. This module holds
ONE such connection per lab server process, opened when the first panel
subscribes and closed when the last one leaves, and fans every ring out to
the panels as one ``changed`` event each. A ring carries nothing: the panel
fetches the whole current state, so a missed ring is covered by the next
open and a duplicate ring is idempotent.

A hub without the route (an older galaxahub) answers 404. The relay then
tells its panels to ``poll`` and does not try the hub again until every
panel has left and a new one subscribes; a hub that cannot be reached is
retried every ``RETRY_SECONDS`` for as long as a panel is listening.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Callable
from urllib.parse import urlparse

from .hub import HubClient, HubUnavailable

CHANGED = "changed"
POLL = "poll"
CLOSE = object()

# seconds between reconnects to the hub, the hub's own `retry:` hint
RETRY_SECONDS = 5
CONNECT_TIMEOUT_SECONDS = 10
# the hub writes a keepalive comment every 25s; a read that waits longer
# than three of them is a dead connection nobody closed (the hub vanished
# without a FIN) and is reconnected
READ_TIMEOUT_SECONDS = 90
# seconds between keepalive comments on a panel stream - proxies and the
# browser keep an idle stream across it
KEEPALIVE_SECONDS = 25


def parse_events(buffer: bytearray, data: bytes) -> list[str]:
    """Consume ``data`` into ``buffer`` and return the names of the complete
    events it closed. Only ``event:`` lines carry meaning on this stream;
    comments (keepalives), ``retry:`` and empty ``data:`` lines are skipped."""
    buffer.extend(data)
    events: list[str] = []
    while b"\n\n" in buffer:
        block, _, rest = buffer.partition(b"\n\n")
        buffer[:] = rest
        for line in block.decode("utf-8", "replace").splitlines():
            if line.startswith("event:"):
                events.append(line[len("event:"):].strip())
    return events


async def hold(on_open: Callable[[], None], on_event: Callable[[str], None]) -> int:
    """Hold the hub stream open until the hub closes it or the holder is
    cancelled; return the HTTP status the hub answered. ``on_open`` runs
    once the hub answered 200, ``on_event`` once per named event.

    Spoken over a raw asyncio socket rather than tornado's client: a fetch
    cannot be cancelled there, so a stream the last panel left behind would
    hold one of the shared client's slots until the hub ended it, and the
    hub never does. Here the cancel closes the socket at once.
    """
    client = HubClient()
    url = urlparse(client.base + "/stream")
    secure = url.scheme == "https"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                url.hostname, url.port or (443 if secure else 80),
                ssl=ssl.create_default_context() if secure else None,
            ),
            CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise HubUnavailable(f"could not reach the hub: {exc or 'connect timeout'}") from None
    try:
        request = (
            f"GET {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\n"
            f"Authorization: token {client.token}\r\nAccept: text/event-stream\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        status = await _read(reader.readline())
        code = int(status.split()[1]) if status.startswith(b"HTTP/") else 0
        chunked = False
        while (line := await _read(reader.readline())) not in (b"\r\n", b"\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            if name.strip().lower() == "transfer-encoding" and "chunked" in value.lower():
                chunked = True
        if code != 200:
            return code
        on_open()
        buffer = bytearray()
        while True:
            if chunked:
                size = int((await _read(reader.readline())).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    break
                data = await _read(reader.readexactly(size + 2))
                data = data[:-2]
            else:
                data = await _read(reader.read(4096))
                if not data:
                    break
            for name in parse_events(buffer, data):
                on_event(name)
        return code
    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
        return 0
    finally:
        writer.close()


async def _read(awaitable):
    """One socket read, bounded so a hub that vanished without closing the
    connection is noticed after a few missed keepalives."""
    return await asyncio.wait_for(awaitable, READ_TIMEOUT_SECONDS)


class Relay:
    """The lab's one hub stream, fanned out to every open panel stream."""

    def __init__(self):
        self._queues: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._unsupported = False

    @property
    def connected(self) -> bool:
        return self._task is not None

    def subscribe(self) -> asyncio.Queue:
        """One panel stream opened: its queue, and the hub stream if this is
        the first. A hub known to lack the route answers ``poll`` at once."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._queues.add(queue)
        if self._unsupported:
            queue.put_nowait(POLL)
        elif self._task is None:
            self._task = asyncio.ensure_future(self._run())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """One panel stream closed. The last one takes the hub stream down
        and forgets the 404 verdict, so a re-subscribe tries the hub again."""
        self._queues.discard(queue)
        if self._queues:
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._unsupported = False

    def ring(self, event: str) -> None:
        for queue in self._queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a ring is already waiting - one fetch covers both

    async def _run(self) -> None:
        # ``_task`` is owned by subscribe/unsubscribe alone: a cancelled run
        # clearing it here would clear the task the next subscribe started
        while self._queues:
            try:
                code = await hold(lambda: self.ring(CHANGED), self._on_event)
            except HubUnavailable:
                code = 0
            if code == 404:
                self._unsupported = True
                self.ring(POLL)
                return
            await asyncio.sleep(RETRY_SECONDS)

    def _on_event(self, name: str) -> None:
        if name == CHANGED:
            self.ring(CHANGED)


RELAY = Relay()
