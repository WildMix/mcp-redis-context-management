"""Project routing and shared-model regression tests; real HTTP tests use a disposable Redis index."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.request import urlopen

import anyio
import httpx
import redis
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

import memory_store as store
import mcp_server as server


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(server, '_shared_http', True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.session = MagicMock()
        self.session.check_client_capability.return_value = False
        self.ctx = SimpleNamespace(session=self.session)

    def roots(self, *directories):
        self.session.check_client_capability.return_value = True
        self.session.list_roots = AsyncMock(return_value=types.ListRootsResult(
            roots=[types.Root(uri=directory.as_uri()) for directory in directories]))

    async def test_explicit_directory_takes_precedence(self):
        self.assertEqual(await server._directory(self.ctx, str(self.root)), str(self.root))
        self.session.check_client_capability.assert_not_called()

    async def test_no_directory_never_uses_server_cwd(self):
        with self.assertRaisesRegex(ValueError, 'Pass workdir'):
            await server._directory(self.ctx, '')

    async def test_relative_and_missing_directories_fail(self):
        for directory in ('.', 'relative', str(self.root / 'missing')):
            with self.assertRaisesRegex(ValueError, 'existing absolute'):
                await server._directory(self.ctx, directory)

    async def test_single_root_is_used_but_multiple_roots_are_ambiguous(self):
        self.roots(self.root)
        self.assertEqual(await server._directory(self.ctx, ''), str(self.root))
        other = self.root / 'other'
        other.mkdir()
        self.roots(self.root, other)
        with self.assertRaisesRegex(ValueError, 'Pass workdir'):
            await server._directory(self.ctx, '')

    async def test_failed_roots_do_not_fall_back(self):
        self.roots(self.root)
        self.session.list_roots.side_effect = RuntimeError('disconnected')
        with self.assertRaisesRegex(ValueError, 'Pass workdir'):
            await server._directory(self.ctx, '')

    async def test_stdio_still_falls_back_to_cwd(self):
        with patch.object(server, '_shared_http', False):
            self.assertEqual(await server._directory(self.ctx, ''), os.getcwd())

    async def test_explicit_context_does_not_request_client_roots(self):
        # Even stale or unrelated client directories cannot interfere with explicit selection.
        self.roots(self.root, self.root / 'missing')
        with patch.object(server, 'search', return_value='selected') as search:
            result = await server.search_project_memory('topic', self.ctx, workdir='missing',
                                                        context='personal', project='game')
            self.assertEqual(result, 'selected')
            search.assert_called_once_with('topic', '', 'personal', 'game')
        self.session.check_client_capability.assert_not_called()

    async def test_partial_selector_does_not_fall_back_to_directory(self):
        with patch.object(server, '_router', None):
            with self.assertRaisesRegex(ValueError, 'require MEMORY_ROUTES'):
                await server.search_project_memory('topic', self.ctx, workdir=str(self.root), project='game')
        self.session.check_client_capability.assert_not_called()


class ModelTests(unittest.TestCase):
    def test_concurrent_clients_load_one_model(self):
        model = object()
        def load():
            time.sleep(0.02)
            return model
        with patch.object(server, '_model', None), patch.object(server, 'load_model', side_effect=load) as loader:
            with ThreadPoolExecutor(max_workers=8) as pool:
                models = list(pool.map(lambda _: server.embedding_model(), range(8)))
            self.assertTrue(all(value is model for value in models))
            loader.assert_called_once()

    def test_shared_mode_rejects_global_project_override(self):
        with patch.object(sys, 'argv', ['mcp_server.py', '--transport', 'streamable-http']), \
             patch.object(server, 'PROJECT_OVERRIDE', 'wrong-project'), \
             patch.object(server, '_shared_http', False), patch.object(server, 'hold_wsl_open') as hold:
            with self.assertRaises(SystemExit) as result:
                server.main()
            self.assertEqual(result.exception.code, 2)
            hold.assert_not_called()


@unittest.skipUnless(os.environ.get('MEMORY_TEST_REAL_MODEL') == '1', 'set MEMORY_TEST_REAL_MODEL=1 for HTTP/model test')
class HttpIntegrationTests(unittest.TestCase):
    def test_shared_process_multi_client_project_isolation_and_roots(self):
        self.check_shared_process(explicit=False)

    def test_explicit_context_without_checkouts_or_client_roots(self):
        self.check_shared_process(explicit=True)

    def check_shared_process(self, *, explicit):
        r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(r.close)
        r.ping()
        prefix = 'test_project_memory_' + uuid.uuid4().hex
        def cleanup():
            for index in r.execute_command('FT._LIST'):
                if index == prefix or index.startswith(prefix + ':'):
                    r.ft(index).dropindex(delete_documents=False)
            keys = list(r.scan_iter(match=prefix + ':*'))
            if keys:
                r.unlink(*keys)
        self.addCleanup(cleanup)
        with tempfile.TemporaryDirectory(prefix='memory-http-') as tmp:
            root = Path(tmp).resolve()
            projects = [root / 'alpha', root / 'beta']
            if not explicit:
                for project in projects:
                    project.mkdir()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            environment = dict(os.environ, MEMORY_INDEX=prefix, MEMORY_PROJECT='', HF_HUB_OFFLINE='1', MEMORY_ROUTES='')
            if explicit:
                r.hset(prefix + ':meta', mapping={'corpus_domain': 'personal'})
                routes = root / 'routes.json'
                routes.write_text(json.dumps({'schema': 1, 'targets': {
                    'personal': {'domain': 'personal', 'redis_url': store.REDIS_URL, 'read_only': False}},
                    'routes': []}))
                environment['MEMORY_ROUTES'] = str(routes)
            with (root / 'server.log').open('w+', encoding='utf-8') as log:
                process = subprocess.Popen([sys.executable, '-u', str(Path(server.__file__).resolve()),
                    '--transport', 'streamable-http', '--port', str(port)], env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    deadline = time.monotonic() + 120
                    while True:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            self.fail('HTTP server failed to become ready: ' + log.read())
                        try:
                            with urlopen(base + '/health', timeout=1) as response:
                                health = json.load(response)
                            break
                        except OSError:
                            time.sleep(0.1)
                    self.assertTrue(health['model_loaded'])
                    # Windows venv python.exe is a launcher; the serving interpreter has a child PID.
                    serving_pid = health['pid']
                    self.assertIsInstance(serving_pid, int)

                    async def protocol_check():
                        async def client(n):
                            project = projects[n]
                            async def roots(ctx):
                                self.assertFalse(explicit, 'Explicit selection must not request client roots')
                                return types.ListRootsResult(roots=[types.Root(uri=project.as_uri())])
                            async with streamable_http_client(base + '/mcp') as (read, write, _):
                                async with ClientSession(read, write, list_roots_callback=roots) as session:
                                    await session.initialize()
                                    selection = {'context': 'personal', 'project': project.name} if explicit else {}
                                    saved = await session.call_tool('save_to_project_memory', {
                                        'title': project.name, 'content': f'Only {project.name} owns this decision.',
                                        **selection})
                                    self.assertFalse(saved.isError, str(saved))
                                    found = await session.call_tool('search_project_memory', {'query': 'decision', **selection})
                                    self.assertFalse(found.isError, str(found))
                                    text = found.content[0].text
                                    self.assertIn(f'Only {project.name}', text)
                                    self.assertNotIn(f'Only {projects[1 - n].name}', text)
                        await asyncio.gather(client(0), client(1))
                        # Reconnect after both sessions ended; no client roots means workdir is required.
                        async with streamable_http_client(base + '/mcp') as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                for name, arguments in [('search_project_memory', {'query': 'decision'}),
                                    ('save_to_project_memory', {'title': 'Unsafe', 'content': 'Missing project'})]:
                                    result = await session.call_tool(name, arguments)
                                    self.assertTrue(result.isError)
                                    self.assertIn('Pass workdir', result.content[0].text)
                                for project in projects:
                                    result = await session.call_tool('search_project_memory', {
                                        'query': 'decision', 'workdir': str(project),
                                        **({'context': 'personal', 'project': project.name} if explicit else {})})
                                    self.assertFalse(result.isError)
                                    self.assertIn(f'Only {project.name}', result.content[0].text)
                                if explicit:
                                    # Resource discovery reads metadata even for a document without
                                    # an embedding; no content or vector needs to enter the catalog.
                                    r.hset(prefix + ':doc:catalog', mapping={
                                        'kind': 'doc', 'project': 'alpha', 'source': 'gameplay.md'})
                                    catalog = await session.call_tool('list_memory_contexts', {})
                                    self.assertFalse(catalog.isError)
                                    contexts = json.loads(catalog.content[0].text)['contexts']
                                    self.assertEqual(contexts, [{'context': 'personal', 'domain': 'personal',
                                        'read_only': False, 'project_required': True, 'projects': ['alpha', 'beta'],
                                        'resources': {'alpha': ['gameplay.md'], 'beta': []}}])
                                    for selection, error in [({'context': 'personal'}, 'requires an explicit project'),
                                                             ({'context': 'unknown', 'project': 'alpha'}, 'Unknown'),
                                                             ({'project': 'alpha'}, 'requires context')]:
                                        result = await session.call_tool('search_project_memory', {'query': 'decision', **selection})
                                        self.assertTrue(result.isError)
                                        self.assertIn(error, result.content[0].text)
                        # The SDK's DNS rebinding protection must remain active.
                        async with httpx.AsyncClient() as client:
                            response = await client.post(base + '/mcp', headers={'Host': 'untrusted.example'}, json={})
                            self.assertEqual(response.status_code, 421)
                            response = await client.post(base + '/mcp', headers={'Origin': 'https://untrusted.example'}, json={})
                            self.assertEqual(response.status_code, 403)
                    async def bounded_check():
                        with anyio.fail_after(60):
                            await protocol_check()
                    anyio.run(bounded_check)
                    with urlopen(base + '/health', timeout=2) as response:
                        self.assertEqual(json.load(response)['pid'], serving_pid)
                    log.flush()
                    log.seek(0)
                    self.assertEqual(log.read().count('Model ready in'), 1)
                    self.assertEqual(len(list(r.scan_iter(match=prefix + ':note:*'))), 2)
                    if explicit:
                        for key in r.scan_iter(match=prefix + ':note:*'):
                            self.assertEqual(r.hmget(key, 'branch', 'worktree'), ['', ''])
                finally:
                    if sys.platform == 'win32' and process.poll() is None:
                        # Kill the owned launcher tree, including the actual venv interpreter.
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    elif process.poll() is None:
                        process.terminate()
                    process.wait(timeout=15)
