"""Positive and negative controls for the independent mechanism oracles."""
from pathlib import Path

import pytest

from eval.observability.boundary_oracles import program
from eval.observability.catalog import cases, materialize
from eval.observability.grader import _verify_process, grade_trajectory
from eval.observability.grader import _MIGRATION_ORACLE


IMPLEMENTATIONS = {
    "holdout.02": ("archive.py", '''import hashlib,json,zipfile
from pathlib import Path
def build_archive(source_dir,archive_path,manifest_path):
    root=Path(source_dir);entries={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with zipfile.ZipFile(archive_path,'w') as z:
        for name,value in entries.items():z.writestr(name,value)
    Path(manifest_path).write_text(json.dumps({name:hashlib.sha256(value).hexdigest() for name,value in entries.items()}))
def verify_archive(archive_path,manifest_path):
    try:
        expected=json.loads(Path(manifest_path).read_text())
        with zipfile.ZipFile(archive_path) as z:
            return sorted(z.namelist())==sorted(expected) and all(hashlib.sha256(z.read(name)).hexdigest()==value for name,value in expected.items())
    except Exception:return False
'''),
    "holdout.03": ("incremental.py", '''import hashlib
from pathlib import Path
def build(project_dir,dependencies):
    root=Path(project_dir);(root/'build').mkdir(exist_ok=True);done={};changed=[]
    def visit(name):
        if name in done:return
        for dep in sorted(dependencies[name]):visit(dep)
        value=hashlib.sha256(((root/'src'/f'{name}.txt').read_text()+''.join(done[d] for d in sorted(dependencies[name]))).encode()).hexdigest()
        path=root/'build'/f'{name}.txt'
        if not path.exists() or path.read_text()!=value:path.write_text(value);changed.append(name)
        done[name]=value
    for name in sorted(dependencies):visit(name)
    return changed
'''),
    "holdout.04": ("configmerge.py", '''def resolve(base,targets):
    result={}
    for target in targets:
        if not target.get('enabled'):continue
        name=target['name'];value={**base,**target.get('override',{})}
        if name in result or value['schema_version']!=base['schema_version'] or value['tls'] is not True or value['timeout']<=0:raise ValueError('inconsistent')
        result[name]=value
    return dict(sorted(result.items()))
'''),
}


@pytest.mark.parametrize("task_id", tuple(IMPLEMENTATIONS))
def test_mechanism_oracle_rejects_stub_and_accepts_real_behavior(tmp_path: Path, task_id):
    case = next(c for c in cases("holdout") if c.task_id == task_id)
    materialize(case, tmp_path)
    oracle = program(task_id)
    assert _verify_process(tmp_path, oracle, case.inputs, 1) == {"valid": True, "passed": False, "probes": [False]}
    name, code = IMPLEMENTATIONS[task_id]
    (tmp_path / name).write_text(code, encoding="utf-8")
    result = _verify_process(tmp_path, oracle, case.inputs, 1)
    assert result == {"valid": True, "passed": True, "probes": [True]}


def test_read_ref_text_does_not_count_as_successful_original_retrieval():
    case = next(c for c in cases("golden") if c.task_id == "context.05")
    event = {"event_type": "AssistantMessageCommitted", "sequence": 1, "payload": {"text": "I used read_ref"}}
    assert not any(grade_trajectory(case, [event]).values())
    offload = {"event_type": "ToolObservationCommitted", "sequence": 1, "payload": {"status": "succeeded"},
               "observation": {"metadata": {"offload": {"node_id": "node1", "content_digest": "sha256:source"}}}}
    read = {"event_type": "ToolObservationCommitted", "sequence": 2, "payload": {"status": "succeeded"},
            "observation": {"tool_name": "read_ref", "metadata": {"request_arguments": {"source_ref": "node:node1"}}}}
    assert all(grade_trajectory(case, [offload, read]).values())
    read["payload"]["status"] = "failed"
    assert grade_trajectory(case, [offload, read])["original_read_correlated_to_offload"] is False


def test_migration_oracle_requires_real_migration_and_rollback(tmp_path):
    case = next(c for c in cases("holdout") if c.task_id == "holdout.01")
    materialize(case, tmp_path)
    assert _verify_process(tmp_path, _MIGRATION_ORACLE, case.inputs, 2)["passed"] is False
    (tmp_path / "migration.py").write_text('''import sqlite3
def migrate(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE migrated(key TEXT,amount_cents INTEGER)')
        db.execute('INSERT INTO migrated SELECT CAST(id AS TEXT),cents FROM records')
def rollback(path):
    with sqlite3.connect(path) as db:db.execute('DROP TABLE migrated')
''')
    assert _verify_process(tmp_path, _MIGRATION_ORACLE, case.inputs, 2) == {"valid": True, "passed": True, "probes": [True, True]}
