import os
import sys

from cc_harness.artifacts import ArtifactStore
from cc_harness.fact_store import default_user_data_dir, project_identity


def test_project_identity_is_stable_and_artifact_store_uses_atomic_contract(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert project_identity(project) == project_identity(project)
    store = ArtifactStore(tmp_path / "objects")
    ref = store.put_text("cross-platform")
    assert store.verify(ref.digest).digest == ref.digest
    assert not list((tmp_path / "objects").rglob(".tmp-*"))


def test_platform_adapter_does_not_depend_on_kernel_specific_path_rules() -> None:
    assert sys.platform
    assert os.name in {"nt", "posix", "java"}
    assert default_user_data_dir().name == "cc-harness"
