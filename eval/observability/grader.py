"""Independent result/state oracle; final model prose never establishes success."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
import os
import sqlite3
import re
import zipfile
from typing import Any

from .catalog import EvalCase


def function_probes(case: EvalCase) -> list[dict[str, Any]]:
    probes = [{"input": case.inputs, "expected": case.expected}]
    extra = {
        "outcome.01": [{"input": [{"email": None}, {"email": "  "}], "expected": []}],
        "outcome.02": [{"input": [], "expected": {}}],
        "outcome.03": [{"input": [{"amount_cents": True}], "error": "ValueError"}, {"input": [{"amount_cents": -1}], "error": "ValueError"}],
        "outcome.04": [{"input": {"defaults": {"port": 80}, "override": {"port": "70000"}}, "error": "ValueError"}],
        "outcome.05": [{"input": [], "expected": {"count": 0, "total": 0}}],
        "outcome.06": [{"input": {"value": 0, "low": 0, "high": 10}, "expected": 0}, {"input": {"value": 3, "low": 4, "high": 2}, "error": "ValueError"}],
        "outcome.07": [{"input": [], "expected": []}, {"input": [2, 1, 2], "expected": [2, 1]}],
        "outcome.08": [{"input": {"value": 3}, "expected": {"healthy": True, "value": 6}}],
        "outcome.09": [{"input": [], "expected": []}],
        "outcome.10": [{"input": [], "expected": []}],
        "holdout.01": [{"input": [], "expected": []}],
        "holdout.02": [{"input": ["a", "a"], "error": "ValueError"}],
        "holdout.03": [{"input": {"a": ["b"], "b": ["a"]}, "error": "ValueError"}],
        "holdout.04": [{"input": [{"name": "off", "enabled": False}], "expected": []}],
    }
    return probes + extra.get(case.task_id, [])


_CANDIDATE_PROBE = """import copy, json, os, shutil, sys, tempfile
workspace=tempfile.mkdtemp(dir='/work')
shutil.copyfile('/source/solution.py', os.path.join(workspace, 'solution.py'))
sys.path[:0] = [workspace, '/source']
os.chdir(workspace)
value=json.load(sys.stdin); before=copy.deepcopy(value)
try:
    from solution import transform
    result=transform(value)
except ValueError:
    response={'kind':'ValueError','unchanged':value==before}
except BaseException:
    response={'kind':'execution_error'}
else:
    response={'kind':'returned','value':result,'unchanged':value==before}
print(json.dumps(response))
"""

_FUNCTION_ORACLE = """import json, subprocess, sys
probes=json.load(sys.stdin)
results=[]
for probe in probes:
    try:
        child=subprocess.run([sys.executable,'-I','-c',__CHILD__],
            input=json.dumps(probe['input']),text=True,capture_output=True,
            timeout=5,user=65534,group=65534,extra_groups=[])
        actual=json.loads(child.stdout)
        passed=child.returncode==0 and actual.get('unchanged') is True
        if probe.get('error')=='ValueError':
            passed=passed and actual.get('kind')=='ValueError'
        else:
            passed=passed and actual.get('kind')=='returned' and actual.get('value')==probe['expected']
    except (ValueError,subprocess.SubprocessError):
        passed=False
    results.append(passed)
print(json.dumps({'passed':all(results),'probes':results}))
""".replace("__CHILD__", repr(_CANDIDATE_PROBE))

_MIGRATION_ORACLE = """import json,os,sqlite3,subprocess,sys,tempfile
rows=json.load(sys.stdin)
folder=tempfile.mkdtemp(dir='/work');os.chmod(folder,0o777);path=folder+'/migration.db'
with sqlite3.connect(path) as db:
    db.execute('CREATE TABLE records(id INTEGER,cents INTEGER)')
    db.executemany('INSERT INTO records VALUES(?,?)',[(r['id'],r['cents']) for r in rows])
os.chmod(path,0o666)
results=[]
for operation in ('migrate','rollback'):
    code="import sys;sys.path.insert(0,'/source');from migration import "+operation+";"+operation+"(sys.argv[1])"
    try:
        child=subprocess.run([sys.executable,'-I','-c',code,path],user=65534,group=65534,extra_groups=[],capture_output=True,timeout=5)
        with sqlite3.connect(path) as db:
            source=list(db.execute('SELECT id,cents FROM records ORDER BY id'))
            ok=child.returncode==0 and source==sorted((r['id'],r['cents']) for r in rows)
            if operation=='migrate':
                ok=ok and list(db.execute('SELECT key,amount_cents FROM migrated ORDER BY key'))==sorted((str(r['id']),r['cents']) for r in rows)
            else:
                ok=ok and db.execute("SELECT count(*) FROM sqlite_master WHERE name='migrated'").fetchone()[0]==0
        results.append(bool(ok))
    except Exception:
        results.append(False)
