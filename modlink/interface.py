"""What a client has to know of a module's address space, and how to find it.

A module is one object below ``Objects`` (its root, e.g. ``Filling``). Every
node is named by its path below the root (``Skills/Dispensing/State``); browse
paths, because the controller renumbers its node ids at every start.

The interface comes from a description of the module (the HMI's profiles, the
module's AAS) or, with ``discover``, from browsing the running server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from asyncua import Client, Node, ua

NAMESPACE = 1


@dataclass(frozen=True)
class Interface:
    """The variables to monitor and the methods to call of one module."""

    root: str
    variables: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    namespace: int = NAMESPACE
    endpoint: str = ""
    labels: dict[str, str] = field(default_factory=dict, compare=False)

    @classmethod
    def of(cls, root: str, variables: Iterable[str], methods: Iterable[str], **more) -> "Interface":
        return cls(root, tuple(dict.fromkeys(variables)), tuple(dict.fromkeys(methods)), **more)

    def browse_path(self, path: str) -> list[str]:
        """Browse names of a path below the root, namespaced."""
        return [f"{self.namespace}:{part}" for part in path.split("/") if part]

    def root_path(self) -> list[str]:
        return ["0:Objects", f"{self.namespace}:{self.root}"]

    def skills(self) -> list[str]:
        """The skills that can be started: those with a ``Skills/<name>/Start`` method."""
        return [m.split("/")[1] for m in self.methods if m.startswith("Skills/") and m.endswith("/Start")]


async def find_root(client: Client, root: str, namespace: int = NAMESPACE) -> Node:
    """``/Objects/<root>``: by browse path, else by browsing (any namespace)."""
    try:
        node = await client.nodes.objects.get_child([f"{namespace}:{root}"])
        await node.read_browse_name()
        return node
    except (ua.UaError, ValueError):
        pass
    for child in await client.nodes.objects.get_children():
        try:
            name = await child.read_browse_name()
        except (ua.UaError, ValueError):
            continue
        if name.Name == root:
            return child
    raise ua.UaError(f"object '/Objects/{root}' not found")


async def discover(endpoint: str, root: str, namespace: int = NAMESPACE, timeout: float = 10.0) -> Interface:
    """The interface of a running module, by browsing everything below its root."""
    async with Client(url=endpoint, timeout=timeout) as client:
        return await discover_with(client, root, namespace, endpoint)


async def discover_with(client: Client, root: str, namespace: int = NAMESPACE, endpoint: str = "") -> Interface:
    node = await find_root(client, root, namespace)
    variables: list[str] = []
    methods: list[str] = []

    async def walk(parent: Node, prefix: str) -> None:
        for child in await parent.get_children():
            name = (await child.read_browse_name()).Name
            path = f"{prefix}{name}"
            node_class = await child.read_node_class()
            if node_class == ua.NodeClass.Variable:
                variables.append(path)
            elif node_class == ua.NodeClass.Method:
                methods.append(path)
            elif node_class == ua.NodeClass.Object:
                await walk(child, f"{path}/")

    await walk(node, "")
    return Interface.of(root, variables, methods, namespace=namespace, endpoint=endpoint)
