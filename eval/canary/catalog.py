"""Frozen deterministic task catalog for cc-harness and Claude Code canaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from eval.core import (
    CapabilityDomain,
    EvalStore,
    GraderContract,
    GraderType,
    ResourceBudget,
    RiskLevel,
    StateProfile,
    TaskContract,
    canonical_json_bytes,
)

from .models import CanaryInstruction, ProtectedFile

_PROMPT = (
    "Fix the implementation in solution.py so all tests pass. "
    "Do not modify tests. Keep the change focused and preserve existing behavior."
)
_GRADER_IMPLEMENTATION = "eval.canary.adapter:HarnessCanaryAdapter"


@dataclass(frozen=True)
class CanaryFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class CanaryDefinition:
    task_id: str
    title: str
    risk: RiskLevel
    domains: tuple[CapabilityDomain, ...]
    files: tuple[CanaryFile, ...]
    task_version: str = "1.1.0"
    state_profile: StateProfile = StateProfile.CLEAN_CODING
    wall_time_seconds: int = 180
    prompt: str = _PROMPT
    test_targets: tuple[str, ...] = ("test_solution.py",)
    protected_paths: tuple[str, ...] = ("test_solution.py",)
    outcome_paths: tuple[str, ...] = ("solution.py",)
    max_model_calls: int = 20
    max_input_tokens: int = 200_000
    max_output_tokens: int = 20_000


def _files(solution: str, tests: str, *, crlf_solution: bool = False) -> tuple[CanaryFile, ...]:
    solution_bytes = solution.encode("utf-8")
    if crlf_solution:
        solution_bytes = solution_bytes.replace(b"\n", b"\r\n")
    return (
        CanaryFile("solution.py", solution_bytes),
        CanaryFile("test_solution.py", tests.encode("utf-8")),
    )


CANARY_DEFINITIONS = (
    CanaryDefinition(
        "canary.add",
        "Repair a basic arithmetic implementation",
        RiskLevel.LOW,
        (CapabilityDomain.CODING_OUTCOME,),
        _files(
            "def add(a, b):\n    return a - b\n",
            "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
            "    assert add(-2, 2) == 0\n",
        ),
    ),
    CanaryDefinition(
        "canary.clamp",
        "Repair nested boundary logic",
        RiskLevel.MEDIUM,
        (CapabilityDomain.CODING_OUTCOME,),
        _files(
            "def clamp(value, low, high):\n    return min(low, max(high, value))\n",
            "from solution import clamp\n\ndef test_clamp():\n"
            "    assert clamp(5, 0, 10) == 5\n    assert clamp(-1, 0, 10) == 0\n"
            "    assert clamp(11, 0, 10) == 10\n",
        ),
    ),
    CanaryDefinition(
        "canary.parse-bool",
        "Repair normalized boolean parsing",
        RiskLevel.MEDIUM,
        (CapabilityDomain.CODING_OUTCOME, CapabilityDomain.AGENT_LOOP),
        _files(
            "def parse_bool(value):\n    return value == 'true'\n",
            "import pytest\nfrom solution import parse_bool\n\ndef test_parse_bool():\n"
            "    assert parse_bool('true') is True\n    assert parse_bool('YES') is True\n"
            "    assert parse_bool('false') is False\n    assert parse_bool('0') is False\n"
            "    with pytest.raises(ValueError):\n        parse_bool('maybe')\n",
        ),
    ),
    CanaryDefinition(
        "canary.stable-unique",
        "Preserve order while removing duplicates",
        RiskLevel.MEDIUM,
        (CapabilityDomain.CODING_OUTCOME,),
        _files(
            "def stable_unique(values):\n    return list(set(values))\n",
            "from solution import stable_unique\n\ndef test_stable_unique():\n"
            "    assert stable_unique([3, 1, 3, 2, 1]) == [3, 1, 2]\n"
            "    assert stable_unique([]) == []\n",
        ),
    ),
    CanaryDefinition(
        "canary.python-extension",
        "Recognize Python extensions case-insensitively",
        RiskLevel.LOW,
        (CapabilityDomain.CODING_OUTCOME, CapabilityDomain.TOOLS_AND_PROTOCOLS),
        _files(
            "def is_python_file(path):\n    return str(path).endswith('.py')\n",
            "from solution import is_python_file\n\ndef test_extension():\n"
            "    assert is_python_file('a.py')\n    assert is_python_file('A.PY')\n"
            "    assert not is_python_file('a.pyc')\n",
        ),
    ),
    CanaryDefinition(
        "canary.safe-divide",
        "Handle a recoverable division edge case",
        RiskLevel.MEDIUM,
        (CapabilityDomain.CODING_OUTCOME, CapabilityDomain.RELIABILITY_AND_RECOVERY),
        _files(
            "def safe_divide(a, b):\n    return a / b\n",
            "from solution import safe_divide\n\ndef test_safe_divide():\n"
            "    assert safe_divide(8, 2) == 4\n    assert safe_divide(8, 0) is None\n",
        ),
    ),
    CanaryDefinition(
        "canary.deep-get",
        "Traverse a nested mapping with a default",
        RiskLevel.MEDIUM,
        (CapabilityDomain.CODING_OUTCOME, CapabilityDomain.CONTEXT_MANAGEMENT),
        _files(
            "def deep_get(data, path, default=None):\n    return data.get(path, default)\n",
            "from solution import deep_get\n\ndef test_deep_get():\n"
            "    data = {'user': {'profile': {'name': 'Ada'}}}\n"
            "    assert deep_get(data, 'user.profile.name') == 'Ada'\n"
            "    assert deep_get(data, 'user.missing', 'x') == 'x'\n"
            "    assert deep_get({}, '', 'x') == 'x'\n",
        ),
    ),
    CanaryDefinition(
        "canary.chunks",
        "Validate chunk size before iteration",
        RiskLevel.HIGH,
        (CapabilityDomain.CODING_OUTCOME, CapabilityDomain.AGENT_LOOP),
        _files(
            "def chunks(values, size):\n"
            "    return [values[i:i + size] for i in range(0, len(values), size)]\n",
            "import pytest\nfrom solution import chunks\n\ndef test_chunks():\n"
            "    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n"
            "    assert chunks([], 2) == []\n    with pytest.raises(ValueError):\n"
            "        chunks([1], 0)\n",
        ),
    ),
    CanaryDefinition(
        "canary.merge-settings",
        "Merge settings without mutating the input",
        RiskLevel.HIGH,
        (CapabilityDomain.CODING_OUTCOME, CapabilityDomain.MEMORY),
        _files(
            "def merge_settings(base, override):\n    base.update(override)\n    return base\n",
            "from solution import merge_settings\n\ndef test_merge_settings():\n"
            "    base = {'timeout': 10, 'retries': 2}\n"
            "    override = {'timeout': 20}\n"
            "    assert merge_settings(base, override) == {'timeout': 20, 'retries': 2}\n"
            "    assert base == {'timeout': 10, 'retries': 2}\n",
        ),
    ),
    CanaryDefinition(
        "canary.retry-delays-crlf",
        "Repair exponential retry delays in a CRLF file",
        RiskLevel.CRITICAL,
        (
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        ),
        _files(
            "def retry_delays(attempts, base):\n    return [base ** i for i in range(attempts)]\n",
            "import pytest\nfrom solution import retry_delays\n\ndef test_retry_delays():\n"
            "    assert retry_delays(4, 0.5) == [0.5, 1.0, 2.0, 4.0]\n"
            "    assert retry_delays(0, 1) == []\n    with pytest.raises(ValueError):\n"
            "        retry_delays(-1, 1)\n",
            crlf_solution=True,
        ),
        state_profile=StateProfile.RECOVERY,
    ),
)


async def install_canary_contracts(
    store: EvalStore,
    *,
    definitions: tuple[CanaryDefinition, ...] = CANARY_DEFINITIONS,
    suite_id: str = "harness-canary",
    suite_version: str = "1.1.0",
) -> tuple[TaskContract, ...]:
    """Persist frozen fixtures and instructions and return catalog contracts."""

    contracts: list[TaskContract] = []
    for definition in definitions:
        files = {item.path: item.content for item in definition.files}
        initial_state_ref = await store.put_artifact(
            _deterministic_zip(definition.files),
            "application/zip",
        )
        protected = tuple(
            ProtectedFile(
                path=path,
                digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            )
            for path, content in files.items()
            if path in definition.protected_paths
        )
        instruction = CanaryInstruction(
            contract_id=definition.task_id,
            prompt=definition.prompt,
            test_targets=definition.test_targets,
            protected_files=protected,
            outcome_paths=definition.outcome_paths,
        )
        instruction_ref = await store.put_artifact(
            canonical_json_bytes(instruction),
            "application/vnd.cc-harness.canary-instruction+json",
        )
        contracts.append(
            TaskContract(
                task_id=definition.task_id,
                task_version=definition.task_version,
                suite_id=suite_id,
                suite_version=suite_version,
                title=definition.title,
                risk=definition.risk,
                state_profile=definition.state_profile,
                domains=definition.domains,
                instruction_ref=instruction_ref,
                initial_state_ref=initial_state_ref,
                budget=ResourceBudget(
                    wall_time_seconds=definition.wall_time_seconds,
                    max_steps=100,
                    max_model_calls=definition.max_model_calls,
                    max_tool_calls=100,
                    max_input_tokens=definition.max_input_tokens,
                    max_output_tokens=definition.max_output_tokens,
                    max_cost_microusd=1_000_000,
                ),
                graders=(
                    GraderContract(
                        grader_id="pytest-exit-and-integrity",
                        grader_type=GraderType.DETERMINISTIC,
                        implementation=_GRADER_IMPLEMENTATION,
                        version="1.0.0",
                        domains=definition.domains,
                        veto=definition.risk in {RiskLevel.CRITICAL, RiskLevel.HIGH},
                    ),
                ),
                tags=("canary", "deterministic", "same-model"),
            )
        )
    return tuple(contracts)


def _deterministic_zip(files: tuple[CanaryFile, ...]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for item in sorted(files, key=lambda value: value.path):
            info = ZipInfo(item.path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, item.content)
    return buffer.getvalue()
