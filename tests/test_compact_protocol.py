"""Compact wire judgments preserve original task and conservative audits."""
import unittest
from deeper_research.acceptance import validate_plan,audit_coverage
from deeper_research.contracts import validate_candidate,validate_review,validate_verification,review_accepted
from deeper_research.semantics import semantic_units,audit_semantics
from deeper_research.protocol import audit_request,expand_audit

class CompactProtocolTests(unittest.TestCase):
 def setUp(self):
  self.plan=validate_plan({'c1':[['fact','State feedback access']]},['State feedback access'],'Question',assign_anchors=True)
  self.candidate=validate_candidate({'answer':'Self-Refine cannot access feedback. [b1]',
   'claims':[{'text':'Self-Refine cannot access feedback.','evidence_ids':['b1']}],'open_issues':[]},{'b1'},criterion_count=1)
  self.evidence=[{'id':'b1','text':'RL methods cannot access feedback, unlike Self-Refine.'}]
 def reply(self,relation='different_subject'):
  return {'basis':[['b1',self.evidence[0]['text'],'RL methods']],
    'coverage':{'c1.r1':['met',['A1'],[1],'']},
    'alignment':{u['id']:[relation,[1],'The source predicate applies to RL methods.'] for u in semantic_units(self.candidate)},
    'feedback':{},'omissions':[]}
 def expand(self,value):
  return expand_audit(value,self.plan,self.candidate,[],self.evidence)
 def test_compact_plan_binds_program_ids_and_original_criterion(self):
  r=self.plan['criteria'][0]['requirements'][0]
  self.assertEqual((r['id'],r['kind'],r['request_quote']),('c1.r1','fact','State feedback access'))
 def test_actual_legacy_keyed_plan_is_supported_without_new_task(self):
  value=validate_plan({'c1':[{'text':'State feedback access','kind':'fact'}]},['State feedback access'],'Question',assign_anchors=True)
  self.assertEqual(value,self.plan)
 def test_recorded_keyed_requirement_container_is_read_without_echoed_task(self):
  value=validate_plan({'c1':{'requirements':[{'kind':'fact','text':'State feedback access'}]}},['State feedback access'],'Question',assign_anchors=True)
  self.assertEqual(value,self.plan)
 def test_subject_reversal_cannot_pass_even_with_a_real_quote(self):
  r=self.expand(self.reply());validate_verification(r,{'b1'},1,['claim-1'],[],self.evidence)
  audit_coverage(r,self.plan,self.candidate,self.evidence,['State feedback access'],'Question')
  audit_semantics(r,self.candidate,self.evidence)
  self.assertFalse(review_accepted(r));self.assertEqual(r['action'],'revise')
 def test_positive_wire_reuses_one_literal_basis(self):
  r=self.expand(self.reply('entailed'));validate_verification(r,{'b1'},1,['claim-1'],[],self.evidence)
  self.assertEqual(len(r['source_quotes']),1)
  self.assertEqual(r['requirement_checks'][0]['source_quote'],self.evidence[0]['text'])
 def test_missing_alignment_or_unknown_reference_cannot_default_to_success(self):
  for mutate in (lambda v:v['alignment'].pop(next(iter(v['alignment']))),
                 lambda v:v['coverage']['c1.r1'][1].append('invented'),
                 lambda v:v['alignment'][next(iter(v['alignment']))][1].append(9)):
   v=self.reply();mutate(v)
   with self.assertRaises(ValueError):self.expand(v)
 def test_unlocated_basis_downgrades_instead_of_certifying(self):
  v=self.reply('entailed');v['basis'][0][1]='fabricated quotation'
  r=self.expand(v);validate_verification(r,{'b1'},1,['claim-1'],[],self.evidence)
  audit_semantics(r,self.candidate,self.evidence)
  self.assertFalse(review_accepted(r))
 def test_inference_alias_is_unclear_not_a_positive_factual_verdict(self):
  r=self.expand(self.reply('inference'))
  self.assertTrue(all(c['relation']=='unclear' for c in r['semantic_checks']))
  self.assertEqual(r['claim_checks'][0]['status'],'unverified')
 def test_feedback_complete_and_not_treated_as_authority(self):
  raw=[{'id':'issue-1','severity':'blocking','reason':'Concern','evidence_ids':[]}]
  v=self.reply('entailed')
  with self.assertRaises(ValueError):expand_audit(v,self.plan,self.candidate,raw,self.evidence)
  v['feedback']={'issue-1':['rejected','Source and answer agree.']}
  self.assertEqual(expand_audit(v,self.plan,self.candidate,raw,self.evidence)['issue_checks'][0]['status'],'rejected')
 def test_unresolved_feedback_with_missing_source_keeps_lookup_available(self):
  raw=[{'id':'issue-1','severity':'blocking','reason':'Check access','evidence_ids':[],
        'feedback_kind':'evidence','query':'intermediate feedback access'}]
  v=self.reply('unclear');v['feedback']={'issue-1':['unresolved','Need another source section.']}
  r=expand_audit(v,self.plan,self.candidate,raw,self.evidence)
  validate_verification(r,{'b1'},1,['claim-1'],['issue-1'],self.evidence)
  audit_coverage(r,self.plan,self.candidate,self.evidence,['State feedback access'],'Question')
  audit_semantics(r,self.candidate,self.evidence)
  self.assertEqual(r['action'],'retrieve');self.assertFalse(review_accepted(r))
 def test_confirmed_external_evidence_concern_preserves_focused_lookup(self):
  raw=[{'id':'issue-1','severity':'blocking','reason':'Missing section','evidence_ids':[],
        'feedback_kind':'evidence','query':'termination condition'}]
  v=self.reply('entailed');v['feedback']={'issue-1':['confirmed','Relevant section still needs lookup.']}
  r=expand_audit(v,self.plan,self.candidate,raw,self.evidence)
  self.assertEqual((r['action'],r['query']),('retrieve','termination condition'))
 def test_request_uses_single_examples_not_repeated_per_item_templates(self):
  body,instruction=audit_request(self.plan,self.candidate,[],False)
  self.assertNotIn('claim_checks',body['response_schema'])
  self.assertNotIn('criteria',body['response_schema'])
  self.assertNotIn('source_quotes',body['response_schema'])
  self.assertNotIn('incumbent',body)
  self.assertEqual(body['comparison_mode'],'independent_coverage')
 def test_omitted_omission_judgment_is_not_assumed_empty(self):
  v=self.reply();v.pop('omissions')
  with self.assertRaises(ValueError):self.expand(v)
 def test_unavailable_is_a_conservative_stop_not_success(self):
  v=self.reply('entailed');v['coverage']['c1.r1'][0]='unavailable';v['coverage']['c1.r1'][3]='Local accuracy was never measured.'
  r=self.expand(v);self.assertEqual(r['action'],'blocked');self.assertFalse(review_accepted(r))

