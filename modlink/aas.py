"""A resource's AAS as a client reads it: where the module is, what it offers, how to reach it.

A module's AAS (iec61499-mgmt-py: ``modreg``) splits the description over submodels that refer to
each other:

- **Capability Description** (IDTA 02020): the capabilities it offers, with their values and
  ranges, each realized by a skill (CapabilityRealizedBy, a reference into the Skills submodel);
- **Skills** (ARSO): the skills that exist and how they are parameterized (unit, range, default),
  each with references to the actions of its commands (Methods) and to the properties publishing
  its state, ErrorID and results; the module's own commands, state and occupation (Module);
- **Asset Interfaces Description** (AID): the OPC UA endpoint and the browse path of every action
  and property.

``describe`` follows those references, never names, to a ``Resource``: the module's ``Interface``
for a ``Link`` and, per skill and capability, the browse paths to call and to watch. ``load`` reads
the resources from an AAS environment file or an AAS repository (HTTP API, part 2).

Plain JSON in, plain dataclasses out: no AAS library is needed.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .interface import Interface

BROWSE_PATH = re.compile(r"^/?0:Objects/(\d+):([^/]+)/(.+)$")
CAPABILITY_DESCRIPTION = "https://admin-shell.io/idta/SubmodelTemplate/CapabilityDescription/1/0"
CAPABILITY = "https://admin-shell.io/idta/CapabilityDescription"
OCCUPATION = ("Occupy", "Release")


class AasError(ValueError):
    """The AAS does not describe a module that can be reached."""


# --- reading an AAS environment -----------------------------------------------------------------

def children(element: dict | None) -> list[dict]:
    """The elements below a submodel, a collection or list (``value``) or an entity (``statements``)."""
    if not element:
        return []
    member = next((m for m in ("submodelElements", "statements") if m in element), "value")
    held = element.get(member)
    return [c for c in held if isinstance(c, dict) and "modelType" in c] if isinstance(held, list) else []


def child(element: dict | None, id_short: str) -> dict | None:
    return next((c for c in children(element) if c.get("idShort") == id_short), None)


def at(element: dict | None, *path: str) -> dict | None:
    for step in path:
        if element is None:
            return None
        element = child(element, step)
    return element


def value(element: dict | None, default: Any = None) -> Any:
    if element is None:
        return default
    found = element.get("value", default)
    return default if found is None else found


def semantic(element: dict | None) -> str:
    keys = ((element or {}).get("semanticId") or {}).get("keys") or []
    return keys[0].get("value", "") if keys else ""


def meanings(element: dict) -> list[str]:
    return [ref["keys"][0]["value"] for ref in element.get("supplementalSemanticIds") or [] if ref.get("keys")]


def qualifier(element: dict | None, name: str) -> str | None:
    return next((q.get("value") for q in (element or {}).get("qualifiers") or [] if q.get("type") == name), None)


def number(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class Environment:
    """One shell and the submodels it refers to."""

    def __init__(self, shell: dict, submodels: Iterable[dict]) -> None:
        self.shell = shell
        self.submodels = {s["id"]: s for s in submodels}
        self.by_name = {s.get("idShort"): s for s in self.submodels.values()}

    def submodel(self, id_short: str, semantic_id: str = "") -> dict | None:
        found = self.by_name.get(id_short)
        if found is None and semantic_id:
            found = next((s for s in self.submodels.values() if semantic(s) == semantic_id), None)
        return found

    def resolve(self, reference: dict | None) -> dict | None:
        """The element a model reference points at, in this shell's submodels."""
        keys = (reference or {}).get("keys") or []
        if not keys or keys[0].get("type") != "Submodel":
            return None
        node = self.submodels.get(keys[0]["value"])
        for key in keys[1:]:
            node = child(node, key["value"]) if node is not None else None
        return node

    @property
    def is_module(self) -> bool:
        return self.submodel("Skills") is not None and self.interface() is not None

    def interface(self) -> dict | None:
        return at(self.submodel("AssetInterfacesDescription"), "interface_opcua")


def environments(env: dict) -> list[Environment]:
    """The shells of an AAS environment, each with the submodels it refers to."""
    submodels = env.get("submodels", [])
    found = []
    for shell in env.get("assetAdministrationShells", []):
        ids = {first for ref in shell.get("submodels", []) if (first := (ref.get("keys") or [{}])[0].get("value"))}
        found.append(Environment(shell, [s for s in submodels if s.get("id") in ids]))
    return found


def b64(identifier: str) -> str:
    """An identifier as the AAS HTTP API takes it in a path (base64url without padding)."""
    return base64.urlsafe_b64encode(identifier.encode()).decode().rstrip("=")


def read_file(path: str | Path) -> dict:
    path = Path(path)
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return json.loads(raw)


