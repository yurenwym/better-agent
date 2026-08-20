from __future__ import annotations

import os

import pytest


def test_projector_rejects_a_symlinked_plan_file_even_when_target_stays_inside_root(tmp_path) -> None:
    from app.plan_files import PlanFileProjector, PlanFileSecurityError

    document_id = "plan_" + "d" * 32
    projector = PlanFileProjector(tmp_path / "data")
    real_target = tmp_path / "data" / "plans" / "real.md"
    real_target.write_text("# external\n", encoding="utf-8")
    path = projector.path_for(document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(real_target, path)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(PlanFileSecurityError):
        projector.read_hash(document_id)
    with pytest.raises(PlanFileSecurityError):
        projector.project(document_id, "# replacement\n")


def test_projector_rechecks_the_document_directory_after_creation(tmp_path, monkeypatch) -> None:
    import tempfile
    from pathlib import Path

    from app.plan_files import PlanFileProjector, PlanFileSecurityError

    document_id = "plan_" + "e" * 32
    data_root = tmp_path / "data"
    projector = PlanFileProjector(data_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def race(*args, **kwargs):
        document_dir = Path(kwargs["dir"])
        document_dir.rmdir()
        try:
            os.symlink(outside, document_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable")
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr("app.plan_files.tempfile.NamedTemporaryFile", race)
    with pytest.raises(PlanFileSecurityError):
        projector.project(document_id, "# replacement\n")


def test_projector_rejects_a_plan_file_replaced_by_a_symlink_during_read(tmp_path, monkeypatch) -> None:
    from app.plan_files import PlanFileProjector, PlanFileSecurityError

    document_id = "plan_" + "f" * 32
    projector = PlanFileProjector(tmp_path / "data")
    projector.project(document_id, "# inside\n")
    path = projector.path_for(document_id)
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    original_open = os.open

    def race(candidate, *args, **kwargs):
        if str(candidate) == str(path):
            path.unlink()
            try:
                os.symlink(outside, path)
            except (OSError, NotImplementedError):
                pytest.skip("symlink creation is unavailable")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr("app.plan_files.os.open", race)
    with pytest.raises(PlanFileSecurityError):
        projector.read_text_stable(document_id)


def test_projector_rechecks_the_file_after_open(tmp_path, monkeypatch) -> None:
    from app.plan_files import PlanFileProjector, PlanFileSecurityError

    document_id = "plan_" + "1" * 32
    projector = PlanFileProjector(tmp_path / "data")
    projector.project(document_id, "# inside\n")
    calls = 0

    def link_after_open(_path):
        nonlocal calls
        calls += 1
        return calls >= 5

    monkeypatch.setattr("app.plan_files._is_link_or_reparse", link_after_open)
    with pytest.raises(PlanFileSecurityError):
        projector.read_text_stable(document_id)
