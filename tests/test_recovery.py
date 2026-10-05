"""Regression coverage for oversized MCP results and the stopped sender incident."""
import concurrent.futures
import io
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
import urllib.error

from lens_codex import batches, content, delivery, state, web
from test_integration import Case


class ContentTests(Case):
    def test_mcp_duplicate_json_kept_once_with_extra_text(self):
        data = {'results': [{'title': 'Documentation', 'body': 'Useful evidence'}]}
        value, notes = content.normalize({'content': [
            {'type': 'text', 'text': json.dumps(data)}, {'type': 'text', 'text': 'Extra note'}],
            'structuredContent': data, '_meta': {'secret': 'internal transport metadata'}})
        self.assertEqual(content.encode(value).count('Useful evidence'), 1)
        self.assertIn('Extra note', content.encode(value))
        self.assertNotIn('_meta', value)
        self.assertEqual(notes['duplicates_omitted'], 1)

    def test_media_variants_never_enter_queue(self):
        self.record(output={'content': [
            {'type': 'image', 'data': 'PRIVATE_PIXELS'},
            {'type': 'audio', 'data': 'PRIVATE_AUDIO'},
            {'type': 'resource', 'resource': {'blob': 'PRIVATE_BINARY'}},
            {'type': 'text', 'text': 'Screenshot data:image/png;base64,QUJDREVGRw=='},
        ], '_meta': {'screenshot': {'url': 'PRIVATE_URL'}}})
        with state.database(write=False) as db:
            saved = '\n'.join(r[0] for r in db.execute('SELECT data FROM events'))
        for secret in ('PRIVATE_PIXELS', 'PRIVATE_AUDIO', 'PRIVATE_BINARY', 'PRIVATE_URL', 'QUJDREVGRw=='):
            self.assertNotIn(secret, saved)
        self.assertIn('Media omitted', saved)

    def test_json_string_mcp_wrapper_is_cleaned(self):
        text = json.dumps({'content': [{'type': 'image', 'data': 'PIXELS'}], '_meta': {'internal': 1}})
        clean, notes = content.normalize(text)
        self.assertNotIn('PIXELS', content.encode(clean))
        self.assertEqual(notes['media_omitted'], 1)

    def test_plain_json_prompt_remains_a_string(self):
        text = '{"name": "test", "type": {"name": "data"}}'
        self.assertEqual(content.normalize(text), (text, {}))
        self.assertEqual(content.normalize(json.loads(text))[0]['type'], {'name': 'data'})

    def test_large_unicode_output_has_marked_beginning_and_end(self):
        clean, notes = content.normalize('START' + '雪🌱' * 50000 + 'END')
        self.assertLessEqual(len(clean.encode()), content.MAX_CONTENT_BYTES)
        self.assertTrue(clean.startswith('START'))
        self.assertTrue(clean.endswith('END'))
        self.assertIn('Content shortened by Lens', clean)
        self.assertEqual(notes['shortened'], 1)

    def test_deep_or_wide_content_is_bounded(self):
        item = {'value': 'hello'}
        for _ in range(100):
            item = {'nested': item}
        clean, notes = content.normalize(item)
        self.assertIn('omitted', content.encode(clean))
        self.assertTrue(notes['shortened'])

    def test_plain_text_under_limit_is_unchanged(self):
        text = 'START\n' + '雪🌱' * 10000 + '\nEND'
        self.assertEqual(content.normalize(text), (text, {}))

    def test_old_queue_rows_are_normalized_at_export(self):
        self.record()
        with state.database() as db:
            row = db.execute("SELECT id,data FROM events WHERE kind='PostToolUse'").fetchone()
            event = json.loads(row['data'])
            event['tool_response'] = {'content': [{'type': 'image', 'data': 'OLD_IMAGE'}],
                                      '_meta': {'old_transport_secret': 'SECRET'}}
            db.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(event), row['id']))
        payload = self.payload()
        self.assertNotIn('OLD_IMAGE', content.encode(payload))
        self.assertNotIn('old_transport_secret', content.encode(payload))
        self.assertIn('codex.capture.media_omitted', content.encode(payload))


