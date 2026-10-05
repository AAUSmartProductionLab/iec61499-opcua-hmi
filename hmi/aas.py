"""The HMI's module descriptions from the modules' AAS.

A module registers its AAS (iec61499-mgmt-py: ``modreg``) in the structure of the resource
ontology: the Skills submodel (skills, their parameters, sequences and occupied equipment, the
module's procedures), Hierarchical Structures (the equipment) and the Asset Interfaces Description
(the OPC UA endpoint and the browse path of every method and variable). That is everything the
HMI needs, so a ``ModuleProfile`` is built from it instead of being written by hand.

Sources: an AAS environment file (``.json`` or ``.json.gz``) or an AAS repository with the HTTP
API of the AAS specification part 2 (BaSyx), whose shells are read with their submodels. Only
shells with a Skills submodel and an OPC UA interface are modules.

Plain JSON in, plain dataclasses out: no AAS library is needed.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator

from .profiles import ModuleProfile, Param, Procedure, Sensor, Skill, Step

OCCUPATION_SKILLS = ("Occupy", "Release")
ROOT_PATH = re.compile(r"^/?0:Objects/(\d+):([^/]+)/")


class AasError(ValueError):
    """The AAS does not describe a module the HMI can show."""


# --- reading an AAS environment -----------------------------------------------------------------

def children(element: dict) -> list[dict]:
    """The elements below a submodel, a collection or list (``value``) or an entity (``statements``)."""
    member = next((m for m in ("submodelElements", "statements") if m in element), "value")
    held = element.get(member)
    return [c for c in held if isinstance(c, dict) and "modelType" in c] if isinstance(held, list) else []


def child(element: dict, id_short: str) -> dict | None:
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


def text(element: dict | None) -> str:
    """The English description (or the first one)."""
    if not element:
        return ""
    entries = element.get("description") or []
    for entry in entries:
        if entry.get("language", "").startswith("en"):
            return entry.get("text", "")
    return entries[0].get("text", "") if entries else ""


def last_key(reference: dict | None) -> str:
    """The element a reference ends at; the last path segment of a global reference."""
    keys = (reference or {}).get("keys") or []
    if not keys:
        return ""
    last = keys[-1]["value"]
    return last.rstrip("/").rsplit("/", 1)[-1] if last.startswith("http") else last


def humanize(name: str) -> str:
    """``MoveNeedleUp`` -> ``Move needle up``."""
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b|\d)|[A-Z]?[a-z]+|\d+", name) or [name]
    return " ".join([words[0], *(w.lower() if not w.isupper() else w for w in words[1:])])


def number(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class Environment:
    """One module's shell and submodels."""

    def __init__(self, shell: dict, submodels: Iterable[dict]) -> None:
        self.shell = shell
        self.submodels = {s["id"]: s for s in submodels}
        self.by_name = {s.get("idShort"): s for s in self.submodels.values()}

    def submodel(self, id_short: str) -> dict | None:
        return self.by_name.get(id_short)

    def resolve(self, reference: dict | None) -> dict | None:
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
        ids = {last for ref in shell.get("submodels", []) if (last := (ref.get("keys") or [{}])[0].get("value"))}
        found.append(Environment(shell, [s for s in submodels if s.get("id") in ids]))
    return found


# --- the module ------------------------------------------------------------------------------------

def module_profile(env: Environment, key: str | None = None) -> ModuleProfile:
    """The HMI's description of the module an AAS describes."""
    interface = env.interface()
    skills_sm = env.submodel("Skills")
    if interface is None or skills_sm is None:
        raise AasError(f"{env.shell.get('idShort')}: no Skills submodel or no OPC UA interface")
    properties = at(interface, "InteractionMetadata", "properties") or {}
    hrefs = {p.get("idShort"): value(at(p, "forms", "href"), "") for p in children(properties)}
    state_href = hrefs.get("Module_State", "")
    match = ROOT_PATH.match(state_href)
    if not match:
        raise AasError(f"{env.shell.get('idShort')}: no Module_State property with a browse path")
    namespace, root = int(match.group(1)), match.group(2)
    if namespace != 1:
        raise AasError(f"{root}: the HMI expects the module's nodes in namespace 1, not {namespace}")
    reader = _Reader(env, properties)
    listed = {s["idShort"]: s for s in children(at(skills_sm, "Skills") or {}) if s.get("idShort")}
    skills = tuple(reader.skill(name, element, listed) for name, element in listed.items()
                   if name not in OCCUPATION_SKILLS)
    procedures = tuple(
        Procedure(p["idShort"], f"{p['idShort']} procedure", reader.steps(p, listed))
        for p in children(at(skills_sm, "Procedures") or {}) if p.get("idShort"))
    entry = at(env.submodel("HierarchicalStructures"), "EntryNode")
    equipment = [n for n in children(entry or {}) if n.get("modelType") == "Entity"]
    sensors = tuple(reader.sensors(n["idShort"] for n in equipment))
    plate = env.submodel("Nameplate")
    designation = value(at(plate, "ManufacturerProductDesignation"), [])
    summary = (designation[0].get("text") if isinstance(designation, list) and designation else "") or text(entry)
    return ModuleProfile(
        key=key or root.lower(),
        title=humanize(root),
        root=root,
        summary=summary,
        sensors=sensors,
        skills=skills,
        procedures=procedures,
        default_endpoint=value(at(interface, "EndpointMetadata", "base"), ""),
        equipment_notes=tuple(f"{n['idShort']}: {text(n).removeprefix('Equipment ')}" for n in equipment if text(n)),
        aas_id=env.shell.get("id", ""),
    )