class Repository:
    """An AAS repository (part 2 HTTP API): shells and submodels by base64url id."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, query: dict | None = None) -> Any:
        url = f"{self.url}/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                    timeout=self.timeout) as response:
            return json.loads(response.read())

    def shells(self) -> list[dict]:
        found, cursor = [], None
        while True:
            page = self.get("shells", {"limit": 100, **({"cursor": cursor} if cursor else {})})
            found += page.get("result", page if isinstance(page, list) else [])
            cursor = (page.get("paging_metadata") or {}).get("cursor") if isinstance(page, dict) else None
            if not cursor:
                return found

    def environment(self) -> dict:
        shells = self.shells()
        ids = dict.fromkeys(ref["keys"][0]["value"] for shell in shells for ref in shell.get("submodels", [])
                            if ref.get("keys"))
        submodels = []
        for identifier in ids:
            try:
                submodels.append(self.get(f"submodels/{b64(identifier)}"))
            except OSError:
                continue
        return {"assetAdministrationShells": shells, "submodels": submodels}


def read(source: str) -> dict:
    """An AAS environment from a file (``.json``, ``.json.gz``) or an AAS repository (``http...``)."""
    return Repository(source).environment() if source.startswith(("http://", "https://")) else read_file(source)


# --- what a client needs ------------------------------------------------------------------------

@dataclass(frozen=True)
class Parameter:
    name: str
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None


@dataclass(frozen=True)
class SkillLink:
    """One skill: the browse paths (below the module's root) of its commands and properties."""

    name: str
    kind: str
    commands: dict[str, str]                 # Start, Stop, Abort, Reset -> method
    state: str
    error: str
    results: dict[str, str] = field(default_factory=dict)
    parameters: tuple[Parameter, ...] = ()   # in the call order of Start, after the session
    description: str = ""

    def arguments(self, values: dict[str, float] | None = None) -> list[float]:
        """Start's arguments after the session: the given values, else the defaults."""
        values = values or {}
        unknown = set(values) - {p.name for p in self.parameters}
        if unknown:
            raise KeyError(f"{self.name} has no parameter {sorted(unknown)}")
        args = []
        for p in self.parameters:
            given = values.get(p.name, p.default)
            if given is None:
                raise ValueError(f"{self.name}: {p.name} has no default, give a value")
            args.append(float(given))
        return args


@dataclass(frozen=True)
class CapabilityValue:
    """One property of an offered capability: a value or a range."""

    name: str
    meaning: str
    value: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""

    def admits(self, required: Any) -> bool:
        """Whether a required value is within what is offered."""
        if self.value is not None:
            return str(required) == self.value
        wanted = number(required)
        return None not in (wanted, self.minimum, self.maximum) and self.minimum <= wanted <= self.maximum


@dataclass(frozen=True)
class CapabilityLink:
    """An offered capability and the skill that realizes it."""

    name: str
    meanings: tuple[str, ...]
    skill: str
    properties: dict[str, CapabilityValue] = field(default_factory=dict)

    def means(self, meaning: str) -> bool:
        return meaning == self.name or meaning in self.meanings


@dataclass(frozen=True)
class Resource:
    """A module as its AAS describes it, reachable over OPC UA."""

    aas_id: str
    id_short: str
    endpoint: str
    interface: Interface
    skills: dict[str, SkillLink]
    capabilities: dict[str, CapabilityLink]
    module_commands: dict[str, str]          # Reset, Start, Stop, Abort, Clear -> method
    module_state: str
    occupation: dict[str, str]               # Occupy, Release -> method; Occupied -> variable

    @property
    def root(self) -> str:
        return self.interface.root

    def capability(self, meaning: str) -> CapabilityLink:
        """An offered capability by its name or meaning (IRI)."""
        found = [c for c in self.capabilities.values() if c.means(meaning)]
        if not found:
            raise KeyError(f"{self.id_short} offers no capability {meaning!r}")
        return found[0]

    def realizing(self, meaning: str) -> SkillLink:
        """The skill that realizes a capability."""
        return self.skills[self.capability(meaning).skill]


def describe(env: Environment) -> Resource:
    """What a client needs of the module an AAS describes, by following its references."""
    name = env.shell.get("idShort", "?")
    interface = env.interface()
    skills_sm = env.submodel("Skills")
    if interface is None or skills_sm is None:
        raise AasError(f"{name}: no Skills submodel or no OPC UA interface")
    roots: set[tuple[int, str]] = set()

    def browse(reference: dict | None, what: str) -> str:
        """The browse path below the root of the action or property a reference points at."""
        return below_root(env.resolve(reference), what)

    def below_root(target: dict | None, what: str) -> str:
        href = value(at(target, "forms", "href"), "")
        match = BROWSE_PATH.match(href)
        if not match:
            raise AasError(f"{name}: {what} does not lead to an interface element with a browse path")
        roots.add((int(match.group(1)), match.group(2)))
        parts = match.group(3).split("/")
        return "/".join(part.split(":", 1)[1] for part in parts)

    def links(container: dict | None, what: str) -> dict[str, str]:
        return {link["idShort"]: browse(value(link), f"{what} {link['idShort']}") for link in children(container)}

    skills: dict[str, SkillLink] = {}
    occupation: dict[str, str] = {}
    for skill in children(at(skills_sm, "Skills")):
        skill_name = skill["idShort"]
        if skill_name in OCCUPATION:
            occupation[skill_name] = browse(value(at(skill, "InterfaceReference")), f"{skill_name}'s action")
            continue
        commands = links(at(skill, "Methods"), f"{skill_name}'s command")
        if not commands:
            commands = {"Start": browse(value(at(skill, "InterfaceReference")), f"{skill_name}'s action")}
        start = env.resolve(value(at(skill, "Methods", "Start")) or value(at(skill, "InterfaceReference")))
        order = [p["idShort"] for p in children(at(start, "input", "properties")) if p["idShort"] != "Session"]
        declared = {p["idShort"]: p for p in children(at(skill, "Parameters"))}
        parameters = tuple(Parameter(p, qualifier(declared.get(p), "Unit") or "",
                                     number(qualifier(declared.get(p), "Minimum")),
                                     number(qualifier(declared.get(p), "Maximum")),
                                     number(qualifier(declared.get(p), "Default") or value(declared.get(p))))
                           for p in order)
        skills[skill_name] = SkillLink(
            name=skill_name, kind=value(at(skill, "Kind"), "Primitive"), commands=commands,
            state=browse(value(at(skill, "StateReference")), f"{skill_name}'s state"),
            error=browse(value(at(skill, "ErrorReference")), f"{skill_name}'s ErrorID") if at(skill, "ErrorReference") else "",
            results=links(at(skill, "Results"), f"{skill_name}'s result"), parameters=parameters,
            description=next((d.get("text", "") for d in skill.get("description") or []), ""))
    machine = at(skills_sm, "Module")
    module_commands = links(at(machine, "Methods"), "module command")
    module_state = browse(value(at(machine, "StateReference")), "the module state") if machine else ""
    if machine and at(machine, "OccupiedReference"):
        occupation["Occupied"] = browse(value(at(machine, "OccupiedReference")), "the occupation")

    capabilities = {}
    described = env.submodel("CapabilityDescription", CAPABILITY_DESCRIPTION)
    for cset in (c for c in children(described) if semantic(c) == f"{CAPABILITY}/CapabilitySet/1/0"):
        for box in (c for c in children(cset) if semantic(c) == f"{CAPABILITY}/CapabilityContainer/1/0"):
            capability = next((c for c in children(box) if c["modelType"] == "Capability"), None)
            offered = capability is not None and any(
                q.get("type") == "CapabilityRoleQualifier/Offered" and str(q.get("value")).lower() in ("true", "1")
                for q in capability.get("qualifiers") or [])
            if not offered:
                continue
            realized = [rel.get("second") for relations in children(box)
                        if semantic(relations) == f"{CAPABILITY}/CapabilityRelations/1/0"
                        for rel in children(relations) if semantic(rel) == f"{CAPABILITY}/CapabilityRealizedBy/1/0"]
            skill = next((env.resolve(ref) for ref in realized if env.resolve(ref) is not None), None)
            if skill is None or skill.get("idShort") not in skills:
                raise AasError(f"{name}: capability {box['idShort']} is not realized by a skill of the Skills submodel")
            values = {}
            for pset in (c for c in children(box) if semantic(c) == f"{CAPABILITY}/PropertySet/1/0"):
                for item in children(pset):
                    for prop in children(item):
                        values[item["idShort"]] = CapabilityValue(
                            item["idShort"], next(iter(meanings(prop)), ""),
                            value=None if prop["modelType"] == "Range" else str(value(prop, "")),
                            minimum=number(prop.get("min")), maximum=number(prop.get("max")),
                            unit=qualifier(prop, "Unit") or "")
            capabilities[box["idShort"]] = CapabilityLink(box["idShort"], tuple(meanings(capability)),
                                                          skill["idShort"], values)

    properties = at(interface, "InteractionMetadata", "properties")
    actions = at(interface, "InteractionMetadata", "actions")
    variables = [below_root(p, f"property {p['idShort']}") for p in children(properties)]
    methods = [below_root(a, f"action {a['idShort']}") for a in children(actions)]
    if len(roots) != 1:
        raise AasError(f"{name}: the interface's browse paths name {len(roots)} root objects")
    (namespace, root), = roots
    endpoint = value(at(interface, "EndpointMetadata", "base"), "")
    return Resource(
        aas_id=env.shell.get("id", ""), id_short=name, endpoint=endpoint,
        interface=Interface.of(root, variables, methods, namespace=namespace, endpoint=endpoint),
        skills=skills, capabilities=capabilities, module_commands=module_commands, module_state=module_state,
        occupation=occupation)


def load(source: str) -> list[Resource]:
    """The modules an AAS environment file or an AAS repository describes."""
    return [describe(env) for env in environments(read(source)) if env.is_module]
