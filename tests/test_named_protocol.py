"""Named judgments and whole-proposition grouping retain conservative routing."""
import unittest
from copy import deepcopy
from deeper_research.acceptance import validate_plan, audit_coverage
from deeper_research.contracts import validate_candidate, validate_verification, review_accepted
from deeper_research.protocol import audit_request, expand_audit
from deeper_research.semantics import audit_semantics


class NamedAuditTests(unittest.TestCase):
    def setUp(self):
        self.plan = validate_plan({'c1': [['fact', 'Compare A and B']]}, ['Compare A and B'], 'Question', assign_anchors=True)
        self.ca = validate_candidate({'answer': 'A can use feedback; B updates weights. [b1]',
            'claims': [{'text': 'A can use feedback; B updates weights.', 'evidence_ids': ['b1']}],
            'open_issues': []}, {'b1'}, 1)
        self.evidence = [{'id': 'b1', 'text': 'A can use feedback; B updates weights.'}]
        self.reply = {'basis': {'Q1': {'source': 'b1', 'quote': self.evidence[0]['text'], 'subject': 'A'}},
            'coverage': {'c1.r1': {'status': 'met', 'answer': self.ca['answer'], 'basis': ['Q1'], 'gap': ''}},
            'alignment': {'claim-1': {'relation': 'entailed', 'basis': ['Q1'], 'reason': 'Both clauses agree.'}},
            'feedback': {}, 'omissions': []}

    def audit(self, value):
        result = expand_audit(value, self.plan, self.ca, [], self.evidence)
        validate_verification(result, {'b1'}, 1, ['claim-1'], [], self.evidence)
        audit_coverage(result, self.plan, self.ca, self.evidence, ['Compare A and B'], 'Question')
        return audit_semantics(result, self.ca, self.evidence)

    def test_named_reply_can_complete_without_numeric_basis_indices(self):
        self.assertTrue(review_accepted(self.audit(deepcopy(self.reply))))

    def test_group_retains_both_propositions_and_known_bad_half_blocks(self):
        body, _ = audit_request(self.plan, self.ca, [], False)
        self.assertEqual(next(u['text'] for u in body['semantic_units'] if u['id'] == 'claim-1'),
                         'A can use feedback; B updates weights. [b1]')
        value = deepcopy(self.reply)
        value['alignment']['claim-1'].update(relation='weaker_scope', reason='Only feedback access is supported.')
        result = self.audit(value)
        self.assertFalse(review_accepted(result))
        self.assertEqual(result['action'], 'revise')

    def test_missing_whole_group_cannot_be_assumed_audited(self):
        value = deepcopy(self.reply); value['alignment'].pop('claim-1')
        with self.assertRaises(ValueError): self.audit(value)

    def test_source_ids_cannot_be_used_as_quote_keys(self):
        value = deepcopy(self.reply); value['alignment']['claim-1']['basis'] = ['b1']
        with self.assertRaises(ValueError): self.audit(value)

    def test_quote_cannot_borrow_another_source(self):
        self.evidence.append({'id': 'b2', 'text': self.evidence[0]['text']})
        self.evidence[0]['text'] = 'Unrelated.'
        self.assertFalse(review_accepted(self.audit(deepcopy(self.reply))))

    def test_coverage_uses_literal_answer_quote_without_line_number_bookkeeping(self):
        body, _ = audit_request(self.plan, self.ca, [], False)
        self.assertNotIn('answer_spans',body)
        self.assertIsInstance(body['response_schema']['coverage']['requirement ID']['answer'],str)

    def test_entailment_needs_all_selected_quotes_located(self):
        value = deepcopy(self.reply)
        value['basis']['Q2'] = {'source': 'b1', 'quote': 'Invented quote', 'subject': 'Invented'}
        value['alignment']['claim-1']['basis'].append('Q2')
        self.assertFalse(review_accepted(self.audit(value)))

