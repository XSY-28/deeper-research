"""Conservative feedback routing and program-owned comparison witnesses."""
from copy import deepcopy
import unittest
from deeper_research.comparison import validate_comparison

BODY={'candidates':[{'label':'A','candidate':{'answer':'Alpha preserves the original factual conclusion.\nSecond explanatory paragraph.'}},
                    {'label':'B','candidate':{'answer':'Beta changes the factual conclusion.'}}]}

def response():
 return {'verdict':'A','reason':'More precise reasoning.','advantages':[{'candidate':'A','dimension':'precision',
  'answer_quote':'Alpha preserves the original factual conclusion.','reason':'Preserves the fact.'}],
  'improvement':'','concerns':[]}

class ComparisonV2Tests(unittest.TestCase):
 def test_negative_feedback_is_not_lost_to_invalid_positive_reasons(self):
  v=response();v['advantages'][0].update(answer_quote='A paraphrase not actually in answer',dimension='invalid')
  v['concerns']=[{'candidate':'B','kind':'evidence','answer_quote':'A paraphrase of the disputed assertion',
   'reason':'The source may contradict this assertion.','query':'source result'}]
  result=validate_comparison(v,deepcopy(BODY))
  self.assertEqual(result['verdict'],'needs_check');self.assertEqual(result['advantages'],[])
  self.assertFalse(result['concerns'][0]['quote_located'])
  self.assertEqual(result['concerns'][0]['scope'],'whole_answer')
 def test_valid_concern_forces_recheck_instead_of_accepting_winner(self):
  v=response();v['concerns']=[{'candidate':'B','kind':'answer','answer_quote':'Beta changes the factual conclusion.',
   'reason':'Potentially wrong conclusion.','query':''}]
  result=validate_comparison(v,deepcopy(BODY))
  self.assertEqual(result['verdict'],'needs_check');self.assertTrue(result['concerns'][0]['quote_located'])
 def test_program_span_binds_actual_text_without_model_recopy(self):
  v=response();v['advantages'][0].pop('answer_quote');v['advantages'][0]['span_id']='A.p2'
  result=validate_comparison(v,deepcopy(BODY))
  self.assertEqual(result['advantages'][0]['answer_quote'],'Second explanatory paragraph.')
  self.assertEqual(result['advantages'][0]['anchor_kind'],'program_span')
 def test_unknown_positive_span_cannot_manufacture_a_winner(self):
  v=response();v['advantages'][0].pop('answer_quote');v['advantages'][0]['span_id']='B.p99'
  with self.assertRaises(ValueError):validate_comparison(v,deepcopy(BODY))
 def test_unlocated_positive_quote_still_fails_closed(self):
  v=response();v['advantages'][0]['answer_quote']='plausible but absent'
  with self.assertRaises(ValueError):validate_comparison(v,deepcopy(BODY))
 def test_unknown_negative_target_and_kind_still_reject(self):
  for label,kind in (('C','evidence'),('B','unknown')):
   v=response();v['concerns']=[{'candidate':label,'kind':kind,'answer_quote':'not copied',
    'reason':'Check this','query':'source'}]
   with self.subTest(label=label,kind=kind),self.assertRaises(ValueError):validate_comparison(v,deepcopy(BODY))

class AdvisoryLengthTests(unittest.TestCase):
 def test_overlong_advisory_text_does_not_block_valid_negative_feedback(self):
  v=response();v['reason']='Long advisory explanation. '*40
  v['concerns']=[{'candidate':'B','kind':'evidence','answer_quote':'paraphrased negative',
    'reason':'A possible source contradiction.','query':'actual subject'}]
  result=validate_comparison(v,deepcopy(BODY))
  self.assertEqual(result['verdict'],'needs_check');self.assertLessEqual(len(result['reason']),600)
  self.assertTrue(result['contract_corrections'])

