#!/usr/bin/env python3
"""Private native SendMessage capture and public copied-state replay.

No authentication/configuration files are copied into replay evidence. Native
root/child identity and project strings stay unchanged; only state/native home
directories are redirected. Public output contains counts and booleans.
"""

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch


def captured_sequence_sources(state, session, private_dir, adapters, evidence, model, *, sequences=None):
    runs = [run for run in (*state.active_runs.values(), *state.recent_runs)
            if run.provider == 'claude' and run.session_id == session]
    if len(runs) != 1:
        raise RuntimeError('native continuation has no unique retained owner')
    if sequences is None:
        sequences = runs[0].assessment.get('_claude_sendmessage_sequences', ())
    if not sequences:
        raise RuntimeError('native continuation lacks committed sequence proof')
    snapshots = []
    for path in private_dir.glob('*.json'):
        capture = json.loads(path.read_text(encoding='utf-8'))
        canonical = capture.get('canonical')
        if isinstance(canonical, dict):
            snapshots.append(canonical)
    events = {}
    for sequence in sequences:
        for witness in sequence['sources']:
            matches = []
            for snapshot in snapshots:
                if snapshot.get('event_id') != witness['event_id'] or snapshot.get('kind') != witness['kind']:
                    continue
                event = model.Event(snapshot['event_id'], snapshot['kind'], witness['observed_at'], snapshot['payload'])
                if evidence.claude_sendmessage_source_hash(event, session) == witness['hash']:
                    matches.append(event)
            # Identical observer retries are one source; different canonical
            # payload snapshots sharing an ID must not be merged.
            unique = {(event.event_id, event.kind, event.observed_at,
                       json.dumps(event.payload, sort_keys=True)): event for event in matches}
            if len(unique) != 1:
                raise RuntimeError('native callback capture does not match its exact durable source')
            event = next(iter(unique.values()))
            previous = events.get(event.event_id)
            if previous is not None and previous != event:
                raise RuntimeError('native callback identity is ambiguous across turns')
            events[event.event_id] = event
    return tuple(sorted(events.values(), key=lambda event: event.observed_at))


def requested_sequence_delta(before, after, messages):
    """Require exactly the new requested native deliveries, never old proof."""
    prior = before.assessment.get('_claude_sendmessage_sequences', ())
    current = after.assessment.get('_claude_sendmessage_sequences', ())
    if (after.run_id != before.run_id or after.lead_identity != before.lead_identity
            or after.owner_generation != before.owner_generation
            or list(current[:len(prior)]) != list(prior)):
        raise RuntimeError('native continuation replaced original sequence ownership')
    delta = current[len(prior):]
    turns = [turn for sequence in delta for turn in sequence['turns']]
    if (len(turns) != len(messages) or len(messages) != 2
            or [turn['message_hash'] for turn in turns] != [hashlib.sha256(message.encode()).hexdigest()
                                                          for message in messages]
            or any(sequence['archived_at'] < before.updated_at
                   or sequence['lead'] != before.lead_identity
                   or sequence['owner_generation'] != before.owner_generation for sequence in delta)
            or any(turn['called_at'] <= before.updated_at for turn in turns)
            or any(len({turn[field] for turn in turns}) != 2
                   for field in ('call_hash', 'native_event_id', 'prompt_id'))):
        raise RuntimeError('native continuation did not admit exactly the two newly requested turns')
    return tuple(delta)


def exact_delta_receipts(state, session, run, sequences):
    receipts = []
    for turn in (turn for sequence in sequences for turn in sequence['turns']):
        matching = [receipt for receipt in state.terminal_receipts
                    if receipt.get('provider') == 'claude' and receipt.get('session') == session
                    and receipt.get('run_id') == run.run_id and receipt.get('agent') == run.lead_identity
                    and receipt.get('turn') == 'prompt_id:' + turn['prompt_id']]
        if (len(matching) != 1 or matching[0].get('status') != 'completed'
                or matching[0].get('parent') != session or matching[0].get('lead') != run.lead_identity
                or matching[0].get('result') not in run.assessment.get('_terminal_event_ids', ())
                or turn['native_event_id'] + ':followup-start' not in run.assessment.get('_start_event_ids', ())
                or matching[0]['turn'] not in run.assessment.get('_terminal_turns', {}).get(run.lead_identity, ())):
            raise RuntimeError('native continuation lacks an exact newly committed receipt')
        receipts.append(dict(matching[0]))
    return tuple(receipts)