print(json.dumps({'probes':results}))
"""


def verify_function(case: EvalCase, root: Path) -> dict[str, Any]:
    if case.recipe != "function":
        return {"passed": True, "applicable": False}
    verdict = _verify_process(root, _FUNCTION_ORACLE, function_probes(case), len(function_probes(case)))
    if case.task_id == "holdout.01":
        migration = _verify_process(root, _MIGRATION_ORACLE, case.inputs, 2)
        verdict = {"valid": verdict.get("valid", False) and migration.get("valid", False),
                   "passed": verdict["passed"] and migration["passed"], "function": verdict, "migration": migration}
    from .boundary_oracles import program
    boundary = program(case.task_id)
    if boundary:
        actual = _verify_process(root, boundary, case.inputs, 1)
        verdict = {"valid": verdict.get("valid", False) and actual.get("valid", False),
                   "passed": verdict["passed"] and actual["passed"], "function": verdict, "boundary": actual}
    return verdict


def _verify_process(root, program, input_value, count):
    command = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
               "--memory", "256m", "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
               "--security-opt", "no-new-privileges", "--tmpfs", "/work:rw,size=32m",
               "--mount", f"type=bind,source={root.resolve()},target=/source,readonly",
               "python:3.13-slim", "python", "-I", "-c", program]
    try:
        completed = subprocess.run(command, input=json.dumps(input_value), text=True,
                                   capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"passed": False, "valid": False, "reason": type(exc).__name__}
    if completed.returncode:
        return {"passed": False, "valid": completed.returncode not in {125, 126, 127}, "reason": "candidate_or_verifier_process_failed", "exit_code": completed.returncode}
    try:
        verdict = json.loads(completed.stdout)
        probes = verdict.get("probes")
        if not isinstance(probes, list) or len(probes) != count or any(type(value) is not bool for value in probes):
            raise ValueError("verifier did not report every probe")
        return {"valid": True, "passed": all(probes), "probes": probes}
    except (ValueError, IndexError):
        return {"passed": False, "valid": False, "reason": "invalid_verifier_output"}


def grade_files(case: EvalCase, root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    for name, digest in manifest["protected"].items():
        path = root / name
        checks["protected:" + name] = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    if case.fault == "cancel":
        checks["first_effect_preserved"] = (root / "first.txt").is_file()
        checks["no_post_cancel_effect"] = not (root / "forbidden-after-cancel.txt").exists()
    else:
        try:
            actual = json.loads((root / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            actual = None
        checks["actual_result"] = actual == case.expected
    if case.task_id == "outcome.05":
        checks["report_delivered"] = (root / "report.md").is_file() and "COUNT=3 TOTAL=12" in (root / "report.md").read_text(encoding="utf-8")
    if case.task_id == "outcome.10":
        try:
            checks["manifest_delivered"] = json.loads((root / "manifest.json").read_text()) == {"schema": "export.v1", "entries": case.expected}
        except (OSError, ValueError):
            checks["manifest_delivered"] = False
    if case.task_id == "outcome.08":
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:38608/health", timeout=3) as response:
                checks["live_health_endpoint"] = response.status == 200 and json.loads(response.read()) == case.expected
        except (OSError, ValueError):
            checks["live_health_endpoint"] = False
    if case.task_id == "holdout.01":
        try:
            with sqlite3.connect(f"file:{(root / 'migration.db').as_posix()}?mode=ro", uri=True) as db:
                checks["migration_source_preserved"] = list(db.execute("SELECT id,cents FROM records ORDER BY id")) == sorted((r["id"], r["cents"]) for r in case.inputs)
                checks["rollback_removed_target"] = db.execute("SELECT count(*) FROM sqlite_master WHERE name='migrated'").fetchone()[0] == 0
        except (OSError, sqlite3.Error):
            checks["migration_source_preserved"] = checks["rollback_removed_target"] = False
    if case.task_id == "holdout.02":
        try:
            expected = {path.relative_to(root / "assets").as_posix(): path.read_bytes() for path in (root / "assets").rglob("*") if path.is_file()}
            with zipfile.ZipFile(root / "bundle.zip") as archive:
                checks["actual_archive_content"] = sorted(archive.namelist()) == sorted(expected) and all(archive.read(name) == data for name, data in expected.items())
            manifest = json.loads((root / "archive-manifest.json").read_text(encoding="utf-8"))
            checks["actual_archive_manifest"] = manifest == {name: hashlib.sha256(data).hexdigest() for name, data in expected.items()}
        except (OSError, ValueError, zipfile.BadZipFile, KeyError):
            checks["actual_archive_content"] = checks["actual_archive_manifest"] = False
    if case.task_id == "holdout.03":
        try:
            checks["native_incremental_sequence"] = json.loads((root / "boundary-proof.json").read_text(encoding="utf-8")) == {"initial": ["core", "lib", "app"], "unchanged": [], "changed": ["core", "lib", "app"]}
        except (OSError, ValueError):
            checks["native_incremental_sequence"] = False
    if case.task_id == "holdout.04":
        try:
            checks["resolved_configuration_consistency"] = json.loads((root / "resolved.json").read_text(encoding="utf-8")) == {"north": {"schema_version": 3, "timeout": 30, "tls": True}, "west": {"schema_version": 3, "timeout": 10, "tls": True}}
        except (OSError, ValueError):
            checks["resolved_configuration_consistency"] = False
    if case.task_id == "context.07":
        try:
            checks["source_updated"] = json.loads((root / "source.json").read_text()) == case.expected
        except (OSError, ValueError):
            checks["source_updated"] = False
    if case.fault in {"pause_resume", "partial_stream", "uncertain"}:
        path = root / "receipt-count.txt"
        checks["exactly_one_effect"] = path.is_file() and path.read_text().splitlines() == ["write"]
    if case.fault == "approval":
        checks["denied_action_not_replayed"] = not (root / "proposed.txt").exists()
    if case.fault in {"transient", "permanent", "stop", "lease", "pagination"}:
        try:
            calls = json.loads((root / ".fixture/state.json").read_text())["calls"]
            checks["required_probe_attempts"] = (calls >= 3 if case.fault == "transient" else calls == 3 if case.fault == "pagination" else calls >= 2 if case.fault == "lease" else 1 <= calls <= 3)
        except (OSError, ValueError, KeyError, TypeError):
            checks["required_probe_attempts"] = False
    return {"passed": bool(checks) and all(checks.values()), "checks": checks,
            "source": "independent-filesystem-oracle", "task_digest": case.fingerprint}


def grade_trajectory(case: EvalCase, events: list[dict[str, Any]]) -> dict[str, bool]:
    types = {e["event_type"] for e in events}
    result = {"required:" + kind: kind in types for kind in case.required_events}
    if case.fault == "cancel":
        result["cancellation_committed"] = "RunCancelled" in types or "TaskCancelled" in types
    if case.fault == "partial_stream":
        failures = [e.get("sequence", 0) for e in events
                    if e["event_type"] == "ModelInvocationFinished" and e.get("payload", {}).get("status") == "failed"]
        result["transport_fault_exercised"] = bool(failures)
        result["transport_resumed_after_fault"] = bool(failures) and any(
            e["event_type"] == "RunResumed" and e.get("sequence", 0) > min(failures)
            for e in events
        )
    if case.task_id == "context.05":
        offloaded = {}
        for event in events:
            node = event.get("observation", {}).get("metadata", {}).get("offload", {})
            if node.get("node_id") and node.get("content_digest"):
                offloaded[node["node_id"]] = event["sequence"]
        retrieved = set()
        for event in events:
            observation = event.get("observation", {})
            args = observation.get("metadata", {}).get("request_arguments", {})
            node = args.get("node_id") or str(args.get("source_ref", "")).removeprefix("node:")
            if event["event_type"] == "ToolObservationCommitted" and event.get("payload", {}).get("status") == "succeeded" and observation.get("tool_name") == "read_ref" and node in offloaded and event["sequence"] > offloaded[node]:
                retrieved.add(node)
        result["actual_offload_recorded"] = bool(offloaded)
        result["original_read_correlated_to_offload"] = bool(retrieved)
    return result


def owned_child(record: dict) -> bool:
    pid, owner = record.get("pid"), record.get("owner", "")
    if type(pid) is not int or pid <= 0 or not re.fullmatch("[a-f0-9]{32}", owner):
        return False
    if os.name == "nt":
        text = subprocess.run(["powershell", "-NoProfile", "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"], capture_output=True, text=True).stdout
    else:
        try:
            text = Path(f"/proc/{pid}/cmdline").read_bytes().decode().replace("\0", " ")
        except OSError:
            return False
    return "--cc-harness-fixture-owner=" + owner in text and "time.sleep(3600)" in text


def child_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
