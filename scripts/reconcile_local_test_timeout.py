"""Audit the captured offline Maven timeout without replaying its command.

Only the exact local node syntax-check + offline Maven test command is
accepted. Existing H2 test results must prove failure and no matching process
may remain. --apply records FAILED, preserving the timeout and test evidence.
"""
import argparse
import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import psutil

from cc_harness.durable_runtime import DurableRuntimeClient


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project', type=Path)
    parser.add_argument('run_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    client = await DurableRuntimeClient.create(args.project)
    try:
        projection = await client.store.load_projection(args.run_id)
        unknown = [a for a in projection.actions if a.status.value == 'outcome_unknown']
        if len(unknown) != 1 or unknown[0].tool_name != 'run_command':
            raise ValueError('Expected exactly one uncertain command')
        if await client.store.current_lease(args.run_id) is not None:
            raise ValueError('A worker still owns this task')
        action = unknown[0]
        arguments = json.loads(client.store.artifacts.read_text(action.arguments_artifact))
        backend = client.cwd / 'library-management-system' / 'backend'
        prefix = f'cd "{backend}"; node --check src\\main\\resources\\static\\assets\\app.js; if ($LASTEXITCODE -eq 0) {{ "SYNTAX OK" }}; & "'
        suffix = '" -o -B -q test 2>&1 | Select-Object -Last 40'
        match = re.fullmatch(re.escape(prefix) + r'([^"\r\n]+mvn\.cmd)' + re.escape(suffix), arguments.get('command', ''))
        if match is None or not Path(match[1]).is_file():
            raise ValueError('Command is outside the exact audited offline local-test scope')
        receipt = next(o for o in reversed(projection.observations) if o.action_id == action.action_id and o.attempt == action.attempt)
        observation = json.loads(client.store.artifacts.read_text(receipt.observation_artifact))
        metadata = observation.get('metadata', {})
        if metadata.get('timed_out') is not True or 'SYNTAX OK' not in metadata.get('stdout', ''):
            raise ValueError('Expected committed timeout and completed syntax check')
        test_source = backend / 'src/test/java/com/library/BorrowFlowIntegrationTest.java'
        config = backend / 'src/test/resources/application-test.yml'
        if '@ActiveProfiles("test")' not in test_source.read_text(encoding='utf-8') or 'jdbc:h2:mem:' not in config.read_text(encoding='utf-8'):
            raise ValueError('Local in-memory test isolation is not established')
        report = backend / 'target/surefire-reports/TEST-com.library.BorrowFlowIntegrationTest.xml'
        events = (await client.store.read(args.run_id, limit=100000)).events
        started = next(event for event in events if event.event_type == 'ActionStarted' and event.payload.get('action_id') == action.action_id and event.payload.get('attempt') == action.attempt)
        ended = next(event for event in events if event.event_type == 'ActionOutcomeUnknown' and event.payload.get('action_id') == action.action_id and event.sequence > started.sequence)
        if not datetime.fromisoformat(started.occurred_at).timestamp() - 2 <= report.stat().st_mtime <= datetime.fromisoformat(ended.occurred_at).timestamp() + 180:
            raise ValueError('The test report does not belong to this command execution window')
        suite = ET.parse(report).getroot()
        counts = {name: int(suite.get(name, '0')) for name in ('tests', 'failures', 'errors', 'skipped')}
        if counts['tests'] <= 0 or counts['failures'] + counts['errors'] <= 0:
            raise ValueError('A failing local test receipt is required; success must not be inferred')
        needle = str(backend).casefold()
        for process in psutil.process_iter(['name', 'cmdline', 'cwd']):
            try:
                name = (process.info['name'] or '').casefold()
                if not any(marker in name for marker in ('java', 'mvn', 'node')):
                    continue
                if needle in ' '.join(process.info['cmdline'] or []).casefold() or needle == (process.info['cwd'] or '').casefold():
                    raise ValueError(f'A matching local test process is still alive: {process.pid}')
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        evidence = {
            'action_id': action.action_id, 'attempt': action.attempt,
            'arguments_artifact': action.arguments_artifact,
            'observation_artifact': receipt.observation_artifact,
            'scope': 'node --check and offline Maven tests against an in-memory H2 database',
            'tests': counts,
            'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
            'report_mtime': report.stat().st_mtime,
            'test_source_sha256': hashlib.sha256(test_source.read_bytes()).hexdigest(),
            'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
            'matching_processes_alive': False,
            'conclusion': 'The command timed out and local tests failed. Do not mark successful or repeat the identical command. Inspect and repair test failures; use timeout_seconds for a sufficiently long bounded verification call.',
        }
        print(json.dumps(evidence, ensure_ascii=True))
        if args.apply:
            artifact = client.store.artifacts.put_text(json.dumps(evidence), media_type='application/json; purpose=local-test-timeout-reconciliation')
            await client.coordinator.reconcile_action(args.run_id, action.action_id, 'failed', result_artifact=artifact.digest,
                error_kind='local_test_timeout_with_failure_receipt', reason=evidence['conclusion'])
            print(json.dumps({'reconciled': True, 'status': 'failed', 'evidence_artifact': artifact.digest}))
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
