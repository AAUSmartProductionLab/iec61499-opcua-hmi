"""One supervised OPC UA connection to the modules of one endpoint, asyncio only.

``Link`` connects, resolves every node of every module by browse path,
subscribes to all variables and calls methods on their parent object. It
reconnects with a back-off when the server goes away and resolves again,
because the controller renumbers its nodes at every start.

Everything runs on the caller's event loop: start the link in a task
(``await link.start()``) or use it as ``async with Link(...) as link``.
Listeners are called on that loop for every value change and every change of
the connection; ``changes()`` gives the same as an async iterator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterable

from asyncua import Client, Node, ua
from asyncua.ua.uaerrors import UaError

from .interface import Interface, find_root

LOGGER = logging.getLogger(__name__)

CALL_TIMEOUT = 10.0
# The controller only notifies changes, and a module that stands still changes
# nothing. After this long without a notification the link reads one value to
# prove the connection still answers; it does not reconnect because of silence.
SILENCE_LIMIT = 5.0
PROBE_TIMEOUT = 5.0
WATCHDOG_INTERVAL = 1.0
BACKOFF_MAX = 15.0


@dataclass
class Value:
    value: Any = None
    status: str = "Good"
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "status": self.status, "ts": self.ts}


@dataclass
class Answer:
    """What a method call gave: ``ok`` if it reached the module, then Accepted and ErrorID."""

    ok: bool = False
    accepted: bool = False
    error_id: int = 0
    transport_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "accepted": self.accepted, "errorId": self.error_id,
                "transportError": self.transport_error}


@dataclass
class LinkState:
    connected: bool = False
    endpoint: str = ""
    detail: str = ""
    since: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"connected": self.connected, "endpoint": self.endpoint, "detail": self.detail,
                "since": self.since}


@dataclass
class Change:
    """A value change (``path`` set) or a change of the connection (``path`` empty)."""

    root: str
    path: str
    value: Any = None
    old: Any = None
    status: str = "Good"
    connected: bool = True


@dataclass
class ModuleValues:
    """One module on the link: its interface, the monitored values and what is missing."""

    interface: Interface
    values: dict[str, Value] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    available: bool = False

    def value(self, path: str, default: Any = None) -> Any:
        entry = self.values.get(path)
        return default if entry is None else entry.value

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {path: entry.to_dict() for path, entry in self.values.items()}

    def reset(self) -> None:
        self.values.clear()
        self.missing.clear()
        self.available = False


Listener = Callable[[Change], None]


class _Notifications:
    """asyncua's subscription handler: data changes go to the link."""

    def __init__(self, link: "Link", keys: dict[Any, tuple[str, str]]) -> None:
        self._link = link
        self._keys = keys

    def datachange_notification(self, node: Node, val: Any, data: Any) -> None:
        key = self._keys.get(node.nodeid)
        if key is None:
            return
        status = "Good"
        with contextlib.suppress(Exception):
            status = data.monitored_item.Value.StatusCode.name
        self._link._store(key[0], key[1], val, status)

    def status_change_notification(self, status: Any) -> None:
        LOGGER.warning("subscription status change: %s", status)


