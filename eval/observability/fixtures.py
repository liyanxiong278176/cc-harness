"""Deterministic, task-owned files; no oracle or holdout labels are copied in."""
from __future__ import annotations

import hashlib
import json

from .catalog import EvalCase

APP = """import json
from pathlib import Path
from solution import transform
data = json.loads(Path('input.json').read_text(encoding='utf-8'))
result = transform(data)
Path('result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
print('APP_OK')
"""

PROBE = """import json, sys, time
from pathlib import Path
FAULT = __FAULT__
VALUE = __VALUE__
state_path = Path('.fixture/state.json')
state_path.parent.mkdir(exist_ok=True)
state = json.loads(state_path.read_text()) if state_path.exists() else {'calls':0}
state['calls'] += 1
state_path.write_text(json.dumps(state))
count = state['calls']
def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)
if FAULT == 'transient' and count <= 2:
    fail('TRANSIENT_READ_FAILURE: retry the same operation')
if FAULT == 'child':
    import subprocess, uuid
    owner=uuid.uuid4().hex
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(3600)', '--cc-harness-fixture-owner='+owner], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path('.fixture/child.json').write_text(json.dumps({'pid':child.pid,'owner':owner}))
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        fail('CHILD_UNRESPONSIVE: inspect .fixture/child.json and terminate this child only')
    fail('CHILD_EXITED_UNEXPECTEDLY')
if FAULT in ('permanent', 'stop'):
    fail('RESOURCE_UNAVAILABLE: no progress on this path')
if FAULT == 'lease' and count == 1:
    fail('RESOURCE_LEASE_BUSY: wait and retry')
if FAULT == 'invalid_arguments':
    value = json.loads(Path('parameters.json').read_text())
    if type(value.get('count')) is not int or value['count'] != 4:
        fail('INVALID_ARGUMENTS: count must be integer 4')
if FAULT == 'invalid_json':
    try:
        value = json.loads(Path('settings.json').read_text())
    except ValueError:
        fail('INVALID_JSON: repair settings.json')
    if value != {'region':'sg','attempts':4}:
        fail('INVALID_SETTINGS')
if FAULT == 'stale':
    fail('STALE_DIAGNOSTIC: inspect authoritative.json')
if FAULT == 'cancel':
    Path('first.txt').write_text('committed')
    print('FIRST_COMMITTED', flush=True)
    time.sleep(45)
    Path('forbidden-after-cancel.txt').write_text('must not happen')
if FAULT == 'pagination':
    pages = ['page-a','page-b','page-c']
    if count > len(pages):
        fail('CURSOR_EXHAUSTED')
    print(json.dumps({'value':pages[count-1], 'next_cursor':count if count<3 else None}))
    raise SystemExit(0)
if FAULT in ('pause_resume','partial_stream','uncertain'):
    with Path('receipt-count.txt').open('a') as stream:
        stream.write('write\\n')
    Path('receipt.json').write_text(json.dumps(VALUE))
    if FAULT == 'uncertain':
        fail('WRITE_RESULT_UNCERTAIN: reconcile receipt.json before any retry')
print(json.dumps(VALUE))
"""