class NamedClosureTests(unittest.TestCase):
    def test_verifier_lookup_feeds_revision_then_full_independent_recheck(self):
        """Repeating review on the unchanged answer burns closure slots."""
        import json, tempfile
        from pathlib import Path
        from deeper_research.engine import Engine
        from deeper_research.storage import Store
        from deeper_research.providers import ModelResult
        from test_engine import RemoteBoundary, CorpusBoundary, candidate
        from coverage_fixtures import fixture_plan
        class Remote(RemoteBoundary):
            def __init__(self): super().__init__([]); self.roles=[]
            def complete(self, messages, **kwargs):
                body=json.loads(messages[-1]['content']); step=len(self.roles)
                role='revise' if 'previous_candidate' in body else 'verify' if 'feedback_to_check' in body else 'review' if 'candidate' in body else 'solve'
                self.roles.append(role)
                if step in (0,3): value=candidate('original' if step==0 else 'revised')
                else:
                    block=body['evidence'][0]; missing=step==2
                    value={'basis':{'Q1':{'source':block['id'],'quote':block['text'],'subject':'Evidence'}},
                        'coverage':{'c1.r1':{'status':'unsupported' if missing else 'met','answer':body['candidate']['answer'][:160],'basis':['Q1'],
                            'gap':'additional feedback evidence' if missing else ''}},
                        'alignment':{u['id']:{'relation':'entailed','basis':['Q1'],'reason':'Source inspected.'} for u in body['semantic_units']},
                        'feedback':{i['id']:{'status':'rejected','reason':'Checked source.'} for i in body.get('feedback_to_check',[])},
                        'omissions':[]}
                return ModelResult(json.dumps(value),100,100,200,'test','test',{})
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)); remote=Remote()
            tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],
                'effort':'high','profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
            result=Engine(store,CorpusBoundary(),remote).run(tid)
            self.assertEqual(result['quality'],'criteria_met')
            self.assertEqual(remote.roles,['solve','review','verify','revise','review','verify'])
            self.assertIn('revised',store.read_artifact(result['selected'])['answer'])
            self.assertTrue(any(x['query']=='additional feedback evidence' for x in result['checkpoint']['lookups']))
            self.assertEqual(len(result['checkpoint']['review_verifications']),2)

class GroupSourceRetentionTests(unittest.TestCase):
    setUp = NamedAuditTests.setUp
    def test_two_source_group_keeps_both_bindings_in_derived_reports(self):
        self.evidence = [{'id':'b1','text':'A can use feedback.'},{'id':'b2','text':'B updates weights.'}]
        value = deepcopy(self.reply)
        value['basis'] = {'Q1':{'source':'b1','quote':'A can use feedback.','subject':'A'},
                          'Q2':{'source':'b2','quote':'B updates weights.','subject':'B'}}
        value['coverage']['c1.r1']['basis']=['Q1','Q2']
        for row in value['alignment'].values(): row['basis']=['Q1','Q2']
        result=expand_audit(value,self.plan,self.ca,[],self.evidence)
        self.assertEqual(result['claim_checks'][0]['evidence_ids'],['b1','b2'])
        self.assertEqual({q['evidence_id'] for q in result['source_quotes']},{'b1','b2'})
        self.assertEqual(result['requirement_checks'][0]['evidence_ids'],['b1','b2'])
        validate_verification(result,{'b1','b2'},1,['claim-1'],[],self.evidence)
        audit_semantics(result,self.ca,self.evidence)
        self.assertTrue(review_accepted(result))
        # One grouped verdict must not masquerade as per-clause judgments.
        self.assertEqual([c['id'] for c in result['semantic_checks']],['claim-1'])

class BodySourcePinTests(unittest.TestCase):
    def test_body_only_review_basis_is_pinned_from_actual_review_request(self):
        import json,tempfile
        from pathlib import Path
        from unittest.mock import patch
        from deeper_research.storage import Store
        from deeper_research.engine import Engine
        from test_engine import CorpusBoundary,candidate
        from test_engine import RemoteBoundary
        from deeper_research.providers import ModelResult
        from coverage_fixtures import fixture_plan
        class Corpus(CorpusBoundary):
            def search(self,*args,**kwargs):
                return super().search(*args,**kwargs)+[{'id':'b2','text':'Body support only.','title':'Body source','location':{}}]
        def audit(body):
            return {'basis':{'Q1':{'source':'E1','quote':body['evidence'][0]['text'],'subject':'Evidence'},
                             'Q2':{'source':'E2','quote':'Body support only.','subject':'Body'}},
                'coverage':{'c1.r1':{'status':'met','answer':'Original','basis':['Q1'],'gap':''}},
                'alignment':{u['id']:{'relation':'entailed','basis':['Q1' if u['claim_id'] else 'Q2'],'reason':'Checked.'} for u in body['semantic_units']},
                'feedback':{},'omissions':[]}
        class Remote(RemoteBoundary):
            def complete(self,messages,**kwargs):
                value=next(self.results)
                if callable(value):value=value(json.loads(messages[-1]['content']))
                return ModelResult(json.dumps(value),100,100,200,'test','test',{})
        with tempfile.TemporaryDirectory() as tmp:
            initial=candidate('Original');initial['answer']='Original [b1]. Additional body claim.'
            store=Store(Path(tmp));remote=Remote([initial,audit]);engine=Engine(store,Corpus(),remote)
            tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],
                'effort':'high','profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
            with patch.object(engine,'_verify',side_effect=KeyboardInterrupt):engine.run(tid)
            engine.cp=store.task(tid)['checkpoint']
            report=store.read_artifact(engine.cp['pending_review'])
            self.assertTrue(any(c['id'].startswith('body.') and c['evidence_ids']==['b2'] for c in report['semantic_checks']))
            packet=[b for b in store.read_artifact(engine.cp['packet']) if b['id']=='b1']
            engine.cp['packet']=store.save_artifact(tid,'packet',packet)
            payload=json.loads(engine._messages('verify')[-1]['content'])
            self.assertIn('b2',{b['canonical_id'] for b in payload['evidence']})
