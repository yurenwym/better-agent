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

