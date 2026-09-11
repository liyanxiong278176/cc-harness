"""Host dependency and frozen artifact guard."""

from __future__ import annotations

import importlib.util
import platform
import shutil

from .base import GuardContext, GuardResult, RecoveryResult


class DependencyGuard:
    name = "dependency"

    def pre_check(self, context: GuardContext) -> GuardResult:
        del context
        requirements = {
            "python_311_plus": tuple(int(item) for item in platform.python_version_tuple()[:2]) >= (3, 11),
            "uv": shutil.which("uv") is not None,
            "uvx": shutil.which("uvx") is not None,
            "docker": shutil.which("docker") is not None,
            "cc_harness_import": importlib.util.find_spec("cc_harness") is not None,
        }
        missing = [name for name, ready in requirements.items() if not ready]
        return GuardResult(self.name, "pre_check", ready=not missing, blocking=True, warnings=[f"missing dependency: {name}" for name in missing], details={"requirements": requirements})

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"frozen_artifact_mutation": False})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"manual_action_required": True, "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True}