class _Reader:
    def __init__(self, env: Environment, properties: dict) -> None:
        self.env = env
        self.properties = {p.get("idShort"): p for p in children(properties)}

    def unit(self, key: str) -> str:
        return value(at(self.properties.get(key), "unit"), "") or ""

    def params(self, skill: dict | None) -> tuple[Param, ...]:
        return tuple(param(p) for p in children(at(skill, "Parameters") or {}))

    def results(self, key: str) -> tuple[Param, ...]:
        names = [k[len(key) + 8:] for k in self.properties if k.startswith(f"{key}_Result_")]
        return tuple(Param(name, self.unit(f"{key}_Result_{name}"), 0.0, 10000.0, 0.0) for name in names)

    def skill(self, name: str, element: dict, listed: dict[str, dict]) -> Skill:
        composite = value(at(element, "Kind")) == "Composite"
        uses = tuple(last_key(value(r)) for r in children(at(element, "Occupies") or {}))
        steps = stop_steps = ()
        if composite:
            steps = self.steps(at(element, "Execute"), listed)
            stop_steps = self.steps(at(element, "Stop"), listed)
        return Skill(
            name=name,
            label=humanize(name),
            description=describe(element),
            params=self.params(element),
            results=self.results(name),
            steps=steps,
            stop_steps=stop_steps,
            module_level=composite,
            uses=uses,
        )

    def steps(self, sequence: dict | None, listed: dict[str, dict]) -> tuple[Step, ...]:
        found = []
        for item in children(sequence or {}):
            instance = value(at(item, "InstancePath"), "")
            name = instance.rsplit(".", 1)[-1]
            skill = last_key(value(at(item, "Skill")))
            state = self.env.resolve(value(at(item, "StateReference")))
            key = (state or {}).get("idShort", "").removesuffix("_State")
            runs = listed.get(skill)
            bindings = {b["idShort"]: value(b) for b in children(at(item, "Bindings") or {})}
            declared = {p.name: p for p in self.params(runs)}
            prefix = f"{key}_Parameter_"
            params = []
            for prop in (k for k in self.properties if key and k.startswith(prefix)):
                pname = prop[len(prefix):]
                base = declared.get(pname) or Param(pname, self.unit(prop), 0.0, 0.0, 0.0)
                bound = bindings.get(pname)
                default = number(bound, base.default) if bound is not None else base.default
                params.append(Param(pname, base.unit or self.unit(prop), base.minimum, base.maximum, default,
                                    base.step, base.digits))
            label = first_sentence(text(runs)) if runs else humanize(skill)
            if name != skill:
                label = f"{humanize(name)}: {label[0].lower()}{label[1:]}" if label else humanize(name)
            found.append(Step(
                name=name,
                label=label,
                uses=tuple(last_key(value(r)) for r in children(at(runs, "Occupies") or {})) if runs else (),
                params=tuple(params),
                results=self.results(key) if key else (),
                skill=skill if skill != name else "",
            ))
        return tuple(found)

    def sensors(self, equipment: Iterable[str]) -> Iterator[Sensor]:
        for item in equipment:
            prefix = f"Equipment_{item}_"
            for key, prop in self.properties.items():
                if not key.startswith(prefix):
                    continue
                name = key[len(prefix):]
                kind = {"boolean": "bool", "number": "double"}.get(value(at(prop, "type"), ""), "int")
                yield Sensor(name, f"{humanize(item)}: {humanize(name).lower()}", kind, item, self.unit(key))


def param(element: dict) -> Param:
    qualifiers = {q.get("type"): q.get("value") for q in element.get("qualifiers") or []}
    minimum = number(qualifiers.get("Minimum"), 0.0)
    maximum = number(qualifiers.get("Maximum"), 0.0)
    span = maximum - minimum
    step = 1.0 if span >= 100 else 0.5 if span >= 5 else 0.1
    return Param(element["idShort"], qualifiers.get("Unit") or "", minimum, maximum,
                 number(qualifiers.get("Default"), number(value(element), minimum)), step=step, digits=1)


def first_sentence(description: str) -> str:
    return description.split(" (")[0].rstrip(".")


def describe(skill: dict) -> str:
    """The skill's description and, for a primitive, its contract in words."""
    about = text(skill)
    contract = at(skill, "Contract")
    if contract is None:
        return about
    terms = {c["idShort"]: value(c, "") for c in children(contract)}
    said = []
    if terms.get("Requires") not in (None, "", "TRUE"):
        said.append(f"requires {terms['Requires']}")
    if terms.get("Ensures"):
        said.append(f"ends when {terms['Ensures']}")
    elif terms.get("After"):
        after = terms["After"]
        said.append(f"ends after {after} s" if re.fullmatch(r"[\d.]+", after) else f"ends after {after}")
    if terms.get("Invariant") not in (None, "", "TRUE"):
        said.append(f"must hold {terms['Invariant']} (else InvariantViolated, 2)")
    if terms.get("Timeout"):
        said.append(f"Timeout (3) after {terms['Timeout']}")
    if not said:
        return about
    terms_text = "; ".join(said)
    return f"{about.rstrip('.')}. {terms_text[0].upper()}{terms_text[1:]}."


# --- sources ---------------------------------------------------------------------------------------

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


def load(source: str) -> list[ModuleProfile]:
    """The modules an AAS environment file or an AAS repository (http...) describes."""
    env = Repository(source).environment() if source.startswith(("http://", "https://")) else read_file(source)
    modules = [module_profile(e) for e in environments(env) if e.is_module]
    keys = [m.key for m in modules]
    if len(set(keys)) != len(keys):
        raise AasError(f"two modules with the same root object: {sorted(keys)}")
    return modules
