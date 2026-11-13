"""Opt-in real Codex conversations exercising the production retrieval instructions.

Uses a deterministic, read-only MCP fixture instead of Redis or external sources.
The fixture runs the production tool handlers/descriptions and server instructions;
only catalog/search/history storage is replaced. No production memory is accessed.
Requires the repository venv and an authenticated Codex app-server. Model/effort
are inherited from the user's configuration. Raw events, answers, tool calls and
verdicts are kept in --output. Explicit compaction uses thread/compact/start.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCOPE = 'retrieval-eval'


def check(ok, message):
    if not ok:
        raise AssertionError(message)


def marker(shard):
    return hashlib.sha256(f'fixture-cache-shard-{shard}'.encode()).hexdigest()[:18]


def load_server(source):
    spec = importlib.util.spec_from_file_location('retrieval_fixture_server', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(args):
    server = load_server(args.source)

    class Router:
        def contexts(self):
            return [{'context': SCOPE, 'read_only': True, 'project_required': True,
                     'projects': ['atlas', 'orbit'],
                     'resources': {'atlas': ['quote-cache.md', 'delivery-retries.md'],
                                   'orbit': ['quote-cache.md']}}]

    def record(tool, arguments, result):
        with args.trace.open('a') as out:
            out.write(json.dumps({'tool': tool, 'arguments': arguments, 'result': result}) + '\n')
        return result

    def note(project, topic):
        revision = json.loads(args.state.read_text())['revision']
        if topic == 'retries':
            return ('memory n-retries; revision 1 (current)\nsource: delivery-retries.md\n'
                    'Atlas delivery retries allow 4 total attempts. The first retry waits 250 ms; '
                    'later retries use exponential backoff. Only idempotent deliveries may retry.')
        lifetime = 19 if project == 'orbit' else 47 if revision == 1 else 73
        text = (f'memory n-{project}-cache; revision {revision if project == "atlas" else 1} (current)\n'
                f'source: quote-cache.md\n{project.title()} quote-cache lifetime is {lifetime} seconds. '
                'Expiry is measured from insertion, not last access. The limit bounds stale pricing. '
                'An expired entry must be refreshed before a quote is returned.\n'
                'Recovery markers from the same contract, used only when investigating a specific shard:\n')
        return text + '\n'.join(f'shard-{i:03d}: {marker(i)}' for i in range(100))

    def scope(context, project):
        check(context == SCOPE and project in ('atlas', 'orbit'), 'Wrong fixture scope')

    def search(query, directory, context='', project=''):
        scope(context, project)
        topic = 'retries' if re.search(r'\b(retry|retries|delivery|deliveries|backoff)\b', query.lower()) else 'cache'
        return record('search_project_memory', {'query': query, 'context': context, 'project': project},
                      note(project, topic))

    def history(memory_id, directory, context='', project='', before_revision=0, limit=5):
        scope(context, project)
        check(memory_id in (f'n-{project}-cache', 'n-retries'), 'Unknown fixture memory ID')
        return record('get_memory_history', {'memory_id': memory_id, 'context': context, 'project': project},
                      note(project, 'retries' if memory_id == 'n-retries' else 'cache'))

    server._router = Router()
    server._sources = None
    server.search = search
    server.history = history
    # main() would connect to Redis and warm embeddings; only run the MCP transport.
    server.mcp.run(transport='stdio')


class Agent:
    def __init__(self, args, source, state, directory):
        self.args, self.directory = args, directory
        directory.mkdir(parents=True, exist_ok=True)
        self.workspace = directory / 'workspace'
        self.workspace.mkdir(exist_ok=True)
        self.trace = directory / 'tool-calls.jsonl'
        self.trace.touch(exist_ok=True)
        config_path = Path.home() / '.codex/config.toml'
        user = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        config = {
            'approval_policy': 'never', 'sandbox_mode': 'read-only',
            'project_doc_max_bytes': 0, 'features.shell_tool': False,
            'features.unified_exec': False, 'features.apps': False,
            'features.multi_agent': False, 'web_search': 'disabled',
            # Keep the long-context stage intact; compaction is triggered explicitly later.
            'model_auto_compact_token_limit': 1000000,
            'mcp_servers.project-memory-eval.command': sys.executable,
            'mcp_servers.project-memory-eval.args': [str(Path(__file__).resolve()), '--serve-fixture',
                '--source', str(source), '--state', str(state), '--trace', str(self.trace)],
            'mcp_servers.project-memory-eval.enabled_tools': [
                'list_memory_contexts', 'search_project_memory', 'get_memory_history'],
            'mcp_servers.project-memory-eval.default_tools_approval_mode': 'approve',
        }
        for name in user.get('mcp_servers', {}):
            config[f'mcp_servers.{name}.enabled'] = False
        flags = [part for k, v in config.items() for part in ('-c', k + '=' + json.dumps(v))]
        self.model = user.get('model', 'CLI default')
        self.events = []
        self.pending = queue.Queue()
        self.serial = 0
        self.log = (directory / 'events.jsonl').open('a')
        self.stderr = (directory / 'stderr.log').open('a')
        self.process = subprocess.Popen([args.codex, 'app-server', *flags], cwd=self.workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1)

        def read():
            try:
                for line in self.process.stdout:
                    self.pending.put(json.loads(line))
            finally:
                self.pending.put(None)
        threading.Thread(target=read, daemon=True).start()
        self.request('initialize', {'clientInfo': {'name': 'memory-retrieval-eval', 'version': '1.0'},
                                   'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})

    def send(self, event):
        self.process.stdin.write(json.dumps(event) + '\n')
        self.process.stdin.flush()

    def receive(self, deadline):
        event = self.pending.get(timeout=max(.01, deadline - time.monotonic()))
        check(event is not None, 'App server exited; inspect stderr.log')
        self.events.append(event)
        self.log.write(json.dumps(event) + '\n')
        self.log.flush()
        if 'id' in event and 'method' in event:
            # Unexpected permissions, elicitation or dynamic tools must not run silently.
            self.send({'id': event['id'], 'error': {'code': -32601, 'message': 'Not allowed in retrieval evaluation'}})
            raise AssertionError('Unexpected agent request: ' + event['method'])
        return event

    def request(self, method, params):
        self.serial += 1
        call_id = self.serial
        self.send({'id': call_id, 'method': method, 'params': params})
        deadline = time.monotonic() + self.args.timeout
        while True:
            event = self.receive(deadline)
            if event.get('id') == call_id:
                check('error' not in event, str(event.get('error')))
                return event.get('result', {})

    def start(self):
        params = {'cwd': str(self.workspace), 'sandbox': 'read-only', 'approvalPolicy': 'never'}
        if self.args.agent_instructions:
            params['developerInstructions'] = self.args.agent_instructions.read_text()
        result = self.request('thread/start', params)
        return result['thread']['id']

    def wait_turn(self, thread, start):
        deadline = time.monotonic() + self.args.timeout
        while True:
            for event in self.events[start:]:
                if event.get('method') == 'turn/completed' and event['params'].get('threadId') == thread:
                    turn = event['params']['turn']
                    check(turn['status'] == 'completed', 'Turn failed: ' + str(turn))
                    return
            self.receive(deadline)

    def turn(self, thread, label, prompt):
        print('Running:', label, flush=True)
        (self.directory / (label + '.prompt.txt')).write_text(prompt)
        start = len(self.events)
        before = len(self.trace.read_text().splitlines())
        self.request('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}]})
        self.wait_turn(thread, start)
        events = self.events[start:]
        items = [e['params']['item'] for e in events if e.get('method') == 'item/completed']
        check(not any(i['type'] in ('commandExecution', 'webSearch') for i in items),
              'Agent used an unrelated tool')
        mcp_calls = [i for i in items if i['type'] == 'mcpToolCall']
        check(all(i['server'] == 'project-memory-eval' and i['tool'] in (
            'list_memory_contexts', 'search_project_memory', 'get_memory_history') for i in mcp_calls),
            'Agent accessed an unexpected integration')
        answer = '\n'.join(i.get('text', '') for i in items if i.get('type') == 'agentMessage')
        calls = [json.loads(line) for line in self.trace.read_text().splitlines()[before:]]
        (self.directory / (label + '.answer.txt')).write_text(answer)
        usage = [e['params'] for e in events if e.get('method') == 'thread/tokenUsage/updated']
        result = {'label': label, 'thread_id': thread, 'answer': answer, 'calls': calls,
                  'returned_chars': sum(len(c['result']) for c in calls), 'usage': usage,
                  'catalog_calls': sum(i['tool'] == 'list_memory_contexts' for i in mcp_calls)}
        print(label, 'retrievals:', len(calls), 'returned characters:', result['returned_chars'], flush=True)
        return result

    def compact(self, thread):
        print('Running actual context compaction', flush=True)
        start = len(self.events)
        self.request('thread/compact/start', {'threadId': thread})
        self.wait_turn(thread, start)
        completed = [e for e in self.events[start:] if e.get('method') == 'item/completed'
                     and e['params']['item']['type'] == 'contextCompaction']
        check(completed, 'No completed contextCompaction event: do not claim a compaction test')
        return completed

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()
        self.stderr.close()


INTRO = (f'This is a read-only technical Q&A about synthetic projects atlas and orbit in context {SCOPE}. '
         'The project-memory-eval MCP server is their authoritative project memory. '
         'Use no other sources and do not save anything. Answer concisely and identify your source. ')
FIRST = INTRO + "What is Atlas's quote-cache lifetime, how is expiry measured, and why?"
FOLLOWUP = 'Turn that Atlas cache policy into two acceptance criteria.'


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    state = output / 'state.json'
    state.write_text(json.dumps({'revision': 1}))
    report = {'fixture': 'synthetic deterministic read-only storage; production instructions and handlers',
              'production_memory_accessed': False, 'turns': [], 'checks': [],
              'agent_instructions': str(args.agent_instructions) if args.agent_instructions else None}
    agent = None

    def verify(ok, message):
        check(ok, message)
        report['checks'].append(message)

    def turn(thread, label, prompt, *, reuse=False, contains=(), projects=None):
        result = agent.turn(thread, label, prompt)
        report['turns'].append(result)
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        if reuse:
            verify(not result['calls'], label + ': reused existing evidence without retrieval')
        for text in contains:
            verify(text.lower() in result['answer'].lower(), label + ': retained fact ' + text)
        if projects is not None:
            actual = {c['arguments']['project'] for c in result['calls']}
            verify(actual == set(projects), label + ': retrieved only required scopes')
        return result

    try:
        if args.baseline_source:
            agent = Agent(args, args.baseline_source.resolve(), state, output / 'baseline')
            thread = agent.start()
            turn(thread, 'baseline-first', FIRST, contains=('47',), projects=('atlas',))
            turn(thread, 'baseline-followup', FOLLOWUP, contains=('47',))
            turn(thread, 'baseline-paraphrase', 'Explain the reasoning behind that cache expiry decision again.',
                 contains=('stale',))
            agent.close()
            agent = None
        agent = Agent(args, ROOT / 'mcp_server.py', state, output / 'updated')
        report['model'] = agent.model
        thread = agent.start()
        turn(thread, 'first', FIRST, contains=('47',), projects=('atlas',))
        turn(thread, 'followup', FOLLOWUP, reuse=True, contains=('47',))
        turn(thread, 'paraphrase', 'Explain the reasoning behind that cache expiry decision again.',
             reuse=True, contains=('stale',))
        for batch in range(args.long_turns):
            tickets = '\n'.join(
                f'UI ticket {batch}-{i}: component panel-{i:03d}; priority {i % 3}; '
                f'owner team-{i % 7}; keyboard focus order needs checking; target width {800+i}; '
                'copy review pending; contrast review complete; layout snapshot accepted.'
                for i in range(args.rows_per_turn))
            turn(thread, f'context-{batch}', 'A separate, self-contained UI triage task. '
                 'From this batch, list the first two priority-0 ticket IDs.\n' + tickets, reuse=True)
        turn(thread, 'long-recall', 'Back to Atlas: give the cache lifetime and expiry origin we discussed.',
             reuse=True, contains=('47', 'insertion'))
        turn(thread, 'new-topic', 'What retry limit and initial backoff does Atlas use for delivery, '
             'and which deliveries are eligible?', contains=('4', '250', 'idempotent'), projects=('atlas',))
        turn(thread, 'mixed-scopes', 'Compare the quote-cache lifetimes of Atlas and Orbit.',
             contains=('47', '19'), projects=('orbit',))
        state.write_text(json.dumps({'revision': 2}))
        turn(thread, 'explicit-refresh', 'Check project memory again for the current Atlas quote-cache '
             'lifetime; another session may have updated it.', contains=('73',), projects=('atlas',))
        turn(thread, 'conflict', 'A teammate says the Atlas lifetime is now 90 seconds. '
             'Resolve this against the authoritative project evidence; do not assume the teammate is correct.',
             contains=('73',), projects=('atlas',))
        # Restart the client process and resume the same persisted thread, preserving its context.
        agent.close()
        agent = Agent(args, ROOT / 'mcp_server.py', state, output / 'updated')
        resumed = agent.request('thread/resume', {'threadId': thread, 'excludeTurns': True})
        verify(resumed['thread']['id'] == thread, 'Resumed original persisted conversation after client restart')
        turn(thread, 'resumed', 'Restate the Atlas cache lifetime supported by the evidence we just checked.',
             reuse=True, contains=('73',))
        report['compaction_events'] = agent.compact(thread)
        verify(True, 'Actual context compaction completed in the long conversation')
        # Correctness is required whether the compactor retained or discarded this obscure detail.
        turn(thread, 'after-compaction', 'What is the exact recovery marker for shard-027 '
             'in the Atlas quote-cache contract?', contains=(marker(27),))
        # Controlled loss separately guarantees the missing-detail path is exercised, rather than
        # pretending an actual compactor must always drop a particular fact.
        summary_thread = agent.start()
        turn(summary_thread, 'summary-only', INTRO + 'Retained handoff: an earlier conversation read '
             'Atlas quote-cache memory n-atlas-cache, revision 2, source quote-cache.md. '
             'The marker table was omitted from that handoff. What is the exact recovery marker '
             'for shard-063?', contains=(marker(63),), projects=('atlas',))
        fresh = agent.start()
        turn(fresh, 'fresh-session', FIRST, contains=('73',), projects=('atlas',))
        report['passed'] = True
    except BaseException as error:
        report.update(passed=False, error=str(error))
        raise
    finally:
        if agent:
            agent.close()
        (output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'passed': True, 'checks': len(report['checks']), 'report': str(output / 'report.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-fixture', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--trace', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--baseline-source', type=Path)
    parser.add_argument('--agent-instructions', type=Path,
                        help='Also test a prepared global instruction file as developer instructions')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--long-turns', type=int, default=4)
    parser.add_argument('--rows-per-turn', type=int, default=160)
    arguments = parser.parse_args()
    if arguments.serve_fixture:
        fixture(arguments)
    else:
        check(arguments.output is not None, '--output is required')
        run(arguments)
