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


def sequence_rejection_probe(state, events, session, project, env, evidence, *, mixed=False):
    """Read the exact failed proof and export only fixed component facts."""
    facts = {'pending_callbacks': len(events), 'native_readers': []}
    sequence_code = evidence.claude_archived_sendmessage_sequence.__code__
    reader_code = evidence._claude_native_lead_event.__code__
    callback_code = evidence._claude_sendmessage_callback.__code__
    extra_readers = {getattr(evidence, name).__code__: label for name, label in (
        ('_claude_historical_worker_origin', 'worker_origin'),
        ('_claude_historical_worker_terminal', 'worker_terminal'),
        ('_claude_historical_worker_deliveries', 'worker_deliveries'),
        ('claude_archived_mixed_sendmessage_sequence', 'mixed_sequence')) if hasattr(evidence, name)}

    def trace(frame, kind, result):
        if frame.f_code is callback_code:
            if kind == 'return':
                checks = facts.setdefault('callback_checks', [])
                values = frame.f_locals
                source, native = values.get('source'), values.get('native')
                when, called, upper = values.get('when'), values.get('called_at'), values.get('next_call')
                if len(checks) < 32 and source is not None and native is not None:
                    payload, expected = source.payload, native.payload
                    report = payload.get('last_assistant_message')
                    checks.append({'source_line': frame.f_lineno, 'accepted': result is True,
                        'source_ordinal': next((index for index, event in enumerate(events)
                                                if event.event_id == source.event_id), None),
                        'kind': source.kind if source.kind in {'subagent_started', 'subagent_stopped'} else 'other',
                        'native_boundaries': values.get('native_boundaries') is True,
                        'session_owned': values.get('owned') is True,
                        'provider_matches': payload.get('provider') == 'claude',
                        'identity_matches': (payload.get('agent_id') or payload.get('subagent_id')) == expected['agent_id'],
                        'parent_matches': payload.get('parent_thread_id') in {None, '', values.get('parent')},
                        'route_matches': all(payload.get(key) in {None, '', expected[key]}
                                             for key in ('agent_type', 'model', 'model_reasoning_effort')),
                        'explicit_turn_matches': 'turn_id' not in payload or payload['turn_id'] == expected['prompt_id'],
                        'at_or_after_call': when is not None and called is not None and when >= called,
                        'upper_bound_present': upper is not None,
                        'before_upper_bound': when is not None and (upper is None or when < upper),
                        'at_or_after_terminal': when is not None and when >= evidence._instant(native.observed_at),
                        'report_matches': isinstance(report, str) and report in {
                            expected['last_assistant_message'], expected.get('_symphony_native_callback_report')},
                        'status_completed': str(payload.get('status') or 'completed').lower() == 'completed',
                        'report_classification_valid': isinstance(report, str) and (
                            'SYMPHONY_OUTCOME:' not in report or evidence._reported_status(report) == 'completed')})
            return trace
        if frame.f_code in extra_readers:
            if kind == 'return':
                stages = facts.setdefault('historical_readers', [])
                if len(stages) < 24:
                    stages.append({'reader': extra_readers[frame.f_code], 'source_line': frame.f_lineno,
                                   'accepted': result is not None})
            return trace
        if frame.f_code not in {sequence_code, reader_code}:
            return None
        if kind == 'return':
            values = frame.f_locals
            item = {'source_line': frame.f_lineno, 'accepted': result is not None}
            if frame.f_code is reader_code:
                launch = values.get('launch_input')
                children = values.get('child_rows', ())
                positions = values.get('prompt_indices', ())
                item.update(metadata_reached='meta' in values, root_launch_reached='launch' in values,
                            child_prompts_reached='prompt_indices' in values,
                            terminal_reached='terminal' in values)
                if isinstance(launch, dict) and positions:
                    item['original_launch_prompt_matches'] = (
                        launch.get('prompt') == children[positions[0]].get('message', {}).get('content'))
                first, archived = values.get('first_prompt_at'), values.get('run')
                if first is not None and archived is not None:
                    boundary = evidence._instant(archived.updated_at)
                    item['original_prompt_before_archive'] = boundary is not None and first < boundary
                assistants = values.get('assistants')
                lead = values.get('lead')
                if assistants is not None and lead is not None:
                    terminal = values.get('message')
                    reason = terminal.get('stop_reason') if isinstance(terminal, dict) else None
                    item.update(prompt_ordinal=values.get('prompt_index'), assistant_count=len(assistants),
                        prior_end_turn_count=sum(row.get('message', {}).get('stop_reason') == 'end_turn'
                                                 for row in assistants[:-1]),
                        terminal_reason=(reason if isinstance(reason, str)
                                         and reason in {'end_turn', 'tool_use', 'max_tokens', 'stop_sequence'}
                                         else 'null' if reason is None else 'other'),
                        terminal_reason_present=isinstance(terminal, dict) and 'stop_reason' in terminal,
                        superseded_proof=values.get('superseded') is True,
                        native_parent_completion_proof=values.get('delivered') is True)
                    item['assistant_model_matches'] = all(row.get('message', {}).get('model') == lead.requested_tier
                                                         for row in assistants)
                    item['assistant_effort_matches'] = all((row.get('perTurnEffort') or row.get('effort')) ==
                                                          lead.requested_effort for row in assistants)
                if len(facts['native_readers']) < 12:
                    facts['native_readers'].append(item)
            else:
                rows = values.get('rows')
                calls, prompts = values.get('calls', ()), values.get('prompts', ())
                item.update(root_rows_reached=rows is not None, child_rows_reached='children' in values,
                            calls=len(calls), prompts=len(prompts), prior_prompts=len(values.get('prior_prompts', ())),
                            native_turns=len(values.get('native_events', ())),
                            mapped_callbacks=len(values.get('mapping', {})))
                if rows is not None:
                    item['all_root_rows_main_session'] = all(row.get('sessionId') == session and
                        row.get('isSidechain') is not True and not row.get('agentId') for row in rows)
                prior = values.get('prior_native')
                if 'prior_native' in values:
                    item['prearchive_native_terminal_accepted'] = prior is not None
                delivery_facts = []
                for index, (called, row, call) in enumerate(calls[:8]):
                    details = call.get('input')
                    details = details if isinstance(details, dict) else {}
                    matches = values.get('results', {}).get(call.get('id'), ())
                    delivery = {'literal_recipient_matches': details.get('to') == values['run'].lead_identity,
                        'literal_message_present': isinstance(details.get('message'), str) and bool(details['message'].strip()),
                        'paired_result_count': len(matches), 'child_prompt_available': index < len(prompts)}
                    if index < len(prompts):
                        delivery['message_matches_child_prompt'] = details.get('message') == prompts[index].get('message', {}).get('content')
                        delivery['coordinator_projection_matches'] = evidence._claude_coordinator_prompt_matches(
                            prompts[index], details.get('message'))
                    if len(matches) == 1:
                        result_row, paired = matches[0]
                        content = paired.get('content')
                        if isinstance(content, list) and content and all(isinstance(block, dict) and
                                block.get('type') == 'text' and isinstance(block.get('text'), str) for block in content):
                            content = '\n'.join(block['text'] for block in content)
                        try:
                            response = json.loads(content) if isinstance(content, str) else None
                        except ValueError:
                            response = None
                        returned = evidence._instant(result_row.get('timestamp'))
                        delivery.update(result_json_object=isinstance(response, dict),
                            result_success=isinstance(response, dict) and response.get('success') is True,
                            result_resumed_identity_matches=isinstance(response, dict) and
                                response.get('resumedAgentId') in {None, values['run'].lead_identity},
                            result_is_error=paired.get('is_error') is True,
                            result_after_call=returned is not None and returned >= called,
                            result_before_next_call=returned is not None and
                                (index + 1 == len(calls) or returned < calls[index + 1][0]))
                    delivery_facts.append(delivery)
                item['deliveries'] = delivery_facts
                facts['sequence'] = item
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        function = evidence.claude_archived_mixed_sendmessage_sequence if mixed else evidence.claude_archived_sendmessage_sequence
        facts['accepted'] = function(
            state, tuple(events), session, project, env) is not None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        facts['probe_error'] = True
    finally:
        sys.settrace(previous)
    return facts


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
        witnesses = list(sequence['sources'])
        digest = hashlib.sha256(json.dumps(sequence, sort_keys=True).encode()).hexdigest()
        witnesses.extend(witness for witness in runs[0].assessment.get(
            '_claude_sendmessage_late_sources', ()) if witness.get('sequence_hash') == digest)
        if sequence.get('historical_workers') is not None:
            witnesses.extend(witness for witness in runs[0].assessment.get(
                '_claude_historical_sendmessage_dispositions', ()) if witness.get('sequence_hash') == digest)
        for witness in witnesses:
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