def replay_native_copies(root, original_state, native_home, project, session, events, env, runtime, store_module,
                         *, expected_sequences, expected_receipts):
    counts = {}
    for mode in ('Stop', 'SessionStart', 'status', 'failed-ack', 'partial-ack'):
        copy = root / ('replay-' + mode)
        copy.mkdir(mode=0o700)
        state_dir = copy / 'state'
        shutil.copytree(original_state, state_dir)
        home = copy / 'native-home'
        home.mkdir(mode=0o700)
        shutil.copytree(native_home / 'projects', home / 'projects')
        store = store_module.StateStore(state_dir)
        before = store.load(project)
        original = next(run for run in before.recent_runs if run.provider == 'claude' and run.session_id == session)
        for event in events:
            store.queue_session_event('claude', session, event, ambiguous_owner=True)
        copied_env = {**env, 'SYMPHONY_STATE_DIR': str(state_dir), 'CLAUDE_CONFIG_DIR': str(home),
                      'SYMPHONY_PROVIDER': 'claude'}
        payload = {'cwd': str(project), 'session_id': session,
                   'hook_event_name': 'UserPromptSubmit' if mode == 'status' else
                                      'SessionStart' if mode == 'SessionStart' else 'Stop',
                   'prompt': '/symphony:status'}
        if mode in ('failed-ack', 'partial-ack'):
            with patch.object(store_module.StateStore, 'finish_session_events', side_effect=OSError('copied ACK crash')):
                try:
                    runtime.handle(payload, copied_env)
                except OSError:
                    pass
                else:
                    raise RuntimeError('copied ACK interruption did not run')
            if mode == 'partial-ack':
                record = store.session_record('claude', session)
                store.finish_session_events(record, {events[0].event_id, events[-1].event_id})
        result = runtime.handle(payload, copied_env)
        if json.loads(result.stdout or '{}').get('decision') == 'block':
            raise RuntimeError('copied native continuation stayed blocked')
        runtime.handle({**payload, 'hook_event_name': 'Stop'}, copied_env)
        after = store.load(project)
        final = [run for run in after.recent_runs if run.provider == 'claude' and run.session_id == session]
        if (any(run.session_id == session for run in after.active_runs.values())
                or len(final) != 1 or final[0].run_id != original.run_id
                or final[0].lead_identity != original.lead_identity
                or final[0].owner_generation != original.owner_generation
                or final[0].status != 'completed' or final[0].outcome.get('status') != 'completed'
                or store.session_record('claude', session)['pending']):
            raise RuntimeError('copied native continuation changed ownership or retained callbacks')
        prior = original.assessment.get('_claude_sendmessage_sequences', ())
        sequences = final[0].assessment.get('_claude_sendmessage_sequences', ())
        if (list(sequences[:len(prior)]) != list(prior)
                or list(sequences[len(prior):]) != list(expected_sequences)
                or exact_delta_receipts(after, session, final[0], expected_sequences) != expected_receipts
                or any(dict(receipt) not in [dict(item) for item in after.terminal_receipts]
                       for receipt in before.terminal_receipts)):
            raise RuntimeError('copied replay lacks exact new proof or changed prior receipts')
        counts[mode] = {'pending_callbacks': 0, 'same_owner': True,
                        'native_turns': sum(len(sequence['turns']) for sequence in expected_sequences)}
    return counts