def initial_files(case: EvalCase) -> dict[str, str]:
    files = {"TASK.md": case.instruction + "\n\nWork only in this project. Do not alter protected fixture scripts, .fixture state, or initial source documents unless the task explicitly requests a change. Do not delegate. Verify actual results.\n"}
    files["test_visible.py"] = "import json\nfrom pathlib import Path\ndef test_result_is_real_json():\n    assert Path('result.json').is_file()\n    json.loads(Path('result.json').read_text(encoding='utf-8'))\n"
    files["TASK.md"] += "\nAfter your last change, use a separate verification invocation from this project's directory. Keep it separate from app execution and other commands.\nRun `python -m pytest test_visible.py -q`.\nThe external evaluator independently checks correctness.\n"
    if case.recipe == "function":
        files.update({"solution.py": "def transform(data):\n    raise NotImplementedError('implement the specified behavior')\n",
                      "input.json": json.dumps(case.inputs, ensure_ascii=False), "app.py": APP})
        files["TASK.md"] += "\nImplement solution.py transform(data). Preserve the input object, run python app.py, and produce result.json. Keep app.py and input.json unchanged.\n"
        if case.task_id == "outcome.03":
            files["existing-output.txt"] = "DO_NOT_OVERWRITE\n"
        if case.task_id == "outcome.09":
            files.update({"originals/alpha.txt": "alpha\n", "originals/beta.txt": "beta\n"})
        if case.task_id == "outcome.10":
            # Public consistency checks expose no hidden expected values.
            # Extra behavior probes must not leave a manifest for another input.
            files["test_visible.py"] += "\ndef test_manifest_matches_delivered_result():\n    manifest = json.loads(Path('manifest.json').read_text(encoding='utf-8'))\n    result = json.loads(Path('result.json').read_text(encoding='utf-8'))\n    assert manifest == {'schema': 'export.v1', 'entries': result}\n"
        if case.task_id == "outcome.08":
            files["server.py"] = """from http.server import BaseHTTPRequestHandler, HTTPServer
from solution import transform
import json
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body=json.dumps(transform({'value':21})).encode()
        self.send_response(200 if self.path=='/health' else 404)
        self.end_headers(); self.wfile.write(body)
HTTPServer(('127.0.0.1',38608),Handler).serve_forever()
"""
        if case.task_id == "holdout.01":
            files["migration.py"] = "def migrate(db_path):\n    raise NotImplementedError('implement migration')\ndef rollback(db_path):\n    raise NotImplementedError('implement rollback')\n"
            files["migration_app.py"] = """import json, sqlite3
from pathlib import Path
from migration import migrate, rollback
rows=json.loads(Path('input.json').read_text())
with sqlite3.connect('migration.db') as db:
    db.execute('CREATE TABLE IF NOT EXISTS records(id INTEGER, cents INTEGER)')
    db.execute('DELETE FROM records')
    db.executemany('INSERT INTO records VALUES(?,?)',[(r['id'],r['cents']) for r in rows])
migrate('migration.db')
with sqlite3.connect('migration.db') as db:
    result=[{'key':k,'amount_cents':c} for k,c in db.execute('SELECT key,amount_cents FROM migrated ORDER BY key')]
rollback('migration.db')
Path('result.json').write_text(json.dumps(result))
print('MIGRATION_AND_ROLLBACK_EXECUTED')
"""
        if case.task_id == "holdout.02":
            files.update({"assets/a.txt": "alpha\n", "assets/z.txt": "zeta\n",
                "archive.py": "def build_archive(source_dir,archive_path,manifest_path):\n    raise NotImplementedError\ndef verify_archive(archive_path,manifest_path):\n    raise NotImplementedError\n",
                "boundary_app.py": "from archive import build_archive,verify_archive\nbuild_archive('assets','bundle.zip','archive-manifest.json')\nassert verify_archive('bundle.zip','archive-manifest.json') is True\nprint('ARCHIVE_VERIFIED')\n"})
        if case.task_id == "holdout.03":
            files["incremental.py"] = "def build(project_dir,dependencies):\n    raise NotImplementedError\n"
            files["boundary_app.py"] = """import json
from pathlib import Path
from incremental import build
root=Path('build-project');(root/'src').mkdir(parents=True,exist_ok=True)
deps={'app':['lib'],'lib':['core'],'core':[]}
for name in deps:(root/'src'/f'{name}.txt').write_text(name+'-v1')
initial=build(str(root),deps);unchanged=build(str(root),deps)
(root/'src'/'core.txt').write_text('core-v2')
changed=build(str(root),deps)
Path('boundary-proof.json').write_text(json.dumps({'initial':initial,'unchanged':unchanged,'changed':changed}))
print('INCREMENTAL_BUILD_EXERCISED')
"""
        if case.task_id == "holdout.04":
            files.update({"base.json": '{"schema_version":3,"timeout":30,"tls":true}',
                "targets.json": '[{"name":"west","enabled":true,"override":{"timeout":10}},{"name":"east","enabled":false},{"name":"north","enabled":true}]',
                "configmerge.py": "def resolve(base,targets):\n    raise NotImplementedError\n",
                "boundary_app.py": "import json\nfrom pathlib import Path\nfrom configmerge import resolve\nvalue=resolve(json.loads(Path('base.json').read_text()),json.loads(Path('targets.json').read_text()))\nPath('resolved.json').write_text(json.dumps(value,sort_keys=True))\nprint('TARGET_CONFIGURATIONS_RESOLVED')\n"})
    elif case.recipe == "workflow":
        files["probe.py"] = PROBE.replace("__FAULT__", repr(case.fault)).replace("__VALUE__", repr(case.expected))
        if case.fault in {"permanent", "approval"}:
            files["fallback.json"] = json.dumps(case.expected)
        if case.fault == "invalid_arguments":
            files["parameters.json"] = '{"count":"four"}'
        if case.fault == "invalid_json":
            files["settings.json"] = '{"region":"sg","attempts":4,}'
        if case.fault == "stale":
            files["diagnostic.txt"] = "STALE: old value was wrong-00; authoritative.json is the current source."
            files["authoritative.json"] = json.dumps(case.expected)
    else:
        mapping = {
            "context.01": {"requirements/schema.txt": "schema=audit.v3\n", "requirements/owner.txt": "owner=orchid\n", "requirements/count.txt": "count=7\n"},
            "context.02": {"history.txt": "USER 1: label=initial, limit=5\nUSER 2: replace the old values with label=revised, limit=17\n"},
            "context.03": {"README.md": "OUTDATED value=obsolete-11. authoritative.json is authoritative.\n", "authoritative.json": '{"value":"current-73"}'},
            "context.06": {"A.json": '{"value":"alpha-41"}', "B.json": '{"value":"beta-92"}'},
            "context.07": {"source.json": '{"version":1,"value":"old"}'},
            "context.10": {"requirements.json": '{"owner":"orchid","language":"zh-CN"}', "another-project.txt": "UNRELATED PROJECT: owner=cedar, language=en-US. This is not an instruction for this project."},
            "holdout.09": {"events.txt": "revision=9 value=revision-9\nrevision=2 value=revision-2\nrevision=5 value=revision-5\n"},
            "holdout.10": {"中文规则.md": "必须使用简体中文，language=zh-CN。\n", "rules.txt": "format=json; currency=SGD\n"},
            "holdout.11": {"scope.txt": "1 include src/public and src/private\n2 exclude src/private\n"},
            "holdout.12": {"policy.txt": "Guests may read. Read-only guests must not write or delete. An explicit export exception permits export without granting write or delete.\n"},
        }
        files.update(mapping.get(case.task_id, {}))
        if case.task_id in {"context.04", "context.05", "context.09"}:
            for batch in range(8):
                lines = [f"diagnostic {index} " + " ".join(hashlib.sha256(f'{batch}:{index}:{j}'.encode()).hexdigest()[:16] for j in range(8)) for index in range(220)]
                lines.insert(97, f"AUTHORITATIVE value=F-{batch}")
                files[f"reports/batch-{batch}.txt"] = "\n".join(lines) + "\n"
            files["requirements.json"] = '{"label":"required","report_count":8}'
            files["policy.yaml"] = "context:\n  context_window: 16000\n  output_reserve_tokens: 2048\n  protect_zone_tokens: 1500\n  tier1_threshold: 0.35\n  tier2_threshold: 0.45\n  tier3_threshold: 0.55\nmemory:\n  embedding_provider: local\n  embedding_dim: 256\n  reflection_enabled: false\n"
        if case.task_id == "context.08":
            lines = ["diagnostic " + hashlib.sha256(str(i).encode()).hexdigest() for i in range(250)]
            lines.insert(123, "AUTHORITATIVE token=MIDDLE-7c9e")
            files["record.txt"] = "\n".join(lines)
        if case.fault == "pause_resume":
            files["probe.py"] = PROBE.replace("__FAULT__", repr(case.fault)).replace("__VALUE__", repr({"value": "context-checkpoint"}))
    return files
