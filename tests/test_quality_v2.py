"""Regression boundaries found in historical no-fee replay."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from deeper_research.corpus import Corpus
from deeper_research.engine import Engine
from deeper_research.contracts import MAX_INPUT, estimate_tokens
from deeper_research.storage import Store
import test_review_verification as fixtures
from test_corpus import make_pdf

class RetrievalV2Tests(unittest.TestCase):
    def test_memory_termination_query_preserves_explicit_memory_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus=Corpus(Path(tmp)/'workspace')
            source=Path(tmp)/'loop.pdf'
            make_pdf(source, ['Memory is truncated to avoid exceeding the maximum limit.',
                              'while evaluator not pass or t < max trials do. Increment t. Return.'])
            sid=corpus.ingest(source)
            packet=corpus.search('内存达到上限时是否停止', [sid],limit=1)
            self.assertEqual(packet[0]['location']['page'],1)

    def test_english_termination_query_keeps_bound_in_long_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus=Corpus(Path(tmp)/'workspace')
            source=Path(tmp)/'loop.txt'
            source.write_text('Background introduction. '*240+'\n\n'+
                              'while evaluator not pass or t < max trials do. Increment t. Return.')
            sid=corpus.ingest(source)
            packet=corpus.search('stopping condition',[sid],max_tokens=700)
            self.assertIn('max trials',packet[0]['text'])

    def test_termination_query_prefers_trial_bound_over_memory_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus=Corpus(Path(tmp)/'workspace')
            source=Path(tmp)/'loop.pdf'
            make_pdf(source, ['Memory is truncated to avoid exceeding the maximum limit.',
                              'while evaluator not pass or t < max trials do. Increment t. Return.'])
            sid=corpus.ingest(source)
            packet=corpus.search('真实的停止条件', [sid],limit=1)
            self.assertEqual(packet[0]['location']['page'],2)
            self.assertIn('max trials',packet[0]['text'])

    def test_chinese_quality_tradeoff_query_reaches_decisive_english_passage(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus=Corpus(Path(tmp)/'workspace')
            source=Path(tmp)/'paper.txt'
            source.write_text('Background discussion. '*240+'\n\n'+
                'The performance may not always monotonically increase with iterations: in multi-aspect feedback tasks, '
                'the output quality can vary with improvement in one aspect but decline in another aspect.')
            sid=corpus.ingest(source)
            packet=corpus.search('多个质量维度发生权衡', [sid],max_tokens=700)
            self.assertTrue(packet)
            self.assertIn('decline in another aspect', ' '.join(b['text'] for b in packet))
            for b in packet:
                full=corpus.get_block(b['id'],[sid])['text']
                self.assertEqual(full[b.get('text_start',0):b.get('text_end',len(full))],b['text'])

    def test_test_false_positive_query_does_not_stop_at_generic_intro(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus=Corpus(Path(tmp)/'workspace')
            source=Path(tmp)/'paper.txt'
            source.write_text('Reflection improves feedback.\n\n'
                'If an invalid test suite returns a false positive completion, the agent will prematurely report an invalid submission.')
            sid=corpus.ingest(source)
            packet=corpus.search('自生成测试的假阳性与误判', [sid])
            self.assertTrue(packet)
            self.assertIn('invalid submission',' '.join(b['text'] for b in packet))

class ContextV2Tests(unittest.TestCase):
    setUp=fixtures.ReviewVerificationTests.setUp
    tearDown=fixtures.ReviewVerificationTests.tearDown
    candidate=fixtures.ReviewVerificationTests.candidate
    review=fixtures.ReviewVerificationTests.review
    make=fixtures.ReviewVerificationTests.make
    verification=fixtures.ReviewVerificationTests.verification

    def test_revision_format_correction_preserves_slots_for_full_recheck(self):
        bad=self.candidate('Bad draft');bad.pop('answer')
        task,engine=self.make([self.candidate(),self.review(False),self.verification(False),bad,
                               self.candidate('New draft'),self.review(),self.verification()],effort='high')
        # A six-slot task can close one revision, but not a paid format repair
        # as well. This exercises resource exhaustion without extra fake calls.
        spec=self.store.task(task)['spec'];spec['limits']['calls']=6
        with self.store.db() as db:
            db.execute('UPDATE tasks SET spec=? WHERE id=?',(json.dumps(spec),task))
        result=engine.run(task)
        self.assertEqual([c['role'] for c in self.store.calls(task)],['solve','review','verify','revise'])
        self.assertEqual(result['quality'],'partial')
        self.assertIn('Weights stay frozen.',self.store.read_artifact(result['selected'])['answer'])

    def test_medium_does_not_spend_last_call_on_uncheckable_revision(self):
        replies=[self.candidate(), self.review(False), self.verification(False), self.candidate('Unused new draft')]
        task,engine=self.make(replies)
        result=engine.run(task)
        self.assertEqual([c['role'] for c in self.store.calls(task)],['solve','review','verify'])
        self.assertEqual(result['quality'],'partial')
        with self.store.db() as db:
            stops=[json.loads(r[0])['limit'] for r in db.execute(
                "SELECT detail FROM events WHERE task_id=? AND kind='budget_stop'",(task,))]
        self.assertIn('closure_budget',stops)

    def test_lookup_query_does_not_mix_feedback_essay_with_evidence_topic(self):
        task,engine=self.make([],criteria=['查清真实停止条件'])
        engine.task_id=task;engine.spec=self.store.task(task)['spec'];engine.cp={}
        value=self.review(False,action='retrieve')
        value['reason']='irrelevant'
        value['criteria'][0]['reason']='CANDIDATE_REASON_DISTRACTOR '*50
        value['query']='termination maximum trials'
        plan=engine._lookup_plan(value)
        self.assertTrue(plan)
        self.assertTrue(any(p['query']=='termination maximum trials' and p['criterion_ids']==['c1'] for p in plan))
        self.assertTrue(all('CANDIDATE_REASON_DISTRACTOR' not in p['query'] for p in plan))

    def prepare_verify(self, issues=None):
        task,engine=self.make([self.candidate(),self.review(False,issues)],effort='high')
        with patch.object(engine,'_verify',side_effect=KeyboardInterrupt()):engine.run(task)
        saved=self.store.task(task)
        engine.task_id=task;engine.spec=saved['spec'];engine.cp=saved['checkpoint']
        return task,engine

    def test_verification_context_does_not_overflow_from_diagnostic_queries(self):
        task,engine=self.prepare_verify()
        evidence_before=json.loads(engine._messages('verify')[-1]['content'])['evidence']
        for entry in engine.cp['lookups']:
            entry['query']='DIAGNOSTIC_ONLY '*2000
        messages=engine._messages('verify')
        self.assertLessEqual(estimate_tokens(messages),MAX_INPUT)
        payload=json.loads(messages[-1]['content'])
        self.assertEqual(payload['evidence'],evidence_before)
        self.assertEqual(payload['candidate']['answer'],self.candidate()['answer'].replace(self.block,'E1'))

    def test_response_template_lists_exact_original_ids_without_inventing_statuses(self):
        issues=[{'severity':'blocking','reason':'One defect','evidence_ids':[self.block]},
                {'severity':'improvement','reason':'Other defect','evidence_ids':[]}]
        task,engine=self.prepare_verify(issues)
        payload=json.loads(engine._messages('verify')[-1]['content'])
        schema=payload['response_schema']
        self.assertEqual([i['id'] for i in payload['feedback_to_check']],['issue-1','issue-2'])
        self.assertEqual([i['id'] for i in payload['candidate']['claims']],['claim-1'])
        self.assertIn('confirmed|rejected|unresolved',schema['feedback']['issue ID']['status'])
        self.assertNotIn('issue_checks',schema)

