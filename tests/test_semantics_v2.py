"""Entailment receipts cover each declared clause AND cited answer span."""
import unittest
from copy import deepcopy

class SemanticAuditTests(unittest.TestCase):
 def setUp(self):
  self.candidate={'answer':'Method A cannot access intermediate feedback [b1].',
   'claims':[{'id':'claim-1','text':'Method A cannot access feedback; Method A does not update weights.','evidence_ids':['b1']}]}
  self.evidence=[{'id':'b1','text':'Method B cannot access intermediate feedback. Method A can access feedback. Method A does not update weights.'}]
  self.report={'action':'accept','criteria':[{'id':'c1','status':'met'}],
   'claim_checks':[{'id':'claim-1','status':'supported','evidence_ids':['b1'],'reason':'One quote found.'}],
   'issues':[],'query':''}
 def checks(self):
  from deeper_research.semantics import semantic_units
  return [{'id':u['id'],'relation':'entailed','source_subject':'Method A','quote':'Method A does not update weights.',
     'evidence_ids':['b1'],'reason':'Same subject and proposition.','query':''} for u in semantic_units(self.candidate)]
 def test_quote_for_good_clause_cannot_omit_the_other_clause(self):
  from deeper_research.semantics import audit_semantics
  r=deepcopy(self.report);r['semantic_checks']=[self.checks()[1]]
  with self.assertRaises(ValueError):audit_semantics(r,self.candidate,self.evidence)
 def test_wrong_subject_clause_downgrades_aggregate_supported_claim(self):
  from deeper_research.semantics import audit_semantics
  r=deepcopy(self.report);r['semantic_checks']=self.checks()
  for c in r['semantic_checks']:
   if c['id']!='claim-1.p2':c.update(relation='different_subject',source_subject='Method B',
    quote='Method B cannot access intermediate feedback.',reason='Cannot access applies to B, not A.')
  result=audit_semantics(r,self.candidate,self.evidence)
  self.assertEqual(result['action'],'revise');self.assertEqual(result['claim_checks'][0]['status'],'unverified')
  self.assertTrue(any(x['severity']=='blocking' for x in result['issues']))
 def test_correct_negative_fact_is_not_rejected_merely_for_negation(self):
  from deeper_research.semantics import audit_semantics
  ca={'answer':'Method A does not update weights [b1].','claims':[{'id':'claim-1','text':'Method A does not update weights.','evidence_ids':['b1']}]}
  from deeper_research.semantics import semantic_units
  r=deepcopy(self.report);r['semantic_checks']=[{'id':u['id'],'relation':'entailed','source_subject':'Method A',
   'quote':'Method A does not update weights.','evidence_ids':['b1'],'reason':'Negative fact is supported.','query':''} for u in semantic_units(ca)]
  result=audit_semantics(r,ca,self.evidence)
  self.assertEqual(result['action'],'accept');self.assertEqual(result['claim_checks'][0]['status'],'supported')
 def test_real_source_quote_must_include_declared_subject(self):
  from deeper_research.semantics import audit_semantics
  r=deepcopy(self.report);r['semantic_checks']=self.checks();r['semantic_checks'][0]['source_subject']='Unknown Method'
  result=audit_semantics(r,self.candidate,self.evidence)
  self.assertNotEqual(result['action'],'accept')
 def test_cited_body_assertion_is_checked_even_if_structured_claim_is_true(self):
  from deeper_research.semantics import semantic_units
  ca=deepcopy(self.candidate);ca['claims'][0]['text']='Method A does not update weights.'
  units=semantic_units(ca)
  self.assertTrue(any(u['claim_id'] is None and 'cannot access' in u['text'] for u in units))

class EngineSemanticTests(unittest.TestCase):
 def test_supported_claim_verdict_cannot_override_bad_clause_receipt(self):
  import json,tempfile
  from pathlib import Path
  from deeper_research.storage import Store
  from deeper_research.engine import Engine
  from test_engine import CorpusBoundary,candidate,review,verification
  from test_multicandidate import ScriptedRemote
  from coverage_fixtures import fixture_plan
  def wrong(body):
   self.assertIn('semantic_units',body)
   r=verification(True);block=body['evidence'][0]
   r['semantic_checks']=[{'id':u['id'],'relation':'contradicted','source_subject':'Evidence',
     'quote':block['text'],'evidence_ids':[block['id']], 'reason':'The source says the opposite.','query':''}
      for u in body['semantic_units']]
   return r
  with tempfile.TemporaryDirectory() as tmp:
   store=Store(Path(tmp));remote=ScriptedRemote([candidate('original'),review(True),wrong])
   tid=store.create_task({'question':'Compare evidence','criteria':['Use evidence'],'source_ids':['s1'],
    'effort':'medium','profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
   result=Engine(store,CorpusBoundary(),remote).run(tid)
   self.assertNotEqual(result['quality'],'criteria_met')
   checked=store.read_artifact(result['checkpoint']['best_review'])
   self.assertEqual(checked['claim_checks'][0]['status'],'contradicted')
   exported=json.loads((store.root/f'tasks/{tid}/exports/run-summary.json').read_text())
   self.assertTrue(any('Semantic unit' in x for x in exported['open_issues']))

class UnsupportedNegativeTests(unittest.TestCase):
 setUp=SemanticAuditTests.setUp
 checks=SemanticAuditTests.checks
 def test_unlocated_negative_receipt_cannot_confirm_a_contradiction(self):
  from deeper_research.semantics import audit_semantics
  r=deepcopy(self.report);r['semantic_checks']=self.checks()
  r['semantic_checks'][0].update(relation='contradicted',quote='Absent source text',source_subject='Absent')
  result=audit_semantics(r,self.candidate,self.evidence)
  self.assertEqual(result['claim_checks'][0]['status'],'unverified')

class UncitedTailTests(unittest.TestCase):
 def test_uncited_tail_cannot_hide_behind_true_cited_claim(self):
  from deeper_research.semantics import semantic_units,audit_semantics
  ca={'answer':'Method A does not update weights [b1]. Method A updates weights.',
      'claims':[{'id':'claim-1','text':'Method A does not update weights.','evidence_ids':['b1']}]}
  units=semantic_units(ca)
  self.assertTrue(any('Method A updates weights' in u['text'] for u in units))
  r={'action':'accept','criteria':[{'id':'c1','status':'met'}], 'issues':[],
   'claim_checks':[{'id':'claim-1','status':'supported','evidence_ids':['b1'],'reason':'True declared claim.'}],
   'semantic_checks':[{'id':'claim-1.p1','relation':'entailed','source_subject':'Method A',
    'quote':'Method A does not update weights.','evidence_ids':['b1'],'reason':'True declared claim.','query':''}]}
  with self.assertRaises(ValueError):audit_semantics(r,ca,[{'id':'b1','text':'Method A does not update weights.'}])
 def test_uncited_answer_is_explicitly_audited_even_with_true_structured_claim(self):
  from deeper_research.semantics import semantic_units
  ca={'answer':'Method A updates weights.','claims':[{'id':'claim-1','text':'Method A does not update weights.','evidence_ids':['b1']}]}
  self.assertTrue(any(u['claim_id'] is None for u in semantic_units(ca)))
