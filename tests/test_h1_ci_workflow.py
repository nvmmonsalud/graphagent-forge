"""Keep the opt-in harness CI disposable, credential-free and non-deploying."""
from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/h1-gates.yml'
ACTIONS = {
    'actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5': {
        'persist-credentials': False,
    },
    'actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065': {
        'python-version': '3.11',
    },
    'actions/setup-java@cf277c60eb25467037889841efdb72551f06f6c3': {
        'distribution': 'temurin', 'java-version': '21',
    },
}
SHA256 = 'ad8ac3398606145502b8f489530bbd39333707ae4668156af16b6086b5b037d7'
URL = 'https://dist.neo4j.org/neo4j-community-5.26.0-unix.tar.gz'
ACCEPTANCE = '\n'.join((
    'set -euo pipefail',
    'umask 077',
    'diagnostic="$(mktemp "$TMPDIR/h1-pytest.XXXXXX")"',
    '''trap 'rm -f -- "$diagnostic"' EXIT''',
    'if python -m pytest --hosted tests/hosted -q --tb=no --show-capture=no '
    '--disable-warnings > "$diagnostic" 2>&1; then',
    "  printf '%s\\n' 'H1 local connected acceptance passed.'",
    'else',
    "  printf '%s\\n' 'H1 local connected acceptance failed; reproduce locally using "
    "docs/H1_LOCAL_ACCEPTANCE.md.'",
    '  exit 1',
    'fi',
))
IMAGE = ('python -m scripts.check_ci_image --commit "$GITHUB_SHA" '
         '--output-dir "$RUNNER_TEMP/h1-image-evidence" --json')
AUDIT_INSTALL = 'python -m pip install pip-audit==2.9.0'
AUDIT = ('python -m pip_audit --no-deps --disable-pip '
         '-r "$RUNNER_TEMP/h1-image-evidence/runtime-requirements.txt"')
UPLOAD = 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02'
METADATA = {
    'name': 'h1-image-public-metadata',
    'path': '\n'.join('${{ runner.temp }}/h1-image-evidence/' + name for name in (
        'report.json', 'dependencies.json', 'runtime-requirements.txt',
    )),
    'retention-days': 7, 'if-no-files-found': 'error', 'include-hidden-files': False,
}
INSTALL = {
    'python -m pip install -r requirements-hosted.txt',
    'python -m playwright install --with-deps chromium',
}


def validate_policy(workflow):
    """An allowlist rejects new privilege, transport and artifact surfaces."""
    assert set(workflow) == {'name', 'on', 'permissions', 'jobs'}
    assert workflow['on'] == {'push': {}, 'pull_request': {}}
    assert workflow['permissions'] == {'contents': 'read'}
    assert set(workflow['jobs']) == {'hosted_acceptance', 'image_gate'}
    assert '${{ secrets.' not in json.dumps(workflow)
    assert '${{ github.event.' not in json.dumps(workflow)
    for name, job in workflow['jobs'].items():
        assert set(job) == {'runs-on', 'timeout-minutes', 'steps'}
        assert job['runs-on'] == 'ubuntu-24.04'
        assert job['timeout-minutes'] == 20
        for step in job['steps']:
            assert set(step) <= {'name', 'uses', 'with', 'run', 'shell'}
            if 'uses' in step:
                if step['uses'] == UPLOAD:
                    assert name == 'image_gate'
                    assert step == job['steps'][-1]
                    assert step.get('with') == METADATA
                else:
                    assert step['uses'] in ACTIONS
                    assert step.get('with') == ACTIONS[step['uses']]
                assert 'run' not in step
            else:
                assert 'with' not in step
                assert step.get('shell', 'bash') == 'bash'
    hosted = workflow['jobs']['hosted_acceptance']['steps']
    image = workflow['jobs']['image_gate']['steps']
    assert [step['uses'] for step in hosted if 'uses' in step] == list(ACTIONS)
    assert [step['uses'] for step in image if 'uses' in step] == [*list(ACTIONS)[:2], UPLOAD]
    runs = [step['run'] for step in hosted if 'run' in step]
    assert len(runs) == 4
    assert set(runs[:2]) == INSTALL
    assert runs[-1] == ACCEPTANCE
    assert [step['run'] for step in image if 'run' in step] == [AUDIT_INSTALL, IMAGE, AUDIT]
    download = runs[2]
    assert download.startswith('set -euo pipefail\numask 077\n')
    assert 'mktemp -d "$RUNNER_TEMP/h1-neo4j.XXXXXX"' in download
    assert 'mktemp -d "$RUNNER_TEMP/h1-fixtures.XXXXXX"' in download
    assert download.count('curl ') == 1
    assert download.count(URL) == 1
    assert SHA256 in download
    assert download.index('sha256sum --check --status') < download.index('tar --extract')
    for argument in (
        "--proto '=https'", "--proto-redir '=https'", '--fail', '--max-redirs 3',
        '--connect-timeout 15', '--max-time 120', '--retry 2', '--retry-max-time 300',
        '--no-same-owner',
    ):
        assert argument in download
    assert 'HOSTED_NEO4J_HOME=%s/neo4j-community-5.26.0' in download
    assert 'TMPDIR=%s' in download
    assert download.count('>> "$GITHUB_ENV"') == 2
    assert 'upload-artifact' not in json.dumps(hosted)
    assert 'docker push' not in json.dumps(workflow)


