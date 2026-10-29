"""Shell client regressions: output bounds, compact context listing, selector precedence and the
retry hint after a failed directory-routed call."""
import contextlib
import json
import os
import unittest
from unittest.mock import patch

from mcp import types

import memory_cli as cli

CATALOG = json.dumps({'contexts': [
    {'context': 'reference', 'read_only': True, 'project_required': False, 'projects': ['nova']}]})


class BoundedTests(unittest.TestCase):
    def result(self, *entries):
        return 'header line\nsync line\n\n' + cli.ENTRY_SEPARATOR.join(entries)

    def test_short_or_unbounded_output_is_unchanged(self):
        text = self.result('[1] a', '[2] b')
        self.assertEqual(cli.bounded(text, 1000), text)
        self.assertEqual(cli.bounded(text * 50, 0), text * 50)

    def test_each_entry_keeps_an_equal_share(self):
        long, short = '[1] doc A.md:1-2\n' + 'x' * 5000, '[2] doc B.md:3-4\nshort'
        out = cli.bounded(self.result(long, short, long.replace('[1]', '[3]')), 1500)
        header, _, body = out.partition('\n\n')
        entries = body.split(cli.ENTRY_SEPARATOR)
        self.assertEqual(header, 'header line\nsync line')
        self.assertEqual([e.split('\n')[0] for e in entries], ['[1] doc A.md:1-2', '[2] doc B.md:3-4', '[3] doc A.md:1-2'])
        self.assertEqual(entries[1], short)
        self.assertIn('more characters; --max-chars 0', entries[0])
        self.assertLess(len(out), 1700)


class ContextsTests(unittest.TestCase):
    def test_one_line_per_context(self):
        payload = json.dumps({'search_guidance': 'long text', 'contexts': [
            {'context': 'reference', 'read_only': True, 'project_required': False, 'projects': ['nova']},
            {'context': 'projects', 'read_only': False, 'project_required': True, 'projects': []}]})
        self.assertEqual(cli.describe_contexts(payload),
                         'reference (read-only, -c reference): nova\n'
                         'projects (writable, -c projects -p <project>): no projects yet')

    def test_document_topics_name_what_each_project_covers(self):
        payload = json.dumps({'contexts': [
            {'context': 'reference', 'read_only': True, 'project_required': False, 'projects': ['nova'],
             'resources': {'nova': ['CAT.md', 'docs/DUC.md']}},
            {'context': 'projects', 'read_only': False, 'project_required': True,
             'projects': ['scribe', 'hub', 'memory'],
             'resources': {'scribe': ['projects/Scribe.md'], 'hub': ['projects/hub.md', 'projects/api.md'], 'memory': []}}]})
        self.assertEqual(cli.describe_contexts(payload),
                         'reference (read-only, -c reference): nova [CAT, DUC]\n'
                         'projects (writable, -c projects -p <project>): scribe, hub [api], memory')

    def test_unrouted_server_message(self):
        self.assertEqual(cli.describe_contexts(json.dumps({'contexts': [], 'message': 'pass workdir'})), 'pass workdir')


class SelectionTests(unittest.TestCase):
    def test_flags_override_environment_and_directory(self):
        with patch.dict(os.environ, {'MEMORY_CONTEXT': 'projects', 'MEMORY_PROJECT': 'ds4'}):
            self.assertEqual(cli.selection('reference', ''), {'context': 'reference', 'project': ''})
            self.assertEqual(cli.selection('', ''), {'context': 'projects', 'project': 'ds4'})

    def test_directory_routing_without_selectors(self):
        with patch.dict(os.environ, {'MEMORY_CONTEXT': '', 'MEMORY_PROJECT': ''}):
            self.assertEqual(cli.selection('', ''), {'workdir': os.getcwd()})


@contextlib.asynccontextmanager
async def fake_transport(url):
    yield None, None, None


class FakeSession:
    """Answers each tool from a table and records the calls; no server or transport."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        pass

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        text, is_error = self.answers[name]
        return types.CallToolResult(content=[types.TextContent(type='text', text=text)], isError=is_error)


class RetryHintTests(unittest.IsolatedAsyncioTestCase):
    def session(self, answers):
        session = FakeSession(answers)
        self.enterContext(patch.object(cli, 'streamable_http_client', fake_transport))
        self.enterContext(patch.object(cli, 'ClientSession', lambda *args, **kwargs: session))
        return session

    async def test_directory_routed_failure_lists_contexts(self):
        session = self.session({'search_project_memory': ('No memory route for this directory.', True),
                                'list_memory_contexts': (CATALOG, False)})
        with self.assertRaises(cli.ToolError) as raised:
            await cli.call('search_project_memory', {'query': 'q', 'workdir': '/tmp'})
        self.assertEqual(str(raised.exception), 'No memory route for this directory.\n'
                         'Retry with -c <context> -p <project>:\nreference (read-only, -c reference): nova')
        self.assertEqual(session.calls, ['search_project_memory', 'list_memory_contexts'])

    async def test_explicit_selection_failure_is_left_alone(self):
        session = self.session({'save_to_project_memory': ('This corpus is read-only.', True)})
        with self.assertRaises(cli.ToolError) as raised:
            await cli.call('save_to_project_memory', {'title': 't', 'content': 'c', 'context': 'reference', 'project': ''})
        self.assertEqual(str(raised.exception), 'This corpus is read-only.')
        self.assertEqual(session.calls, ['save_to_project_memory'])

    async def test_malformed_catalog_keeps_the_original_error(self):
        self.session({'search_project_memory': ('No memory route.', True),
                      'list_memory_contexts': ('not json', False)})
        with self.assertRaises(cli.ToolError) as raised:
            await cli.call('search_project_memory', {'query': 'q', 'workdir': '/tmp'})
        self.assertEqual(str(raised.exception), 'No memory route.')


if __name__ == '__main__':
    unittest.main()
