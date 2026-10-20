#!/usr/bin/env python3
"""Static (AST) inventory of ``backend/tests`` for the PG01 migration.

Deliberately *not* pytest collection: it parses files, never imports or runs
them, and therefore cannot execute a fixture, open a database or hit the
network.

It resolves same-file helpers, test-module/class fixtures, conftest fixtures,
the integration autouse fixture, ``pytestmark``/``usefixtures`` and imports under
``backend/tests`` transitively. Direct parameter values are not treated as
fixtures; ``indirect=True``/``indirect=[...]`` names are. Unknown dependencies
are marked unresolved rather than claimed pure. Imported ``pg_fixtures`` helpers
that do not touch a database (e.g. ``owned_database_name``) are not PG evidence.

Parse errors, duplicate node ids, a ``test_*.py`` with no test functions, or an
assertion-extraction mismatch fail the generator (exit 1).

Outputs (regenerated on every successful run):
  docs/harness-mainline/postgres-test-inventory.json
  docs/harness-mainline/postgres-test-inventory.md
"""
from __future__ import annotations

import ast
import hashlib
import json
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "backend" / "tests"
OUT_JSON = ROOT / "docs" / "harness-mainline" / "postgres-test-inventory.json"
OUT_MD = ROOT / "docs" / "harness-mainline" / "postgres-test-inventory.md"

#: SQLite storage tokens in a resolved body. The env flag is not here: the
#: conftest autouse fixture sets it for every test without using SQLite.
SQLITE_RE = re.compile(r"sqlite3|:memory:|sqlite://|\.db['\"]")
SUPPLEMENTARY_RE = re.compile(
    r"sqlite3|:memory:|sqlite://|BETTER_AGENT_TEST_ALLOW_SQLITE|\.db['\"]"
)
#: Direct database construction. Fixture names are not matched here; only a
#: resolved postgres fixture/helper dependency counts.
DB_RE = re.compile(r"\bDatabase\s*\(|build_runtime\s*\(|psycopg\.connect")
NETWORK_FILE = ("live", "acceptance", "network", "web_search", "tavily", "mcp_acceptance")
NETWORK_MARKERS = {"live", "network", "external", "acceptance", "real_network"}
POSTGRES_FIXTURES = {"postgres_url", "migrated_postgres_url", "isolated_postgres_test"}
#: ``pg_fixtures`` helpers that actually touch a database. Pure helpers such as
#: ``owned_database_name``/``_with_database`` are not PG evidence.
PG_EVIDENCE_HELPERS = {
    "allocate_owned_database", "create_owned_database", "drop_owned_database",
    "truncate_application_tables", "isolated_database_lifecycle",
}
POSTGRES_SENTINEL = "__postgres__"
BUILTIN_FIXTURES = {
    "self", "cls", "request", "tmp_path", "tmp_path_factory", "monkeypatch",
    "capsys", "capfd", "capsysbinary", "capfdbinary", "caplog", "recwarn",
    "pytestconfig", "record_property", "record_testsuite_property",
    "doctest_namespace", "event_loop", "anyio_backend", "browser", "page",
    "context", "testrun_uid",
}
FIXTURE_DECORATORS = {"fixture", "pytest.fixture", "pytest_asyncio.fixture"}