def exact_historical_delta(run, sequences, identity, message):
    turns = [turn for sequence in sequences for turn in sequence.get('historical_workers', {}).get('turns', ())]
    if (len(turns) != 1 or turns[0].get('agent') != identity
            or turns[0].get('message_hash') != hashlib.sha256(message.encode()).hexdigest()):
        raise RuntimeError('native continuation lacks its exact newly requested worker acknowledgment')
    digests = {hashlib.sha256(json.dumps(sequence, sort_keys=True).encode()).hexdigest() for sequence in sequences}
    dispositions = tuple(dict(item) for item in run.assessment.get('_claude_historical_sendmessage_dispositions', ())
                         if item.get('sequence_hash') in digests)
    if (not dispositions or any(item.get('agent') != identity for item in dispositions)
            or not any(item.get('kind') == 'subagent_stopped' for item in dispositions)):
        raise RuntimeError('native continuation lacks newly committed worker ACK dispositions')
    return dispositions


def replay_native_copies(root, original_state, native_home, project, session, events, env, runtime, store_module,
                         *, expected_sequences, expected_receipts, expected_worker_dispositions=(),
                         expected_late_sources=()):
    # Reproduce the original transaction trigger time, not replay wall time.
    # Callback observations already retain their exact captured timestamps.
    from dataclasses import replace
    if len(expected_sequences) != 1 or not isinstance(expected_sequences[0].get('committed_at'), str):
        raise RuntimeError('copied replay lacks original transaction timestamp')
    original_trigger_time = expected_sequences[0]['committed_at']
    normalizer = runtime.event_from_payload

    def copied_handle(payload, copied_env):
        def original_observation(provider, source):
            return replace(normalizer(provider, source), observed_at=original_trigger_time)
        with patch.object(runtime, 'event_from_payload', side_effect=original_observation):
            return runtime.handle(payload, copied_env)

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
        sibling_sessions = {run.session_id for run in (*before.active_runs.values(), *before.recent_runs)
                            if run.provider == 'claude' and run.session_id != session}
        sibling_inboxes = {root_session: store._session_path('claude', root_session).read_bytes()
                           if store._session_path('claude', root_session).is_file() else None
                           for root_session in sibling_sessions}
        original = next(run for run in before.recent_runs if run.provider == 'claude' and run.session_id == session)
        late_ids = {item['event_id'] for item in expected_late_sources}
        for event in events:
            if event.event_id in late_ids:
                continue
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
                    copied_handle(payload, copied_env)
                except OSError:
                    pass
                else:
                    raise RuntimeError('copied ACK interruption did not run')
            if mode == 'partial-ack':
                record = store.session_record('claude', session)
                present = [event for event in events if event.event_id not in late_ids]
                store.finish_session_events(record, {present[0].event_id, present[-1].event_id})
        elif late_ids:
            initial = copied_handle(payload, copied_env)
            if json.loads(initial.stdout or '{}').get('decision') == 'block':
                raise RuntimeError('copied initial native continuation stayed blocked')
        for event in events:
            if event.event_id in late_ids:
                store.queue_session_event('claude', session, event, ambiguous_owner=True)
        result = copied_handle(payload, copied_env)
        if json.loads(result.stdout or '{}').get('decision') == 'block':
            raise RuntimeError('copied native continuation stayed blocked')
        copied_handle({**payload, 'hook_event_name': 'Stop'}, copied_env)
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
        if any(item not in final[0].assessment.get('_claude_sendmessage_late_sources', ())
               for item in expected_late_sources):
            raise RuntimeError('copied replay lacks exact late callback dispositions')
        if expected_worker_dispositions:
            stored = final[0].assessment.get('_claude_historical_sendmessage_dispositions', ())
            if (any(item not in stored for item in expected_worker_dispositions)
                    or final[0].assessment.get('_substantive_children') != original.assessment.get('_substantive_children')
                    or tuple(item for item in final[0].delegations if item.role == 'worker') !=
                       tuple(item for item in original.delegations if item.role == 'worker')
                    or tuple(item for item in after.terminal_receipts if item.get('agent') in
                             original.assessment.get('_substantive_children', {})) !=
                       tuple(item for item in before.terminal_receipts if item.get('agent') in
                             original.assessment.get('_substantive_children', {}))):
                raise RuntimeError('copied worker acknowledgment changed original substantive credit')
        siblings_before = [run for run in (*before.active_runs.values(), *before.recent_runs)
                           if run.provider == 'claude' and run.session_id != session]
        siblings_after = [run for run in (*after.active_runs.values(), *after.recent_runs)
                          if run.provider == 'claude' and run.session_id != session]
        if siblings_before != siblings_after:
            raise RuntimeError('copied continuation changed sibling ownership')
        if any((store._session_path('claude', root_session).read_bytes()
                if store._session_path('claude', root_session).is_file() else None) != original_bytes
               for root_session, original_bytes in sibling_inboxes.items()):
            raise RuntimeError('copied continuation changed sibling pending callbacks')
        counts[mode] = {'pending_callbacks': 0, 'same_owner': True,
                        'native_turns': sum(len(sequence['turns']) for sequence in expected_sequences),
                        'historical_worker_replies': int(bool(expected_worker_dispositions))}
    return counts