def test_h1_workflow_is_valid_json_subset_of_yaml_with_safe_policy():
    # JSON is YAML 1.2; stdlib parsing avoids a dependency and YAML 1.1's `on` coercion.
    validate_policy(json.loads(WORKFLOW.read_text()))


@pytest.mark.parametrize('exit_code', [0, 1])
def test_hosted_wrapper_discards_private_output_and_fails_closed(tmp_path, exit_code):
    workflow = json.loads(WORKFLOW.read_text())
    command = workflow['jobs']['hosted_acceptance']['steps'][-1]['run']
    fixture_dir = tmp_path / 'private'
    fixture_dir.mkdir(mode=0o700)
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    fake_python = binary_dir / 'python'
    fake_python.write_text(
        '#!/bin/bash\n'
        'printf "%s\\n" private-sentinel-stdout\n'
        'printf "%s\\n" private-sentinel-stderr >&2\n'
        f'exit {exit_code}\n'
    )
    fake_python.chmod(0o700)
    result = subprocess.run(
        ['bash', '-c', command], capture_output=True, text=True, check=False,
        env={'PATH': str(binary_dir) + os.pathsep + os.defpath,
             'TMPDIR': str(fixture_dir)},
    )
    assert result.returncode == exit_code
    assert result.stderr == ''
    expected = (
        'H1 local connected acceptance passed.\n' if exit_code == 0 else
        'H1 local connected acceptance failed; reproduce locally using '
        'docs/H1_LOCAL_ACCEPTANCE.md.\n'
    )
    assert result.stdout == expected
    assert 'private-sentinel' not in result.stdout + result.stderr
    assert list(fixture_dir.iterdir()) == []


@pytest.mark.parametrize('unsafe', [
    'write', 'privileged_trigger', 'self_hosted', 'secrets', 'artifact', 'mutable_action',
    'credentials', 'failure_payload', 'untrusted_image_commit', 'job_permissions',
    'provider_env', 'skip_hash', 'artifact_wildcard', 'artifact_failure',
    'artifact_retention', 'audit_skip', 'acceptance_fail_open', 'diagnostics_leak',
])
def test_h1_policy_rejects_unsafe_workflow_changes(unsafe):
    workflow = deepcopy(json.loads(WORKFLOW.read_text()))
    job = workflow['jobs']['hosted_acceptance']
    if unsafe == 'write':
        workflow['permissions']['contents'] = 'write'
    elif unsafe == 'privileged_trigger':
        workflow['on'] = {'pull_request_target': {}}
    elif unsafe == 'self_hosted':
        job['runs-on'] = 'self-hosted'
    elif unsafe == 'secrets':
        job['steps'].append({'run': 'echo "${{ secrets.KIMI_API_KEY }}"'})
    elif unsafe == 'artifact':
        job['steps'].append({'uses': 'actions/upload-artifact@' + 'a' * 40,
                             'with': {'path': '${{ runner.temp }}'}})
    elif unsafe == 'mutable_action':
        job['steps'][0]['uses'] = 'actions/checkout@v4'
    elif unsafe == 'credentials':
        job['steps'][0]['with']['persist-credentials'] = True
    elif unsafe == 'failure_payload':
        job['steps'][-1]['run'] = ACCEPTANCE.replace('--tb=no', '--showlocals')
    elif unsafe == 'untrusted_image_commit':
        workflow['jobs']['image_gate']['steps'][3]['run'] = (
            'python -m scripts.check_ci_image --commit "${{ github.event.inputs.sha }}" --json'
        )
    elif unsafe == 'job_permissions':
        job['permissions'] = {'packages': 'write'}
    elif unsafe == 'provider_env':
        job['env'] = {'KIMI_API_KEY': 'operator-credential'}
    elif unsafe == 'artifact_wildcard':
        workflow['jobs']['image_gate']['steps'][-1]['with']['path'] = '${{ runner.temp }}/**'
    elif unsafe == 'artifact_failure':
        workflow['jobs']['image_gate']['steps'][-1]['if'] = '${{ always() }}'
    elif unsafe == 'artifact_retention':
        workflow['jobs']['image_gate']['steps'][-1]['with']['retention-days'] = 90
    elif unsafe == 'audit_skip':
        workflow['jobs']['image_gate']['steps'][-2]['run'] = 'echo audit skipped'
    elif unsafe == 'acceptance_fail_open':
        job['steps'][-1]['run'] = ACCEPTANCE.replace('exit 1', 'exit 0')
    elif unsafe == 'diagnostics_leak':
        job['steps'][-1]['run'] = ACCEPTANCE + '\ncat "$diagnostic"'
    else:
        job['steps'][-2]['run'] = job['steps'][-2]['run'].replace(SHA256, '0' * 64)
    with pytest.raises(AssertionError):
        validate_policy(workflow)