def digest(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()[:12]


def decorator_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def is_fixture(node: ast.AST) -> bool:
    return any(
        decorator_name(item.func if isinstance(item, ast.Call) else item) in FIXTURE_DECORATORS
        for item in getattr(node, "decorator_list", [])
    )


def is_autouse(node: ast.AST) -> bool:
    for item in getattr(node, "decorator_list", []):
        if isinstance(item, ast.Call) and decorator_name(item.func) in FIXTURE_DECORATORS:
            for keyword in item.keywords:
                if (
                    keyword.arg == "autouse"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    return True
    return False


def arg_names(node: ast.AST) -> list[str]:
    arguments = getattr(node, "args", None)
    if not isinstance(arguments, ast.arguments):
        return []
    return [item.arg for item in arguments.args + arguments.kwonlyargs]


def referenced_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            base = child
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)
    return names


def self_attribute_names(node: ast.AST) -> set[str]:
    """Names accessed as ``self.<attr>`` (class helper methods / fixtures)."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id == "self":
            names.add(child.attr)
    return names


def source_span(lines: list[str], node: ast.AST) -> tuple[int, int, bool, str]:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", None)
    complete = end is not None
    end = end or start
    return start, end, complete, "\n".join(lines[start - 1:end])


def _call_assertion_kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        name = decorator_name(node.func)
        if name == "raises" or name.endswith(".raises"):
            return "pytest.raises"
        if name == "warns" or name.endswith(".warns"):
            return "pytest.warns"
    return None


def _assertion_kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assert):
        return "assert"
    return _call_assertion_kind(node)


def expected_assertion_count(node: ast.AST) -> int:
    return sum(1 for child in ast.walk(node) if _assertion_kind(child) is not None)


def assertion_records(lines: list[str], node: ast.AST) -> list[dict]:
    records: list[dict] = []
    for child in ast.walk(node):
        kind = _assertion_kind(child)
        if kind is None:
            continue
        start, end, complete, text = source_span(lines, child)
        records.append({
            "line": start, "end_line": end, "kind": kind,
            "span_complete": complete, "digest": digest(text), "source": text,
        })
    records.sort(key=lambda item: (item["line"], item["end_line"]))
    return records


def _consume_marker(value: ast.AST, usefixtures: set[str], markers: set[str]) -> None:
    if isinstance(value, ast.Call):
        name = decorator_name(value.func)
        if name.endswith("mark.usefixtures"):
            for argument in value.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    usefixtures.add(argument.value)
        elif ".mark." in name:
            markers.add(name.rsplit(".", 1)[-1])
    elif isinstance(value, ast.Attribute):
        name = decorator_name(value)
        if ".mark." in name:
            markers.add(name.rsplit(".", 1)[-1])
    elif isinstance(value, (ast.List, ast.Tuple)):
        for element in value.elts:
            _consume_marker(element, usefixtures, markers)


def marker_spec(nodes: list[ast.stmt]) -> tuple[set[str], set[str]]:
    usefixtures: set[str] = set()
    markers: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    _consume_marker(node.value, usefixtures, markers)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "pytestmark"
            and node.value is not None
        ):
            _consume_marker(node.value, usefixtures, markers)
    return usefixtures, markers


def parametrize_spec(decorators: list[ast.AST]) -> dict:
    names: set[str] = set()
    indirect: set[str] = set()
    for item in decorators:
        if not (isinstance(item, ast.Call) and decorator_name(item.func).endswith("parametrize")):
            continue
        if not item.args:
            continue
        first = item.args[0]
        declared: set[str] = set()
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            declared = {part.strip() for part in first.value.split(",") if part.strip()}
        elif isinstance(first, (ast.Tuple, ast.List)):
            declared = {
                element.value.strip()
                for element in first.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
        names |= declared
        indirect_value = None
        for keyword in item.keywords:
            if keyword.arg == "indirect":
                indirect_value = keyword.value
        if isinstance(indirect_value, ast.Constant) and indirect_value.value is True:
            indirect |= declared
        elif isinstance(indirect_value, (ast.List, ast.Tuple)):
            indirect |= {
                element.value.strip()
                for element in indirect_value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    return {"names": names, "direct": names - indirect, "indirect": indirect}


class FuncInfo:
    def __init__(self, module: "ModuleInfo", node: ast.AST, name: str) -> None:
        self.module = module
        self.node = node
        self.name = name
        self.fixture = is_fixture(node)
        self.autouse = is_autouse(node)
        start, _end, complete, self.source = source_span(module.lines, node)
        self.line = start
        self.span_complete = complete
        self.args = arg_names(node)


class ClassInfo:
    def __init__(self, module: "ModuleInfo", node: ast.ClassDef) -> None:
        self.module = module
        self.name = node.name
        self.methods: dict[str, FuncInfo] = {}
        self.fixtures: dict[str, FuncInfo] = {}
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info = FuncInfo(module, child, child.name)
                self.methods[child.name] = info
                if info.fixture:
                    self.fixtures[child.name] = info
        self.usefixtures, self.markers = marker_spec(node.body)
        for decorator in node.decorator_list:
            _consume_marker(decorator, self.usefixtures, self.markers)


MODULES: dict[str, "ModuleInfo"] = {}
ERRORS: list[str] = []


def resolve_import(from_rel: str, base: str, name: str, level: int) -> tuple[str, str | None]:
    if level:
        directory = posixpath.dirname(from_rel)
        for _ in range(level - 1):
            directory = posixpath.dirname(directory)
        candidate = posixpath.join(directory, base.replace(".", "/"))
    else:
        candidate = base.replace(".", "/")
    path = TESTS / (candidate + ".py")
    if path.is_file():
        return ("module", str(path.relative_to(ROOT)).replace("\\", "/"), name)
    package = TESTS / candidate / "__init__.py"
    if package.is_file():
        return ("module", str(package.relative_to(ROOT)).replace("\\", "/"), name)
    return ("production", base or name, name)


class ModuleInfo:
    def __init__(self, path: Path, rel: str) -> None:
        self.path = path
        self.rel = rel
        self.lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        self.tree = ast.parse("\n".join(self.lines), filename=rel)
        self.functions: dict[str, FuncInfo] = {}
        self.fixtures: dict[str, FuncInfo] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.imports: dict[str, tuple[str, str | None]] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info = FuncInfo(self, node, node.name)
                self.functions[node.name] = info
                if info.fixture:
                    self.fixtures[node.name] = info
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                self.classes[node.name] = ClassInfo(self, node)
        self.usefixtures, self.markers = marker_spec(self.tree.body)
        self._index_imports()

    def _index_imports(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = resolve_import(
                        self.rel, base, alias.name, node.level
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    self.imports[local] = ("production", alias.name, None)

    def test_items(self) -> list[tuple[ast.AST, list[str], list[ast.AST], ClassInfo | None]]:
        items: list[tuple[ast.AST, list[str], list[ast.AST], ClassInfo | None]] = []

        def walk(body: list[ast.stmt], prefix: list[str], inherited: list[ast.AST],
                 owner: ClassInfo | None) -> None:
            for child in body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    items.append((child, prefix + [child.name], inherited, owner))
                elif isinstance(child, ast.ClassDef) and child.name.startswith("Test"):
                    walk(child.body, prefix + [child.name], inherited + list(child.decorator_list),
                         self.classes.get(child.name))

        walk(self.tree.body, [], [], None)
        return items


def load_modules() -> None:
    MODULES.clear()
    ERRORS.clear()
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            MODULES[rel] = ModuleInfo(path, rel)
        except (SyntaxError, OSError) as exc:
            ERRORS.append(f"{rel}: {exc}")


def applicable_conftests(rel: str) -> list[ModuleInfo]:
    directory = posixpath.dirname(rel)
    found: list[ModuleInfo] = []
    for candidate, module in MODULES.items():
        if posixpath.basename(candidate) != "conftest.py":
            continue
        base = posixpath.dirname(candidate)
        if directory == base or directory.startswith(base + "/"):
            found.append(module)
    found.sort(key=lambda item: len(posixpath.dirname(item.rel)), reverse=True)
    return found


def resolve_name(module: ModuleInfo, name: str, class_info: ClassInfo | None = None):
    if class_info is not None and name in class_info.methods:
        return ("func", class_info.methods[name])
    if name in module.functions:
        return ("func", module.functions[name])
    if name in module.imports:
        kind, target, target_name = module.imports[name]
        if kind == "production":
            return ("production", target, target_name)
        target_module = MODULES.get(target or "")
        if target_module is None:
            return ("production", target, target_name)
        if target_module.rel.endswith("pg_fixtures.py"):
            return ("postgres", target_module, target_name or name)
        if target_name in target_module.functions:
            return ("func", target_module.functions[target_name])
    return None


def fixture_in_module(module: ModuleInfo, name: str):
    if name in module.fixtures:
        return module.fixtures[name]
    resolved = resolve_name(module, name)
    if resolved and resolved[0] == "func" and resolved[1].fixture:
        return resolved[1]
    return None


def resolve_fixture(module: ModuleInfo, name: str, class_info: ClassInfo | None = None):
    if class_info is not None and name in class_info.fixtures:
        return class_info.fixtures[name]
    found = fixture_in_module(module, name)
    if found is not None:
        return found
    for conftest in applicable_conftests(module.rel):
        found = fixture_in_module(conftest, name)
        if found is not None:
            return found
        resolved = resolve_name(conftest, name)
        if resolved and resolved[0] == "postgres":
            return POSTGRES_SENTINEL
    resolved = resolve_name(module, name)
    if resolved and resolved[0] == "postgres":
        return POSTGRES_SENTINEL
    return None


def effective_autouse(module: ModuleInfo, class_info: ClassInfo | None) -> tuple[list[str], list[str]]:
    """Pytest autouse semantics: accumulate names, then resolve in test context.

    Every scope's ``autouse=True`` names are collected (class, module, nearest
    conftest, parent conftest), then each name is resolved through
    ``resolve_fixture`` in the collecting module/class context. A nearer
    non-autouse fixture with the same name is the effective implementation and
    still executes as an autouse fixture; it is only metadata, not suppression.
    """
    names: list[str] = []
    seen: set[str] = set()
    definitions: dict[str, list[tuple[int, FuncInfo]]] = {}
    autouse_ranks: dict[str, list[int]] = {}
    rank = 0

    def collect(mapping: dict[str, FuncInfo]) -> None:
        nonlocal rank
        for name, info in mapping.items():
            definitions.setdefault(name, []).append((rank, info))
            if info.autouse:
                if name not in seen:
                    seen.add(name)
                    names.append(name)
                autouse_ranks.setdefault(name, []).append(rank)
        rank += 1

    if class_info is not None:
        collect(class_info.fixtures)
    collect(module.fixtures)
    for conftest in applicable_conftests(module.rel):
        collect(conftest.fixtures)

    shadowed: list[str] = []
    for name in names:
        effective = resolve_fixture(module, name, class_info)
        if not isinstance(effective, FuncInfo):
            continue
        effective_rank = next(
            (value for value, info in definitions.get(name, []) if info is effective), None
        )
        if effective_rank is not None and any(
            declared > effective_rank for declared in autouse_ranks.get(name, [])
        ):
            shadowed.append(name)
    return names, shadowed


def gather(module: ModuleInfo, node: ast.AST, parametrize: dict,
           fixture_module: ModuleInfo | None = None,
           class_info: ClassInfo | None = None,
           usefixtures: set[str] | None = None,
           autouse_names: set[str] | None = None):
    fixture_module = fixture_module or module
    usefixtures = usefixtures or set()
    autouse_names = autouse_names or set()
    tokens = [source_span(module.lines, node)[3]]
    dependencies: list[dict] = []
    evidence: list[dict] = []
    unresolved: list[str] = []
    flags: set[str] = set()
    visited: set[tuple[str, str]] = set()
    queue: list[tuple[str, ModuleInfo, str]] = []

    def class_for(current: ModuleInfo) -> ClassInfo | None:
        return class_info if current is module or current is fixture_module else None

    for name in sorted(referenced_names(node)):
        queue.append(("ref", module, name))
    for name in sorted(self_attribute_names(node)):
        queue.append(("ref", module, name))
    for name in arg_names(node):
        if name in parametrize["direct"]:
            continue  # direct parameter value, not a fixture
        queue.append(("fixture", fixture_module, name))
    for name in sorted(parametrize["indirect"] | usefixtures | autouse_names):
        queue.append(("fixture", fixture_module, name))

    while queue:
        kind, current, name = queue.pop(0)
        if kind == "ref":
            resolved = resolve_name(current, name, class_for(current))
            if resolved is None:
                continue
            if resolved[0] == "func":
                info = resolved[1]
                if info.fixture:
                    # A fixture referenced by name is a fixture request, not an
                    # ordinary helper: route it through the fixture branch so its
                    # own fixture dependencies are resolved too.
                    queue.append(("fixture", fixture_module, name))
                    continue
                key = (info.module.rel, info.name)
                if key in visited:
                    continue
                visited.add(key)
                tokens.append(info.source)
                dependencies.append({
                    "kind": "class_helper" if info.module is module and class_info is not None and info.name in class_info.methods
                    else ("same_file_helper" if info.module is current else "test_module_helper"),
                    "name": info.name, "module": info.module.rel, "line": info.line,
                })
                for ref in sorted(referenced_names(info.node)):
                    queue.append(("ref", info.module, ref))
                for attr in sorted(self_attribute_names(info.node)):
                    queue.append(("ref", info.module, attr))
            elif resolved[0] == "postgres":
                target_name = resolved[2]
                if target_name in POSTGRES_FIXTURES or target_name in PG_EVIDENCE_HELPERS:
                    flags.add("uses_postgres_fixture")
                dependencies.append({
                    "kind": "postgres_helper", "name": target_name,
                    "module": resolved[1].rel, "line": 0,
                })
            else:
                evidence.append({"kind": "production_import", "name": name, "module": resolved[1]})
            continue

        if name in BUILTIN_FIXTURES:
            continue
        info = resolve_fixture(current, name, class_for(current))
        if info == POSTGRES_SENTINEL:
            flags.add("uses_postgres_fixture")
            dependencies.append({
                "kind": "postgres_fixture", "name": name,
                "module": "backend/tests/pg_fixtures.py", "line": 0,
            })
            continue
        if info is None:
            unresolved.append(name)
            continue
        key = (info.module.rel, info.name)
        if key in visited:
            continue
        visited.add(key)
        tokens.append(info.source)
        dependencies.append({
            "kind": "class_fixture" if info.module is module and class_info is not None and info.name in class_info.fixtures
            else "fixture",
            "name": info.name, "module": info.module.rel, "line": info.line,
        })
        if info.module.rel.endswith("pg_fixtures.py") and (
            info.name in POSTGRES_FIXTURES or info.name in PG_EVIDENCE_HELPERS
        ):
            flags.add("uses_postgres_fixture")
        for arg in info.args:
            # Fixture dependencies are pytest requests resolved in the original
            # collecting test module/class context, so a module fixture override
            # is honoured even for a conftest fixture's argument.
            queue.append(("fixture", fixture_module, arg))
        for ref in sorted(referenced_names(info.node)):
            queue.append(("ref", info.module, ref))
        for attr in sorted(self_attribute_names(info.node)):
            queue.append(("ref", info.module, attr))
    return tokens, dependencies, evidence, unresolved, flags


def classify(rel: str, text: str, flags: set[str], unresolved: list[str],
             reexported: bool = False, markers: set[str] | None = None) -> tuple[str, set[str]]:
    base = posixpath.basename(rel).lower()
    hint = any(token in base for token in NETWORK_FILE)
    if hint:
        flags = flags | {"network_filename_hint"}
    if markers and (markers & NETWORK_MARKERS):
        return "real_network", flags | {"network_marker"}
    if "migration" in base:
        return "legacy_migration", flags | {"migration_file"}
    if SQLITE_RE.search(text) and not (reexported and "uses_postgres_fixture" in flags):
        return "direct_sqlite", flags | {"sqlite_evidence"}
    if reexported and SQLITE_RE.search(text):
        flags = flags | {"source_sqlite_evidence"}
    if DB_RE.search(text) or "uses_postgres_fixture" in flags:
        return "database_backed", flags | {"database_evidence"}
    if hint:
        return "unresolved", flags | {"network_unverified"}
    if unresolved:
        return "unresolved", flags | {"unknown_dependency"}
    return "pure_no_db", flags


def planned(cls: str, rel: str, nodeid: str, flags: set[str], parametrized: bool):
    if cls == "pure_no_db":
        target = ("unchanged; no database (integration guard/utility, not PostgreSQL-backed)"
                  if "integration_file" in flags else "unchanged (no database)")
    elif cls == "real_network":
        target = "explicit opt-in only; out of the default PostgreSQL suite"
    elif cls == "legacy_migration":
        target = (
            "keep the SQLite/old-revision input rows; add an old-revision -> Alembic head "
            "upgrade on migrated_postgres_url; do not rewrite every migration"
        )
    elif cls == "direct_sqlite":
        target = "same nodeid; replace the SQLite fixture with isolated_postgres_test"
    elif cls == "unresolved":
        target = "in place; resolve the listed uncertainty before claiming a database class"
    elif "integration_file" in flags and "uses_postgres_fixture" in flags:
        target = "unchanged (already isolated PostgreSQL)"
    elif "integration_file" in flags:
        target = "in place; not counted as PostgreSQL-backed (no postgres fixture)"
    else:
        target = "same nodeid; opt in to migrated_postgres_url + isolated_postgres_test"
    new_nodeid = nodeid + (" [runtime ids unresolved]" if parametrized else "")
    return target, new_nodeid


def supplementary(limit: int = 200) -> list[dict]:
    found: list[dict] = []
    for root in (ROOT / "scripts", ROOT / "frontend" / "e2e"):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".js", ".json", ".ps1"}:
                continue
            if any(part in {".git", "node_modules"} for part in path.parts):
                continue
            if path.name == Path(__file__).name:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if SUPPLEMENTARY_RE.search(line):
                    found.append({
                        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                        "line": number, "text": line.strip()[:200],
                    })
                    if len(found) >= limit:
                        return found
    return found


def analyze() -> tuple[list[dict], list[str], dict[str, int]]:
    """Build the inventory for the current ROOT/TESTS globals (no file writes)."""
    load_modules()
    tests: list[dict] = []
    seen_nodeids: set[str] = set()

    for rel, module in sorted(MODULES.items()):
        if not posixpath.basename(rel).startswith("test_"):
            continue
        items = [(node, parts, decorators, owner, module)
                 for node, parts, decorators, owner in module.test_items()]
        for local, (kind, target, target_name) in module.imports.items():
            if kind != "module" or not local.startswith("test_"):
                continue
            target_module = MODULES.get(target or "")
            if target_module is None or target_name not in target_module.functions:
                continue
            info = target_module.functions[target_name]
            items.append((info.node, [local], [], None, target_module))
        if not items:
            ERRORS.append(f"{rel}: no test functions found (missing test or extraction failure)")
            continue
        integration = "/integration/" in f"/{rel}"
        for node, parts, class_decorators, class_info, source_module in items:
            nodeid = f"{rel}::{'::'.join(parts)}"
            if nodeid in seen_nodeids:
                ERRORS.append(f"{nodeid}: duplicate node id")
            seen_nodeids.add(nodeid)
            decorators = list(getattr(node, "decorator_list", [])) + list(class_decorators)
            parametrize = parametrize_spec(decorators)
            parametrized = bool(parametrize["names"])
            usefixtures, markers = marker_spec([])
            for decorator in decorators:
                _consume_marker(decorator, usefixtures, markers)
            usefixtures |= module.usefixtures
            if class_info is not None:
                usefixtures |= class_info.usefixtures
                markers |= class_info.markers
            markers |= module.markers
            autouse, shadowed = effective_autouse(module, class_info)
            module_flags = {"shadows_autouse"} if shadowed else set()
            tokens, dependencies, evidence, unresolved, flags = gather(
                source_module, node, parametrize, fixture_module=module,
                class_info=class_info, usefixtures=usefixtures,
                autouse_names=set(autouse),
            )
            if source_module is not module:
                dependencies.append({
                    "kind": "imported_test", "name": node.name,
                    "module": source_module.rel, "line": node.lineno,
                })
            flags |= module_flags
            if integration:
                flags.add("integration_file")
            if parametrized:
                flags.add("parametrized")
            reexported = source_module is not module
            text = "\n".join(tokens)
            cls, flags = classify(rel, text, flags, unresolved, reexported, markers)
            target, new_nodeid = planned(cls, rel, nodeid, flags, parametrized)
            assertions = assertion_records(source_module.lines, node)
            expected = expected_assertion_count(node)
            if len(assertions) != expected:
                ERRORS.append(
                    f"{nodeid}: assertion extraction mismatch ({len(assertions)} != {expected})"
                )
            tests.append({
                "old_nodeid": nodeid,
                "new_nodeid": new_nodeid,
                "status": "planned",
                "new_location": rel,
                "file": rel,
                "definition_file": source_module.rel,
                "line": node.lineno,
                "classification": cls,
                "flags": sorted(flags),
                "evidence": evidence,
                "dependencies": dependencies,
                "unresolved_dependencies": sorted(set(unresolved)),
                "parametrized": parametrized,
                "parametrize": {
                    "names": sorted(parametrize["names"]),
                    "direct": sorted(parametrize["direct"]),
                    "indirect": sorted(parametrize["indirect"]),
                    "runtime_ids_unresolved": parametrized,
                },
                "markers": sorted(markers),
                "usefixtures": sorted(usefixtures),
                "autouse_fixtures": autouse,
                "planned_target": target,
                "assertions": assertions,
            })

    counts: dict[str, int] = {}
    for item in tests:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    return tests, ERRORS, counts


def main() -> int:
    tests, errors, counts = analyze()
    artifact = {
        "schema": "pg01-test-inventory-v3",
        "method": "static AST parse + transitive helper/fixture/class/marker resolution; no pytest collection",
        "counts": counts,
        "total_test_functions": len(tests),
        "unresolved_runtime_ids": sum(1 for item in tests if item["parametrized"]),
        "unresolved_dependencies": sum(1 for item in tests if item["classification"] == "unresolved"),
        "errors": errors,
        "supplementary_sqlite_references": supplementary(),
        "tests": tests,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if not tests:
        print("ERROR: no test functions found", file=sys.stderr)
        return 1

    md = [
        "# Backend test inventory (PG01, static AST)",
        "",
        "Generated by `scripts/inventory_backend_tests.py`. Static parse only; this is **not** pytest collection.",
        "Dependencies are resolved transitively through same-file helpers, class/module fixtures,",
        "conftest fixtures, autouse fixtures, pytestmark/usefixtures and imports under `backend/tests`.",
        "Direct parametrize values are not fixtures; indirect ones are.",
        "",
        "| classification | count |",
        "|---|---:|",
    ]
    for key in sorted(counts):
        md.append(f"| {key} | {counts[key]} |")
    md += [
        f"| **total** | **{len(tests)}** |",
        "",
        f"Runtime parameter ids unresolved: {artifact['unresolved_runtime_ids']}. "
        f"Unresolved dependency tests: {artifact['unresolved_dependencies']}.",
        "",
        "| old nodeid | line | classification | flags | parametrized | new nodeid | planned target | assertions (line:kind:digest) |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for item in tests:
        marks = ", ".join(f"{a['line']}:{a['kind']}:{a['digest']}" for a in item["assertions"][:12])
        if len(item["assertions"]) > 12:
            marks += f", …(+{len(item['assertions']) - 12})"
        md.append(
            f"| `{item['old_nodeid']}` | {item['line']} | {item['classification']} | "
            f"{','.join(item['flags'])} | {'yes' if item['parametrized'] else 'no'} | "
            f"`{item['new_nodeid']}` | {item['planned_target']} | {marks} |"
        )
    md += ["", "## Supplementary SQLite references (bounded)", ""]
    if artifact["supplementary_sqlite_references"]:
        for ref in artifact["supplementary_sqlite_references"]:
            md.append(f"- `{ref['path']}:{ref['line']}` — `{ref['text']}`")
    else:
        md.append("- none found in `scripts/` or `frontend/e2e/`")
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps({
        "counts": counts,
        "total": len(tests),
        "unresolved_runtime_ids": artifact["unresolved_runtime_ids"],
        "unresolved_dependencies": artifact["unresolved_dependencies"],
        "supplementary": len(artifact["supplementary_sqlite_references"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