class CompactEngineTests(unittest.TestCase):
 def test_plan_solve_review_verify_complete_through_actual_wire_boundary(self):
  import json,tempfile
  from pathlib import Path
  from deeper_research.engine import Engine
  from deeper_research.storage import Store
  from deeper_research.providers import ModelResult
  from test_engine import RemoteBoundary,CorpusBoundary,candidate
  class CompactRemote(RemoteBoundary):
   def __init__(self):super().__init__([]);self.roles=[]
   def complete(self,messages,**kwargs):
    body=json.loads(messages[-1]['content'])
    if not body['evidence']:
     v={'c1':[['fact','Use evidence']]};self.roles.append('plan')
    elif 'candidate' not in body:
     v=candidate('Evidence says original and revised have different conditions.');self.roles.append('solve')
    else:
     self.roles.append('verify' if 'feedback_to_check' in body else 'review')
     self.assertions(body)
     v={'basis':[['E1',body['evidence'][0]['text'],'Evidence']],
        'coverage':{r['id']:['met',['A1'],[1],''] for c in body['requirement_plan']['criteria'] for r in c['requirements']},
        'alignment':{u['id']:['entailed',[1],'Subject and predicate agree.'] for u in body['semantic_units']},
        'feedback':{i['id']:['rejected','No defect.'] for i in body.get('feedback_to_check',[])},'omissions':[]}
    return ModelResult(json.dumps(v),100,100,200,'request','test',{})
   def assertions(self,body):
    assert 'claim_checks' not in body['response_schema']
  with tempfile.TemporaryDirectory() as tmp:
   store=Store(Path(tmp));remote=CompactRemote()
   tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],
                         'effort':'high','profile':remote.profile.public_dict()})
   result=Engine(store,CorpusBoundary(),remote).run(tid)
   self.assertEqual(result['quality'],'criteria_met')
   self.assertEqual(remote.roles,['plan','solve','review','verify'])
   checked=store.read_artifact(result['checkpoint']['best_review'])
   requested=json.loads(store.calls(tid)[-1]['request'][-1]['content'])['semantic_units']
   self.assertEqual({c['id'] for c in checked['semantic_checks']},{u['id'] for u in requested})

