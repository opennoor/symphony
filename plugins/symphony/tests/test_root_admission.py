"""The root is never blocked from its own tools (best-effort rule: never stop the user).

The 1.x admission gate denied Bash/Write/Edit until a Symphony agent started,
so even a one-line command waited on the pipeline. Routing is guidance now.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import types
import unittest

from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.routing import assessor_selection, fast_lead_selection, profiles_for, snapshot_for
from plugins.symphony.symphony.runtime import handle, _root_admission_key
from plugins.symphony.symphony.store import StateStore
from plugins.symphony.scripts.generate_hooks import generated


class RootAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.store = StateStore(self.root / 'state')
        self.env = {'SYMPHONY_STATE_DIR': str(self.store.root), 'SYMPHONY_PROVIDER': 'claude',
                    'SYMPHONY_PROFILE': profiles_for('claude')[0]['id']}
        self.sequence = 0
        self.hook('SessionStart')

    def hook(self, event, session='root', project=None, **fields):
        self.sequence += 1
        return handle({'hook_event_name': event, 'session_id': session,
                       'cwd': str(project or self.project), 'prompt_id': f'context-{self.sequence}',
                       **fields}, self.env)

    def prompt(self, text, **fields):
        return self.hook('UserPromptSubmit', prompt=text, **fields)

    def tool(self, name='Bash', **fields):
        return self.hook('PreToolUse', tool_name=name, **{'tool_input': {'command': 'echo harmless'}, **fields})

    def pending(self, session='root'):
        key = _root_admission_key('claude', {'session_id': session})
        return key in self.store.load(self.project).configuration.get('root_admission_intents', {})

    def assertAllowed(self, result):
        output = json.loads(result.stdout) if result.stdout else {}
        self.assertNotEqual(output.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_root_tools_are_never_blocked_in_an_enabled_objective(self):
        self.prompt('/symphony:enable')
        self.prompt('Implement the requested change')
        for tool in ('Bash', 'PowerShell', 'Write', 'Edit', 'NotebookEdit', 'Read', 'Grep'):
            with self.subTest(tool=tool):
                self.assertAllowed(self.tool(tool))
                self.assertAllowed(self.tool(tool, prompt_id=None))

    def test_one_shot_start_and_controls_never_block_root_tools(self):
        for prompt in ('/symphony:start Implement one task', '/symphony:enable Implement one task',
                       '/symphony:status', '/symphony:bypass Direct task', '/symphony:stop --force'):
            with self.subTest(prompt=prompt):
                self.prompt(prompt)
                self.assertAllowed(self.tool())

    def test_codex_root_tools_remain_unblocked(self):
        codex = {**self.env, 'SYMPHONY_PROVIDER': 'codex', 'SYMPHONY_PROFILE': profiles_for('codex')[0]['id']}
        result = handle({'hook_event_name': 'PreToolUse', 'session_id': 'codex-root',
                         'cwd': str(self.project), 'tool_name': 'Bash', 'tool_input': {}}, codex)
        self.assertAllowed(result)

    def test_generated_matcher_observes_only_required_builtins(self):
        root = Path(__file__).resolve().parents[1]
        document = json.loads(generated(root)[root / 'hooks/hooks.json'])
        self.assertEqual(document['hooks']['PreToolUse'][0]['matcher'],
                         'Agent|SendMessage|Bash|PowerShell|Write|Edit|NotebookEdit')
        self.assertEqual(document['hooks']['PostToolUse'][0]['matcher'], 'Agent')


if __name__ == '__main__':
    unittest.main()