def check_native_sendmessage(root, candidate, baseline_env, baseline_result, timeout, budget,
                             *, label='a', historical_worker=False):
    import native_managed_concurrency as managed
    candidate = candidate.resolve()
    sys.path.insert(0, str(candidate))
    from symphony import adapters, host_evidence, model, runtime, store as store_module

    case = root / 'same-worktree'
    state_dir = case / 'state'
    native_home = Path(baseline_env['CLAUDE_CONFIG_DIR'])
    owner = baseline_result['original_owners'][label]
    session, lead = owner['session_id'], owner['lead_id']
    store = store_module.StateStore(state_dir)
    record = store.session_record('claude', session)
    project = Path(record['project'])
    state = store.load(project)
    if not managed.claude_original_owner_archived(store_module._state_to_dict(state), session, owner['run_id'], lead):
        raise RuntimeError('explicit followup requires the exact completed original owner')
    private = root / ('private-sendmessage-replay-' + label)
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
    before_run = next(run for run in state.recent_runs if run.run_id == owner['run_id'])
    deliveries = [{'to': lead, 'message': message} for message in messages]
    worker, worker_message = None, None
    if historical_worker:
        proofs = before_run.assessment.get('_substantive_children', {})
        workers = [identity for identity, proof in proofs.items() if proof.get('role') == 'worker'
                   and proof.get('successful') is True and proof.get('run_id') == before_run.run_id
                   and proof.get('lead') == lead and proof.get('owner_generation') == before_run.owner_generation]
        if len(workers) != 1:
            raise RuntimeError('native historical acknowledgment requires one originally credited worker')
        worker = workers[0]
        worker_message = ('Acknowledge ONLY your already completed original work. Do not inspect, edit, '
                          'execute tools or delegate. Return exactly HISTORICAL_WORKER_ACK with no outcome marker.')
        deliveries.insert(1, {'to': worker, 'message': worker_message})
    request = ('Continue ONLY these original archived Symphony identities. Use SendMessage exactly '
               + ('three times' if historical_worker else 'twice') + ', '
               'sequentially, targeting the literal original agent ID. Await the first CHILD end_turn '
               'and its CONTINUATION_ACK_ONE report before sending the second; a success:true dispatch '
               'acknowledgment alone is not child completion. Do not use Agent, names, extra messages, new tasks or tools. '
               'Send the following exact string messages without summarizing them: ' + json.dumps(deliveries) +
               '. Await EACH addressed CHILD end_turn and report before sending the next message. '
               'After the final lead native terminal, end this root turn so Stop reconciles the same owner.')
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
    matching_runs = [run for run in (*after.active_runs.values(), *after.recent_runs)
                     if run.session_id == session and run.run_id == owner['run_id']]
    if len(matching_runs) != 1:
        raise RuntimeError('native continuation has no unique original owner after delivery')
    run = matching_runs[0]
    try:
        delta = requested_sequence_delta(before_run, run, messages)
    except RuntimeError:
        pending = store.session_record('claude', session)['pending']
        events = [model.Event(item['event_id'], item['kind'], item['observed_at'], item['payload'])
                  for item in pending]
        # Evaluate the untouched pre-followup owner against the exact current
        # native files and retained callbacks before temporary files disappear.
        diagnostic = sequence_rejection_probe(state, events, session, project, env, host_evidence,
                                             mixed=historical_worker)
        (root / ('sendmessage-proof-' + label + '.json')).write_text(json.dumps(diagnostic), encoding='utf-8')
        raise
    if not managed.claude_original_owner_archived(store_module._state_to_dict(after), session, owner['run_id'], lead):
        raise RuntimeError('native continuation did not archive the exact original owner')
    receipts = exact_delta_receipts(after, session, run, delta)
    worker_dispositions = exact_historical_delta(run, delta, worker, worker_message) if historical_worker else ()
    digests = {hashlib.sha256(json.dumps(sequence, sort_keys=True).encode()).hexdigest() for sequence in delta}
    late_sources = tuple(item for item in run.assessment.get('_claude_sendmessage_late_sources', ())
                         if item.get('sequence_hash') in digests)
    sources = captured_sequence_sources(after, session, root / 'private-child-hooks', adapters,
                                        host_evidence, model, sequences=delta)
    turns = sum(len(sequence['turns']) for sequence in delta)
    replays = replay_native_copies(private, original_state, native_home, project, session,
                                   sources, env, runtime, store_module,
                                   expected_sequences=delta, expected_receipts=receipts,
                                   expected_worker_dispositions=worker_dispositions,
                                   expected_late_sources=late_sources)
    return {'case': 'archived-sendmessage-sequence', 'native_turns': turns,
            'captured_callbacks': len(sources), 'copied_native_replays': replays,
            'historical_worker_replies': int(historical_worker),
            'pending_callbacks': 0, 'same_owner': True}


def check_native_sendmessage_pair(root, candidate, baseline_env, baseline_result, timeout, budget):
    """Both required sibling scenarios run even when one fails."""
    results, errors = [], {}
    for label, historical in (('a', False), ('b', True)):
        try:
            results.append(check_native_sendmessage(root, candidate, baseline_env, baseline_result,
                timeout, budget, label=label, historical_worker=historical))
        except Exception as error:
            errors[label] = {'failed': True, 'timed_out': isinstance(error, subprocess.TimeoutExpired),
                             'proof_failed': isinstance(error, RuntimeError)}
            frame = error.__traceback__
            while frame is not None:
                if frame.tb_frame.f_code.co_filename == __file__:
                    errors[label]['proof_source_line'] = frame.tb_lineno
                frame = frame.tb_next
    if errors:
        (root / 'sendmessage-required.json').write_text(json.dumps(errors), encoding='utf-8')
        raise RuntimeError('required native lead or historical-worker continuation failed')
    return results
