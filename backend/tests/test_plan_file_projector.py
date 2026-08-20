import hashlib

import pytest


def test_projector_writes_utf8_markdown_and_verifies_hash(tmp_path) -> None:
    from app.plan_files import PlanFileProjector

    document_id = "plan_" + "a" * 32
    projector = PlanFileProjector(tmp_path / "data")
    content = "# 计划\n\n内容\n"

    path = projector.path_for(document_id)
    written_hash = projector.project(document_id, content)

    assert path == tmp_path / "data" / "plans" / document_id / "plan.md"
    assert path.read_bytes() == content.encode("utf-8")
    assert written_hash == "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert projector.read_hash(document_id) == written_hash


def test_projector_rejects_path_escape_and_non_uuid_document_names(tmp_path) -> None:
    from app.plan_files import PlanFileProjector, PlanFileSecurityError

    projector = PlanFileProjector(tmp_path / "data")
    with pytest.raises(PlanFileSecurityError):
        projector.path_for("../outside")
    with pytest.raises(PlanFileSecurityError):
        projector.path_for("plan_not-a-server-id")


def test_projector_does_not_overwrite_when_expected_hash_is_stale(tmp_path) -> None:
    from app.plan_files import PlanFileConflict, PlanFileProjector

    document_id = "plan_" + "b" * 32
    projector = PlanFileProjector(tmp_path / "data")
    projector.project(document_id, "old\n")

    with pytest.raises(PlanFileConflict):
        projector.project(document_id, "new\n", expected_file_hash="sha256:" + "0" * 64)

    assert projector.path_for(document_id).read_text(encoding="utf-8") == "old\n"


def test_projector_rechecks_expected_hash_before_replacing_the_file(tmp_path, monkeypatch) -> None:
    import tempfile

    from app.plan_files import PlanFileConflict, PlanFileProjector

    document_id = "plan_" + "2" * 32
    projector = PlanFileProjector(tmp_path / "data")
    projector.project(document_id, "old\n")
    path = projector.path_for(document_id)
    old_hash = projector.read_hash(document_id)
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def race(*args, **kwargs):
        path.write_text("changed before replace\n", encoding="utf-8", newline="\n")
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr("app.plan_files.tempfile.NamedTemporaryFile", race)
    with pytest.raises(PlanFileConflict, match="changed"):
        projector.project(document_id, "new\n", expected_file_hash=old_hash)

    assert path.read_text(encoding="utf-8") == "changed before replace\n"
