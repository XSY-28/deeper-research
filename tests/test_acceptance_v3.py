"""Missing required answers must not pass just because included claims are true."""
import json,tempfile,unittest
from pathlib import Path
from copy import deepcopy
from deeper_research.engine import Engine
from deeper_research.storage import Store
from deeper_research.corpus import Corpus
from deeper_research.providers import ModelResult,ProviderProfile

PLAN={'criteria':[{'id':'c1','requirements':[
 {'id':'c1.r1','text':'Give method A stopping rule','kind':'fact','request_quote':'method A'},
 {'id':'c1.r2','text':'Give method B stopping rule','kind':'fact','request_quote':'method B'}]}]}

class Remote:
 def __init__(self,replies):
  self.replies=iter(replies);self.profile=ProviderProfile.from_dict({'protocol':'openai_chat','base_url':'https://example.com/v1','model':'test','api_key_env':'TEST_KEY','input_usd_per_million':'0.3','output_usd_per_million':'1.2'})
 def validate_credentials(self):pass
 def complete(self,messages,*,max_output_tokens,timeout):
  value=next(self.replies)
  if callable(value):value=value(json.loads(messages[-1]['content']))
  from coverage_fixtures import with_semantic_receipts
  value=with_semantic_receipts(value,json.loads(messages[-1]['content']))
  return ModelResult(json.dumps(value),100,100,200,'r','test',{})