class Link:
    """Supervises one OPC UA connection for the modules served on one endpoint."""

    def __init__(self, endpoint: str, interfaces: Iterable[Interface], sampling_ms: int = 200,
                 call_timeout: float = CALL_TIMEOUT) -> None:
        self.endpoint = endpoint
        self.modules: dict[str, ModuleValues] = {i.root: ModuleValues(i) for i in interfaces}
        self.sampling_ms = sampling_ms
        self.call_timeout = call_timeout
        self.state = LinkState(endpoint=endpoint)
        self.log: deque[str] = deque(maxlen=200)
        self._listeners: list[Listener] = []
        self._nodes: dict[str, dict[str, Node]] = {}
        self._owners: dict[str, dict[str, Node]] = {}
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._up: asyncio.Event | None = None
        self._call_lock: asyncio.Lock | None = None
        self._last_notification = 0.0

    # --- life ------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._stop, self._up, self._call_lock = asyncio.Event(), asyncio.Event(), asyncio.Lock()
            self._task = asyncio.create_task(self._supervise(), name=f"link {self.endpoint}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        assert self._stop is not None
        self._stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def __aenter__(self) -> "Link":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    @property
    def connected(self) -> bool:
        return self.state.connected

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """Whether the link is (or gets) connected within ``timeout`` seconds."""
        if self._up is None:
            return False
        try:
            await asyncio.wait_for(self._up.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # --- values ----------------------------------------------------------

    def module(self, root: str) -> ModuleValues:
        try:
            return self.modules[root]
        except KeyError:
            raise KeyError(f"{self.endpoint} has no module '{root}'") from None

    def value(self, root: str, path: str, default: Any = None) -> Any:
        return self.module(root).value(path, default)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with every Change; returns a function that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    async def changes(self, root: str | None = None) -> AsyncIterator[Change]:
        """Every change from now on (of one module, if given), as an async iterator."""
        queue: asyncio.Queue[Change] = asyncio.Queue()

        def put(change: Change) -> None:
            if root is None or not change.path or change.root == root:
                queue.put_nowait(change)

        remove = self.add_listener(put)
        try:
            while True:
                yield await queue.get()
        finally:
            remove()

    async def wait_for(self, root: str, path: str, predicate: Callable[[Any], bool],
                       timeout: float | None = None) -> Any:
        """The value of ``path`` once ``predicate`` holds for it; TimeoutError otherwise."""
        current = self.value(root, path)
        if path in self.module(root).values and predicate(current):
            return current
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        def check(change: Change) -> None:
            if change.root == root and change.path == path and predicate(change.value) and not found.done():
                found.set_result(change.value)

        remove = self.add_listener(check)
        try:
            return await asyncio.wait_for(found, timeout)
        finally:
            remove()

    async def read(self, root: str, path: str) -> Any:
        """A fresh read of one resolved variable."""
        node = self._nodes.get(root, {}).get(path)
        if node is None or not self.connected:
            raise LookupError(f"'{path}' of {root} is not resolved")
        return _plain(await asyncio.wait_for(node.read_value(), self.call_timeout))

    # --- calls -----------------------------------------------------------

    async def call(self, root: str, path: str, *args: Any) -> Answer:
        """Call a method of a module; calls are serialised per connection.

        Every method of a module answers ``[Accepted: Boolean, ErrorID: UInt16]``.
        """
        if not self.connected:
            return Answer(transport_error="not connected to the controller")
        node = self._nodes.get(root, {}).get(path)
        owner = self._owners.get(root, {}).get(path.rsplit("/", 1)[0])
        if node is None or owner is None:
            return Answer(transport_error=f"node '{path}' is not resolved")
        variants = [to_variant(arg) for arg in args]
        assert self._call_lock is not None
        async with self._call_lock:
            try:
                # On the parent object: open62541 refuses any other object with
                # BadNodeClassInvalid.
                answers = await asyncio.wait_for(owner.call_method(node.nodeid, *variants), self.call_timeout)
            except asyncio.TimeoutError:
                return Answer(transport_error="no answer from the controller")
            except (UaError, OSError, ConnectionError) as exc:
                return Answer(transport_error=f"{type(exc).__name__}: {exc}")
        accepted, error_id = _unpack(answers)
        return Answer(ok=True, accepted=accepted, error_id=error_id)

    # --- supervision -----------------------------------------------------

    async def _supervise(self) -> None:
        assert self._stop is not None
        backoff = 1.0
        while not self._stop.is_set():
            client = Client(url=self.endpoint)
            try:
                await client.connect()
                await self._resolve(client)
                backoff = 1.0
                self._set_connected()
                await self._watch(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                LOGGER.warning("connection to %s failed: %s", self.endpoint, exc)
                self._set_disconnected(f"{type(exc).__name__}: {exc}")
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                self._clear()
                with contextlib.suppress(Exception):
                    await client.disconnect()
        self._set_disconnected("stopped")

    async def _sleep(self, seconds: float) -> None:
        assert self._stop is not None
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _resolve(self, client: Client) -> None:
        """Every node of every module, by browse path."""
        for root, module in self.modules.items():
            interface = module.interface
            base = await find_root(client, root, interface.namespace)
            nodes: dict[str, Node] = {}
            owners: dict[str, Node] = {}
            missing: list[str] = []
            for path in interface.variables:
                try:
                    nodes[path] = await base.get_child(interface.browse_path(path))
                except (UaError, ValueError) as exc:
                    missing.append(f"{path} ({type(exc).__name__})")
            for path in interface.methods:
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                try:
                    if parent not in owners:
                        owners[parent] = await base.get_child(interface.browse_path(parent)) if parent else base
                    nodes[path] = await base.get_child(interface.browse_path(path))
                except (UaError, ValueError) as exc:
                    missing.append(f"{path} ({type(exc).__name__})")
            module.reset()
            module.missing = missing
            module.available = not missing
            self._nodes[root] = nodes
            self._owners[root] = owners
            if missing:
                self._note(f"{root}: {len(missing)} node(s) missing: {', '.join(missing[:6])}")

    async def _watch(self, client: Client) -> None:
        assert self._stop is not None
        variables = [(root, path, self._nodes[root][path])
                     for root, module in self.modules.items()
                     for path in module.interface.variables if path in self._nodes.get(root, {})]
        initial = await asyncio.gather(*(node.read_value() for _, _, node in variables), return_exceptions=True)
        for (root, path, _), value in zip(variables, initial):
            if isinstance(value, BaseException):
                self._store(root, path, None, f"Bad: {type(value).__name__}")
            else:
                self._store(root, path, value)
        handler = _Notifications(self, {node.nodeid: (root, path) for root, path, node in variables})
        subscription = await client.create_subscription(self.sampling_ms, handler)
        try:
            if variables:
                await subscription.subscribe_data_change([node for _, _, node in variables],
                                                         sampling_interval=self.sampling_ms)
            while not self._stop.is_set():
                await self._sleep(WATCHDOG_INTERVAL)
                if self._stop.is_set():
                    break
                await client.check_connection()
                if variables and time.monotonic() - self._last_notification > SILENCE_LIMIT:
                    # Quiet is normal for a module at rest: one read proves the
                    # server still answers.
                    await asyncio.wait_for(variables[0][2].read_value(), timeout=PROBE_TIMEOUT)
                    self._last_notification = time.monotonic()
        finally:
            with contextlib.suppress(Exception):
                await subscription.delete()

    # --- state -----------------------------------------------------------

    def _store(self, root: str, path: str, value: Any, status: str = "Good") -> None:
        module = self.modules.get(root)
        if module is None:
            return
        old = module.values.get(path)
        plain = _plain(value)
        module.values[path] = Value(plain, status, time.time())
        self._last_notification = time.monotonic()
        self._emit(Change(root, path, plain, None if old is None else old.value, status))

    def _emit(self, change: Change) -> None:
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:  # noqa: BLE001
                LOGGER.exception("link listener failed")

    def _note(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def _clear(self) -> None:
        self._nodes, self._owners = {}, {}
        self.state.connected = False
        if self._up is not None:
            self._up.clear()
        self._last_notification = time.monotonic()
        for module in self.modules.values():
            module.reset()

    def _set_connected(self) -> None:
        self.state = LinkState(True, self.endpoint, "", time.time())
        self._last_notification = time.monotonic()
        assert self._up is not None
        self._up.set()
        self._note(f"connected to {self.endpoint}")
        self._emit(Change("", "", connected=True))

    def _set_disconnected(self, detail: str) -> None:
        was = self.state.connected
        self.state = LinkState(False, self.endpoint, detail, time.time() if was or not self.state.since
                               else self.state.since)
        for module in self.modules.values():
            module.available = False
        if was:
            self._note(f"disconnected: {detail}")
        elif detail != "stopped":
            self._note(f"not connected: {detail}")
        self._emit(Change("", "", connected=False))


def to_variant(arg: Any) -> ua.Variant:
    """The OPC UA type of an argument: the modules take String, Double and UInt16."""
    if isinstance(arg, ua.Variant):
        return arg
    if isinstance(arg, bool):
        return ua.Variant(arg, ua.VariantType.Boolean)
    if isinstance(arg, int):
        return ua.Variant(arg, ua.VariantType.UInt16)
    if isinstance(arg, float):
        return ua.Variant(arg, ua.VariantType.Double)
    return ua.Variant(str(arg), ua.VariantType.String)


def _plain(value: Any) -> Any:
    if isinstance(value, ua.Variant):
        return _plain(value.Value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, ua.StatusCode):
        return value.name
    return value


def _unpack(answers: Any) -> tuple[bool, int]:
    if answers is None:
        return False, 0
    if not isinstance(answers, (list, tuple)):
        answers = [answers]
    accepted = bool(answers[0]) if answers else False
    error_id = int(answers[1]) if len(answers) > 1 else 0
    return accepted, error_id
