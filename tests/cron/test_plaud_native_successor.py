"""Opt-in installed Plaud CLI integration through real Cron, agent and terminal.

--plaud-fixture-runner names the knowledge-library SDK fixture executable;
--plaud-pipeline-source names the installed Grace scripts. Neither is a provider.
"""
import json
from pathlib import Path
import shlex
import socket
import sqlite3
import sys

import httpx
import pytest
import requests
from openai.types.chat import ChatCompletion

from cron import executions, jobs, scheduler
from hermes_constants import get_hermes_home
from run_agent import AIAgent


@pytest.mark.parametrize('scenario,guarded', [
    ('grounding_failure', False), ('store_failure', False),
    ('grounding_failure', True), ('store_failure', True),
    ('unknown_write_failure', True),
    ('success', True), ('empty', True), ('replay', True),
])
def test_native_plaud_successor(monkeypatch, request, scenario, guarded):
    fixture = request.config.getoption('--plaud-fixture-runner')
    pipeline = request.config.getoption('--plaud-pipeline-source')
    if not fixture or not pipeline:
        pytest.skip('Requires explicit offline Plaud fixture and installed script roots')
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(
        'model:\n  provider: openrouter\n  default: fixture-model\n'
        'terminal:\n  env: local\n'
        'approvals:\n  mode: off\n  cron_mode: off\n')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-key')
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', lambda **kwargs: {
        'provider': 'openrouter', 'api_mode': 'chat_completions',
        'base_url': 'https://example.invalid/v1', 'api_key': 'fixture-key',
    })
    network_attempts = []
    def deny_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('Unexpected live network access')
    def metadata_transport(self, request, **kwargs):
        if str(request.url) == 'https://example.invalid/api/show':
            return httpx.Response(404, json={'error': 'Not Ollama'}, request=request)
        return deny_network()
    def requests_transport(self, request, **kwargs):
        if request.url not in {'https://openrouter.ai/api/v1/models', 'https://example.invalid/v1/models'}:
            return deny_network()
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'data': [{'id': 'fixture-model', 'context_length': 128000, 'pricing': {}}]}).encode()
        return response
    from agent import model_metadata
    monkeypatch.setattr(model_metadata, '_model_metadata_cache', {})
    monkeypatch.setattr(model_metadata, '_model_metadata_cache_time', 0)
    monkeypatch.setattr(requests.Session, 'send', requests_transport)
    monkeypatch.setattr(httpx.Client, 'send', metadata_transport)
    monkeypatch.setattr(httpx.AsyncClient, 'send', deny_network)
    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    output_root = home / 'fixture-output'
    command = shlex.join([sys.executable, fixture, str(output_root),
        'success' if scenario == 'replay' else scenario,
        str(Path(__file__).resolve().parents[2]), pipeline])
    requests_seen = []
    def respond(self, api_kwargs):
        requests_seen.append(api_kwargs)
        use_tool = len(requests_seen) == 1 and scenario != 'empty'
        final = '[SILENT]'
        if not use_tool and scenario == 'success':
            final = json.loads((output_root / 'outcome.json').read_text())['delivery'].rstrip()
        if guarded and not use_tool:
            # Deliberately false success after a helper failure: dependency observation must win.
            final += '\n[WORKFLOW_STATUS:completed]'
        message = {'role': 'assistant', 'content': final}
        if use_tool:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call_native_plaud', 'type': 'function', 'function': {'name': 'terminal',
                'arguments': json.dumps({'command': command, 'workdir': str(home), 'timeout': 90})}}]}
        return ChatCompletion.model_validate({'id': 'fixture-response', 'object': 'chat.completion',
            'created': 1, 'model': 'fixture-model', 'choices': [{'index': 0, 'message': message,
            'finish_reason': 'tool_calls' if use_tool else 'stop'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})
    monkeypatch.setattr(AIAgent, '_interruptible_api_call', respond)
    deliveries = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda job, body, **kwargs: deliveries.append(body))
    options = {'track_workflow_status': True, 'required_tool_dependencies': ['terminal'],
               'required_tool_dependency_mode': 'when_invoked'} if guarded else {}
    if scenario == 'empty':
        script_root = home / 'scripts'
        script_root.mkdir()
        preflight = script_root / 'empty_preflight.py'
        preflight.write_text('print(\'{"wakeAgent": false, "status": "empty", "recording": null}\')\n')
        options['script'] = preflight.name
    job = jobs.create_job(prompt='Process one offline fixture through its fixed helper.',
        schedule='every 1h', name='Plaud offline native acceptance', deliver='local',
        model='fixture-model', provider='openrouter', max_iterations=4,
        enabled_toolsets=['terminal'], **options)
    assert scheduler.run_one_job(job) is True
    failed = scenario.endswith('_failure') and guarded
    saved = next(row for row in jobs.list_jobs(include_disabled=True) if row['id'] == job['id'])
    assert saved['last_status'] == ('error' if failed else 'ok')
    assert executions.list_executions(job_id=job['id'])[0]['status'] == ('failed' if failed else 'completed')
    if guarded:
        assert saved['last_workflow_status'] == ('unknown' if scenario == 'empty' else 'failed' if failed else 'completed')
    if scenario == 'empty':
        assert not deliveries and not output_root.exists()
        assert not requests_seen
    else:
        outcome = json.loads((output_root / 'outcome.json').read_text())
        assert outcome['exit_code'] == (2 if scenario == 'grounding_failure' else 1 if scenario.endswith('_failure') else 0)
        with sqlite3.connect(home / 'state.db') as conn:
            tools = conn.execute("SELECT content FROM messages WHERE role='tool' AND tool_name='terminal'").fetchall()
        assert len(tools) == 1 and f'"exit_code": {outcome["exit_code"]}' in tools[0][0]
        if scenario == 'grounding_failure':
            assert not outcome['provider_calls']
        elif scenario.endswith('_failure'):
            assert outcome['binding']['verified'] is False
            assert outcome['binding']['attachment_verified'] is False
            if scenario == 'unknown_write_failure':
                assert outcome['provider_calls'].count('edit_note') == 1
        else:
            assert outcome['complete'] is True
            assert outcome['binding']['replay_verified'] is True
            assert outcome['resource_count'] == 2
            assert outcome['provider_calls'].count('finalize_attachment') == 1
            assert 'create_note' not in outcome['provider_calls']
        if scenario == 'success':
            assert deliveries == [outcome['delivery'].rstrip()]
        elif failed:
            assert len(deliveries) == 1 and 'Required tool dependency' in deliveries[0]
        else:
            assert deliveries == []
    assert not network_attempts