def check_native_sendmessage(root, candidate, baseline_env, baseline_result, timeout, budget):
    import native_managed_concurrency as managed
    candidate = candidate.resolve()
    sys.path.insert(0, str(candidate))
    from symphony import adapters, host_evidence, model, runtime, store as store_module

    case = root / 'same-worktree'
    state_dir = case / 'state'
    native_home = Path(baseline_env['CLAUDE_CONFIG_DIR'])
    owner = baseline_result['original_owners']['a']
    session, lead = owner['session_id'], owner['lead_id']
    store = store_module.StateStore(state_dir)
    record = store.session_record('claude', session)
    project = Path(record['project'])
    state = store.load(project)
    if not managed.claude_original_owner_archived(store_module._state_to_dict(state), session, owner['run_id'], lead):
        raise RuntimeError('explicit followup requires the exact completed original owner')
    private = root / 'private-sendmessage-replay'
    private.mkdir(mode=0o700)
    original_state = private / 'before-followups-state'
    shutil.copytree(state_dir, original_state)
    env = {**os.environ, **baseline_env, 'SYMPHONY_STATE_DIR': str(state_dir),
           'SYMPHONY_PROFILE': 'sonnet-5-5', 'SYMPHONY_RUNTIME_DIR': str(case / 'retained runtimes'),
           'SYMPHONY_HOOK_DECISIONS_DIR': str(case / 'hook decisions')}
    deadline = time.monotonic() + timeout
    # One root turn sends twice sequentially. The first successful child report
    # intentionally lacks an outcome; only the final report completes the task.
    messages = ['Same-work native continuation one. Do not inspect, edit, execute tools or delegate. '
                'Return exactly CONTINUATION_ACK_ONE with no outcome marker.',
                'Same-work native continuation two. Preserve the original worker evidence. '
                'Do not inspect, edit, execute tools or delegate. Return exactly these lines:\n'
                'CONTINUATION_ACK_TWO\nSYMPHONY_OUTCOME: {"status":"completed"}']
    request = ('Continue ONLY this original archived Symphony lead. Use SendMessage exactly twice, '
               'sequentially, targeting the literal original agent ID. Await the first native result '
               'before sending the second. Do not use Agent, names, extra messages, new tasks or tools. '
               'Send the following exact string messages without summarizing them: ' + json.dumps([
                   {'to': lead, 'message': message} for message in messages]) +
               '. After the second native terminal, end this root turn so Stop reconciles the same owner.')
    completed = subprocess.run([shutil.which('claude'), '--print', '--model', 'haiku',
        '--max-budget-usd', str(budget), '--permission-mode', 'bypassPermissions', '--resume', session,
        '--output-format', 'json', request], cwd=project, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, encoding='utf-8', errors='replace', shell=False,
        timeout=max(1, deadline - time.monotonic()))
    for name, content in (('root.stdout', completed.stdout), ('root.stderr', completed.stderr)):
        path = private / name
        path.write_text(content, encoding='utf-8')
        path.chmod(0o600)
    if completed.returncode:
        raise RuntimeError('native explicit SendMessage root failed')
    after = store.load(project)
    run = next(run for run in after.recent_runs if run.session_id == session and run.run_id == owner['run_id'])
    before_run = next(run for run in state.recent_runs if run.run_id == owner['run_id'])
    delta = requested_sequence_delta(before_run, run, messages)
    receipts = exact_delta_receipts(after, session, run, delta)
    sources = captured_sequence_sources(after, session, root / 'private-child-hooks', adapters,
                                        host_evidence, model, sequences=delta)
    turns = sum(len(sequence['turns']) for sequence in delta)
    replays = replay_native_copies(private, original_state, native_home, project, session,
                                   sources, env, runtime, store_module,
                                   expected_sequences=delta, expected_receipts=receipts)
    return {'case': 'archived-sendmessage-sequence', 'native_turns': turns,
            'captured_callbacks': len(sources), 'copied_native_replays': replays,
            'pending_callbacks': 0, 'same_owner': True}
