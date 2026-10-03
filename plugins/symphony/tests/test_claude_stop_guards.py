"""A repeated native Stop releases the turn without resolving uncertain work."""
from contextlib import nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from plugins.symphony.symphony.adapters import event_from_payload, render
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.reducer import reduce
# Strict ownership/credit reducer; public host-turn liveness has separate tests.
from plugins.symphony.symphony.runtime import _handle_core as handle, _render_actions
from plugins.symphony.symphony.store import StateStore


class ClaudeStopGuardTests(unittest.TestCase):
    def test_normal_reducer_retry_renders_only_actionable_system_message(self):
        state = ProjectState(active_run=RunState('run', 'unfinished', provider='claude',
            lead_identity='lead', delegations=(Delegation('worker', 'worker', 'task', 'working', '', ''),)))
        first = Event('first', 'stop_requested', '2026-10-01T00:00:00+00:00',
                      {'provider': 'claude', 'hook_event_name': 'Stop'})
        blocked, actions = reduce(state, first)
        output = json.loads(render('claude', _render_actions(actions, blocked, 'claude', first.kind), 'Stop').stdout)
        self.assertEqual(output['decision'], 'block')
        retry = Event('retry', 'stop_requested', first.observed_at, {**first.payload, 'stop_hook_active': True})
        released, actions = reduce(blocked, retry)
        output = json.loads(render('claude', _render_actions(actions, released, 'claude', retry.kind), 'Stop').stdout)
        self.assertEqual(set(output), {'systemMessage'})
        self.assertIsInstance(output['systemMessage'], str)
        self.assertIn('/symphony:status', output['systemMessage'])
        self.assertNotIn('blocked', output['systemMessage'])
        self.assertEqual(released.active_run, state.active_run)
        self.assertEqual(released.recent_runs, ())
        codex = Event('codex-retry', 'stop_requested', first.observed_at,
                      {'provider': 'codex', 'hook_event_name': 'Stop', 'stop_hook_active': True})
        released, actions = reduce(state, codex)
        output = json.loads(render('codex', _render_actions(actions, released, 'codex', codex.kind), 'Stop').stdout)
        self.assertEqual(set(output), {'systemMessage'})
        self.assertIn('$symphony:symphony status', output['systemMessage'])
        self.assertEqual(released.active_run, state.active_run)

    def test_every_pre_reducer_guard_preserves_state_and_releases_native_retry_on_both_providers(self):
        for provider in ('claude', 'codex'):
            for scenario in ('queued-conflict', 'owner-unavailable', 'multiple-owners',
                             'alias-unavailable', 'overflow', 'alias-overflow', 'missing-state'):
                with self.subTest(provider=provider, scenario=scenario), TemporaryDirectory() as directory:
                    root = Path(directory)
                    project = root / 'project'
                    project.mkdir()
                    store = StateStore(root / 'state')
                    environ = {'SYMPHONY_STATE_DIR': str(store.root), 'SYMPHONY_PROVIDER': provider,
                               'SYMPHONY_PROFILE': 'full'}
                    run = RunState('run', 'unfinished task', provider=provider, session_id='root',
                                   lead_identity='lead', delegations=(
                                       Delegation('lead', 'lead', 'task', 'working', '', ''),))
                    if scenario != 'missing-state':
                        store.save(project, ProjectState(active_run=run))
                    if scenario == 'multiple-owners':
                        other = root / 'other'
                        other.mkdir()
                        store.save(other, ProjectState(active_run=run))
                    else:
                        store.bind_session(provider, 'root', store._path(project),
                                           scenario == 'owner-unavailable',
                                           None if scenario == 'missing-state' else project)
                    if scenario in {'queued-conflict', 'overflow', 'alias-overflow'}:
                        inbox_session = 'lead' if scenario == 'alias-overflow' else 'root'
                        if inbox_session != 'root':
                            store.bind_session(provider, inbox_session, store._path(project),
                                               False, project, 'root')
                        event = event_from_payload(provider, {
                            'session_id': inbox_session, 'hook_event_name': 'SubagentStop',
                            'agent_id': 'unowned', 'parent_thread_id': 'root',
                            'last_assistant_message': 'x' * (140_000 if 'overflow' in scenario else 1),
                        })
                        store.queue_session_event(provider, inbox_session, event, ambiguous_owner=True)
                    request = {'session_id': 'root', 'cwd': str(project), 'hook_event_name': 'Stop'}

                    def guard():
                        if scenario == 'owner-unavailable':
                            return patch.object(StateStore, 'active_owner_paths', return_value=None)
                        if scenario == 'alias-unavailable':
                            return patch.object(StateStore, 'aliases_for_owner', side_effect=OSError('unavailable'))
                        return nullcontext()

                    # No repeat-release for explicit controls or truthy nonbooleans.
                    requests = [dict(request, stop_hook_active=value) for value in (False, 'true', 1)]
                    requests.append(dict(request, hook_event_name='UserPromptSubmit', stop_hook_active=True,
                                         prompt='/symphony:stop' if provider == 'claude' else '$symphony:symphony stop'))
                    for payload in requests:
                        with guard():
                            result = handle(payload, environ)
                        self.assertEqual(json.loads(result.stdout).get('decision'), 'block')

                    before = store.load(project) if scenario != 'missing-state' else None
                    inbox = store.session_record(provider, 'root')
                    alias_inbox = store.session_record(provider, 'lead')
                    with guard():
                        result = handle(dict(request, stop_hook_active=True), environ)
                    output = json.loads(result.stdout)
                    if provider in {'claude', 'codex'}:
                        self.assertEqual(set(output), {'systemMessage'})
                        self.assertIn('host turn only', output['systemMessage'])
                    else:
                        self.assertEqual(output.get('decision'), 'block')
                    self.assertNotIn('additionalContext', result.stdout)
                    self.assertEqual(store.session_record(provider, 'root'), inbox)
                    self.assertEqual(store.session_record(provider, 'lead'), alias_inbox)
                    if before is not None:
                        self.assertEqual(store.load(project), before)
                    else:
                        self.assertFalse(store._path(project).exists())
