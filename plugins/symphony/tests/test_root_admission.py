"""Root execution requires actual managed admission; discovery remains usable."""
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

    def assertDenied(self, result):
        output = json.loads(result.stdout)
        self.assertEqual(output['hookSpecificOutput']['hookEventName'], 'PreToolUse')
        self.assertEqual(output['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('accepted Start', output['hookSpecificOutput']['permissionDecisionReason'])

    def assertAllowed(self, result):
        output = json.loads(result.stdout) if result.stdout else {}
        self.assertNotEqual(output.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def objective(self):
        self.prompt('/symphony:enable')
        self.prompt('Implement the requested change')
        self.assertTrue(self.pending())

    def spawn(self, role='assessor'):
        boost = self.store.load(self.project).configuration.get('assessor_boosts', {}).get('claude:root', 'off')
        selected = (assessor_selection(snapshot_for('claude', self.env['SYMPHONY_PROFILE']), boost)
                    if role == 'assessor' else fast_lead_selection(snapshot_for('claude', self.env['SYMPHONY_PROFILE'])))
        kind = f"symphony:symphony-{role}-{selected['model']}-{selected['effort']}"
        prompt = f'SYMPHONY_ROLE: {role}\n'
        if role == 'lead':
            prompt += 'SYMPHONY_FAST_ROUTE: lead\n'
        packet = {'subagent_type': kind, 'prompt': prompt + 'Bounded task'}
        result = self.hook('PreToolUse', tool_name='Agent', tool_use_id='launch-child', tool_input=packet)
        self.assertAllowed(result)
        return kind, packet

    def start(self, kind, identity='admitted', **fields):
        return self.hook('SubagentStart', agent_id=identity, agent_type=kind,
                         parent_thread_id='root', **fields)

    def test_enabled_objective_denies_only_exact_root_execution_tools(self):
        self.objective()
        initial = self.store.load(self.project).active_runs
        for tool in ('Bash', 'PowerShell', 'Write', 'Edit', 'NotebookEdit'):
            with self.subTest(tool=tool):
                self.assertDenied(self.tool(tool))
                self.assertDenied(self.tool(tool, prompt_id='different-context', agent_type='custom-root-agent'))
                self.assertDenied(self.tool(tool, prompt_id=None))
        for tool in ('Read', 'Glob', 'Grep', 'Skill', 'AskUserQuestion', 'mcp__discovery__read'):
            with self.subTest(tool=tool):
                self.assertAllowed(self.tool(tool))
        self.assertEqual(self.store.load(self.project).active_runs, initial)
        record = self.store.load(self.project).configuration['root_admission_intents']
        self.assertNotIn('Implement', json.dumps(record))
        self.assertNotIn('context-', json.dumps(record))

    def test_no_flag_legacy_disabled_and_codex_remain_unchanged(self):
        self.assertAllowed(self.tool())
        self.prompt('Ordinary disabled task')
        self.assertFalse(self.pending())
        self.assertAllowed(self.tool())
        self.objective()
        codex = {**self.env, 'SYMPHONY_PROVIDER': 'codex', 'SYMPHONY_PROFILE': profiles_for('codex')[0]['id']}
        result = handle({'hook_event_name': 'PreToolUse', 'session_id': 'codex-root',
                         'cwd': str(self.project), 'tool_name': 'Bash', 'tool_input': {}}, codex)
        self.assertAllowed(result)

    def test_control_detours_cannot_authorize_execution_or_parse_shell_arguments(self):
        self.objective()
        for control in ('help', 'status', 'version', 'agents', 'boost off', 'proceed', 'enable', 'start', 'bypass'):
            with self.subTest(control=control):
                self.prompt('/symphony:' + control)
                self.assertTrue(self.pending())
                self.assertDenied(self.tool(tool_input={'command': 'SYMPHONY_CONTROL: status'}))
        self.assertDenied(self.tool(tool_input={'command': '/symphony:bypass task'}))

    def test_native_stop_retains_and_explicit_no_run_stop_cancels(self):
        self.objective()
        self.hook('Stop', stop_hook_active=False)
        self.hook('Stop', stop_hook_active=True)
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())
        self.prompt('/symphony:stop')
        self.assertFalse(self.pending())
        self.assertAllowed(self.tool())
        self.prompt('Next governed task')
        self.assertTrue(self.pending())

    def test_bypass_disable_and_force_stop_lifecycle(self):
        for cancellation in ('/symphony:bypass Direct task', '/symphony:stop --force', '/symphony:disable'):
            with self.subTest(cancellation=cancellation):
                self.objective()
                self.prompt(cancellation)
                self.assertFalse(self.pending())
                self.assertAllowed(self.tool())
                self.prompt('/symphony:enable')
                self.prompt('Next task re-arms admission')
                self.assertTrue(self.pending())
        self.prompt('/symphony:disable')
        self.prompt('Disabled ordinary task')
        self.assertAllowed(self.tool())

    def test_disabled_one_shot_and_enable_with_task_are_governed(self):
        for prompt in ('/symphony:start Implement one task', '/symphony:enable Implement one task'):
            with self.subTest(prompt=prompt):
                self.prompt('/symphony:disable')
                self.prompt(prompt)
                self.assertTrue(self.pending())
                self.assertDenied(self.tool())
                self.prompt('/symphony:stop')
        self.prompt('/symphony:disable')
        self.prompt('/symphony:enable')
        self.assertFalse(self.pending())
        self.assertAllowed(self.tool())

    def test_child_and_parallel_root_and_project_isolation(self):
        self.objective()
        self.assertAllowed(self.tool(agent_id='native-child', agent_type='symphony-worker'))
        self.assertAllowed(self.tool(subagent_id='native-child'))
        self.hook('SessionStart', session='other-root')
        self.assertAllowed(self.tool(session='other-root'))
        self.prompt('Parallel task', session='other-root')
        self.assertDenied(self.tool(session='other-root'))
        self.prompt('/symphony:bypass Task', session='other-root')
        self.assertDenied(self.tool())
        other = self.root / 'other-project'
        other.mkdir()
        self.hook('SessionStart', session='foreign-project', project=other)
        self.assertAllowed(self.tool(session='foreign-project', project=other))

    def test_validation_denied_or_failed_native_spawn_keeps_gate(self):
        self.objective()
        self.assertDenied(self.tool())
        bad = self.hook('PreToolUse', tool_name='Agent', tool_input={'prompt': 'missing role'})
        self.assertIn('permissionDecision', bad.stdout)
        self.assertTrue(self.pending())
        kind, packet = self.spawn()
        self.assertTrue(self.pending())  # PreToolUse created a run but no child exists yet.
        self.assertDenied(self.tool())
        self.hook('PostToolUseFailure', tool_name='Agent', tool_input=packet, tool_use_id='launch-child')
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())
        self.assertIsNotNone(self.store.load(self.project).active_run)
        self.hook('SubagentStart', agent_id='foreign-worker', agent_type='symphony:symphony-worker-sonnet-medium')
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())
        self.start(kind)
        self.assertFalse(self.pending())
        self.assertAllowed(self.tool())

    def test_actual_assessor_and_fast_start_consume_only_own_intent(self):
        for role in ('assessor', 'lead'):
            with self.subTest(role=role):
                self.store.save(self.project, ProjectState(enabled=True))
                self.hook('SessionStart')
                self.prompt('Governed task')
                self.hook('SessionStart', session='parallel')
                self.prompt('Parallel task', session='parallel')
                kind, _ = self.spawn(role)
                self.start(kind)
                self.assertFalse(self.pending())
                self.assertTrue(self.pending('parallel'))
                self.assertAllowed(self.tool())
                self.assertDenied(self.tool(session='parallel'))
                before = self.store.load(self.project).active_runs['claude:root']
                self.prompt('Continue same managed task')
                self.assertFalse(self.pending())
                self.assertEqual(self.store.load(self.project).active_runs['claude:root'].run_id, before.run_id)

    def test_mismatched_start_does_not_consume_intent(self):
        self.objective()
        self.spawn()
        self.start('symphony:symphony-assessor-foreign-model-high')
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())

    def test_mismatched_fast_start_does_not_consume_intent(self):
        self.objective()
        self.spawn('lead')
        self.start('symphony:symphony-lead-foreign-model-medium')
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())

    def test_legacy_active_run_is_not_retroactively_armed(self):
        run = RunState('legacy', 'Existing task', 'recovering', provider='claude', session_id='root',
                       lead_identity='existing-lead', owner_generation=1,
                       delegations=(Delegation('existing-lead', 'lead', 'Existing work', 'interrupted', 'opus', 'high'),))
        self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                   active_runs={'claude:root': run}))
        self.prompt('Continue the existing task')
        self.assertFalse(self.pending())
        self.assertAllowed(self.tool())
        self.assertEqual(self.store.load(self.project).active_run.lead_identity, 'existing-lead')

    def test_outstanding_roots_are_not_evicted_and_cancel_independently(self):
        records = {'claude:' + hashlib.sha256(str(n).encode()).hexdigest():
                   {'version': 1, 'prompt_context': ''} for n in range(65)}
        self.store.save(self.project, ProjectState(enabled=True,
            configuration={'root_admission_intents': records}))
        self.prompt('Another root task')
        loaded = self.store.load(self.project)
        self.assertEqual(len(loaded.configuration['root_admission_intents']), 66)
        self.assertDenied(self.tool())
        self.spawn()
        self.start('symphony:symphony-assessor-foreign-model-high')
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())
        self.prompt('/symphony:bypass Explicit opt-out')
        self.assertFalse(self.pending())
        self.assertEqual(self.store.load(self.project).configuration['root_admission_intents'], records)
        self.assertAllowed(self.tool())
        self.prompt('/symphony:disable')
        self.assertNotIn('root_admission_intents', self.store.load(self.project).configuration)

    def test_boost_and_unavailable_fast_floor_keep_assessor_admission(self):
        for profile, boost in ((profiles_for('claude')[0]['id'], 'xhigh'), ('sonnet', 'off')):
            with self.subTest(profile=profile, boost=boost):
                self.env['SYMPHONY_PROFILE'] = profile
                self.store.save(self.project, ProjectState(enabled=True))
                self.hook('SessionStart')
                self.prompt('Substantive work')
                self.prompt('/symphony:boost ' + boost)
                self.assertDenied(self.tool())
                kind, _ = self.spawn()
                self.start(kind)
                self.assertFalse(self.pending())
                self.assertAllowed(self.tool())

    def test_successful_launch_result_without_start_is_not_admission(self):
        self.objective()
        _, packet = self.spawn()
        self.hook('PostToolUse', tool_name='Agent', tool_use_id='launch-child',
                  tool_input=packet, tool_response={'agentId': 'not-started', 'status': 'async_launched'})
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())
        self.prompt('/symphony:stop')  # An open but unadmitted run still requires recovery.
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())

    def test_child_user_prompt_cannot_cancel_root_intent(self):
        self.objective()
        self.prompt('/symphony:bypass Task', agent_id='child')
        self.assertTrue(self.pending())
        self.assertDenied(self.tool())

    def test_malformed_present_intent_cannot_exempt_execution(self):
        for invalid in (None, [], 'invalid'):
            with self.subTest(invalid=invalid):
                self.store.save(self.project, ProjectState(enabled=True,
                    configuration={'root_admission_intents': invalid}))
                result = self.tool()
                self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertIn('/symphony:disable', result.stdout)
                self.prompt('/symphony:disable')
                self.assertAllowed(self.tool())

    def test_actual_released160_codec_and_heartbeat_preserve_configuration(self):
        self.objective()
        expected = self.store.load(self.project).configuration
        fixture = Path(__file__).parent / 'fixtures/released-v1.6.0'
        modules = {}
        for name in ('store', 'reducer'):
            raw = (fixture / (name + '.py.txt')).read_bytes()
            provenance = json.loads((fixture / ('provenance.json' if name == 'store'
                                               else 'reducer-provenance.json')).read_text(encoding='utf-8'))
            self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance['sha256'])
            self.assertEqual(provenance['tag'], 'v1.6.0')
            self.assertEqual(provenance['commit'], '729f02db40e7093f65b5db71a4681a53a1e68693')
            module = types.ModuleType('plugins.symphony.symphony.released_160_' + name)
            module.__package__ = 'plugins.symphony.symphony'
            exec(compile(raw, 'released-v1.6.0/' + name + '.py', 'exec'), module.__dict__)
            modules[name] = module
        old = modules['store'].StateStore(self.store.root)
        state = old.load(self.project)
        state, _ = modules['reducer'].reduce(state, Event('old-heartbeat', 'session_heartbeat',
            '2026-10-01T20:00:00+00:00', {'provider': 'claude', 'session_id': 'old-root',
                                        'profile': self.env['SYMPHONY_PROFILE']}))
        old.save(self.project, state)
        self.assertEqual(self.store.load(self.project).configuration, expected)
        self.assertDenied(self.tool())

    def test_generated_matcher_observes_only_required_builtins(self):
        root = Path(__file__).resolve().parents[1]
        document = json.loads(generated(root)[root / 'hooks/hooks.json'])
        self.assertEqual(document['hooks']['PreToolUse'][0]['matcher'],
                         'Agent|SendMessage|Bash|PowerShell|Write|Edit|NotebookEdit')
        self.assertEqual(document['hooks']['PostToolUse'][0]['matcher'], 'Agent')


if __name__ == '__main__':
    unittest.main()
