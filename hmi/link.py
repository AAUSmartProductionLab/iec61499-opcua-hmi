"""OPC UA client: connect, resolve the address space, subscribe, call methods.

asyncua is asyncio only, Flask is not. The link therefore owns one event loop
in a background thread; the Flask threads hand work over with
``run_coroutine_threadsafe``. Node ids are resolved by browse path on every
(re)connect because the controller renumbers them on every restart.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from asyncua import Client, Node, ua
from asyncua.ua.uaerrors import UaError

from . import profiles as prof
from .profiles import ModuleProfile

LOGGER = logging.getLogger(__name__)

CALL_TIMEOUT = 10.0
# The controller only notifies changes, and a module that stands still changes
# nothing. After this long without a notification the link reads one value to
# prove the connection still answers; it does not reconnect because of silence.
SILENCE_LIMIT = 5.0
PROBE_TIMEOUT = 5.0
WATCHDOG_INTERVAL = 1.0


class LinkUnavailable(RuntimeError):
    """Raised when a call is requested while the link is not connected."""


@dataclass
class ValueEntry:
    value: Any = None
    status: str = "Good"
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "status": self.status, "ts": self.ts}


@dataclass
class CallResult:
    ok: bool = False
    accepted: bool = False
    error_id: int = 0
    transport_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "accepted": self.accepted,
            "errorId": self.error_id,
            "transportError": self.transport_error,
        }


@dataclass
class ConnectionState:
    connected: bool = False
    endpoint: str = ""
    detail: str = ""
    since: float = 0.0
    last_notification: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "endpoint": self.endpoint,
            "detail": self.detail,
            "since": self.since,
        }


class ModuleChannel:
    """One module on one endpoint: the resolved nodes and the monitored values."""

    def __init__(self, profile: ModuleProfile) -> None:
        self.profile = profile
        self.values: dict[str, ValueEntry] = {}
        self.missing: list[str] = []
        self.available = False

    def reset(self) -> None:
        self.values.clear()
        self.missing.clear()
        self.available = False

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {path: entry.to_dict() for path, entry in self.values.items()}

    def value(self, path: str, default: Any = None) -> Any:
        entry = self.values.get(path)
        return default if entry is None else entry.value


class _NotificationHandler:
    """Data change notifications, written straight into the channel values."""

    def __init__(self, keys: dict[Any, tuple[str, str]], store: Callable[..., None]) -> None:
        self._keys = keys
        self._store = store

    def datachange_notification(self, node: Node, val: Any, data: Any) -> None:
        key = self._keys.get(node.nodeid)
        if key is None:
            self._keys[node.nodeid] = key = (str(node.nodeid), str(node.nodeid))
        module_key, path = key
        status = "Good"
        try:
            status = data.monitored_item.Value.StatusCode.name
        except Exception:  # noqa: BLE001
            pass
        self._store(module_key, path, val, status)

    def status_change_notification(self, status: Any) -> None:
        LOGGER.warning("subscription status change: %s", status)


class OpcuaLink:
    """Supervises one OPC UA connection for a set of modules."""

    def __init__(
        self,
        endpoint: str,
        module_profiles: Iterable[ModuleProfile],
        sampling_ms: int = 200,
        timeout: float = CALL_TIMEOUT,
    ) -> None:
        self.endpoint = endpoint
        self.channels: dict[str, ModuleChannel] = {
            profile.key: ModuleChannel(profile) for profile in module_profiles
        }
        self.sampling_ms = sampling_ms
        self.timeout = timeout
        self.state = ConnectionState(endpoint=endpoint)
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._call_lock: asyncio.Lock | None = None
        self._nodes: dict[str, dict[str, Node]] = {}
        self._parents: dict[str, dict[str, Node]] = {}
        self._roots: dict[str, Node] = {}
        self._stopping = False
        self._listeners: list[Callable[[bool], None]] = []
        self._log: deque[str] = deque(maxlen=200)

    def add_state_listener(self, listener: Callable[[bool], None]) -> None:
        self._listeners.append(listener)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main, name=f"opcua-{self.endpoint}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping = True
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._request_stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def log_lines(self, limit: int = 50) -> list[str]:
        with self._lock:
            return list(self._log)[-limit:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.to_dict(),
                "modules": {
                    key: {
                        "available": channel.available,
                        "missing": list(channel.missing),
                        "values": channel.snapshot(),
                    }
                    for key, channel in self.channels.items()
                },
            }

    def call(self, module_key: str, path: str, args: list[Any]) -> CallResult:
        """Call a method of a module; calls are serialized per connection."""
        if not self.state.connected:
            return CallResult(ok=False, transport_error="not connected to the controller")
        loop = self._loop
        if loop is None or self._stopping:
            return CallResult(ok=False, transport_error="shutting down")
        future = asyncio.run_coroutine_threadsafe(self._call(module_key, path, args), loop)
        try:
            return future.result(timeout=self.timeout)
        except TimeoutError:
            future.cancel()
            return CallResult(ok=False, transport_error="no answer from the controller")
        except asyncio.CancelledError:
            return CallResult(ok=False, transport_error="cancelled")
        except (UaError, OSError, ConnectionError, RuntimeError) as exc:
            return CallResult(ok=False, transport_error=f"{type(exc).__name__}: {exc}")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervise())
        finally:
            loop.close()

    def _request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def _supervise(self) -> None:
        self._stop_event = asyncio.Event()
        self._call_lock = asyncio.Lock()
        backoff = 1.0
        while not self._stopping:
            client = Client(url=self.endpoint)
            try:
                await client.connect()
                await self._resolve(client)
                backoff = 1.0
                await self._set_connected()
                await self._watch(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._stopping:
                    break
                LOGGER.warning("connection to %s failed: %s", self.endpoint, exc)
                self._append_log(f"not connected: {type(exc).__name__}: {exc}")
                self._set_disconnected(f"{type(exc).__name__}: {exc}")
                await self._sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            finally:
                self._clear_channels()
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    async def _sleep(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _resolve(self, client: Client) -> None:
        """Resolve every node of every module by browse path."""
        with self._lock:
            channels = dict(self.channels)
        nodes: dict[str, dict[str, Node]] = {}
        parents: dict[str, dict[str, Node]] = {}
        roots: dict[str, Node] = {}
        for key, channel in channels.items():
            profile = channel.profile
            root = await self._resolve_root(client, profile)
            roots[key] = root
            resolved: dict[str, Node] = {}
            owners: dict[str, Node] = {}
            missing: list[str] = []
            for path in prof.monitored_paths(profile):
                try:
                    resolved[path] = await root.get_child(prof.browse_path(path))
                except (UaError, ValueError) as exc:
                    missing.append(f"{path} ({type(exc).__name__})")
            # A method is called on the object it belongs to: the controller's
            # server (open62541) refuses any other object with BadNodeClassInvalid.
            for path in prof.method_paths(profile):
                parent_path = path.rsplit("/", 1)[0]
                try:
                    if parent_path not in owners:
                        owners[parent_path] = await root.get_child(prof.browse_path(parent_path))
                    resolved[path] = await root.get_child(prof.browse_path(path))
                except (UaError, ValueError) as exc:
                    missing.append(f"{path} ({type(exc).__name__})")
            with self._lock:
                channel.reset()
                channel.missing = missing
                channel.available = not missing
            nodes[key] = resolved
            parents[key] = owners
            if missing:
                self._append_log(
                    f"{profile.title}: {len(missing)} node(s) missing: {', '.join(missing[:6])}"
                )
        with self._lock:
            self._nodes = nodes
            self._parents = parents
            self._roots = roots

    async def _resolve_root(self, client: Client, profile: ModuleProfile) -> Node:
        """Find /Objects/<Root>: by browse path in namespace 1, else by browsing."""
        try:
            root = await client.get_root_node().get_child(prof.root_browse_path(profile))
            await root.read_browse_name()
            return root
        except (UaError, ValueError):
            pass
        for child in await client.nodes.objects.get_children():
            try:
                name = await child.read_browse_name()
            except (UaError, ValueError):
                continue
            if name.Name == profile.root:
                return child
        raise UaError(f"object '/Objects/{profile.root}' not found")

    async def _watch(self, client: Client) -> None:
        with self._lock:
            channels = dict(self.channels)
            nodes = {key: dict(value) for key, value in self._nodes.items()}
        variables: list[tuple[str, str, Node]] = []
        for key, channel in channels.items():
            for path in prof.monitored_paths(channel.profile):
                node = nodes.get(key, {}).get(path)
                if node is not None:
                    variables.append((key, path, node))
        keys = {node.nodeid: (key, path) for key, path, node in variables}
        initial = await asyncio.gather(
            *(node.read_value() for _, _, node in variables), return_exceptions=True
        )
        for (key, path, _), value in zip(variables, initial, strict=False):
            if isinstance(value, BaseException):
                self._store(key, path, None, f"Bad: {type(value).__name__}")
            else:
                self._store(key, path, value)
        handler = _NotificationHandler(keys, self._store)
        subscription = await client.create_subscription(self.sampling_ms, handler)
        try:
            for _, _, node in variables:
                await subscription.subscribe_data_change(node, sampling_interval=self.sampling_ms)
            while not self._stopping:
                await self._sleep(WATCHDOG_INTERVAL)
                if self._stopping:
                    break
                await client.check_connection()
                with self._lock:
                    silence = time.monotonic() - self.state.last_notification
                if silence > SILENCE_LIMIT and variables:
                    # Quiet is normal for a module at rest: prove the server
                    # still answers with one read instead of reconnecting.
                    await asyncio.wait_for(variables[0][2].read_value(), timeout=PROBE_TIMEOUT)
                    with self._lock:
                        self.state.last_notification = time.monotonic()
        finally:
            try:
                await subscription.delete()
            except Exception:  # noqa: BLE001
                pass

    async def _call(self, module_key: str, path: str, args: list[Any]) -> CallResult:
        assert self._call_lock is not None
        with self._lock:
            node = self._nodes.get(module_key, {}).get(path)
            parent = self._parents.get(module_key, {}).get(path.rsplit("/", 1)[0])
        if node is None or parent is None:
            return CallResult(ok=False, transport_error=f"node '{path}' is not resolved")
        variants = [self._to_variant(arg) for arg in args]
        async with self._call_lock:
            try:
                answers = await parent.call_method(node.nodeid, *variants)
            except (UaError, OSError, ConnectionError) as exc:
                return CallResult(ok=False, transport_error=f"{type(exc).__name__}: {exc}")
        accepted, error_id = _unpack_answer(answers)
        return CallResult(ok=True, accepted=accepted, error_id=error_id)

    def _to_variant(self, arg: Any) -> ua.Variant:
        if isinstance(arg, ua.Variant):
            return arg
        if isinstance(arg, bool):
            return ua.Variant(arg, ua.VariantType.Boolean)
        if isinstance(arg, int):
            return ua.Variant(arg, ua.VariantType.UInt16)
        if isinstance(arg, float):
            return ua.Variant(arg, ua.VariantType.Double)
        return ua.Variant(str(arg), ua.VariantType.String)

    def _store(self, module_key: str, path: str, value: Any, status: str = "Good") -> None:
        channel = self.channels.get(module_key)
        if channel is None:
            return
        entry = ValueEntry(value=_plain(value), status=status, ts=time.time())
        with self._lock:
            channel.values[path] = entry
            self.state.last_notification = time.monotonic()

    def _append_log(self, message: str) -> None:
        with self._lock:
            self._log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def _clear_channels(self) -> None:
        with self._lock:
            self._nodes = {}
            self._parents = {}
            self._roots = {}
            self.state.connected = False
            self.state.last_notification = time.monotonic()
            for channel in self.channels.values():
                channel.reset()

    async def _set_connected(self) -> None:
        with self._lock:
            self.state.connected = True
            self.state.detail = ""
            self.state.since = time.time()
            self.state.last_notification = time.monotonic()
        self._append_log(f"connected to {self.endpoint}")
        self._notify(True)

    def _set_disconnected(self, detail: str) -> None:
        with self._lock:
            was_connected = self.state.connected
            self.state.connected = False
            self.state.detail = detail
            self.state.since = time.time()
            for channel in self.channels.values():
                channel.available = False
        if was_connected:
            self._append_log(f"disconnected: {detail}")
        self._notify(False)

    def _notify(self, connected: bool) -> None:
        for listener in list(self._listeners):
            try:
                listener(connected)
            except Exception:  # noqa: BLE001
                LOGGER.exception("state listener failed")


def _plain(value: Any) -> Any:
    if isinstance(value, ua.Variant):
        return _plain(value.Value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, ua.StatusCode):
        return value.name
    return value


def _unpack_answer(answers: Any) -> tuple[bool, int]:
    """Every method answers with [Accepted: Boolean, ErrorID: UInt16]."""
    if answers is None:
        return False, 0
    if not isinstance(answers, (list, tuple)):
        answers = [answers]
    accepted = bool(answers[0]) if answers else False
    error_id = int(answers[1]) if len(answers) > 1 else 0
    return accepted, error_id