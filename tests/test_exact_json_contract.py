import json

import pytest

from cc_harness.exact_json import literal_json_artifacts
from cc_harness.loop_control import (
    ArtifactRequirement,
    TaskContract,
    artifact_validation_issues,
    completion_contract_from_instruction,
)
from cc_harness.run_model import GoalContract


INSTRUCTION = "Try at most three times, then write result.json as {'blocked':true,'reason':'resource_unavailable'} and explain the limitation."


def test_reject_extra_fields_then_accept_exact_output(tmp_path):
    contract = completion_contract_from_instruction(INSTRUCTION)
    artifact = tmp_path / 'result.json'
    value = {'blocked': True, 'reason': 'resource_unavailable', 'attempts': 2}
    artifact.write_text(json.dumps(value))
    issues = artifact_validation_issues(tmp_path, contract.required_artifacts)
    assert any('exact value' in issue and 'no extra fields' in issue for issue in issues)
    artifact.write_text('{"reason": "resource_unavailable", "blocked": true}')
    assert artifact_validation_issues(tmp_path, contract.required_artifacts) == ()


@pytest.mark.parametrize('text', [
    '{"blocked":1,"reason":"resource_unavailable"}',
    '{"blocked":true,"reason":"wrong"}',
    '{"blocked":false,"blocked":true,"reason":"resource_unavailable"}',
])
def test_wrong_types_values_and_duplicate_keys_do_not_pass(tmp_path, text):
    (tmp_path / 'result.json').write_text(text)
    contract = completion_contract_from_instruction(INSTRUCTION)
    assert artifact_validation_issues(tmp_path, contract.required_artifacts)


def test_contract_survives_serialization():
    task = TaskContract.from_goal(GoalContract(INSTRUCTION, ('deliver',)),
                                 completion=completion_contract_from_instruction(INSTRUCTION))
    assert TaskContract.from_dict(task.to_dict()) == task
    assert 'expected_json' not in ArtifactRequirement('old.json', format='json').to_dict()


@pytest.mark.parametrize('instruction', [
    'Read input.json with example {"a":1}. Write result.json.',
    'For example, write result.json as {"a":1}.',
    'Do not write result.json as {"a":1}.',
    'Write result.json as {"a": number_of_items}.',
    'Write result.json as {"a":1} and include extra fields.',
    'Write result.json as {"a":1,"a":2}.',
    'Write result.json as {1:"a"}.',
    'Write result.json as {"a":__import__("os").system("echo unsafe")}.',
])
def test_does_not_infer_exact_values_from_examples_open_schemas_or_code(instruction):
    assert literal_json_artifacts(instruction) == {}


def test_nested_strings_and_nulls_are_literal_values():
    result = literal_json_artifacts('Write `nested/out.json` exactly as {"x":[null,{"text":"} ["}]} with no extra fields.')
    assert json.loads(result['nested/out.json']) == {'x': [None, {'text': '} ['}]}