class QueueTests(Case):
    def lock_database(self):
        with state.database():
            pass
        lock = sqlite3.connect(state.state_dir() / 'events.sqlite3')
        lock.execute('BEGIN IMMEDIATE')
        self.addCleanup(lock.close)
        return lock

    def test_capture_during_write_lock_is_durable_and_status_is_read_only(self):
        lock = self.lock_database()
        events = (
            self.event('UserPromptSubmit', prompt='Run'),
            self.event('PreToolUse', tool_name='Bash', tool_use_id='tool-1', tool_input={'command': 'echo hello'}),
            self.event('PostToolUse', tool_name='Bash', tool_use_id='tool-1', tool_response='retained evidence'),
            self.event('Stop', last_assistant_message='Done'),
        )
        for event in events:
            started = time.monotonic()
            state.capture(event)
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(delivery.status()['counts']['queued_events'], 4)
        self.assertEqual(len(list((state.state_dir() / 'inbox').glob('*.json'))), 4)
        state.health('helper_error', 'Still working')
        self.assertEqual(state.health_notes()['helper_error'], 'Still working')
        lock.rollback()
        state.drain_inbox()
        self.assertEqual(self.turn()['status'], 'pending')
        self.assertIn('retained evidence', content.encode(self.payload()))
        self.assertEqual(delivery.status()['counts']['queued_events'], 0)

    def test_concurrent_sessions_while_database_locked(self):
        lock = self.lock_database()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.record(session=f's-{i}', turn=f't-{i}'), range(15)))
        lock.rollback()
        state.drain_inbox()
        with state.database(write=False) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turns WHERE status='pending'").fetchone()[0], 15)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 60)

    def test_crash_after_import_commit_does_not_duplicate(self):
        lock = self.lock_database()
        self.record()
        lock.rollback()
        with patch.object(Path, 'unlink', side_effect=OSError('simulated crash')):
            state.drain_inbox()
        state.drain_inbox()
        with state.database(write=False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 4)
        self.assertEqual(self.turn()['status'], 'pending')

    def test_pause_imports_completed_queue_and_discards_incomplete(self):
        lock = self.lock_database()
        self.record()
        state.capture(self.event('UserPromptSubmit', turn='incomplete', prompt='private'))
        lock.rollback()
        state.set_enabled(False)
        self.assertEqual(self.turn()['status'], 'pending')
        self.assertEqual(self.turn('incomplete')['status'], 'discarded')

    def test_pause_during_database_lock_cannot_resurrect_incomplete_turn(self):
        lock = self.lock_database()
        self.record()
        state.capture(self.event('UserPromptSubmit', turn='partial', prompt='private'))
        state.set_enabled(False)
        state.set_enabled(True)
        lock.rollback()
        state.drain_inbox(blocking=True)
        self.assertEqual(self.turn()['status'], 'pending')
        self.assertEqual(self.turn('partial')['status'], 'discarded')
        self.assertFalse(state.capture(self.event('Stop', turn='partial', last_assistant_message='late')))

    def test_sender_survives_database_error_even_when_reporting_also_fails(self):
        class Stop:
            calls = 0
            def wait(self, _):
                self.calls += 1
                return self.calls > 3
        with patch.object(web, 'flush', side_effect=[sqlite3.OperationalError('database is locked'), None, None]) as flush, \
                patch.object(state, 'health', side_effect=OSError('disk full')):
            web.deliver(Stop())
        self.assertEqual(flush.call_count, 3)

    def test_dead_sender_is_visible_even_with_old_successful_turns(self):
        state.health('helper_heartbeat', str(time.time() - 300))
        self.assertIn('stopped responding', delivery.status()['health']['helper_error'])


