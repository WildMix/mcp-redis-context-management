"""Opt-in real Codex-session evaluation on an empty, disposable Redis container.

Run with the repository venv. Requires cached embeddings, Docker, authenticated
Codex CLI with --ignore-user-config, and a container labelled
project-memory-test=explicit-relationships. Never falls back to a normal DB.
Evidence (JSONL agent events, MCP responses, Redis records, verdicts) stays in
--output. Assertions inspect actual persisted relationships, not model claims.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlparse
from urllib.request import urlopen

import redis

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "relationship-test"
INDEX = "agent_relationship_eval"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def disposable_database(container, url):
    endpoint = urlparse(url)
    check(endpoint.hostname == '127.0.0.1' and endpoint.port not in (None, 6379, 6380, 6381)
          and endpoint.path == '/0' and endpoint.scheme == 'redis', 'Use a separate loopback test Redis on database 0')
    spec = json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]
    check(spec['Config'].get('Labels', {}).get('project-memory-test') == 'explicit-relationships', 'Container lacks test label')
    check(not [m for m in spec['Mounts'] if m['Type'] in ('bind', 'volume')], 'Test container must not mount persistent data')
    bindings = spec['NetworkSettings']['Ports']['6379/tcp']
    check({'HostIp': '127.0.0.1', 'HostPort': str(endpoint.port)} in bindings, 'URL does not point to the labelled test container')
    client = redis.Redis.from_url(url, decode_responses=True)
    check(client.dbsize() == 0, 'Test Redis must be empty; never clear an existing database automatically')
    return client


def notes(client, project):
    fields = ['project', 'note_id', 'memory_id', 'revision', 'state', 'content', 'heading',
              'agent', 'relationship', 'related_memory_id', 'related_revision', 'conflicts', 'parts', 'part']
    grouped = {}
    for key in client.scan_iter(match=INDEX + ':note:*'):
        row = dict(zip(fields, client.hmget(key, fields)))
        if row['project'] == project:
            grouped.setdefault(row['note_id'], []).append(row)
    result = []
    for chunks in grouped.values():
        chunks.sort(key=lambda c: int(c['part']))
        check(len(chunks) == int(chunks[0]['parts']), 'Incomplete note')
        result.append({**chunks[0], 'content': ''.join(c['content'] for c in chunks)})
    return result


def seed_prompt(project):
    return f'''Use only project-memory MCP, context="{CONTEXT}", project="{project}".
This is a synthetic technical project; all facts below are the authoritative evidence for this exercise.
Record the following two unrelated, accepted technical decisions as separate durable notes.
1. EvalAtlasHost and its embedded EvalCouponApp share the host's NgZone. The accepted
interim fix is to enable zone-based change detection in EvalAtlasHost so successful
coupon responses render. The consumer currently overrides its own NgZone with the
host's global zone. Source: architecture/zone-decision.txt, decision 1.
2. EvalGateway's production coupon cache has a 30-second lifetime. Contract version 4
specifies that limit to bound stale pricing. Source: architecture/cache-contract-v4.txt.
Keep the component names, rationale and source references. Confirm saves from tool results.
Do not inspect local files, browse, or use any other context. Do not merely describe a proposed save.'''


def update_prompt(project):
    return f'''Use only project-memory MCP, context="{CONTEXT}", project="{project}".
Record the lasting knowledge from these new authoritative project facts. Retrieve and
read the relevant existing memories first. Use the server's documented workflow.
1. After testing both solutions, the architecture owners chose consumer-owned change
detection. EvalCouponApp must remove its override of NgZone with the host's global
zone and use its own active zone; EvalAtlasHost stays zoneless. Do not ship the previously
chosen host-side compatibility change. The old host workaround still works technically,
but consumer ownership avoids coupling independently upgraded apps. Source:
architecture/zone-decision.txt, decision 2. This is an accepted technical decision.
2. EvalGateway production cache contract version 4 says 30 seconds, but two verified
traces against that same version and environment show entries expiring at 60 seconds.
The owner has not resolved whether the implementation or contract is wrong. Preserve
the disagreement and both pieces of evidence; do not invent a new accepted cache policy.
New evidence: diagnostics/cache-traces-v4.txt.
Keep the component names and supporting evidence. Do not inspect local files, browse,
or use another context. Confirm actual persistence rather than proposing future saves.'''


def validate_seed(rows):
    check(len(rows) == 2 and all(r['revision'] == '1' and r['state'] == 'current' for r in rows), 'Seed must create two independent current notes')
    zone = [r for r in rows if 'EvalAtlasHost' in r['content']]
    cache = [r for r in rows if 'EvalGateway' in r['content']]
    check(len(zone) == len(cache) == 1 and zone[0]['memory_id'] != cache[0]['memory_id'], 'Agent must separate the two technical subjects')
    return zone[0]['memory_id'], cache[0]['memory_id']


def validate_update(rows, seed):
    zone_id, cache_id = seed
    zone = [r for r in rows if r['memory_id'] == zone_id]
    check(len(zone) == 2 and {(r['revision'], r['state']) for r in zone} == {('1', 'historical'), ('2', 'current')}, 'Decision change must revise the original memory')
    latest = next(r for r in zone if r['state'] == 'current')
    check(latest['relationship'] == 'supersedes' and latest['related_memory_id'] == zone_id
          and latest['related_revision'] == '1', 'Missing explicit supersession provenance')
    check('EvalCouponApp' in latest['content'] and 'zoneless' in latest['content'].lower(), 'Replacement lost the chosen technical behavior')
    cache = next(r for r in rows if r['memory_id'] == cache_id)
    contrary = [r for r in rows if r['relationship'] == 'contradicts' and r['related_memory_id'] == cache_id]
    check(len(contrary) == 1 and contrary[0]['state'] == cache['state'] == 'current', 'Unresolved conflict must keep both current')
    check(cache['revision'] == contrary[0]['revision'] == '1', 'Contradiction must not silently replace the old claim')
    check(cache_id in json.loads(contrary[0]['conflicts']) and contrary[0]['memory_id'] in json.loads(cache['conflicts']), 'Conflict must link both ways')
    check(len(rows) == 4, 'Unexpected extra notes or duplicate families')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis-url', required=True)
    parser.add_argument('--container', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    client = disposable_database(args.container, args.redis_url)
    client.hset(INDEX + ':meta', mapping={'corpus_domain': CONTEXT})
    routes = output / 'routes.json'
    routes.write_text(json.dumps({'schema': 1, 'targets': {CONTEXT: {
        'domain': CONTEXT, 'redis_url': args.redis_url, 'read_only': False}}, 'routes': []}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    environment = dict(os.environ, REDIS_URL=args.redis_url, MEMORY_ROUTES=str(routes),
                       MEMORY_INDEX=INDEX, MEMORY_PROJECT='', MEMORY_SOURCES='', HF_HUB_OFFLINE='1',
                       TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='2')
    # Inherit only the user's model preferences, never their configured MCP servers.
    user_config = Path.home() / '.codex/config.toml'
    config = tomllib.loads(user_config.read_text()) if user_config.exists() else {}
    overrides = {
        'approval_policy': 'never', 'sandbox_mode': 'read-only',
        'project_doc_max_bytes': 0, 'features.shell_tool': False, 'features.unified_exec': False,
        'features.apps': False, 'web_search': 'disabled',
        'mcp_servers.project-memory.url': base + '/mcp',
        'mcp_servers.project-memory.tool_timeout_sec': 60,
        'mcp_servers.project-memory.default_tools_approval_mode': 'approve',
        'mcp_servers.project-memory.enabled_tools': ['list_memory_contexts', 'search_project_memory',
                                                    'save_to_project_memory', 'get_memory_history'],
    }
    for name in ('model', 'model_reasoning_effort'):
        if config.get(name):
            overrides[name] = config[name]
    flags = [piece for k, v in overrides.items() for piece in ('-c', k + '=' + json.dumps(v))]
    sessions = []
    report = {'context': CONTEXT, 'redis_url': args.redis_url, 'mcp_url': base + '/mcp',
              'container': args.container, 'model': config.get('model', 'CLI default'),
              'production_connections_configured': False, 'sessions': sessions, 'checks': []}
    (output / 'configuration.json').write_text(json.dumps(overrides, indent=2))

    with tempfile.TemporaryDirectory(prefix='memory-agent-eval-') as workspace, \
            (output / 'server.log').open('w') as log:
        server = subprocess.Popen([sys.executable, '-u', str(ROOT / 'mcp_server.py'), '--transport',
                                   'streamable-http', '--port', str(port)], env=environment,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 120
            while True:
                check(server.poll() is None and time.monotonic() < deadline, 'Isolated MCP server failed to start; see server.log')
                try:
                    with urlopen(base + '/health', timeout=1) as response:
                        health = json.load(response)
                    check(health.get('memory_relationships') == 'explicit', 'Wrong server version')
                    break
                except OSError:
                    time.sleep(.2)

            def agent(label, prompt, session_id=None):
                (output / (label + '.prompt.txt')).write_text(prompt)
                command = [args.codex, 'exec'] + (['resume', session_id] if session_id else [])
                command += ['--ignore-user-config', '--skip-git-repo-check', '--json', *flags,
                            '--output-last-message', str(output / (label + '.answer.txt')), '-']
                print('Running real agent turn:', label, flush=True)
                with (output / (label + '.events.jsonl')).open('w') as events, \
                        (output / (label + '.stderr.log')).open('w') as errors:
                    result = subprocess.run(command, input=prompt, text=True, cwd=workspace,
                                            stdout=events, stderr=errors, timeout=args.timeout)
                check(result.returncode == 0, f'Agent {label} exited {result.returncode}; see its logs')
                events = [json.loads(line) for line in (output / (label + '.events.jsonl')).read_text().splitlines() if line.strip()]
                thread = next((e.get('thread_id') for e in events if e['type'] == 'thread.started'), session_id)
                check(thread is not None, 'Missing real session identifier')
                calls = [e['item'] for e in events if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'mcp_tool_call']
                check(calls or label == 'status-only', 'Agent did not call any memory tools')
                check(all(c.get('server') == 'project-memory' for c in calls), 'Unexpected MCP server used')
                for c in calls:
                    arguments = c.get('arguments', {})
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    check(arguments.get('context', CONTEXT) == CONTEXT, 'Agent attempted another memory context')
                sessions.append({'turn': label, 'thread_id': thread,
                                 'calls': [{'tool': c.get('tool'), 'arguments': c.get('arguments')} for c in calls]})
                (output / 'report.json').write_text(json.dumps(report, indent=2))
                return thread

            same = agent('same-session-seed', seed_prompt('same-session'))
            seed_same = validate_seed(notes(client, 'same-session'))
            check(agent('same-session-update', update_prompt('same-session'), same) == same, 'Resume must retain the original session')
            validate_update(notes(client, 'same-session'), seed_same)
            report['checks'].extend(['same-session supersedes', 'same-session contradicts'])
            writer = agent('prior-session-seed', seed_prompt('cross-session'))
            seed_cross = validate_seed(notes(client, 'cross-session'))
            updater = agent('fresh-session-update', update_prompt('cross-session'))
            check(len({same, writer, updater}) == 3, 'Cross-session update must use a genuinely new conversation')
            validate_update(notes(client, 'cross-session'), seed_cross)
            report['checks'].extend(['cross-session supersedes', 'cross-session contradicts'])
            before = client.hget(INDEX + ':meta', 'revision')
            agent('status-only', f'''Use only project-memory, context="{CONTEXT}", project="same-session".
The PR implementing the EvalCouponApp decision you recorded has moved from approved
to merged. No technical facts, decisions or applicability changed; no deployment was
verified. Assess whether this yields durable knowledge and handle memory accordingly.
Do not modify files or review checkpoints; this isolated exercise concerns memory only.''', same)
            check(client.hget(INDEX + ':meta', 'revision') == before, 'PR status-only change must not write a note or revision')
            report['checks'].append('status-only change saves nothing')
            agent('fresh-reader', f'''Use only project-memory, context="{CONTEXT}", project="cross-session".
Based on existing memory, explain the accepted EvalAtlasHost/EvalCouponApp change-detection
architecture and what is established about EvalGateway cache lifetime. Read the relevant
complete memories and any linked unresolved disagreement before answering. Is the
host-side workaround still the chosen solution? Do not save anything; no new evidence is supplied.''')
            check(client.hget(INDEX + ':meta', 'revision') == before, 'Reader unexpectedly wrote memory')
            answer = (output / 'fresh-reader.answer.txt').read_text().lower()
            check('zoneless' in answer and ('consumer' in answer or 'evalcouponapp' in answer), 'Reader missed the current architectural decision')
            check('30' in answer and '60' in answer and any(s in answer for s in ('unresolved', 'conflict', 'disagree', 'not resolved')), 'Reader failed to preserve uncertainty about the conflicting cache evidence')
            report['checks'].append('fresh session retrieves current decision and unresolved conflict')
            report['notes'] = {project: notes(client, project) for project in ('same-session', 'cross-session')}
            report['passed'] = True
        except BaseException as error:
            report.update(passed=False, error=str(error))
            raise
        finally:
            (output / 'report.json').write_text(json.dumps(report, indent=2))
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            client.close()
    print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'report': str(output / 'report.json')}, indent=2))


if __name__ == '__main__':
    main()
