import json
from pathlib import Path

from test_integration import Case
from lens_codex import delivery, state, trace
from lens_codex.session import read_recording


def attributes(span):
    return {a['key']: next(iter(a['value'].values())) for a in span['attributes']}


class SessionTests(Case):
    def file(self, client, records):
        base = self.codex / 'sessions'
        base.mkdir(exist_ok=True)
        path = base / 'session.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        return path

    def codex_event(self, kind, **extra):
        return {'type': 'event_msg', 'timestamp': '2026-10-05T00:00:01Z', 'payload': {'type': kind, **extra}}

    def item(self, item, turn='t', start=1000):
        return self.codex_event('item_completed', turn_id=turn, item=item, started_at_ms=start, completed_at_ms=start+50)

    def test_visible_items_preserve_commentary_repeats_failure_and_model(self):
        records = [self.codex_event('task_started', turn_id='t'),
                   {'type': 'turn_context', 'payload': {'turn_id': 't', 'model': 'model-a'}},
                   self.item({'type': 'UserMessage', 'id': 'u', 'content': [{'type': 'Text', 'text': 'Run'}]}),
                   self.item({'type': 'AgentMessage', 'id': 'a', 'content': [{'type': 'Text', 'text': 'Checking'}]}),
                   self.item({'type': 'Reasoning', 'id': 'secret', 'text': 'PRIVATE'}),
                   self.item({'type': 'CommandExecution', 'id': 'tool', 'command': ['sh','-c','exit 3'], 'exit_code':3, 'status':'failed', 'aggregated_output':'expected'}),
                   {'type': 'response_item', 'payload': {'type':'message','id':'a','role':'assistant','content':[{'type':'output_text','text':'Checking'}]}},
                   self.item({'type': 'AgentMessage', 'id': 'b', 'content': [{'type': 'Text', 'text': 'Checking'}]}),
                   self.codex_event('task_complete', turn_id='t')]
        result = read_recording(str(self.file('codex', records)), 0, 't')
        self.assertTrue(result.complete)
        self.assertEqual([e.text for e in result.entries if not e.tool], ['Run', 'Checking', 'Checking'])
        self.assertEqual(result.entries[2].error, 'Command exited with code 3.')
        self.assertEqual({e.model for e in result.entries}, {'model-a'})

    def test_long_transcript_is_streamed_without_old_turn_backfill(self):
        path = self.file('codex', [self.codex_event('task_started',turn_id='old')])
        with path.open('a') as stream:
            for _ in range(35): stream.write(json.dumps({'type':'ignored','padding':'x'*1024*1024})+'\n')
        offset = path.stat().st_size
        with path.open('a') as stream:
            for r in [self.codex_event('task_started',turn_id='t'),self.item({'type':'AgentMessage','id':'last','content':[{'type':'Text','text':'Tail'}]}),self.codex_event('task_complete',turn_id='t')]:stream.write(json.dumps(r)+'\n')
        result = read_recording(str(path), offset, 't')
        self.assertEqual([e.text for e in result.entries], ['Tail'])
        self.assertTrue(result.complete)

    def test_mcp_result_error_media_and_unknown_item_are_visible(self):
        path = self.file('codex',[self.codex_event('task_started',turn_id='t'),
            self.item({'type':'McpToolCall','id':'m','tool':'lookup','status':'completed','arguments':{'id':7},'result':{'isError':True,'content':[{'type':'text','text':'No match'}]}}),
            self.item({'type':'UserMessage','id':'u','content':[{'type':'Image','url':'data:image/png;base64,PRIVATE'}]}),
            self.item({'type':'FutureItem','id':'future','private':'DO NOT EXPORT'}),
            self.codex_event('task_complete',turn_id='t')])
        result=read_recording(str(path),0,'t')
        self.assertEqual(result.entries[0].error,'Tool returned an error.')
        self.assertIn('Attachment omitted',result.entries[1].text)
        self.assertIn('not supported',result.entries[2].text)
        self.assertNotIn('PRIVATE',repr(result))
        self.assertNotIn('DO NOT EXPORT',repr(result))

    def test_missing_transcript_warns_and_retains_hook_content(self):
        self.record()
        parts=delivery.prepare(self.turn(),state.config(),200)
        root=json.loads(parts[0]['payload'])['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        values=attributes(root)
        self.assertIn('Transcript unavailable',values['lens.capture.warning'])
        self.assertIn('Done.',values['gen_ai.output.messages'])

    def test_transcript_messages_use_chain_convention_without_fake_llm_calls(self):
        from lens_codex.session import Entry,Recording
        self.record()
        spans=trace.build_trace(self.turn(),[],state.config(),None,Recording((Entry('a',101,102,text='Hello',model='model-a'),),True))['resourceSpans'][0]['scopeSpans'][0]['spans']
        values=attributes(spans[-1])
        self.assertEqual(values['openinference.span.kind'],'CHAIN')
        self.assertEqual(json.loads(values['output.value']),[{'role':'assistant','content':'Hello'}])
        self.assertEqual(values['llm.model_name'],'model-a')
        self.assertNotIn('gen_ai.usage.input_tokens',values)

    def test_missing_stop_is_recovered_only_after_explicit_transcript_completion(self):
        path = self.file('codex', [])
        state.capture(self.event('UserPromptSubmit', turn='t', prompt='Run', transcript_path=str(path)), 100)
        with path.open('a') as f:
            for r in [self.codex_event('task_started', turn_id='t'),
                      self.codex_event('error', message='Usage limit reached'),
                      self.codex_event('task_complete', turn_id='t')]:
                f.write(json.dumps(r) + '\n')
        delivery.finish_recorded_turns(state.config(), 200)
        with state.database() as db:
            turn = dict(db.execute('SELECT * FROM turns').fetchone())
        self.assertEqual(turn['status'], 'pending')
        parts = delivery.prepare(turn, state.config(), turn['ended'] + 20)
        root = json.loads(parts[0]['payload'])['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        self.assertEqual(root['status'], {'code': 2, 'message': 'Usage limit reached'})

    def test_delivery_revalidates_transcript_path_after_symlink_change(self):
        path = self.file('codex', [])
        state.capture(self.event('UserPromptSubmit', turn='t', prompt='Run', transcript_path=str(path)), 100)
        state.capture(self.event('Stop', turn='t', last_assistant_message='Done'), 110)
        outside = path.parent.parent.parent / 'unrelated.jsonl'
        outside.write_text(json.dumps(self.codex_event('task_started', turn_id='t')) + '\n' +
                           json.dumps(self.item({'type':'AgentMessage','id':'private','content':[{'type':'Text','text':'UNRELATED PRIVATE DATA'}]})) + '\n')
        path.unlink()
        path.symlink_to(outside)
        parts = delivery.prepare(self.turn('t'), state.config(), 200)
        self.assertNotIn('UNRELATED PRIVATE DATA', parts[0]['payload'])
        self.assertIn('Transcript unavailable', parts[0]['payload'])

    def test_child_metadata_tolerates_malformed_source(self):
        from lens_codex.session import codex_parent
        path = self.file('codex', [{'type':'session_meta','payload':{'source':{'subagent':{'thread_spawn':None}}}},
                                  self.codex_event('task_started', turn_id='t', root_turn_id='parent')])
        self.assertEqual(codex_parent(path, 't', path.stat().st_size), (None, 'parent', None))

    def test_real_interactive_session_preserves_main_and_child_timelines(self):
        fixtures = Path(__file__).parent / 'fixtures'
        recordings = [read_recording(str(fixtures / f'codex-interactive-{index}.jsonl'), 0, 'recorded-turn')
                      for index in range(3)]
        self.assertTrue(all(recording.complete for recording in recordings))
        main, reader, checker = recordings
        texts = [entry.text for entry in main.entries if entry.role == 'assistant' and not entry.tool]
        self.assertEqual(texts[0].strip(), 'CODEX-TUI-COMMENTARY')
        self.assertEqual(texts[-1].strip(), 'CODEX-TUI-FINAL')
        failed = next(entry for entry in main.entries if entry.error and entry.tool == 'Bash')
        self.assertIn('CODEX-TUI-EXPECTED', failed.result['output'])
        self.assertEqual(failed.result['exit_code'], 3)
        self.assertTrue(any(entry.tool == 'record_work' and entry.error for entry in main.entries))
        self.assertTrue(any('LENS-REPLAY-ALPHA' in entry.text for entry in reader.entries))
        self.assertTrue(any('15' in entry.text for entry in checker.entries))
        self.assertTrue(all(entry.model for entry in main.entries if entry.role == 'assistant'))

    def test_nested_agents_share_session_trace_and_keep_their_actual_parent(self):
        state.capture(self.event('UserPromptSubmit', turn='main', prompt='Run'), 100)
        for turn, parent_agent, agent_id, at in [('reader', 'session-1', 'reader-id', 101),
                                                  ('nested', 'reader-id', 'nested-id', 102)]:
            path = self.file('codex', [
                {'type': 'session_meta', 'payload': {'source': {'subagent': {'thread_spawn': {
                    'parent_thread_id': parent_agent, 'agent_path': '/root/' + turn}}}}},
                self.codex_event('task_started', turn_id=turn, root_turn_id='main')])
            state.capture(self.event('SubagentStart', turn=turn, agent_id=agent_id,
                                     transcript_path=str(path)), at)
            state.capture(self.event('SubagentStop', turn=turn, agent_id=agent_id,
                                     last_assistant_message='Done'), at + 0.5)
        state.capture(self.event('Stop', turn='main', last_assistant_message='Done'), 104)
        main = self.payload('main')['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        reader = self.payload('reader')['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        nested = self.payload('nested')['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        self.assertEqual(reader['traceId'], main['traceId'])
        self.assertEqual(nested['traceId'], main['traceId'])
        self.assertEqual(reader['parentSpanId'], main['spanId'])
        self.assertEqual(nested['parentSpanId'], reader['spanId'])
