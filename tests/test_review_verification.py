"""Real state/corpus regressions; only the external model boundary is scripted."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deeper_research.corpus import Corpus
from deeper_research.engine import Engine
from deeper_research.providers import ModelResult, ProviderError, ProviderProfile
from deeper_research.storage import Store
from deeper_research.contracts import MAX_INPUT, estimate_tokens
from coverage_fixtures import fixture_plan, with_receipts


class ScriptedProvider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.profile = ProviderProfile.from_dict({
            'protocol': 'openai_chat', 'base_url': 'https://example.com/v1',
            'model': 'test', 'api_key_env': 'TEST_KEY',
            'input_usd_per_million': '0.3', 'output_usd_per_million': '1.2'})

    def validate_credentials(self):
        pass

    def complete(self, messages, *, max_output_tokens, timeout):
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        reply=with_receipts(reply,json.loads(messages[-1]['content']))
        return ModelResult(json.dumps(reply), 100, 100, 200, 'request', 'test', {})


class ReviewVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'workspace', Decimal('10'))
        self.corpus = Corpus(self.store.root)
        source = self.root/'source.txt'
        source.write_text('Weights stay frozen. Feedback may come from the actor or the environment.\n\n'
                          'Stopping: terminate after the maximum trial count.\n')
        self.source_id = self.corpus.ingest(source, title='Method')
        self.block = self.corpus.search('frozen', [self.source_id])[0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, text='Weights stay frozen.'):
        return {'answer':text+' ['+self.block+']', 'claims':[{'text':text,'evidence_ids':[self.block]}],
                'open_issues':[]}

    def review(self, accept=True, issues=None, action=None):
        return {'criteria':[{'id':'c1','status':'met' if accept else 'partial',
                            'reason':'Frozen-weight criterion checked','evidence_ids':[self.block]}],
                'claim_checks':[{'id':'claim-1','status':'supported' if accept else 'contradicted',
                                 'reason':'Source supports the finding','evidence_ids':[self.block]}],
                'issues':issues or [], 'action':action or ('accept' if accept else 'revise'),
                'preferred':'candidate','comparison_reason':'Compare source support',
                'query':'frozen weight parameter update', 'strategy':''}

    def verification(self, accept=True, rejected_issue=False, quote='Weights stay frozen.', action=None):
        value=self.review(accept,action=action)
        value['issue_checks']=([{'id':'issue-1','status':'rejected','reason':'Source says frozen',
                                'evidence_ids':[self.block]}] if rejected_issue else [])
        value['source_quotes']=[{'claim_id':'claim-1','evidence_id':self.block,'quote':quote}]
        return value

    def make(self,replies,effort='medium',question='Explain frozen weights',criteria=None):
        provider=ScriptedProvider(replies)
        task=self.store.create_task({'question':question,
                                    'criteria':criteria or ['Explain frozen weights accurately'],
                                    'source_ids':[self.source_id],'effort':effort,
                                    'profile':provider.profile.public_dict(),
                                    'requirements':fixture_plan(criteria or ['Explain frozen weights accurately'])})
        return task,Engine(self.store,self.corpus,provider)

    def test_acceptance_requires_separate_source_grounded_verification(self):
        task,engine=self.make([self.candidate(),self.review(),self.verification()])
        result=engine.run(task)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual([c['role'] for c in self.store.calls(task)],['solve','review','verify'])
        report=json.loads((self.store.root/f'tasks/{task}/exports/run-summary.json').read_text())
        self.assertTrue(report['review_verifications'])
        self.assertNotEqual(report['review_verifications'][0]['original_review'],
                            report['review_verifications'][0]['verification'])

    def test_mistaken_blocking_feedback_is_rejected_before_rewriting(self):
        issues=[{'severity':'blocking','reason':'Weights must be updated','evidence_ids':[self.block]}]
        task,engine=self.make([self.candidate(),self.review(False,issues),
                               self.verification(rejected_issue=True)])
        result=engine.run(task)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],
                         'Weights stay frozen. ['+self.block+']')
        self.assertEqual([c['role'] for c in self.store.calls(task)],['solve','review','verify'])

    def test_unlocatable_quote_cannot_make_an_acceptance_verified(self):
        task,engine=self.make([self.candidate(),self.review(),
                              self.verification(quote='Weights are updated by gradient descent.'),
                              self.verification(quote='Weights are updated by gradient descent.')])
        result=engine.run(task)
        self.assertEqual(result['quality'],'partial')
        self.assertEqual(result['reason'],'verification_unresolved')
        self.assertEqual(len(self.store.calls(task)),4)

    def test_one_paid_quote_correction_rechecks_same_evidence_before_accepting(self):
        task,engine=self.make([self.candidate(),self.review(),
                              self.verification(quote='Weights stay ... frozen.'),self.verification()])
        result=engine.run(task)
        self.assertEqual(result['quality'],'criteria_met')
        calls=self.store.calls(task)
        self.assertEqual([c['role'] for c in calls],['solve','review','verify','verify'])
        first=json.loads(calls[2]['request'][-1]['content'])
        second=json.loads(calls[3]['request'][-1]['content'])
        self.assertEqual(second['evidence'],first['evidence'])
        self.assertTrue(second['grounding_errors'])
        self.assertEqual(len(result['checkpoint']['review_verifications']),2)

    def test_quote_correction_is_bounded_even_when_high_has_more_calls(self):
        wrong=self.verification(quote='Weights stay ... frozen.')
        task,engine=self.make([self.candidate(),self.review(),wrong,wrong,self.verification()],effort='high')
        result=engine.run(task)
        self.assertEqual(result['reason'],'verification_unresolved')
        self.assertEqual(len(self.store.calls(task)),4)

    def test_quote_correction_unknown_outcome_requires_explicit_retry(self):
        task,engine=self.make([self.candidate(),self.review(),
                              self.verification(quote='Weights stay ... frozen.'),
                              ProviderError('timeout',outcome_unknown=True),self.verification()],effort='high')
        first=engine.run(task)
        self.assertEqual((first['state'],first['quality']),('interrupted','partial'))
        engine.run(task)
        self.assertEqual(len(self.store.calls(task)),4)
        result=engine.run(task,retry_unknown=True)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(self.store.calls(task)),5)
        self.assertEqual(sum(c['status']=='outcome_unknown' for c in self.store.calls(task)),1)

    def test_call_cap_keeps_partial_verdict_instead_of_quote_correction(self):
        task,engine=self.make([ProviderError('timeout',outcome_unknown=True),self.candidate(),self.review(),
                              self.verification(quote='Weights stay ... frozen.'),self.verification()])
        engine.run(task)
        result=engine.run(task,retry_unknown=True)
        self.assertEqual(result['quality'],'partial')
        self.assertEqual(result['reason'],'verification_unresolved')
        self.assertEqual(len(self.store.calls(task)),4)

    def test_scheduled_quote_correction_survives_interruption_without_replaying_first_audit(self):
        task,engine=self.make([self.candidate(),self.review(),
                              self.verification(quote='Weights stay ... frozen.'),self.verification()])
        verify=engine._verify
        stopped=[False]
        def interrupt():
            result=verify()
            if engine.cp.get('quote_recheck') and not stopped[0]:
                stopped[0]=True
                raise KeyboardInterrupt()
            return result
        with patch.object(engine,'_verify',interrupt):
            self.assertEqual(engine.run(task)['state'],'interrupted')
        result=engine.run(task,retry_unknown=True)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(self.store.calls(task)),4)
        self.assertEqual(len(result['checkpoint']['review_verifications']),2)

    def test_located_quote_must_belong_to_the_claims_verified_sources(self):
        stop_block=self.corpus.search('stopping', [self.source_id])[0]['id']
        value=self.verification(quote='terminate after the maximum trial count.')
        value['source_quotes'][0]['evidence_id']=stop_block
        task,engine=self.make([self.candidate(),self.review(),value,value],
                              question='Explain frozen weights and stopping')
        result=engine.run(task)
        self.assertEqual(result['quality'],'partial')
        self.assertEqual(result['reason'],'verification_unresolved')

    def test_confirmed_blocking_issue_cannot_disappear_from_verifier_verdict(self):
        issue={'severity':'blocking','reason':'Important condition remains wrong','evidence_ids':[self.block]}
        value=self.verification()
        value['issue_checks']=[{'id':'issue-1','status':'confirmed','reason':'Condition remains wrong',
                               'evidence_ids':[self.block]}]
        task,engine=self.make([self.candidate(),self.review(False,[issue]),value,
                               self.candidate('Revised frozen-weight answer.')])
        result=engine.run(task)
        self.assertEqual(result['quality'],'partial')
        report=self.store.read_artifact(result['checkpoint']['best_review'])
        self.assertTrue(any(i['severity']=='blocking' for i in report['issues']))

    def test_unknown_verifier_is_not_automatically_retried(self):
        task,engine=self.make([self.candidate(),self.review(),
                              ProviderError('timeout',outcome_unknown=True),self.verification()])
        self.assertEqual(engine.run(task)['state'],'interrupted')
        self.assertEqual(engine.run(task)['state'],'interrupted')
        self.assertEqual(len(self.store.calls(task)),3)
        result=engine.run(task,retry_unknown=True)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(self.store.calls(task)),4)
        self.assertEqual(sum(c['status']=='outcome_unknown' for c in self.store.calls(task)),1)

    def test_budget_does_not_skip_verification_or_promote_unchecked_revision(self):
        task,engine=self.make([self.candidate(),self.review(False),
                              self.verification(False),self.candidate('Revised frozen-weight answer.')])
        result=engine.run(task)
        self.assertEqual(result['quality'],'partial')
        self.assertEqual(len(self.store.calls(task)),3)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],
                         'Weights stay frozen. ['+self.block+']')

    def test_missing_criterion_fetches_new_text_before_verifying_feedback(self):
        missing={'id':'c2','status':'partial','reason':'Stopping rule is absent from supplied excerpts.',
                 'evidence_ids':[]}
        raw=self.review();raw['criteria'].append(missing);raw['action']='revise'
        verified=self.verification();verified['criteria'].append(missing);verified['action']='revise'
        stop_block=self.corpus.search('stopping', [self.source_id])[0]['id']
        revised=self.candidate()
        revised['answer']+=' Stop after the maximum trial count. ['+stop_block+']'
        revised['claims'].append({'text':'Stop after the maximum trial count.','evidence_ids':[stop_block]})
        task,engine=self.make([self.candidate(),raw,verified,revised],
                              criteria=['Explain frozen weights accurately','说明终止条件'])
        result=engine.run(task)
        calls=self.store.calls(task)
        first=json.loads(calls[0]['request'][-1]['content'])['evidence']
        audit=json.loads(calls[2]['request'][-1]['content'])['evidence']
        self.assertNotIn(stop_block,{b['canonical_id'] for b in first})
        self.assertIn(stop_block,{b['canonical_id'] for b in audit})
        lookups=result['checkpoint']['lookups']
        self.assertEqual(lookups[1]['criterion_ids'],['c2'])
        self.assertGreater(lookups[1]['new_content'],0)
        self.assertEqual(result['quality'],'partial')

    def test_empty_supplemental_lookup_preserves_source_and_honest_abstention(self):
        missing={'id':'c2','status':'unmet','reason':'Target quantum measurement unavailable.',
                 'evidence_ids':[]}
        raw=self.review();raw['criteria'].append(missing);raw['action']='blocked'
        verified=self.verification();verified['criteria'].append(missing);verified['action']='blocked'
        task,engine=self.make([self.candidate(),raw,verified],
                              criteria=['Explain frozen weights accurately','报告量子纠缠的实测数据'])
        result=engine.run(task)
        self.assertEqual(result['quality'],'partial')
        self.assertEqual(result['reason'],'evidence_blocked')
        self.assertEqual(len(self.store.calls(task)),3)
        self.assertEqual(result['checkpoint']['lookups'][1]['new_content'],0)
        self.assertEqual(result['checkpoint']['lookups'][1]['blocks'],[])
        request=json.loads(self.store.calls(task)[2]['request'][-1]['content'])
        self.assertIn(self.block,{b['canonical_id'] for b in request['evidence']})

    def test_known_verification_response_replays_after_interruption(self):
        task,engine=self.make([self.candidate(),self.review(),self.verification()])
        save=self.store.save_artifact
        interrupted=[False]
        def interrupt(task_id,kind,value):
            path=save(task_id,kind,value)
            if kind=='verification' and not interrupted[0]:
                interrupted[0]=True
                raise KeyboardInterrupt()
            return path
        with patch.object(self.store,'save_artifact',interrupt):
            first=engine.run(task)
        self.assertEqual(first['state'],'interrupted')
        result=engine.run(task,retry_unknown=True)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(self.store.calls(task)),3)

    def test_completed_lookup_is_not_repeated_after_interruption_at_read_limit(self):
        task,engine=self.make([self.candidate(),self.review(False),
                              self.verification(False,action='blocked')])
        spec=self.store.task(task)['spec']
        spec['limits']['reads']=2
        with self.store.db() as db:
            db.execute('UPDATE tasks SET spec=? WHERE id=?',(json.dumps(spec),task))
        retrieve=engine._retrieve
        stopped=[False]
        def interrupt(*args,**kwargs):
            result=retrieve(*args,**kwargs)
            if kwargs.get('optional') and not stopped[0]:
                stopped[0]=True
                raise KeyboardInterrupt()
            return result
        with patch.object(engine,'_retrieve',interrupt):
            self.assertEqual(engine.run(task)['state'],'interrupted')
        result=engine.run(task,retry_unknown=True)
        self.assertEqual(result['reason'],'evidence_blocked')
        self.assertEqual(result['reads'],2)
        self.assertEqual([c['role'] for c in self.store.calls(task)],['solve','review','verify'])

    def test_contained_excerpt_is_not_new_evidence(self):
        source=self.root/'long.txt'
        source.write_text('Weights stay frozen. '+ 'Background detail. '*48 +
                          'Stopping uses an iteration limit and feedback indicator.')
        sid=self.corpus.ingest(source,title='Long method')
        self.source_id=sid
        self.block=self.corpus.search('frozen',[sid])[0]['id']
        raw=self.review(False)
        raw['criteria'][0]['reason']='终止条件及反馈历史的完整定义'
        task,engine=self.make([self.candidate(),raw,self.verification(False,action='blocked')])
        result=engine.run(task)
        initial=self.store.read_artifact(result['checkpoint']['packet_history'][0])[0]
        self.assertNotIn('truncated',initial)
        self.assertEqual(result['checkpoint']['lookups'][1]['new_content'],0)

    def test_short_complete_source_quote_is_not_rejected_by_length(self):
        source=self.root/'short.txt';source.write_text('Value: 42.')
        self.source_id=self.corpus.ingest(source,title='Value')
        self.block=self.corpus.search('value',[self.source_id])[0]['id']
        task,engine=self.make([self.candidate('Value: 42.'),self.review(),
                              self.verification(quote='Value: 42.')],
                              question='State the value',criteria=['Report the source value'])
        self.assertEqual(engine.run(task)['quality'],'criteria_met')

    def test_duplicate_confirmed_issue_does_not_hide_improved_candidate(self):
        issue={'severity':'blocking','reason':'A condition remains unresolved','evidence_ids':[self.block]}
        first=self.verification(False)
        first['issue_checks']=[{'id':'issue-1','status':'confirmed','reason':issue['reason'],
                               'evidence_ids':[self.block]}]
        final=self.verification(True,action='blocked')
        final['issue_checks']=first['issue_checks']
        final['issues']=[{**issue,'original_issue_id':'issue-1'}]
        task,engine=self.make([self.candidate('Original frozen-weight answer.'),
                              self.review(False,[issue]),first,self.candidate(),
                              self.review(True,[issue]),final],effort='high')
        result=engine.run(task)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],
                         'Weights stay frozen. ['+self.block+']')
        verified=self.store.read_artifact(result['checkpoint']['best_review'])
        self.assertEqual(sum(i['severity']=='blocking' for i in verified['issues']),1)

    def test_unlinked_original_issue_with_rephrased_check_is_not_counted_twice(self):
        issue={'severity':'blocking','reason':'A condition remains unresolved','evidence_ids':[self.block]}
        first=self.verification(False)
        first['issue_checks']=[{'id':'issue-1','status':'confirmed','reason':'Verifier confirmed this condition',
                               'evidence_ids':[self.block]}]
        final=self.verification(True,action='blocked')
        final['issue_checks']=first['issue_checks']
        final['issues']=[issue]
        task,engine=self.make([self.candidate('Original answer.'),self.review(False,[issue]),first,
                              self.candidate(),self.review(True,[issue]),final],effort='high')
        result=engine.run(task)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],
                         'Weights stay frozen. ['+self.block+']')
        verified=self.store.read_artifact(result['checkpoint']['best_review'])
        self.assertEqual(sum(i['severity']=='blocking' for i in verified['issues']),1)

    def test_overlapping_pinned_regions_fit_without_losing_any_supplied_text(self):
        source=self.root/'overlap.txt'
        text='Weights stay frozen. '+'Background detail. '*600
        text=text[:7000]+' A later required condition. '+text[7000:]
        source.write_text(text)
        self.source_id=self.corpus.ingest(source,title='Overlapping source')
        full=self.corpus.get_block(self.source_id+':b1',[self.source_id])
        self.block=full['id']
        original=self.corpus.get_block(self.block,[self.source_id])
        blocks=[]
        for start,end in [(0,6000),(500,6500),(1000,7000),(1500,7500)]:
            block={**original,'text':text[start:end],'truncated':True,'text_start':start,'text_end':end}
            blocks.append(block)
        messages=Engine._render_messages('Audit all source text.',
                                        {'evidence':Engine._coalesce_evidence(blocks)})
        self.assertLessEqual(estimate_tokens(messages),MAX_INPUT)
        supplied=json.loads(messages[-1]['content'])['evidence']
        matching=[b for b in supplied if b['canonical_id']==self.block]
        self.assertEqual(len(matching),1)
        self.assertEqual(matching[0]['text'],text[:7500])
        self.assertEqual((matching[0]['text_start'],matching[0]['text_end']),(0,7500))

    def test_citations_pin_actual_origin_request_instead_of_unseen_history(self):
        source=self.root/'origin.txt'
        text='Weights stay frozen. '+'Background detail. '*600
        source.write_text(text)
        self.source_id=self.corpus.ingest(source,title='Origin source')
        self.block=self.corpus.get_block(self.source_id+':b1',[self.source_id])['id']
        task,engine=self.make([self.candidate(),self.review()])
        # Retain an actual historical-style source window to test provenance;
        # new retrieval deliberately refuses incomplete oversized paragraphs.
        raw=self.corpus.get_block(self.block,[self.source_id])
        origin_block={**raw,'text':text[:700],'truncated':True,'text_start':0,'text_end':700}
        with patch.object(self.corpus,'search',return_value=[origin_block]):
            with patch.object(engine,'_verify',side_effect=KeyboardInterrupt()):engine.run(task)
        engine.cp=self.store.task(task)['checkpoint']
        raw=self.corpus.get_block(self.block,[self.source_id])
        for start,end in [(0,6000),(500,6500),(1000,7000),(1500,7500)]:
            block={**raw,'text':text[start:end],'truncated':True,'text_start':start,'text_end':end}
            path=self.store.save_artifact(task,'packet',[block])
            engine.cp['packet_history'].append(path)
        messages=engine._messages('verify')
        self.assertLessEqual(estimate_tokens(messages),MAX_INPUT)
        supplied=json.loads(messages[-1]['content'])['evidence']
        origin=json.loads(self.store.calls(task)[0]['request'][-1]['content'])['evidence']
        self.assertEqual([b['text'] for b in supplied],[b['text'] for b in origin])

    def test_query_metadata_cannot_crowd_out_source_text(self):
        block={'id':'b1','source_id':'s1','text':'Weights stay frozen.',
               'title':'Source','location':{'page':1},'query_terms':['x'*100]*40}
        body={'question':'问题'*3500,'evidence':[block]}
        messages=Engine._render_messages('Audit the source.',body)
        self.assertLessEqual(estimate_tokens(messages),MAX_INPUT)
        payload=json.loads(messages[-1]['content'])
        self.assertEqual(payload['question'],'问题'*3500)
        self.assertEqual(payload['evidence'][0]['text'],'Weights stay frozen.')
        self.assertEqual(payload['evidence'][0]['canonical_id'],'b1')
        self.assertEqual(payload['evidence'][0]['location'],{'page':1})

    def test_disjoint_regions_cannot_be_joined_into_a_quote_that_omits_negation(self):
        regions=[{'id':'b1','text':'Weights are','text_start':0,'text_end':11,'truncated':True},
                 {'id':'b1','text':'updated.','text_start':16,'text_end':24,'truncated':True}]
        result=Engine._coalesce_evidence(regions)
        self.assertEqual([b['text'] for b in result],['Weights are','updated.'])
        self.assertFalse(any('Weights areupdated.' in b['text'] for b in result))
