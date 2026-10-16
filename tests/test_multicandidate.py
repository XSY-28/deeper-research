"""Real loop and ledger tests; only the remote model is replaced."""
import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from copy import deepcopy
from unittest.mock import patch

from deeper_research.storage import Store, BudgetExceeded
from deeper_research.providers import ModelResult, ProviderError
from test_engine import RemoteBoundary, CorpusBoundary, candidate, review, verification
from coverage_fixtures import fixture_plan


class GroupBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name))
        self.profile=RemoteBoundary([]).profile.public_dict()
    def tearDown(self): self.tmp.cleanup()
    def root(self, **extra):
        task=self.store.create_task({'mode':'multi','effort':'high','question':'Use evidence',
            'criteria':['Use evidence'],'source_ids':['s1'],'profile':self.profile,
            'requirements':fixture_plan(['Use evidence']),**extra})
        self.store.update(task,state='running')
        return task
    def reserve(self, task, cost):
        return self.store.reserve_call(task,'solve',[],1,1,Decimal(cost)*1000000,Decimal(0))
    def test_child_membership_and_contract_are_frozen_and_slot_is_idempotent(self):
        root=self.root();child=self.store.create_child(root,1,'direct')
        self.assertEqual(child,self.store.create_child(root,1,'direct'))
        spec=self.store.task(child)['spec']
        self.assertEqual((spec['group_root'],spec['candidate_number'],spec['limits']['calls']),(root,1,10))
        self.assertEqual(spec['profile'],self.profile)
        with self.assertRaises(ValueError): self.store.create_child(child,1,'direct')
        with self.assertRaises(ValueError): self.root(group_root=root)
    def test_first_candidate_cannot_spend_second_and_third_candidates_shares(self):
        root=self.root(); child=self.store.create_child(root,1,'direct')
        self.reserve(child,'0.07')
        with self.assertRaises(BudgetExceeded): self.reserve(child,'0.011')
        self.assertEqual(len(self.store.calls(child)),1)
    def test_root_counts_children_and_keeps_control_reserve(self):
        root=self.root();children=[self.store.create_child(root,n,'direct') for n in range(1,4)]
        for child in children: self.reserve(child,'0.08')
        fourth=self.store.create_child(root,4,'challenge')
        with self.assertRaises(BudgetExceeded): self.reserve(fourth,'0.001')
        self.reserve(root,'0.06')
        with self.assertRaises(BudgetExceeded): self.reserve(root,'0.001')
        summary=self.store.group_usage(root)
        self.assertEqual(Decimal(summary['accounted_usd']),Decimal('0.30'))
        self.assertEqual(summary['calls'],4)
    def test_parent_cancel_blocks_child_dispatch(self):
        root=self.root(); child=self.store.create_child(root,1,'direct')
        self.store.update(root,cancel_requested=1)
        with self.assertRaises(InterruptedError): self.reserve(child,'0.001')
        self.assertEqual(self.store.calls(child),[])
    def test_invalid_modes_and_nonfinite_usd_are_rejected(self):
        for extra in ({'mode':'multi','effort':'low'},{'budget_usd':'NaN'}, {'mode':'made-up'}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.root(**extra)
    def test_project_cap_is_still_atomic_across_separate_roots(self):
        a=self.root(budget_usd='8');b=self.root(budget_usd='8')
        self.reserve(a,'6')
        with self.assertRaises(BudgetExceeded): self.reserve(b,'5')
        self.assertEqual(Decimal(self.store.budget()['accounted_usd']),Decimal('6'))


def prefer_word(word='', improvement=''):
    def compare(body):
        sides=body['candidates']; chosen=next((x for x in sides if word and word in x['candidate']['answer']),sides[0])
        anchor=({'span_id':chosen['candidate']['answer_spans'][0]['id']} if chosen['candidate'].get('answer_spans')
                else {'answer_quote':chosen['candidate']['answer'].split(' [')[0]})
        return {'verdict':chosen['label'],'reason':'The requested point is explained more precisely.',
            'advantages':[{'candidate':chosen['label'],'dimension':'precision',**anchor,'reason':'More useful explanation.'}],
            'improvement':improvement,'concerns':[]}
    return compare


class ScriptedRemote(RemoteBoundary):
    def complete(self,messages,*,max_output_tokens,timeout):
        from coverage_fixtures import with_receipts
        body=json.loads(messages[-1]['content']);value=next(self.results)
        if isinstance(value,BaseException): raise value
        if callable(value): value=value(body)
        value=with_receipts(value,body)
        return ModelResult(json.dumps(value),100,100,200,'request','test',{})


class FullLoopSearchTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    reserve=GroupBudgetTests.reserve
    # Ledger unit cases above also run against the coordinator's task shapes.
    def run_search(self,replies):
        from deeper_research.multicandidate import MultiCandidate
        provider=ScriptedRemote(replies);root=self.root()
        result=MultiCandidate(self.store,CorpusBoundary(),provider).run(root)
        return root,result
    def outputs(self,text): return [candidate(text),review(True),verification(True)]
    def test_each_of_three_candidates_gets_real_solve_review_and_verification(self):
        root,result=self.run_search(self.outputs('best')+self.outputs('other')+self.outputs('third')+
            [prefer_word('best'),prefer_word('best')])
        self.assertEqual(result['state'],'finished')
        self.assertEqual(result['quality'],'criteria_met')
        children=self.store.children(root)
        self.assertEqual(len(children),3)
        for child in children:
            self.assertEqual([c['role'] for c in self.store.calls(child['task_id'])],['solve','review','verify'])
            self.assertEqual(self.store.task(child['task_id'])['quality'],'criteria_met')
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'best [b1]')
        exported=json.loads((self.store.root/f'tasks/{root}/exports/run-summary.json').read_text())
        self.assertTrue(exported['search_complete'])
        self.assertEqual(exported['calls'],11)
    def test_inner_revision_stays_inside_its_own_number_and_is_rechecked(self):
        first=[candidate('incomplete'),review(),verification(),candidate('best'),review(True),verification(True)]
        root,result=self.run_search(first+self.outputs('other')+self.outputs('third')+
            [prefer_word('best'),prefer_word('best')])
        child=self.store.children(root)[0]['task_id']
        self.assertEqual([c['role'] for c in self.store.calls(child)],['solve','review','verify','revise','review','verify'])
        self.assertEqual(len(self.store.children(root)),3)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'best [b1]')
    def test_fourth_tie_keeps_historical_best_and_stops_without_fifth(self):
        def tie(body):return {'verdict':'tie','reason':'No substantive difference','advantages':[],
            'improvement':'','concerns':[]}
        replies=self.outputs('best')+self.outputs('second')+self.outputs('third')+[
            prefer_word('best','Clarify the assumptions.'),prefer_word('best','Clarify the assumptions.')]
        root,result=self.run_search(replies+self.outputs('fourth')+[tie])
        self.assertEqual(len(self.store.children(root)),4)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'best [b1]')
        self.assertEqual(result['reason'],'no_improvement')
    def test_fifth_is_limit_and_can_win_after_two_verified_challenges(self):
        replies=self.outputs('first')+self.outputs('second')+self.outputs('third')+[
            prefer_word('first','Improve the explanation.'),prefer_word('first','Improve the explanation.')]
        replies+=self.outputs('fourth')+[prefer_word('fourth','Clarify another assumption.')]
        replies+=self.outputs('fifth')+[prefer_word('fifth')]
        root,result=self.run_search(replies)
        self.assertEqual(len(self.store.children(root)),5)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'fifth [b1]')
        self.assertEqual(result['reason'],'candidate_limit')
    def test_unknown_child_call_stops_entire_search_and_resume_does_not_retry(self):
        from deeper_research.multicandidate import MultiCandidate
        root,result=self.run_search([ProviderError('uncertain',outcome_unknown=True)])
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(len(self.store.children(root)),1)
        MultiCandidate(self.store,CorpusBoundary(),ScriptedRemote([])).run(root)
        self.assertEqual(self.store.group_usage(root)['calls'],1)
    def test_incomplete_candidate_is_not_promoted_by_polished_comparison(self):
        replies=[candidate('unchecked'),ValueError('local')]
        # A known response contract failure ends this slot; other slots still run.
        replies=[candidate('unchecked'),{},{}]+self.outputs('best')+self.outputs('third')+[prefer_word('best')]
        root,result=self.run_search(replies)
        self.assertNotEqual(result['quality'],'criteria_met')
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'best [b1]')
        exported=json.loads((self.store.root/f'tasks/{root}/exports/run-summary.json').read_text())
        self.assertFalse(exported['search_complete'])
        self.assertEqual(exported['selected_quality'],'criteria_met')
    def test_blind_comparison_has_no_candidate_number_task_id_or_strategy(self):
        def blind(body):
            wire=json.dumps(body)
            for child in self.store.children(root):self.assertNotIn(child['task_id'],wire)
            for side in body['candidates']:
                self.assertEqual(set(side),{'label','candidate','checks'})
            return prefer_word('best')(body)
        from deeper_research.multicandidate import MultiCandidate
        root=self.root();remote=ScriptedRemote(self.outputs('best')+self.outputs('other')+self.outputs('third')+[blind,blind])
        MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
    def test_comparison_forged_advantage_gets_one_paid_repair_then_fails_closed(self):
        def forged(body):
            value=prefer_word()(body);value['advantages'][0]['span_id']='A.p999';return value
        root,result=self.run_search(self.outputs('first')+self.outputs('second')+self.outputs('third')+[forged,forged])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(len([c for c in self.store.calls(root) if c['role']=='compare']),2)
        self.assertEqual(len(self.store.children(root)),3)
    def test_completed_search_is_read_only_when_run_again(self):
        from deeper_research.multicandidate import MultiCandidate
        root,_=self.run_search(self.outputs('best')+self.outputs('other')+self.outputs('third')+[prefer_word('best'),prefer_word('best')])
        before=self.store.group_usage(root)
        MultiCandidate(self.store,CorpusBoundary(),ScriptedRemote([])).run(root)
        self.assertEqual(self.store.group_usage(root),before)

class RecoveryAndFeedbackTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    run_search=FullLoopSearchTests.run_search
    outputs=FullLoopSearchTests.outputs
    def test_interrupted_fourth_resumes_that_slot_before_starting_fifth(self):
        from deeper_research.multicandidate import MultiCandidate
        replies=self.outputs('best')+self.outputs('second')+self.outputs('third')+[
            prefer_word('best','Clarify the assumptions.'),prefer_word('best','Clarify the assumptions.'),
            ProviderError('unknown fourth',outcome_unknown=True)]
        root,result=self.run_search(replies)
        self.assertEqual((result['state'],len(self.store.children(root))),('interrupted',4))
        remote=ScriptedRemote(self.outputs('fourth')+[lambda body:{'verdict':'tie','reason':'Equivalent',
            'advantages':[],'improvement':'','concerns':[]}])
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root,retry_unknown=True)
        self.assertEqual(result['state'],'finished')
        self.assertEqual(len(self.store.children(root)),4)
        fourth=self.store.children(root)[3]['task_id']
        self.assertEqual([c['status'] for c in self.store.calls(fourth)],['outcome_unknown','succeeded','succeeded','succeeded'])
    def test_comparison_concern_goes_through_actual_original_verification_loop(self):
        def concerned(body):
            side=next(c for c in body['candidates'] if 'best' in c['candidate']['answer'])
            return {'verdict':'needs_check','reason':'Check the possible factual defect before selecting.',
                'advantages':[],'improvement':'','concerns':[{'candidate':side['label'],'kind':'evidence',
                    'answer_quote':'best','reason':'Potentially unsupported specific condition','query':'different conditions'}]}
        check=verification(True)
        check['issue_checks']=[{'id':'issue-1','status':'rejected','reason':'The source really supports this.', 'evidence_ids':['b1']}]
        root,result=self.run_search(self.outputs('best')+self.outputs('second')+self.outputs('third')+[
            concerned,check,prefer_word('best'),prefer_word('best')])
        first=self.store.children(root)[0]['task_id']
        self.assertEqual([c['role'] for c in self.store.calls(first)],['solve','review','verify','verify'])
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(result['checkpoint']['search']['comparisons']),3)
    def test_successful_comparison_response_is_replayed_after_artifact_interrupt(self):
        from deeper_research.multicandidate import MultiCandidate
        root=self.root();remote=ScriptedRemote(self.outputs('best')+self.outputs('second')+self.outputs('third')+
            [prefer_word('best'),prefer_word('best')])
        save=self.store.save_artifact;fired=[False]
        def interrupt(task,kind,value):
            path=save(task,kind,value)
            if kind=='comparison' and not fired[0]:fired[0]=True;raise KeyboardInterrupt()
            return path
        with patch.object(self.store,'save_artifact',interrupt):
            result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
        self.assertEqual(result['state'],'interrupted')
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root,retry_unknown=True)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(self.store.calls(root)),2)

class FeedbackBoundaryTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    run_search=FullLoopSearchTests.run_search
    outputs=FullLoopSearchTests.outputs
    def concern(self,body):
        side=next(x for x in body['candidates'] if 'best' in x['candidate']['answer'])
        return {'verdict':'needs_check','reason':'Verify a possible source error.', 'advantages':[],
            'improvement':'','concerns':[{'candidate':side['label'],'kind':'evidence','answer_quote':'best',
            'reason':'A material fact may be wrong.','query':'different conditions'}]}
    def test_budget_blocked_feedback_never_reuses_stale_accepted_verdict(self):
        first=[{},candidate('draft'),review(),verification(),candidate('revised'),review(),verification(),
            candidate('best'),review(True),verification(True)]
        # If stale acceptance survives, the implementation wrongly requests these
        # two winner judgements; keeping them permits observing the false success.
        root,result=self.run_search(first+self.outputs('other')+self.outputs('third')+
            [self.concern,prefer_word('best'),prefer_word('best')])
        first_task=self.store.children(root)[0]['task_id']
        self.assertNotEqual(self.store.task(first_task)['quality'],'criteria_met')
        summary=json.loads((self.store.root/f'tasks/{root}/exports/run-summary.json').read_text())
        self.assertFalse(summary['search_complete'])
        self.assertNotEqual(result['quality'],'criteria_met')
    def test_unknown_feedback_keeps_result_unresolved_and_stops_all_candidates(self):
        root,result=self.run_search(self.outputs('best')+self.outputs('other')+self.outputs('third')+
            [self.concern,ProviderError('unknown feedback',outcome_unknown=True)])
        self.assertEqual(result['state'],'interrupted')
        first=self.store.children(root)[0]['task_id']
        self.assertNotEqual(self.store.task(first)['quality'],'criteria_met')
        self.assertEqual(len(self.store.children(root)),3)

class AdditionalBudgetTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    def test_parent_call_ceiling_includes_reserved_unknown_and_children(self):
        root=self.root();child=self.store.create_child(root,1,'direct')
        for _ in range(60):self.store.reserve_call(root,'compare',[],1,1,Decimal(0),Decimal(0))
        with self.assertRaises(BudgetExceeded):self.store.reserve_call(child,'solve',[],1,1,Decimal(0),Decimal(0))
        self.assertEqual(self.store.calls(child),[])
    def test_parent_token_ceiling_includes_other_children(self):
        root=self.root()
        for n in range(1,4):
            c=self.store.create_child(root,n,'direct')
            self.store.reserve_call(c,'solve',[],159000,1,Decimal(0),Decimal(0))
        with self.assertRaises(BudgetExceeded):self.store.reserve_call(root,'compare',[],163000,1,Decimal(0),Decimal(0))
        self.assertEqual(self.store.group_usage(root)['tokens'],477003)
    def test_child_timeout_cannot_exceed_parent_remaining_time(self):
        import time
        from deeper_research.engine import Engine
        root=self.root();self.store.update(root,state='running',active_seconds=3599.5,
            checkpoint={'search':{'segment_started':time.time()}})
        child=self.store.create_child(root,1,'direct');timeouts=[]
        remote=ScriptedRemote(FullLoopSearchTests.outputs(self,'best'))
        original=remote.complete
        def complete(messages,*,max_output_tokens,timeout):
            timeouts.append(timeout);return original(messages,max_output_tokens=max_output_tokens,timeout=timeout)
        remote.complete=complete
        result=Engine(self.store,CorpusBoundary(),remote).run(child)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertTrue(timeouts)
        self.assertTrue(all(0<t<=0.5 for t in timeouts),timeouts)
    def test_ready_or_interrupted_parent_cannot_be_bypassed_by_running_child_directly(self):
        root=self.root();child=self.store.create_child(root,1,'direct')
        for state in ('ready','interrupted','failed','finished'):
            self.store.update(root,state=state)
            with self.subTest(state=state),self.assertRaises(BudgetExceeded):
                self.store.reserve_call(child,'solve',[],1,1,Decimal(0),Decimal(0))

class CompletionBoundaryTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    run_search=FullLoopSearchTests.run_search
    outputs=FullLoopSearchTests.outputs
    def test_pair_decision_and_cursor_survive_interrupt_without_paid_duplicate(self):
        from deeper_research.multicandidate import MultiCandidate
        root=self.root();remote=ScriptedRemote(self.outputs('best')+self.outputs('second')+self.outputs('third')+
            [prefer_word('best'),prefer_word('best'),prefer_word('best')])
        pair=MultiCandidate._pair;fired=[False]
        def stop(instance,left,right):
            value=pair(instance,left,right)
            if not fired[0]:fired[0]=True;raise KeyboardInterrupt()
            return value
        with patch.object(MultiCandidate,'_pair',stop):
            first=MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
        self.assertEqual(first['state'],'interrupted')
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root,retry_unknown=True)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual(len(self.store.calls(root)),2)
        self.assertEqual(len(result['checkpoint']['search']['comparisons']),2)
    def test_failed_fourth_is_reported_as_incomplete_even_with_accepted_incumbent(self):
        replies=self.outputs('best')+self.outputs('second')+self.outputs('third')+[
            prefer_word('best','Clarify assumptions.'),prefer_word('best','Clarify assumptions.')]
        root,result=self.run_search(replies+[candidate('fourth'),{},{}])
        self.assertEqual(len(self.store.children(root)),4)
        summary=json.loads((self.store.root/f'tasks/{root}/exports/run-summary.json').read_text())
        self.assertFalse(summary['search_complete'])
        self.assertNotEqual(result['quality'],'criteria_met')
        self.assertEqual(summary['selected_quality'],'criteria_met')
    def test_shared_plan_is_compiled_once_and_all_three_are_independently_verified(self):
        from deeper_research.multicandidate import MultiCandidate
        root=self.store.create_task({'mode':'multi','effort':'high','question':'Use evidence',
            'criteria':['Use evidence'],'source_ids':['s1'],'profile':self.profile})
        remote=ScriptedRemote([fixture_plan(['Use evidence'])]+self.outputs('best')+self.outputs('second')+
            self.outputs('third')+[prefer_word('best'),prefer_word('best')])
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual([c['role'] for c in self.store.calls(root)],['plan','compare','compare'])
        for c in self.store.children(root):
            self.assertEqual([x['role'] for x in self.store.calls(c['task_id'])],['solve','review','verify'])

class ChallengerStopRecoveryTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    outputs=FullLoopSearchTests.outputs
    def test_interrupt_after_fourth_loses_does_not_start_fifth_on_resume(self):
        from deeper_research.multicandidate import MultiCandidate
        tie=prefer_word('best','Another possible improvement.')
        root=self.root();remote=ScriptedRemote(self.outputs('best')+self.outputs('second')+self.outputs('third')+[
            prefer_word('best','Clarify assumptions.'),prefer_word('best','Clarify assumptions.')]+
            self.outputs('fourth')+[tie]+self.outputs('fifth')+[tie])
        pair=MultiCandidate._pair
        def stop(instance,left,right):
            result=pair(instance,left,right)
            if self.store.task(right)['spec']['candidate_number']==4:raise KeyboardInterrupt()
            return result
        with patch.object(MultiCandidate,'_pair',stop):MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root,retry_unknown=True)
        self.assertEqual(len(self.store.children(root)),4)
        self.assertEqual(result['reason'],'no_improvement')

class FeedbackResumeTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    outputs=FullLoopSearchTests.outputs
    concern=FeedbackBoundaryTests.concern
    def test_export_fallback_does_not_change_inflight_comparison_identity(self):
        from deeper_research.multicandidate import MultiCandidate
        root=self.root();remote=ScriptedRemote(self.outputs('best')+self.outputs('other')+self.outputs('third')+
            [self.concern,ProviderError('unknown feedback',outcome_unknown=True)])
        first=MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
        self.assertEqual(first['state'],'interrupted')
        rejected=verification(True)
        rejected['issue_checks']=[{'id':'issue-1','status':'rejected','reason':'Evidence supports it.','evidence_ids':['b1']}]
        remote=ScriptedRemote([rejected,prefer_word('best'),prefer_word('best')])
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root,retry_unknown=True)
        self.assertEqual((result['state'],result['quality']),('finished','criteria_met'))
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'best [b1]')
        self.assertEqual(len(self.store.calls(root)),3)

class ConcernContextTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    outputs=FullLoopSearchTests.outputs
    run_search=FullLoopSearchTests.run_search
    concern=FeedbackBoundaryTests.concern
    def test_feedback_verifier_sees_exact_answer_span_and_focused_query(self):
        def adjudicate(body):
            issue=body['feedback_to_check'][0]
            self.assertEqual(issue['answer_quote'],'best')
            self.assertEqual(issue['query'],'different conditions')
            self.assertEqual(issue['feedback_kind'],'evidence')
            result=verification(True)
            result['issue_checks']=[{'id':'issue-1','status':'rejected','reason':'Supported.','evidence_ids':['b1']}]
            return result
        _,result=self.run_search(self.outputs('best')+self.outputs('second')+self.outputs('third')+
            [self.concern,adjudicate,prefer_word('best'),prefer_word('best')])
        self.assertEqual(result['quality'],'criteria_met')

class ConfirmedConcernTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    outputs=FullLoopSearchTests.outputs
    run_search=FullLoopSearchTests.run_search
    concern=FeedbackBoundaryTests.concern
    def test_confirmed_outer_concern_triggers_original_lookup_revision_and_reverification(self):
        confirmed=verification(False,blocking=True,action='retrieve')
        root,result=self.run_search(self.outputs('best')+self.outputs('other')+self.outputs('third')+[
            self.concern,confirmed,candidate('best corrected'),review(True),verification(True),
            prefer_word('best corrected'),prefer_word('best corrected')])
        first=self.store.children(root)[0]['task_id'];child=self.store.task(first)
        self.assertEqual(result['quality'],'criteria_met')
        self.assertEqual([c['role'] for c in self.store.calls(first)],
            ['solve','review','verify','verify','revise','review','verify'])
        self.assertGreater(len(child['checkpoint']['lookups']),1)
        self.assertIn('best corrected',self.store.read_artifact(result['selected'])['answer'])

class FifthAttemptTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    outputs=FullLoopSearchTests.outputs
    run_search=FullLoopSearchTests.run_search
    def test_clear_fourth_winner_gets_final_attempt_even_without_new_suggested_weakness(self):
        replies=self.outputs('first')+self.outputs('second')+self.outputs('third')+[
            prefer_word('first','Improve precision.'),prefer_word('first','Improve precision.')]
        replies+=self.outputs('fourth')+[prefer_word('fourth')]+self.outputs('fifth')+[prefer_word('fifth')]
        root,result=self.run_search(replies)
        self.assertEqual(len(self.store.children(root)),5)
        self.assertEqual(self.store.read_artifact(result['selected'])['answer'],'fifth [b1]')

