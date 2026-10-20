"""PG01 generator self-regressions on small temporary AST roots.

These tests never touch a database, Docker or the business suite: they write a
few source files under ``tmp_path`` and run the static inventory over them.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import inventory_backend_tests as inv  # noqa: E402


def _classify(tmp_path: Path, files: dict[str, str]) -> dict[str, dict]:
    tests_dir = tmp_path / "backend" / "tests"
    for relative, source in files.items():
        path = tests_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    inv.ROOT = tmp_path
    inv.TESTS = tests_dir
    tests, errors, _counts = inv.analyze()
    assert errors == []
    return {item["old_nodeid"]: item for item in tests}


def _only(tests: dict[str, dict], suffix: str) -> dict:
    matches = [item for nodeid, item in tests.items() if nodeid.endswith(suffix)]
    assert len(matches) == 1, matches
    return matches[0]


def test_same_file_helper_that_builds_sqlite_is_not_pure(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_sqlite_helper.py": (
            "from app.db import Database\n"
            "def _db(tmp_path):\n"
            "    return Database(tmp_path / 'x.db')\n"
            "def test_uses_helper(tmp_path):\n"
            "    assert _db(tmp_path)\n"
        ),
    })
    item = _only(tests, "test_uses_helper")
    assert item["classification"] == "direct_sqlite"


def test_custom_fixture_requiring_migrated_postgres_is_database_backed(tmp_path: Path):
    tests = _classify(tmp_path, {
        "pg_fixtures.py": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def migrated_postgres_url():\n"
            "    return 'postgresql://u:p@127.0.0.1:5432/x_test'\n"
        ),
        "test_custom.py": (
            "import pytest\n"
            "from pg_fixtures import migrated_postgres_url\n"
            "@pytest.fixture\n"
            "def attempt(migrated_postgres_url):\n"
            "    return migrated_postgres_url\n"
            "def test_attempt(attempt):\n"
            "    assert attempt\n"
        ),
    })
    item = _only(tests, "test_attempt")
    assert item["classification"] == "database_backed"
    assert "uses_postgres_fixture" in item["flags"]


def test_shadowed_integration_autouse_is_not_counted_as_postgres(tmp_path: Path):
    tests = _classify(tmp_path, {
        "pg_fixtures.py": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def migrated_postgres_url():\n"
            "    return 'postgresql://u:p@127.0.0.1:5432/x_test'\n"
        ),
        "integration/conftest.py": (
            "import pytest\n"
            "from pg_fixtures import migrated_postgres_url\n"
            "@pytest.fixture(autouse=True)\n"
            "def isolated_postgres_test(migrated_postgres_url):\n"
            "    yield\n"
        ),
        "integration/test_guard.py": (
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def isolated_postgres_test():\n"
            "    yield\n"
            "def test_guard():\n"
            "    assert True\n"
        ),
    })
    item = _only(tests, "test_guard")
    assert item["classification"] == "pure_no_db"
    assert "shadows_autouse" in item["flags"]
    assert "uses_postgres_fixture" not in item["flags"]


def test_direct_parametrize_value_is_not_a_fixture_and_indirect_is(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_params.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "@pytest.fixture\n"
            "def db():\n"
            "    return Database(':memory:')\n"
            "@pytest.mark.parametrize('status', ['a', 'b'])\n"
            "def test_direct(status):\n"
            "    assert status\n"
            "@pytest.mark.parametrize('db', [None], indirect=True)\n"
            "def test_indirect(db):\n"
            "    assert db\n"
        ),
    })
    direct = _only(tests, "test_direct")
    assert direct["parametrize"]["direct"] == ["status"]
    assert direct["unresolved_dependencies"] == []
    assert direct["classification"] == "pure_no_db"
    indirect = _only(tests, "test_indirect")
    assert indirect["parametrize"]["indirect"] == ["db"]
    assert "db" not in indirect["unresolved_dependencies"]
    assert indirect["classification"] == "direct_sqlite"


def test_normal_pytest_raises_is_mapped_with_its_line(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_raises.py": (
            "import pytest\n"
            "def test_raises():\n"
            "    with pytest.raises(ValueError):\n"
            "        raise ValueError('x')\n"
        ),
    })
    item = _only(tests, "test_raises")
    raises = [record for record in item["assertions"] if record["kind"] == "pytest.raises"]
    assert len(raises) == 1
    assert raises[0]["line"] == 3
    assert "pytest.raises" in raises[0]["source"]


def test_network_requires_a_marker_not_a_filename(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_web_search.py": (
            "class FakeClient:\n"
            "    pass\n"
            "def test_fake_search():\n"
            "    assert FakeClient()\n"
        ),
        "test_live_marker.py": (
            "import pytest\n"
            "@pytest.mark.live\n"
            "def test_live():\n"
            "    assert True\n"
        ),
    })
    fake = _only(tests, "test_fake_search")
    assert fake["classification"] != "real_network"
    assert "network_unverified" in fake["flags"]
    live = _only(tests, "test_live")
    assert live["classification"] == "real_network"
    assert "network_marker" in live["flags"]


def test_class_fixture_and_class_helper_are_not_pure(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_class.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "class TestThing:\n"
            "    @pytest.fixture\n"
            "    def store(self):\n"
            "        return Database(':memory:')\n"
            "    def test_uses_fixture(self, store):\n"
            "        assert store\n"
            "    def test_uses_helper(self):\n"
            "        assert self.make()\n"
            "    def make(self):\n"
            "        return Database(':memory:')\n"
        ),
    })
    fixture_item = _only(tests, "test_uses_fixture")
    helper_item = _only(tests, "test_uses_helper")
    assert fixture_item["classification"] == "direct_sqlite"
    assert helper_item["classification"] == "direct_sqlite"


def test_alias_import_helper_is_followed(tmp_path: Path):
    tests = _classify(tmp_path, {
        "helpers.py": (
            "from app.db import Database\n"
            "def make_db():\n"
            "    return Database(':memory:')\n"
        ),
        "test_alias.py": (
            "from helpers import make_db as build\n"
            "def test_alias():\n"
            "    assert build()\n"
        ),
    })
    item = _only(tests, "test_alias")
    assert item["classification"] == "direct_sqlite"


def test_pure_pg_fixture_helper_is_not_database_evidence(tmp_path: Path):
    tests = _classify(tmp_path, {
        "pg_fixtures.py": (
            "def owned_database_name(base):\n"
            "    return base + '_x'\n"
        ),
        "test_infra.py": (
            "from pg_fixtures import owned_database_name\n"
            "def test_name():\n"
            "    assert owned_database_name('base')\n"
        ),
    })
    item = _only(tests, "test_name")
    assert item["classification"] == "pure_no_db"
    assert "uses_postgres_fixture" not in item["flags"]


def test_generator_output_is_independent_of_python_hash_seed(tmp_path: Path):
    root = tmp_path / "root"
    tests_dir = root / "backend" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "pg_fixtures.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def migrated_postgres_url():\n"
        "    return 'postgresql://u:p@127.0.0.1:5432/x_test'\n",
        encoding="utf-8",
    )
    (tests_dir / "helpers.py").write_text(
        "from app.db import Database\n"
        "def zeta():\n    return Database(':memory:')\n"
        "def alpha():\n    return 1\n"
        "def mid():\n    return 2\n",
        encoding="utf-8",
    )
    (tests_dir / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def first():\n    return 1\n"
        "@pytest.fixture\n"
        "def second():\n    return 2\n",
        encoding="utf-8",
    )
    (tests_dir / "test_case.py").write_text(
        "from helpers import zeta, alpha, mid\n"
        "class TestThing:\n"
        "    def test_one(self, first, second):\n"
        "        assert alpha() and mid()\n"
        "        assert self.helper_b() and self.helper_a()\n"
        "    def helper_a(self):\n        return 1\n"
        "    def helper_b(self):\n        return 2\n"
        "def test_two():\n    assert zeta()\n",
        encoding="utf-8",
    )
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import inventory_backend_tests as inv\n"
        f"inv.ROOT = Path({str(root)!r})\n"
        f"inv.TESTS = Path({str(tests_dir)!r})\n"
        "inv.OUT_JSON = Path(sys.argv[1])\n"
        "inv.OUT_MD = Path(sys.argv[2])\n"
        "raise SystemExit(inv.main())\n"
    )
    artifacts: list[tuple[bytes, bytes]] = []
    for seed in ("1", "424242"):
        out_json = tmp_path / f"inventory-{seed}.json"
        out_md = tmp_path / f"inventory-{seed}.md"
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script, str(out_json), str(out_md)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env=environment,
        )
        assert result.returncode == 0, result.stderr
        artifacts.append((out_json.read_bytes(), out_md.read_bytes()))
    assert artifacts[0][0] == artifacts[1][0], "JSON differs across hash seeds"
    assert artifacts[0][1] == artifacts[1][1], "Markdown differs across hash seeds"


def test_module_autouse_sqlite_fixture_is_direct_sqlite(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_mod_autouse.py": (
            "import pytest\n"
            "import sqlite3\n"
            "@pytest.fixture(autouse=True)\n"
            "def memory_only_database():\n"
            "    return sqlite3.connect(':memory:')\n"
            "def test_a():\n"
            "    assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "direct_sqlite"
    assert item["autouse_fixtures"] == ["memory_only_database"]
    assert any(dep["name"] == "memory_only_database" for dep in item["dependencies"])


def test_class_autouse_sqlite_fixture_is_direct_sqlite(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_class_autouse.py": (
            "import pytest\n"
            "import sqlite3\n"
            "class TestThing:\n"
            "    @pytest.fixture(autouse=True)\n"
            "    def memory_only_database(self):\n"
            "        return sqlite3.connect(':memory:')\n"
            "    def test_a(self):\n"
            "        assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "direct_sqlite"
    assert item["autouse_fixtures"] == ["memory_only_database"]


def test_conftest_autouse_recurses_through_fixture_and_helper(tmp_path: Path):
    tests = _classify(tmp_path, {
        "helpers.py": (
            "from app.db import Database\n"
            "def make_db():\n"
            "    return Database(':memory:')\n"
        ),
        "conftest.py": (
            "import pytest\n"
            "from helpers import make_db\n"
            "@pytest.fixture\n"
            "def custom_db():\n"
            "    return make_db()\n"
            "@pytest.fixture(autouse=True)\n"
            "def install(custom_db):\n"
            "    yield\n"
        ),
        "test_x.py": "def test_x():\n    assert True\n",
    })
    item = _only(tests, "test_x")
    assert item["classification"] == "direct_sqlite"
    assert item["autouse_fixtures"] == ["install"]
    names = {dep["name"] for dep in item["dependencies"]}
    assert {"install", "custom_db", "make_db"} <= names


def test_nearer_fixture_shadows_conftest_autouse(tmp_path: Path):
    tests = _classify(tmp_path, {
        "conftest.py": (
            "import pytest\n"
            "import sqlite3\n"
            "@pytest.fixture(autouse=True)\n"
            "def install():\n"
            "    return sqlite3.connect(':memory:')\n"
        ),
        "test_shadow.py": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def install():\n"
            "    return object()\n"
            "def test_shadow():\n"
            "    assert True\n"
        ),
    })
    item = _only(tests, "test_shadow")
    assert item["classification"] == "pure_no_db"
    assert item["autouse_fixtures"] == ["install"]
    assert "shadows_autouse" in item["flags"]


def test_class_fixture_shadows_module_autouse(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_class_shadow.py": (
            "import pytest\n"
            "import sqlite3\n"
            "@pytest.fixture(autouse=True)\n"
            "def install():\n"
            "    return sqlite3.connect(':memory:')\n"
            "class TestThing:\n"
            "    @pytest.fixture\n"
            "    def install(self):\n"
            "        return object()\n"
            "    def test_a(self):\n"
            "        assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "pure_no_db"
    assert item["autouse_fixtures"] == ["install"]
    assert "shadows_autouse" in item["flags"]


def test_real_m5_module_autouse_sqlite_is_visible():
    """Static check only: the real m5 autouse SQLite fixture must be evidence."""
    inv.ROOT = ROOT
    inv.TESTS = ROOT / "backend" / "tests"
    tests, errors, _counts = inv.analyze()
    assert errors == []
    m5 = [item for item in tests if item["file"].endswith("test_m5_controlled_evolution.py")]
    assert len(m5) == 9
    for item in m5:
        assert item["classification"] == "direct_sqlite"
        assert "memory_only_database" in item["autouse_fixtures"]
        assert any(dep["name"] == "memory_only_database" for dep in item["dependencies"])


def test_referenced_fixture_recurses_into_conftest_fixture(tmp_path: Path):
    tests = _classify(tmp_path, {
        "conftest.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "@pytest.fixture\n"
            "def store():\n"
            "    return Database(':memory:')\n"
        ),
        "test_relay.py": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def relay(store):\n"
            "    return store\n"
            "def test_one(relay):\n"
            "    assert relay\n"
        ),
    })
    item = _only(tests, "test_one")
    assert item["classification"] == "direct_sqlite"
    names = {dep["name"] for dep in item["dependencies"]}
    assert {"relay", "store"} <= names


def test_conftest_autouse_arg_uses_module_override(tmp_path: Path):
    tests = _classify(tmp_path, {
        "conftest.py": (
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def install(dep):\n"
            "    yield\n"
            "@pytest.fixture\n"
            "def dep():\n"
            "    return object()\n"
        ),
        "test_override.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "@pytest.fixture\n"
            "def dep():\n"
            "    return Database(':memory:')\n"
            "def test_a():\n"
            "    assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "direct_sqlite"
    assert "install" in item["autouse_fixtures"]
    assert "dep" in {dep["name"] for dep in item["dependencies"]}


def test_module_override_wins_over_conftest_fixture_for_autouse_arg(tmp_path: Path):
    tests = _classify(tmp_path, {
        "conftest.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "@pytest.fixture(autouse=True)\n"
            "def install(dep):\n"
            "    yield\n"
            "@pytest.fixture\n"
            "def dep():\n"
            "    return Database(':memory:')\n"
        ),
        "test_override.py": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def dep():\n"
            "    return object()\n"
            "def test_a():\n"
            "    assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "pure_no_db"
    assert "dep" in {dep["name"] for dep in item["dependencies"]}


def test_parent_autouse_name_executes_closer_module_override(tmp_path: Path):
    tests = _classify(tmp_path, {
        "conftest.py": (
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def install():\n"
            "    yield\n"
        ),
        "test_mod_override.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "@pytest.fixture\n"
            "def install():\n"
            "    return Database(':memory:')\n"
            "def test_a():\n"
            "    assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "direct_sqlite"
    assert "install" in item["autouse_fixtures"]
    assert "install" in {dep["name"] for dep in item["dependencies"]}


def test_module_autouse_name_executes_closer_class_override(tmp_path: Path):
    tests = _classify(tmp_path, {
        "test_class_override.py": (
            "import pytest\n"
            "from app.db import Database\n"
            "@pytest.fixture(autouse=True)\n"
            "def install():\n"
            "    yield\n"
            "class TestThing:\n"
            "    @pytest.fixture\n"
            "    def install(self):\n"
            "        return Database(':memory:')\n"
            "    def test_a(self):\n"
            "        assert True\n"
        ),
    })
    item = _only(tests, "test_a")
    assert item["classification"] == "direct_sqlite"
    assert "install" in item["autouse_fixtures"]
    assert "install" in {dep["name"] for dep in item["dependencies"]}