class BatchTests(Case):
    def large_turn(self, turn='turn-1'):
        state.capture(self.event('UserPromptSubmit', turn=turn, prompt='Check all these results'), 100)
        for i in range(12):
            state.capture(self.event('PreToolUse', turn=turn, tool_use_id=str(i), tool_name='search',
                                     tool_input={'query': str(i)}), 101 + i)
            state.capture(self.event('PostToolUse', turn=turn, tool_use_id=str(i), tool_name='search',
                                     tool_response=('Useful evidence: ' + str(i) + '雪\\"\n') * 8000), 102 + i)
        state.capture(self.event('Stop', turn=turn, last_assistant_message='Final answer'), 120)

    def test_wire_size_limit_accounts_for_unicode_escaping_and_keeps_each_span_once(self):
        self.large_turn()
        payload = self.payload()
        parts = batches.split(payload)
        self.assertGreater(len(parts), 2)
        for part in parts:
            self.assertLessEqual(len(content.encode(part).encode()), batches.MAX_BATCH_BYTES)
        ids = [s['spanId'] for p in parts for s in batches.spans(p)]
        self.assertEqual(ids, [s['spanId'] for s in batches.spans(payload)])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({s['traceId'] for p in parts for s in batches.spans(p)}), 1)

    def test_single_large_root_keeps_valid_message_json(self):
        state.capture(self.event('UserPromptSubmit', prompt='\\"\n' * 150000), 100)
        state.capture(self.event('Stop', last_assistant_message='\\"\n' * 150000), 120)
        part = batches.split(self.payload())[0]
        self.assertLessEqual(len(content.encode(part).encode()), batches.MAX_BATCH_BYTES)
        for attr in batches.spans(part)[0]['attributes']:
            if attr['key'] in {'gen_ai.input.messages', 'gen_ai.output.messages'}:
                self.assertIsInstance(json.loads(attr['value']['stringValue']), list)

    def test_restart_after_one_batch_does_not_send_it_again(self):
        self.large_turn()
        expected = batches.split(self.payload())
        error = urllib.error.HTTPError('https://example', 429, 'rate limit', {}, io.BytesIO())
        with patch.object(delivery, 'request', side_effect=[{}, error]) as request:
            delivery.flush(200)
        self.assertEqual(request.call_count, 2)
        with patch.object(delivery, 'request', return_value={}) as request:
            delivery.flush(1000)
        self.assertEqual(self.turn()['status'], 'sent')
        self.assertEqual([call.args[2] for call in request.call_args_list], expected[1:])

    def test_lost_ack_mid_turn_reconciles_only_that_batch(self):
        self.large_turn()
        expected = batches.split(self.payload())
        with patch.object(delivery, 'request', side_effect=[{}, TimeoutError()]):
            delivery.flush(200)
        received = {'spans': [{'span_id': s['spanId']} for s in batches.spans(expected[1])]}
        with patch.object(delivery, 'request', side_effect=[received] + [{}] * len(expected)) as request:
            delivery.flush(240)
        self.assertEqual(self.turn()['status'], 'sent')
        posts = [call.args[2] for call in request.call_args_list if len(call.args) == 3]
        self.assertEqual(posts, expected[2:])

    def test_unconfirmed_batch_never_blindly_replayed_and_other_chat_continues(self):
        self.large_turn()
        self.record(turn='other')
        with patch.object(delivery, 'request', side_effect=[TimeoutError(), {}]):
            delivery.flush(200)
        with patch.object(delivery, 'request', return_value={'spans': []}) as request:
            delivery.flush(240)
        self.assertEqual(self.turn()['status'], 'uncertain')
        self.assertEqual(self.turn('other')['status'], 'sent')
        self.assertTrue(all(len(c.args) == 2 for c in request.call_args_list))

    def test_old_rejected_413_rebuilds_and_recovers_automatically(self):
        self.large_turn()
        with state.database() as db:
            db.execute("UPDATE turns SET status='blocked',error='Gateway returned HTTP 413. Open settings',payload=?",
                       ('{"old_unusable_payload": true}',))
        with patch.object(delivery, 'request', return_value={}) as request:
            delivery.flush(200)
        self.assertEqual(self.turn()['status'], 'sent')
        self.assertGreater(request.call_count, 1)
        self.assertNotIn('old_unusable_payload', str(request.call_args_list))

    def test_old_uncertain_request_is_checked_without_rebuilding(self):
        self.record()
        old = self.payload()
        with state.database() as db:
            db.execute("UPDATE turns SET status='uncertain',payload=?", (json.dumps(old),))
        with patch.object(delivery, 'request', return_value={'spans': []}) as request:
            delivery.flush(200)
        self.assertTrue(all(len(c.args) == 2 for c in request.call_args_list))
        with state.database(write=False) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM batches').fetchone()[0]), old)

    def test_partial_acceptance_is_not_replayed_by_retry_button(self):
        self.record()
        with patch.object(delivery, 'request', return_value={'partialSuccess': {'rejectedSpans': 1}}):
            delivery.flush(200)
        delivery.retry_blocked()
        with patch.object(delivery, 'request', return_value={'spans': []}) as request:
            delivery.flush(240)
        self.assertEqual(self.turn()['status'], 'uncertain')
        self.assertTrue(all(len(c.args) == 2 for c in request.call_args_list))

