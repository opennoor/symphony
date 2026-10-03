"""Released freeform risk evidence survives strict new-assessment validation."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from plugins.symphony.symphony.routing import (
    Assessment, profiles_for, resolve_tier, route_for, route_for_recorded, snapshot_for,
)
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


FIXTURE = Path(__file__).parent / 'fixtures/released-v1.6.0'


class LegacyRiskTests(unittest.TestCase):
    def fixture(self, provider, risk, root, project):
        raw = (FIXTURE / 'accepted-risk.json').read_bytes()
        provenance = json.loads((FIXTURE / 'accepted-risk-provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance['fixture_sha256'])
        self.assertEqual(provenance['tag'], 'v1.6.0')
        document = json.loads(raw)[provider + '/' + risk]
        store = StateStore(root / 'state')
        path = store._path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding='utf-8')
        loaded = store.load(project)
        self.assertEqual(loaded.active_run.assessment['risk'], 'low' if risk == 'low-drift' else risk)
        self.assertNotIn('substantive_contract', loaded.active_run.assessment)
        return store, copy.deepcopy(loaded.active_run.assessment)

    @staticmethod
    def hook(provider, event, project, env, **fields):
        return handle({'provider': provider, 'session_id': 'legacy-root', 'cwd': str(project),
                       'hook_event_name': event, **fields}, env)

    @staticmethod
    def packet(provider, model, effort, risk='normal', size='small', complexity='simple'):
        report = 'SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: ' + json.dumps({
            'size': size, 'complexity': complexity, 'risk': risk}) + '\nContinue retained work'
        if provider == 'codex':
            return {'tool_name': 'spawn_agent', 'tool_input': {
                'message': report, 'model': model, 'reasoning_effort': effort}}
        return {'tool_name': 'Agent', 'tool_input': {
            'subagent_type': f'symphony:symphony-lead-{model}-{effort}', 'prompt': report}}

    def test_released_persisted_risk_reresolves_through_proceed_and_lead_registration(self):
        for provider in ('codex', 'claude'):
            for risk in ('low', 'material concerns', 'high'):
                for prepared in (False, True):
                    with self.subTest(provider=provider, risk=risk, prepared=prepared), TemporaryDirectory() as directory:
                        root = Path(directory)
                        project = root / 'project'
                        project.mkdir()
                        store, original = self.fixture(provider, risk, root, project)
                        profile = profiles_for(provider)[0]['id']
                        env = {'SYMPHONY_STATE_DIR': str(store.root), 'SYMPHONY_PROFILE': profile,
                               'SYMPHONY_PROVIDER': provider}
                        self.hook(provider, 'SessionStart', project, env)
                        control = '$symphony:symphony proceed' if provider == 'codex' else '/symphony:proceed'
                        self.hook(provider, 'UserPromptSubmit', project, env, prompt=control)
                        effective = 'high' if risk == 'high' else 'normal'
                        selected = resolve_tier(route_for(Assessment('small', 'simple', effective)), snapshot_for(provider, profile))
                        model, effort = selected['lead_model'], selected['lead_effort']
                        if prepared:
                            # A newly submitted low/freeform marker still cannot pass ingress.
                            rejected = self.hook(provider, 'PreToolUse', project, env,
                                **self.packet(provider, model, effort, 'low'))
                            self.assertIn('valid SYMPHONY_ROUTE', rejected.stdout)
                            accepted = self.hook(provider, 'PreToolUse', project, env,
                                **self.packet(provider, model, effort, effective))
                            self.assertNotIn('"decision": "block"', accepted.stdout)
                            self.assertNotIn('"permissionDecision": "deny"', accepted.stdout)
                        # Without a pending intent, this also exercises session-profile
                        # re-resolution at native lead registration and its clamp check.
                        self.hook(provider, 'SubagentStart', project, env, agent_id='retained-lead',
                            agent_type=(f'symphony:symphony-lead-{model}-{effort}' if provider == 'claude'
                                        else 'symphony_lead_' + model.replace('-', '_').replace('.', '_') + '_' + effort),
                            model=model, model_reasoning_effort=effort, parent_thread_id='legacy-root',
                            **({'turn_id': 'lead-turn'} if provider == 'codex' else {'prompt_id': 'request-context'}))
                        run = store.load(project).active_run
                        self.assertEqual(run.lead_identity, 'retained-lead')
                        self.assertEqual(run.owner_generation, 1)
                        self.assertEqual(run.run_id, 'legacy-run')
                        for key in ('risk', 'rationale', 'route', 'topology'):
                            self.assertEqual(run.assessment[key], original[key])
                        self.assertNotIn('_lead_route_mismatch', run.assessment)
                        self.assertEqual(run.assessment['_lead_expected_route']['model'], model)
                        self.assertEqual(run.assessment['_lead_expected_route']['effort'], effort)

    def test_legacy_freeform_risk_does_not_bypass_weaker_profile_consent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            store, original = self.fixture('codex', 'low-drift', root, project)
            env = {'SYMPHONY_STATE_DIR': str(store.root), 'SYMPHONY_PROFILE': 'base', 'SYMPHONY_PROVIDER': 'codex'}
            self.hook('codex', 'SessionStart', project, env)
            selected = resolve_tier(route_for(Assessment('medium', 'complex')), snapshot_for('codex', 'base'))
            packet = self.packet('codex', selected['lead_model'], selected['lead_effort'], size='medium', complexity='complex')
            blocked = self.hook('codex', 'PreToolUse', project, env, **packet)
            self.assertIn('proceed', blocked.stdout)
            self.assertIn('"decision": "block"', blocked.stdout)
            self.hook('codex', 'UserPromptSubmit', project, env, prompt='$symphony:symphony proceed')
            allowed = self.hook('codex', 'PreToolUse', project, env, **packet)
            self.assertNotIn('"decision": "block"', allowed.stdout)
            self.assertEqual(store.load(project).active_run.assessment['risk'], original['risk'])

    def test_only_accepted_absent_contract_legacy_records_get_freeform_compatibility(self):
        documents = json.loads((FIXTURE / 'accepted-risk.json').read_text(encoding='utf-8'))
        recorded = documents['codex/low']['active_run']['assessment']
        self.assertEqual(route_for_recorded(recorded).risk, 'normal')
        for value in (None, False, {}, {'version': 1}, {'version': 99}):
            with self.subTest(contract=value), self.assertRaises(ValueError):
                route_for_recorded({**recorded, 'substantive_contract': value})
        for route in (None, {}, {'lead_model': '', 'lead_effort': 'medium'}):
            with self.subTest(route=route), self.assertRaises(ValueError):
                route_for_recorded({**recorded, 'route': route})
        for risk in (None, False, 1, [], {}):
            with self.subTest(risk=risk), self.assertRaises(ValueError):
                route_for_recorded({**recorded, 'risk': risk})