class ConcernAliasTests(unittest.TestCase):
 def test_coverage_concern_is_conservatively_an_answer_recheck(self):
  v=response();v['concerns']=[{'candidate':'B','kind':'coverage','answer_quote':'paraphrase',
   'reason':'Some requested result is absent.','query':'missing result'}]
  result=validate_comparison(v,deepcopy(BODY))
  self.assertEqual(result['verdict'],'needs_check');self.assertEqual(result['concerns'][0]['kind'],'answer')

class RepairContextTests(unittest.TestCase):
 def test_large_invalid_response_does_not_displace_same_task_and_evidence(self):
  import tempfile,json
  from pathlib import Path
  from deeper_research.storage import Store
  from deeper_research.engine import Engine
  from test_engine import RemoteBoundary,CorpusBoundary,candidate,review,verification
  from coverage_fixtures import fixture_plan
  with tempfile.TemporaryDirectory() as tmp:
   store=Store(Path(tmp));bad=review(True);bad.pop('claim_checks');bad['untrusted_padding']='z'*30000
   remote=RemoteBoundary([candidate('saved'),bad,review(True),verification(True)])
   tid=store.create_task({'question':'Compare evidence','criteria':['Use evidence'],'source_ids':['s1'],
    'effort':'medium','profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
   result=Engine(store,CorpusBoundary(),remote).run(tid)
   self.assertEqual(result['quality'],'criteria_met')
   calls=store.calls(tid);before=json.loads(calls[1]['request'][-1]['content']);after=json.loads(calls[2]['request'][-1]['content'])
   self.assertEqual(before['evidence'],after['evidence'])
   self.assertEqual(before['candidate'],after['candidate'])
   self.assertNotIn('z'*100,calls[2]['request'][-1]['content'])
   self.assertIn('z'*100,calls[1]['response']['content'])

class WholeAnswerAdviceTests(unittest.TestCase):
 def test_unlocated_advice_metadata_reaches_the_independent_verifier(self):
  import tempfile
  from pathlib import Path
  from deeper_research.storage import Store
  from deeper_research.multicandidate import MultiCandidate
  from test_engine import CorpusBoundary,candidate,review,verification
  from test_multicandidate import ScriptedRemote,prefer_word
  from coverage_fixtures import fixture_plan
  def concern(body):
   side=next(x for x in body['candidates'] if 'best' in x['candidate']['answer'])
   return {'verdict':side['label'],'reason':'Recheck before preferring it.','advantages':[], 'improvement':'',
    'concerns':[{'candidate':side['label'],'kind':'evidence','answer_quote':'paraphrased question, not actual quote',
    'reason':'A possible unsupported claim.','query':'source condition'}]}
  def verify(body):
   issue=body['feedback_to_check'][0]
   self.assertFalse(issue['quote_located']);self.assertEqual(issue['scope'],'whole_answer')
   self.assertEqual(issue['anchor_kind'],'untrusted_advice')
   v=verification(True);v['issue_checks']=[{'id':'issue-1','status':'rejected','reason':'Source supports it.','evidence_ids':['b1']}]
   return v
  replies=[]
  for name in ('best','other','third'):replies += [candidate(name),review(True),verification(True)]
  replies += [concern,verify,prefer_word('best'),prefer_word('best')]
  with tempfile.TemporaryDirectory() as tmp:
   s=Store(Path(tmp));p=ScriptedRemote(replies)
   tid=s.create_task({'mode':'multi','question':'Compare evidence','criteria':['Use evidence'],'source_ids':['s1'],
    'effort':'high','profile':p.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
   result=MultiCandidate(s,CorpusBoundary(),p).run(tid)
   self.assertEqual(result['quality'],'criteria_met')

class NewProtocolWitnessTests(unittest.TestCase):
 def test_new_request_requires_program_span_for_positive_winner(self):
  from deeper_research.comparison import answer_spans
  body=deepcopy(BODY)
  for side in body['candidates']:side['candidate']['answer_spans']=answer_spans(side['candidate']['answer'],side['label'])
  with self.assertRaises(ValueError):validate_comparison(response(),body)