class AcceptanceV3Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'work');self.corpus=Corpus(self.store.root)
  p=self.root/'source.md';p.write_text('Method A stops after three trials.\n\nMethod B stops after five trials.\n')
  self.source=self.corpus.ingest(p);self.a,self.b=[b['id'] for b in self.corpus.search('stops',[self.source])]
 def tearDown(self):self.tmp.cleanup()
 def candidate(self,both=False):
  text='Method A stops after three trials.'+(' Method B stops after five trials.' if both else '')
  return {'answer':text+' ['+self.a+']'+(' ['+self.b+']' if both else ''),
   'claims':[{'id':'claim-1','text':text,'evidence_ids':[self.a,self.b] if both else [self.a]}],'open_issues':[]}
 def audit(self,verify=False,complete=False):
  textA='Method A stops after three trials.';textB='Method B stops after five trials.'
  value={'criteria':[{'id':'c1','status':'met','reason':'Included claim true','evidence_ids':[self.a]}],
   'claim_checks':[{'id':'claim-1','status':'supported','reason':'Supported','evidence_ids':[self.a,self.b] if complete else [self.a]}],
   'issues':[],'action':'accept','preferred':'candidate','comparison_reason':'Coverage checked','query':'','strategy':'',
   'requirement_checks':[{'id':'c1.r1','status':'met','answer_mode':'result','answer_quote':textA,'evidence_ids':[self.a],'source_quote':textA,'gap_type':'none','query':''},
    {'id':'c1.r2','status':'met' if complete else 'unmet','answer_quote':textB if complete else '',
     'answer_mode':'result' if complete else 'missing',
     'evidence_ids':[self.b] if complete else [],'source_quote':textB if complete else '',
     'gap_type':'none' if complete else 'answer','query':''}]}
  if verify:value.update(issue_checks=[],source_quotes=[{'claim_id':'claim-1','evidence_id':self.a,'quote':textA}])
  return value
 def make(self,replies,effort='medium',plan=PLAN):
  remote=Remote(replies);spec={'question':'Give method A and method B stopping rules','criteria':['Give a real stopping rule for each method'],
   'source_ids':[self.source],'effort':effort,'profile':remote.profile.public_dict()}
  if plan is not None:spec['requirements']=deepcopy(plan)
  tid=self.store.create_task(spec);return tid,Engine(self.store,self.corpus,remote)
 def test_included_true_claim_does_not_cover_omitted_method(self):
  tid,e=self.make([self.candidate(),self.audit(),self.audit(True)])
  result=e.run(tid)
  self.assertEqual(result['quality'],'partial')
  self.assertNotEqual(result['reason'],'criteria_met')
 def test_gap_drives_revision_and_full_recheck(self):
  tid,e=self.make([self.candidate(),self.audit(),self.audit(True),self.candidate(True),self.audit(complete=True),self.audit(True,True)],'high')
  result=e.run(tid)
  self.assertEqual(result['quality'],'criteria_met')
  self.assertIn('Method B stops after five',self.store.read_artifact(result['selected'])['answer'])
  self.assertEqual([c['role'] for c in self.store.calls(tid)],['solve','review','verify','revise','review','verify'])
 def test_requirement_compilation_precedes_candidate_and_costs_a_call(self):
  raw=deepcopy(PLAN)
  for c in raw['criteria']:
   for r in c['requirements']:r.pop('id')
  tid,e=self.make([raw,self.candidate(True),self.audit(complete=True),self.audit(True,True)],plan=None)
  result=e.run(tid)
  self.assertEqual(result['quality'],'criteria_met')
  calls=self.store.calls(tid)
  self.assertEqual([c['role'] for c in calls],['plan','solve','review','verify'])
  body=json.loads(calls[0]['request'][-1]['content']);self.assertNotIn('candidate',body)
  self.assertNotIn('previous_candidate',body)
 def test_missing_evidence_receipt_triggers_scoped_lookup_before_verification(self):
  first=self.audit();first['requirement_checks'][1]['gap_type']='evidence';first['requirement_checks'][1]['query']='Method B five trials'
  tid,e=self.make([self.candidate(),first,self.audit(True),self.candidate(True),self.audit(complete=True),self.audit(True,True)],'high')
  result=e.run(tid)
  self.assertEqual(result['quality'],'criteria_met')
  lookups=result['checkpoint']['lookups']
  self.assertTrue(any(x['query']=='Method B five trials' and x['criterion_ids']==['c1'] for x in lookups))
  verify=self.store.calls(tid)[2];body=json.loads(verify['request'][-1]['content'])
  self.assertTrue(any('Method B stops after five' in b['text'] for b in body['evidence']))

 def test_successful_plan_is_replayed_after_interrupted_artifact_save(self):
  from unittest.mock import patch
  raw=deepcopy(PLAN)
  for group in raw['criteria']:
   for r in group['requirements']:r.pop('id')
  tid,e=self.make([raw,self.candidate(True),self.audit(complete=True),self.audit(True,True)],plan=None)
  save=self.store.save_artifact;interrupted=[False]
  def stop(task,kind,value):
   path=save(task,kind,value)
   if kind=='requirements' and not interrupted[0]:
    interrupted[0]=True;raise KeyboardInterrupt()
   return path
  with patch.object(self.store,'save_artifact',stop):first=e.run(tid)
  self.assertEqual(first['state'],'interrupted')
  final=e.run(tid,retry_unknown=True)
  self.assertEqual(final['quality'],'criteria_met')
  self.assertEqual([c['role'] for c in self.store.calls(tid)],['plan','solve','review','verify'])

 def test_missing_receipt_cannot_accept_and_format_correction_is_bounded(self):
  missing=self.audit();missing.pop('requirement_checks')
  tid,e=self.make([self.candidate(),missing,missing],'high')
  result=e.run(tid)
  self.assertEqual(result['state'],'failed')
  self.assertNotEqual(result['quality'],'criteria_met')
  self.assertEqual([c['role'] for c in self.store.calls(tid)],['solve','review','review'])

 def test_low_skips_paid_planning_and_remains_unchecked(self):
  tid,e=self.make([self.candidate()],effort='low',plan=None)
  result=e.run(tid)
  self.assertEqual(result['quality'],'unchecked')
  self.assertEqual([c['role'] for c in self.store.calls(tid)],['solve'])

 def test_forged_answer_receipt_is_downgraded(self):
  from deeper_research.acceptance import audit_coverage
  report=self.audit(complete=True)
  evidence=self.corpus.search('stops',[self.source])
  audited=audit_coverage(report,PLAN,self.candidate(),evidence,['Give a real stopping rule for each method'],
                         'Give method A and method B stopping rules')
  self.assertNotEqual(audited['criteria'][0]['status'],'met')
  self.assertEqual(audited['action'],'revise')
  self.assertFalse(audited['requirement_checks'][1]['answer_located'])

 def test_unlocated_source_receipt_requests_more_evidence(self):
  from deeper_research.acceptance import audit_coverage
  report=self.audit(complete=True);report['requirement_checks'][1]['source_quote']='Method B runs for nine trials.'
  audited=audit_coverage(report,PLAN,self.candidate(True),self.corpus.search('stops',[self.source]),
                        ['Give a real stopping rule for each method'],'Give method A and method B stopping rules')
  self.assertEqual(audited['action'],'retrieve')
  self.assertFalse(audited['requirement_checks'][1]['source_located'])

 def test_factual_negation_is_a_result_not_a_missing_answer(self):
  from deeper_research.acceptance import audit_coverage
  text='Method A does not update model weights.'
  plan={'criteria':[{'id':'c1','requirements':[{'id':'c1.r1','text':'Does it update weights?','kind':'fact','request_quote':'update'}]}]}
  report=self.audit();report['requirement_checks']=report['requirement_checks'][:1]
  report['requirement_checks'][0].update(answer_quote=text,source_quote=text,evidence_ids=[self.a])
  audited=audit_coverage(report,plan,{'answer':text},[{'id':self.a,'text':text}],['Does it update?'],'Does it update?')
  self.assertEqual(audited['criteria'][0]['status'],'met')

 def test_reported_insufficient_evidence_failure_is_not_automatic_abstention(self):
  from deeper_research.acceptance import audit_coverage
  text='Insufficient evidence causes Method A to return the wrong result.'
  plan={'criteria':[{'id':'c1','requirements':[{'id':'c1.r1','text':'Report method A failure cause','kind':'fact','request_quote':'failure'}]}]}
  report=self.audit();report['requirement_checks']=report['requirement_checks'][:1]
  report['requirement_checks'][0].update(answer_quote=text,source_quote=text,evidence_ids=[self.a])
  audited=audit_coverage(report,plan,{'answer':text},[{'id':self.a,'text':text}],['Report failure'],'Report failure')
  self.assertEqual(audited['criteria'][0]['status'],'met')
 def test_verifier_does_not_repeat_complete_prior_coverage_receipts(self):
  from unittest.mock import patch
  tid,e=self.make([self.candidate(True),self.audit(complete=True)],'high')
  with patch.object(e,'_verify',side_effect=KeyboardInterrupt()):e.run(tid)
  e.cp=self.store.task(tid)['checkpoint']
  path=e.cp['pending_review'];raw=self.store.read_artifact(path)
  raw['audit_diagnostics']='diagnostic detail '*2000
  changed=self.store.save_artifact(tid,'review',raw)
  e.cp['pending_review']=changed;e.cp['review_meta'][changed]=e.cp['review_meta'][path]
  payload=json.loads(e._messages('verify')[-1]['content'])
  self.assertNotIn('requirement_checks',json.dumps(payload['feedback_to_check']))
  self.assertNotIn('audit_diagnostics',json.dumps(payload['feedback_to_check']))
  self.assertEqual(self.store.read_artifact(changed)['audit_diagnostics'],raw['audit_diagnostics'])
  self.assertIn('Method B stops after five',payload['candidate']['answer'])
 def test_explicit_fact_abstention_cannot_fulfil_requested_fact(self):
  from deeper_research.acceptance import audit_coverage
  report=self.audit(complete=True);text='No B rule is reported in these excerpts.'
  report['requirement_checks'][1].update(answer_quote=text,answer_mode='abstention',
    evidence_ids=[self.a],source_quote='Method A stops after three trials.')
  candidate=self.candidate();candidate['answer']+=' '+text
  audited=audit_coverage(report,PLAN,candidate,self.corpus.search('stops',[self.source]),
    ['Give a real stopping rule for each method'],'Give method A and method B stopping rules')
  self.assertNotEqual(audited['criteria'][0]['status'],'met')

 def test_source_reported_provided_evidence_failure_is_a_valid_fact(self):
  from deeper_research.acceptance import audit_coverage
  text='Method A fails because the provided evidence lacks positive examples.'
  plan={'criteria':[{'id':'c1','requirements':[{'id':'c1.r1','text':'Report failure cause','kind':'fact','request_quote':'failure'}]}]}
  report=self.audit();report['requirement_checks']=report['requirement_checks'][:1]
  report['requirement_checks'][0].update(answer_quote=text,source_quote=text,evidence_ids=[self.a])
  audited=audit_coverage(report,plan,{'answer':text},[{'id':self.a,'text':text}],['Report failure'],'Report failure')
  self.assertEqual(audited['criteria'][0]['status'],'met')
 def test_missing_design_or_format_result_never_passes(self):
  from deeper_research.acceptance import audit_coverage
  for kind in ('design','format','abstention'):
   with self.subTest(kind=kind):
    text='No design was proposed.'
    plan={'criteria':[{'id':'c1','requirements':[{'id':'c1.r1','text':'Provide result','kind':kind,'request_quote':'result'}]}]}
    report=self.audit();report['requirement_checks']=report['requirement_checks'][:1]
    report['requirement_checks'][0].update(answer_quote=text,answer_mode='missing',source_quote='',evidence_ids=[])
    audited=audit_coverage(report,plan,{'answer':text},[],['Provide result'],'Provide result')
    self.assertNotEqual(audited['criteria'][0]['status'],'met')

 def test_requested_abstention_can_pass_without_fabricating_source_proof(self):
  from deeper_research.acceptance import audit_coverage
  text='本地中文正确率尚未测量。'
  plan={'criteria':[{'id':'c1','requirements':[{'id':'c1.r1','text':'Declare local uncertainty','kind':'abstention','request_quote':'uncertainty'}]}]}
  report=self.audit();report['requirement_checks']=report['requirement_checks'][:1]
  report['requirement_checks'][0].update(answer_quote=text,answer_mode='abstention',source_quote='',evidence_ids=[])
  audited=audit_coverage(report,plan,{'answer':text},[],['Declare uncertainty'],'Declare uncertainty')
  self.assertEqual(audited['criteria'][0]['status'],'met')
 def test_local_receipts_do_not_upgrade_an_explicit_unmet_criterion(self):
  from deeper_research.acceptance import audit_coverage
  report=self.audit();report['criteria'][0]['status']='unmet'
  audited=audit_coverage(report,PLAN,self.candidate(),self.corpus.search('stops',[self.source]),
    ['Give a real stopping rule for each method'],'Give method A and method B stopping rules')
  self.assertEqual(audited['criteria'][0]['status'],'unmet')

 def test_export_reports_uncovered_required_item(self):
  tid,e=self.make([self.candidate(),self.audit(),self.audit(True)])
  e.run(tid)
  text=(self.store.root/f'tasks/{tid}/exports/final.md').read_text()
  self.assertIn('Give method B stopping rule',text)
 def test_planner_input_label_alias_and_discontinuous_task_anchor_are_checked(self):
  from deeper_research.acceptance import validate_plan
  raw={'acceptance_criteria':[{'id':'c1','requirements':[{'kind':'fact','text':'Describe method B stop rule',
       'request_quote':'method B ... stopping rules'}]}]}
  value=validate_plan(raw,['Give a real stopping rule for each method'],'Give method A and method B stopping rules')
  self.assertEqual(value['criteria'][0]['requirements'][0]['request_quotes'],['method B','stopping rules'])
  raw['acceptance_criteria'][0]['requirements'][0]['request_quote']='method C ... stopping rules'
  with self.assertRaises(ValueError):validate_plan(raw,['Give a real stopping rule for each method'],'Give method A and method B stopping rules')

 def test_six_real_obligations_are_not_dropped_to_fit_artificial_four_item_limit(self):
  from deeper_research.acceptance import validate_plan
  question='For A and B give trigger, allowed actions, and record policy.'
  raw={'criteria':[{'id':'c1','requirements':[{'text':f'{subject} {aspect}','kind':'fact','request_quote':subject+' ... '+aspect}
   for subject in ('A','B') for aspect in ('trigger','allowed actions','record policy')]}]}
  plan=validate_plan(raw,[question],question)
  self.assertEqual(len(plan['criteria'][0]['requirements']),6)

 def test_many_receipts_get_larger_reserved_response_cap_without_extra_calls(self):
  tid,e=self.make([],'high')
  e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.cp={}
  import time
  e.started=time.monotonic();e.prior_seconds=0
  e._ensure_requirements()
  plan=self.store.read_artifact(e.cp['requirements'])
  base=plan['criteria'][0]['requirements'][0]
  plan['criteria'][0]['requirements']=[dict(base,id=f'c1.r{i+1}') for i in range(14)]
  e.cp['requirements']=self.store.save_artifact(tid,'requirements',plan)
  self.assertGreater(e._output_limit('review'),3000)
  self.assertLessEqual(e._output_limit('review'),6000)
 def test_wire_plan_omits_repeated_task_anchor_metadata_but_keeps_task(self):
  from unittest.mock import patch
  tid,e=self.make([self.candidate(),self.audit()],'high')
  with patch.object(e,'_verify',side_effect=KeyboardInterrupt()):e.run(tid)
  e.cp=self.store.task(tid)['checkpoint']
  body=json.loads(e._messages('verify')[-1]['content'])
  self.assertNotIn('request_quote',body['requirement_plan']['criteria'][0]['requirements'][0])
  self.assertIn('request_quotes',self.store.read_artifact(e.cp['requirements'])['criteria'][0]['requirements'][0])
  self.assertEqual(body['question'],'Give method A and method B stopping rules')

 def test_adaptive_output_is_used_by_provider_and_ledger_reservation(self):
  from unittest.mock import patch
  caps=[]
  tid,e=self.make([self.candidate(),self.audit(),self.audit(True)])
  complete=e.provider.complete
  def collect(messages,*,max_output_tokens,timeout):
   caps.append(max_output_tokens);return complete(messages,max_output_tokens=max_output_tokens,timeout=timeout)
  with patch.object(e.provider,'complete',collect),patch.object(e,'_output_limit',return_value=4500):e.run(tid)
  calls=self.store.calls(tid)
  self.assertEqual(caps,[4500,4500,4500])
  self.assertEqual([c['max_output'] for c in calls],[4500,4500,4500])
 def test_real_many_item_request_fits_without_dropping_candidate_or_sources(self):
  from deeper_research.contracts import estimate_tokens,MAX_INPUT
  import time
  f=json.loads((Path(__file__).parent/'fixtures/acceptance-large.json').read_text())
  tid=self.store.create_task({'question':f['question'],'criteria':f['criteria'],'source_ids':f['source_ids'],
   'effort':'high','profile':Remote([]).profile.public_dict(),'requirements':f['plan']})
  e=Engine(self.store,None,Remote([]));e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.started=time.monotonic();e.prior_seconds=0
  packet=self.store.save_artifact(tid,'packet',f['packet']);target=self.store.save_artifact(tid,'candidate',f['candidate'])
  req=self.store.save_artifact(tid,'requirements',f['plan'])
  e.cp={'packet':packet,'packet_history':[packet],'target':target,'requirements':req}
  messages=e._messages('review');payload=json.loads(messages[-1]['content'])
  self.assertLessEqual(estimate_tokens(messages),MAX_INPUT)
  self.assertEqual([b['text'] for b in payload['evidence']],[b['text'] for b in f['packet']])
  self.assertEqual(len(payload['candidate']['claims']),len(f['candidate']['claims']))
  self.assertEqual(payload['question'],f['question'])
 def test_long_incumbent_uses_independent_audit_without_truncating_target(self):
  from deeper_research.contracts import estimate_tokens,MAX_INPUT
  import time
  tid,e=self.make([],'high');e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.started=time.monotonic();e.prior_seconds=0
  packet=self.store.save_artifact(tid,'packet',self.corpus.search('stops',[self.source]))
  target=self.store.save_artifact(tid,'candidate',self.candidate(True))
  old=self.candidate();old['answer']+=' Old narrative.'*2500
  best=self.store.save_artifact(tid,'candidate',old)
  req=self.store.save_artifact(tid,'requirements',self.store.task(tid)['spec']['requirements'])
  e.cp={'packet':packet,'packet_history':[packet],'target':target,'best':best,'requirements':req}
  messages=e._messages('review');payload=json.loads(messages[-1]['content'])
  self.assertLessEqual(estimate_tokens(messages),MAX_INPUT)
  self.assertNotIn('incumbent',payload)
  self.assertEqual(payload['comparison_mode'],'independent_coverage')
  self.assertIn('Method B stops after five',payload['candidate']['answer'])
  self.assertEqual(self.store.read_artifact(best)['answer'],old['answer'])

 def test_independent_comparison_preserves_incumbent_for_coverage_tradeoff(self):
  import time
  tid,e=self.make([],'high');e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.started=time.monotonic();e.prior_seconds=0
  old=self.store.save_artifact(tid,'candidate',self.candidate());new=self.store.save_artifact(tid,'candidate',self.candidate(True))
  before=self.audit();before['criteria'][0]['status']='partial'
  before['action']='revise'
  rp=self.store.save_artifact(tid,'verification',before)
  after=deepcopy(before);after['preferred']='candidate';after['comparison_mode']='independent_coverage'
  after['requirement_checks'][0]['status']='unmet';after['requirement_checks'][1]['status']='met'
  np=self.store.save_artifact(tid,'verification',after)
  e.cp={'target':new,'initial':old,'best':old,'best_review':rp,'stage':'precheck','directions':1}
  e._apply_review(after,np)
  self.assertEqual(e.cp['best'],old)

 def test_independent_comparison_accepts_strict_item_coverage_improvement(self):
  import time
  tid,e=self.make([],'high');e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.started=time.monotonic();e.prior_seconds=0
  old=self.store.save_artifact(tid,'candidate',self.candidate());new=self.store.save_artifact(tid,'candidate',self.candidate(True))
  before=self.audit();before['criteria'][0]['status']='partial'
  rp=self.store.save_artifact(tid,'verification',before)
  after=self.audit(True,True);after['preferred']='incumbent';after['comparison_mode']='independent_coverage'
  np=self.store.save_artifact(tid,'verification',after)
  req=self.store.save_artifact(tid,'requirements',self.store.task(tid)['spec']['requirements'])
  e.cp={'target':new,'initial':old,'best':old,'best_review':rp,'requirements':req,'stage':'precheck','directions':1}
  e._apply_review(after,np)
  self.assertEqual(e.cp['best'],new)
 def test_revision_uses_actionable_gaps_not_repeated_success_receipts(self):
  from unittest.mock import patch
  tid,e=self.make([self.candidate(),self.audit(),self.audit(True)],'high')
  with patch.object(e,'_generate',wraps=e._generate) as generate:
   # Stop immediately before the revision provider call.
   original=e._messages
   def messages(role,**kwargs):
    if role=='revise':raise KeyboardInterrupt()
    return original(role,**kwargs)
   with patch.object(e,'_messages',messages):e.run(tid)
  e.cp=self.store.task(tid)['checkpoint']
  payload=json.loads(e._messages('revise')[-1]['content'])
  self.assertNotIn('requirement_checks',payload['findings'])
  self.assertTrue(payload['findings']['coverage_gaps'])
  self.assertIn('Method A stops after three',payload['previous_candidate']['answer'])

 def test_item_coverage_progress_changes_rank_inside_partial_criterion(self):
  from deeper_research.contracts import review_rank
  first=self.audit();first['criteria'][0]['status']='partial'
  second=deepcopy(first);second['requirement_checks'][1]['status']='met'
  self.assertGreater(review_rank(second),review_rank(first))
 def test_independent_selection_does_not_trade_partial_for_unverified(self):
  import time
  tid,e=self.make([],'high');e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.started=time.monotonic();e.prior_seconds=0
  old=self.store.save_artifact(tid,'candidate',self.candidate());new=self.store.save_artifact(tid,'candidate',self.candidate(True))
  before=self.audit();before['criteria'][0]['status']='partial';before['claim_checks'][0]['status']='partial'
  rp=self.store.save_artifact(tid,'verification',before)
  after=self.audit(complete=True);after['action']='revise';after['claim_checks'][0]['status']='unverified';after['comparison_mode']='independent_coverage'
  np=self.store.save_artifact(tid,'verification',after)
  e.cp={'target':new,'initial':old,'best':old,'best_review':rp,'stage':'precheck','directions':1}
  e._apply_review(after,np)
  self.assertEqual(e.cp['best'],old)
 def test_program_owned_plan_anchor_does_not_depend_on_model_recopying_task(self):
  from deeper_research.acceptance import validate_plan
  raw={'criteria':[{'id':'c1','requirements':[{'kind':'fact','text':'Give method B stop rule','request_quote':'a paraphrased request'}]}]}
  plan=validate_plan(raw,['Give a real stopping rule for each method'],'Give method A and method B stopping rules',assign_anchors=True)
  self.assertEqual(plan['criteria'][0]['requirements'][0]['request_quotes'],['Give a real stopping rule for each method'])
  self.assertEqual(plan['criteria'][0]['requirements'][0]['anchor_owner'],'program')

 def test_complex_high_allocates_context_inside_unchanged_task_token_ceiling(self):
  tid,e=self.make([],'high');e.task_id=tid;e.spec=self.store.task(tid)['spec'];e.cp={}
  plan=deepcopy(PLAN);base=plan['criteria'][0]['requirements'][0]
  plan['criteria'][0]['requirements']=[dict(base,id=f'c1.r{i+1}') for i in range(20)]
  e.cp['requirements']=self.store.save_artifact(tid,'requirements',plan)
  self.assertGreater(e._input_limit(),12000)
  self.assertLessEqual(e._input_limit(),20000)
  self.assertEqual(e.spec['limits']['tokens'],160000)
  self.assertEqual(e.spec['limits']['calls'],10)
 def test_partial_delivery_label_routes_gap_without_positive_invention(self):
  from deeper_research.acceptance import audit_coverage
  report=self.audit(complete=True);report['requirement_checks'][1].update(answer_mode='partial',status='met')
  value=audit_coverage(report,PLAN,self.candidate(True),self.corpus.search('stops',[self.source]),
   ['Give a real stopping rule for each method'],'Give method A and method B stopping rules')
  self.assertNotEqual(value['criteria'][0]['status'],'met')
  self.assertEqual(value['requirement_checks'][1]['status'],'partial')
  self.assertNotEqual(value['action'],'accept')

 def test_inferred_fact_does_not_pass_as_explicit_paper_fact(self):
  from deeper_research.acceptance import audit_coverage
  report=self.audit(complete=True);report['requirement_checks'][1].update(answer_mode='inference',status='met')
  value=audit_coverage(report,PLAN,self.candidate(True),self.corpus.search('stops',[self.source]),
   ['Give a real stopping rule for each method'],'Give method A and method B stopping rules')
  self.assertNotEqual(value['criteria'][0]['status'],'met')
  self.assertEqual(value['requirement_checks'][1]['status'],'partial')