class HttpRecoveryTests(Case):
    """Exercise real HTTP, including a disconnected response after ingestion."""
    large_turn = BatchTests.large_turn

    def test_end_to_end_lost_ack_rate_limit_and_resumed_session(self):
        from http.server import BaseHTTPRequestHandler
        import socket
        import threading
        stored, body_sizes, ids = [], [], set()
        outcomes = ['disconnect', 'limit']

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'spans': [{'span_id': x} for x in ids]}).encode())
            def do_POST(self):
                length = int(self.headers['Content-Length'])
                body_sizes.append(length)
                payload = json.loads(self.rfile.read(length))
                if length > batches.MAX_BATCH_BYTES:
                    self.send_response(413)
                    self.end_headers()
                    return
                outcome = outcomes.pop(0) if outcomes else 'ok'
                if outcome == 'limit':
                    self.send_response(429)
                    self.end_headers()
                    return
                for span in batches.spans(payload):
                    stored.append(span)
                    ids.add(span['spanId'])
                if outcome == 'disconnect':
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        server = web.LoopbackHTTPServer(('127.0.0.1', 0), Gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        state.save_config({**state.config(), 'gateway': f'http://127.0.0.1:{server.server_port}'})
        self.large_turn()
        delivery.flush(200)
        self.assertEqual(self.turn()['status'], 'uncertain')
        delivery.flush(240)
        self.assertEqual(self.turn()['status'], 'retry')
        delivery.flush(1000)
        self.assertEqual(self.turn()['status'], 'sent')
        self.record(turn='follow-up')
        delivery.flush(1001)
        delivery.flush(1002)
        self.assertEqual(len(ids), 15)  # 13 spans in the large turn, 2 in the follow-up.
        self.assertEqual(len(stored), len(ids))
        self.assertEqual(len({s['traceId'] for s in stored}), 1)
        self.assertTrue(all(s.get('parentSpanId', s['spanId']) in ids for s in stored))
        self.assertTrue(all(size <= batches.MAX_BATCH_BYTES for size in body_sizes))
        self.assertEqual(delivery.status()['counts']['sent'], 2)

class NetworkTests(Case):
    def test_refused_connection_retries_without_waiting_for_impossible_ack(self):
        self.record()
        with patch.object(delivery, 'request', side_effect=urllib.error.URLError(ConnectionRefusedError())):
            delivery.flush(200)
        self.assertEqual(self.turn()['status'], 'retry')
        with patch.object(delivery, 'request', return_value={}) as req:
            delivery.flush(240)
        self.assertEqual(self.turn()['status'], 'sent')
        self.assertEqual(len(req.call_args.args), 3)

    def test_rate_limit_obeys_retry_after(self):
        self.record()
        failure = urllib.error.HTTPError('https://example', 429, 'limited', {'Retry-After': '90'}, io.BytesIO())
        with patch.object(delivery, 'request', side_effect=failure):
            delivery.flush(200)
        with patch.object(delivery, 'request', return_value={}) as req:
            delivery.flush(240)
            self.assertFalse(req.called)
            delivery.flush(291)
        self.assertEqual(self.turn()['status'], 'sent')

    def test_corrupt_turn_does_not_block_a_later_session(self):
        self.record()
        self.record(turn='healthy')
        with state.database() as db:
            db.execute("UPDATE events SET data='not json' WHERE turn='turn-1'")
        with patch.object(delivery, 'request', return_value={}):
            delivery.flush(200)
        self.assertEqual(self.turn()['status'], 'blocked')
        self.assertEqual(self.turn('healthy')['status'], 'sent')