class BasisBindingTests(unittest.TestCase):
 def test_a_quote_cannot_borrow_another_basis_source_id(self):
  ca=validate_candidate({'answer':'Method A runs and B stops. [b1] [b2]',
   'claims':[{'text':'Method A runs; Method B stops.','evidence_ids':['b1','b2']}],'open_issues':[]},{'b1','b2'},criterion_count=1)
  plan=validate_plan({'c1':[['fact','Describe methods']]},['Describe methods'],'Question',assign_anchors=True)
  evidence=[{'id':'b1','text':'Unrelated source.'},{'id':'b2','text':'Method A runs. Method B stops.'}]
  v={'basis':[['b1','Method A runs.','Method A'],['b2','Method B stops.','Method B']],
   'coverage':{'c1.r1':['met',['A1'],[1,2],'']},
   'alignment':{u['id']:['entailed',[1,2] if u['id'].endswith('p1') else [2],'Test proposition.'] for u in semantic_units(ca)},
   'feedback':{},'omissions':[]}
  r=expand_audit(v,plan,ca,[],evidence)
  validate_verification(r,{'b1','b2'},1,['claim-1'],[],evidence)
  audit_coverage(r,plan,ca,evidence,['Describe methods'],'Question');audit_semantics(r,ca,evidence)
  self.assertFalse(review_accepted(r))

 def test_coverage_cannot_borrow_id_even_when_all_claim_alignments_are_correct(self):
  ca=validate_candidate({'answer':'Method A runs. [b1] [b2]',
   'claims':[{'text':'Method A runs.','evidence_ids':['b1','b2']}],'open_issues':[]},{'b1','b2'},criterion_count=1)
  plan=validate_plan({'c1':[['fact','Describe A']]},['Describe A'],'Question',assign_anchors=True)
  evidence=[{'id':'b1','text':'Unrelated.'},{'id':'b2','text':'Method A runs.'}]
  v={'basis':[['b1','Method A runs.','Method A'],['b2','Method A runs.','Method A']],
   'coverage':{'c1.r1':['met',['A1'],[1,2],'']},
   'alignment':{u['id']:['entailed',[2],'Subject and predicate agree.'] for u in semantic_units(ca)},'feedback':{},'omissions':[]}
  r=expand_audit(v,plan,ca,[],evidence)
  validate_verification(r,{'b1','b2'},1,['claim-1'],[],evidence)
  audit_coverage(r,plan,ca,evidence,['Describe A'],'Question');audit_semantics(r,ca,evidence)
  self.assertFalse(review_accepted(r));self.assertEqual(r['action'],'retrieve')

class ControllerPolicyTests(unittest.TestCase):
 def test_stalled_compact_revision_keeps_original_alternative_branch(self):
  import tempfile
  from pathlib import Path
  from deeper_research.engine import Engine
  from deeper_research.storage import Store
  from test_engine import RemoteBoundary,CorpusBoundary,candidate,review
  from coverage_fixtures import fixture_plan
  with tempfile.TemporaryDirectory() as tmp:
   store=Store(Path(tmp));remote=RemoteBoundary([])
   tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],
       'effort':'high','profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
   engine=Engine(store,CorpusBoundary(),remote);engine.task_id=tid;engine.spec=store.task(tid)['spec']
   ca=store.save_artifact(tid,'candidate',candidate('Answer'))
   report=review();report['wire_protocol']='compact-v1';path=store.save_artifact(tid,'verification',report)
   engine.cp={'target':ca,'best':ca,'best_review':path,'stage':'precheck','baseline_rank':[0,0,-1],
       'stalled':1,'directions':1,'last_review':path}
   engine._apply_review(report,path)
   self.assertEqual(engine.cp['action'],'alternative')
   self.assertTrue(store.read_artifact(engine.cp['last_review'])['strategy'])