class FailedCheckedCandidateTests(unittest.TestCase):
    setUp=GroupBudgetTests.setUp
    tearDown=GroupBudgetTests.tearDown
    root=GroupBudgetTests.root
    def test_failed_candidate_with_historical_verification_never_enters_pair_judge(self):
        from deeper_research.multicandidate import MultiCandidate
        from deeper_research.engine import Engine
        replies=[]
        for text in ('first','second','third'):
            replies += [candidate(text),review(),verification(action='blocked')]
        class Remote(ScriptedRemote):
            def complete(self,messages,**kwargs):
                body=json.loads(messages[-1]['content'])
                if 'candidates' in body:raise AssertionError('Incomplete candidate was sent to the pair judge')
                return super().complete(messages,**kwargs)
        root=self.root();original=Engine.run
        def run(engine,tid,*args,**kwargs):
            result=original(engine,tid,*args,**kwargs)
            if result['spec'].get('candidate_number')==1:
                # A failed later step may retain an earlier checked answer.
                self.store.update(tid,state='failed',reason='error')
                return self.store.task(tid)
            return result
        with patch.object(Engine,'run',run):
            result=MultiCandidate(self.store,CorpusBoundary(),Remote(replies)).run(root)
        first=self.store.children(root)[0]['task_id']
        self.assertTrue(MultiCandidate(self.store,None,None)._item(first)['checked'])
        self.assertEqual(self.store.task(first)['state'],'failed')
        self.assertEqual((result['state'],result['quality'],result['reason']),('finished','partial','initial_candidates_incomplete'))
        self.assertFalse(self.store.calls(root))
        self.assertFalse(json.loads((self.store.root/f'tasks/{root}/exports/run-summary.json').read_text())['search_complete'])
    def test_feedback_save_interrupt_cannot_admit_failed_candidate_on_resume(self):
        from deeper_research.multicandidate import MultiCandidate
        from deeper_research.engine import Engine
        root=self.root();check=verification(True)
        check['issue_checks']=[{'id':'issue-1','status':'rejected','reason':'Evidence supports it.','evidence_ids':['b1']}]
        replies=[]
        for text in ('best','other','third'):replies+=FullLoopSearchTests.outputs(self,text)
        replies += [lambda body:FeedbackBoundaryTests.concern(self,body),check,prefer_word('best'),prefer_word('best')]
        remote=ScriptedRemote(replies);original_run=Engine.run;original_save=MultiCandidate._save;fired=[False]
        def fail_after_feedback(engine,tid,*args,**kwargs):
            result=original_run(engine,tid,*args,**kwargs)
            if result['checkpoint'].get('external_feedback_keys'):
                self.store.update(tid,state='failed',reason='error');return self.store.task(tid)
            return result
        def interrupt_after_save(controller,**fields):
            original_save(controller,**fields)
            job=controller.s.get('comparison_job',{})
            if job.get('feedback_round')==1 and not job.get('request') and not fired[0]:
                fired[0]=True;raise KeyboardInterrupt()
        with patch.object(Engine,'run',fail_after_feedback),patch.object(MultiCandidate,'_save',interrupt_after_save):
            first=MultiCandidate(self.store,CorpusBoundary(),remote).run(root)
        self.assertEqual(first['state'],'interrupted')
        self.assertEqual(len(self.store.calls(root)),1)
        result=MultiCandidate(self.store,CorpusBoundary(),remote).run(root,retry_unknown=True)
        self.assertEqual(len(self.store.calls(root)),1)
        self.assertEqual((result['state'],result['quality']),('finished','partial'))
        self.assertTrue(result['checkpoint']['search']['comparison_unresolved'])
