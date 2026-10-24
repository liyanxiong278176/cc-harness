"""Reconcile the captured startup using live process and HTTP evidence, never replay it."""
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

import psutil

from cc_harness.durable_runtime import DurableRuntimeClient


async def main():
    root = Path('D:/ai八股/admin-system').resolve()
    run_id = 'ad686df6-16e7-4acf-9afa-8ef325e6e13c'
    client = await DurableRuntimeClient.create(root)
    try:
        projection = await client.store.load_projection(run_id)
        actions = [a for a in projection.actions if a.status.value == 'outcome_unknown']
        assert len(actions) == 1
        action = actions[0]
        raw_args = client.store.artifacts.read_text(action.arguments_artifact)
        args = json.loads(raw_args)
        if action.action_id == 'call_00_34AQrVfOQwqLZlrry8qQ5375':
            assert args['command'] == '$env:JAVA_HOME="D:\\jdk17\\jdk-17.0.13+11"; Start-Process -FilePath "D:\\jdk17\\jdk-17.0.13+11\\bin\\java.exe" -ArgumentList \'-jar\',\'backend\\target\\admin-system-backend-0.1.0-SNAPSHOT.jar\' -RedirectStandardOutput \'logs\\backend-run.log\' -RedirectStandardError \'logs\\backend-run.err.log\' -PassThru | Select-Object Id'
            log_path = root / 'logs/backend-run.log'
        else:
            known = {
                'call_01_r0kHlSyaVm1xMXi7S0Lq1255': '3d0fdcb126b935980a74a537bb39cc0a310624b6dfe14417180f62a8ace27961',
                'call_00_K8kItycwWByjDbD8z2k33371': 'ef79ec14a8a5d12b87444630f8699da0a45e9cf8037fd266618e6886e009c2a6',
            }
            assert action.action_id in known
            assert hashlib.sha256(raw_args.encode()).hexdigest() == known[action.action_id]
            log_path = root / 'logs/backend.out.log'
        assert await client.store.current_lease(run_id) is None
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd', 'create_time']):
            try:
                if proc.info['name'].lower() == 'java.exe' and 'admin-system-backend-0.1.0-SNAPSHOT.jar' in ' '.join(proc.info['cmdline'] or []):
                    assert Path(proc.info['cwd']).resolve() == root
                    processes.append(proc.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        assert len(processes) == 1
        pid = processes[0]['pid']
        assert any(c.status == 'LISTEN' and c.laddr.port == 8082 for c in psutil.Process(pid).net_connections(kind='tcp'))
        with urlopen('http://127.0.0.1:8082/api/health', timeout=5) as response:
            assert response.status == 200
            health = json.load(response)
        log = log_path.read_bytes()
        assert b'Started AdminSystemApplication' in log
        evidence = {'action_id': action.action_id, 'arguments_artifact': action.arguments_artifact,
                    'processes': processes, 'health': health, 'port': 8082,
                    'log_sha256': hashlib.sha256(log).hexdigest(),
                    'conclusion': 'The startup effect succeeded: one matching Java process in the intended directory owns 8082, health is HTTP 200 and the log confirms startup. Shell exit status remains unknown; startup was not replayed.'}
        artifact = client.store.artifacts.put_text(json.dumps(evidence), media_type='application/json')
        await client.coordinator.reconcile_action(run_id, action.action_id, 'succeeded', result_artifact=artifact.digest, reason=evidence['conclusion'])
        print(json.dumps({'reconciled': True, 'pid': pid, 'health': health, 'evidence_artifact': artifact.digest}))
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
