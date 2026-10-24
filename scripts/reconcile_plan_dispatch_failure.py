"""Reconcile only the verified pre-handler task_plan dispatch defect.

Does not execute the intended plan, touch project files, or resume the task.
Requires --apply to commit the failed outcome with an audit artifact.
"""
import argparse
import asyncio
import inspect
import json
from pathlib import Path

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.execution_tools import task_plan
from cc_harness.run_model import PlanGraph, PlanNode


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project')
    parser.add_argument('run_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    client = await DurableRuntimeClient.create(Path(args.project))
    try:
        projection = await client.store.load_projection(args.run_id)
        unknown = [action for action in projection.actions if action.status.value == 'outcome_unknown']
        if len(unknown) != 1:
            raise ValueError('Expected exactly one uncertain action; no reconciliation performed')
        action = unknown[0]
        if action.tool_name != 'task_plan' or action.error_kind != 'TypeError' or action.effect_class != 'read_only':
            raise ValueError('This is not the known read-only planning dispatch failure')
        arguments = json.loads(client.store.artifacts.read_text(action.arguments_artifact))
        PlanGraph(nodes=tuple(PlanNode.from_dict(node) for node in arguments['nodes'])).validate()
        try:
            inspect.signature(task_plan).bind(arguments, cwd=str(client.cwd), coordinator=client.coordinator)
        except TypeError as error:
            binding_error = str(error)
            if 'run_id' not in binding_error:
                raise
        else:
            raise ValueError('The missing run ID binding error was not reproduced')
        events = (await client.store.read(args.run_id, limit=100000)).events
        started = next(event for event in events if event.event_type == 'ActionStarted' and event.payload.get('action_id') == action.action_id and event.payload.get('attempt') == action.attempt)
        ended = next(event for event in events if event.event_type == 'ActionOutcomeUnknown' and event.payload.get('action_id') == action.action_id and event.sequence > started.sequence)
        mutations = [event.event_type for event in events if started.sequence < event.sequence < ended.sequence and event.event_type in {'PlanCreated', 'PlanRevised', 'TodoCreated', 'TodoUpdated', 'TodoCompleted'}]
        if mutations:
            raise ValueError('Planning state changed during the uncertain call; manual inspection required')
        evidence = {'defect': 'task_plan_dispatch_missing_run_id', 'action_id': action.action_id, 'attempt': action.attempt, 'argument_digest': action.arguments_artifact, 'binding_error': binding_error, 'observed_error_kind': action.error_kind, 'planning_mutations_during_action': mutations, 'plan_digest_after_failure': projection.plan.digest, 'conclusion': 'Handler binding failed before plan execution; intended plan was not applied. The outcome is failed, not successful.'}
        print(json.dumps(evidence, ensure_ascii=True), flush=True)
        if args.apply:
            artifact = client.store.artifacts.put_text(json.dumps(evidence, ensure_ascii=False), media_type='application/json; purpose=verified-dispatch-reconciliation')
            await client.coordinator.reconcile_action(args.run_id, action.action_id, 'failed', result_artifact=artifact.digest, error_kind='task_control_missing_run_id', reason='Verified missing run_id argument binding before task_plan handler; no plan or todo mutation occurred')
            print(json.dumps({'reconciled': True, 'status': 'failed', 'evidence_artifact': artifact.digest}), flush=True)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
