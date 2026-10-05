"""A module as a client uses it: occupy, command the module, run skills.

``Module`` wraps one module on a ``Link`` for one occupation session, so an
agent (or a test) can drive a module without knowing the address space:

    async with Link(endpoint, [interface]) as link:
        await link.wait_connected(10)
        filling = Module(link, "Filling", session="agent-1")
        await filling.occupy()
        await filling.bring_to_execute()
        run = await filling.run("Dispensing", 1.0)
        print(run.state, run.results)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .codes import MODULE_COMMANDS, SKILL_COMMANDS, SKILL_ENDS, ModuleState, SkillState, as_int
from .link import Answer, Change, Link

ERROR_WAIT = 1.0


class Refused(RuntimeError):
    """The module did not accept a call (or it did not reach the module)."""

    def __init__(self, what: str, answer: Answer) -> None:
        reason = answer.transport_error or f"ErrorID {answer.error_id}"
        super().__init__(f"{what} refused: {reason}")
        self.answer = answer


@dataclass
class Run:
    """How a skill run ended."""

    skill: str
    state: int
    error_id: int = 0
    results: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.state == SkillState.SUCCEEDED


class Module:
    def __init__(self, link: Link, root: str, session: str) -> None:
        self.link = link
        self.root = root
        self.session = session
        self.interface = link.module(root).interface

    # --- what it shows ---------------------------------------------------

    def value(self, path: str, default: Any = None) -> Any:
        return self.link.value(self.root, path, default)

    @property
    def state(self) -> int:
        return as_int(self.value("Module/State"))

    @property
    def occupied(self) -> bool:
        return bool(self.value("Occupation/Occupied"))

    def skill_state(self, skill: str) -> int:
        return as_int(self.value(f"Skills/{skill}/State"))

    def results(self, skill: str) -> dict[str, Any]:
        prefix = f"Skills/{skill}/Results/"
        return {path[len(prefix):]: self.value(path) for path in self.interface.variables if path.startswith(prefix)}

    # --- calls -----------------------------------------------------------

    async def call(self, path: str, *args: Any, check: bool = True) -> Answer:
        answer = await self.link.call(self.root, path, self.session, *args)
        if check and not answer.accepted:
            raise Refused(f"{self.root}/{path}", answer)
        return answer

    async def occupy(self, check: bool = True) -> Answer:
        return await self.call("Occupation/Occupy", check=check)

    async def release(self, check: bool = True) -> Answer:
        return await self.call("Occupation/Release", check=check)

    async def command(self, command: str, check: bool = True) -> Answer:
        if command not in MODULE_COMMANDS:
            raise ValueError(f"unknown module command '{command}'")
        return await self.call(f"Module/{command}", check=check)

    async def skill(self, skill: str, command: str, *params: float, check: bool = True) -> Answer:
        if command not in SKILL_COMMANDS:
            raise ValueError(f"unknown skill command '{command}'")
        args = [float(p) for p in params] if command == "Start" else []
        return await self.call(f"Skills/{skill}/{command}", *args, check=check)

    # --- waiting ---------------------------------------------------------

    async def wait_state(self, *states: int, timeout: float | None = 60.0) -> int:
        wanted = {int(s) for s in states}
        return as_int(await self.link.wait_for(self.root, "Module/State", lambda v: as_int(v) in wanted, timeout))

    async def bring_to_execute(self, timeout: float = 120.0) -> None:
        """Clear, reset and start the module, from wherever it is, to Execute."""
        async def settle() -> int:
            return await self.wait_state(ModuleState.STOPPED, ModuleState.IDLE, ModuleState.EXECUTE,
                                         ModuleState.ABORTED, timeout=timeout)

        for _ in range(4):
            state = await settle()
            if state == ModuleState.EXECUTE:
                return
            command = {ModuleState.ABORTED: "Clear", ModuleState.STOPPED: "Reset", ModuleState.IDLE: "Start"}[state]
            await self.command(command)
            await self.link.wait_for(self.root, "Module/State", lambda v, s=state: as_int(v) != s, timeout)
        if self.state != ModuleState.EXECUTE:
            raise RuntimeError(f"{self.root} did not reach Execute (state {self.state})")

    async def run(self, skill: str, *params: float, timeout: float | None = 120.0) -> Run:
        """Start a skill and wait until it has succeeded, failed or been aborted.

        The end is taken from the first terminal state notified after the start,
        so a Succeeded left over from the previous run does not count.
        """
        path = f"Skills/{skill}/State"
        ended: asyncio.Future = asyncio.get_running_loop().create_future()
        started = False

        def watch(change: Change) -> None:
            if not started or ended.done():
                return
            if not change.path and not change.connected:
                ended.set_exception(ConnectionError(f"lost {self.link.endpoint} while {skill} ran"))
            elif change.root == self.root and change.path == path and as_int(change.value) in SKILL_ENDS:
                ended.set_result(as_int(change.value))

        remove = self.link.add_listener(watch)
        try:
            started = True
            await self.skill(skill, "Start", *params)
            state = await asyncio.wait_for(ended, timeout)
        finally:
            remove()
        return await self._ended(skill, state)

    async def wait_skill_end(self, skill: str, timeout: float | None = 120.0) -> Run:
        """Wait until a skill that runs now has ended (at once if it is in an end state)."""
        state = as_int(await self.link.wait_for(self.root, f"Skills/{skill}/State",
                                                lambda v: as_int(v) in SKILL_ENDS, timeout))
        return await self._ended(skill, state)

    async def _ended(self, skill: str, state: int) -> Run:
        error_id = 0
        if state != SkillState.SUCCEEDED:
            # The ErrorID may be notified just after the state.
            try:
                error_id = as_int(await self.link.wait_for(self.root, f"Skills/{skill}/ErrorID",
                                                           lambda v: as_int(v, 0) > 0, ERROR_WAIT), 0)
            except asyncio.TimeoutError:
                error_id = as_int(self.value(f"Skills/{skill}/ErrorID"), 0)
        return Run(skill, state, error_id, self.results(skill))
