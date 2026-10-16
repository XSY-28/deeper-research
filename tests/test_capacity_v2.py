"""Actual format boundaries preserve task/evidence rather than weaken truth."""
import unittest
from copy import deepcopy
from deeper_research.acceptance import validate_plan,audit_coverage

class PlanEnvelopeTests(unittest.TestCase):
 def test_echo_envelope_can_contain_a_valid_actual_plan(self):
  schema={'criteria':[{'id':'c1','requirements':[{'kind':'fact','text':'State the mechanism.'}]}]}
  reply={'question':'What mechanism?','acceptance_criteria':[{'id':'c1','text':'State mechanism'}],
   'evidence':[],'response_schema':schema}
  value=validate_plan(reply,['State mechanism'],'What mechanism?',assign_anchors=True)
  self.assertEqual(value['criteria'][0]['requirements'][0]['text'],'State the mechanism.')
  self.assertTrue(value['plan_format_corrections'])
 def test_conflicting_wrapper_task_or_direct_plan_is_rejected(self):
  nested={'criteria':[{'id':'c1','requirements':[{'kind':'fact','text':'State mechanism.'}]}]}
  for extra in ({'question':'Different task'}, {'criteria':[]}, {'acceptance_criteria':[{'id':'c1','requirements':[]}]} ):
   with self.subTest(extra=extra),self.assertRaises(ValueError):
    validate_plan({'response_schema':nested,**extra},['State mechanism'],'What mechanism?',assign_anchors=True)
 def test_echoed_criterion_text_cannot_replace_frozen_criterion(self):
  reply={'response_schema':{'criteria':[{'id':'c1','requirements':[{'kind':'fact','text':'Mechanism.'}]}]},
   'acceptance_criteria':[{'id':'c1','text':'A paraphrased echo'}]}
  value=validate_plan(reply,['Original frozen requirement'],'Question',assign_anchors=True)
  self.assertEqual(value['criteria'][0]['requirements'][0]['request_quote'],'Original frozen requirement')
 def test_wrapping_a_template_does_not_bypass_plan_validation(self):
  with self.assertRaises(ValueError):validate_plan({'response_schema':{'criteria':[{'id':'c1','requirements':[]}]}},['State mechanism'],'Question',assign_anchors=True)

class ReceiptSizeTests(unittest.TestCase):
 def packet(self,source_text):
  plan={'criteria':[{'id':'c1','requirements':[{'id':'c1.r1','text':'Result','kind':'fact'}]}]}
  candidate={'answer':'Result','claims':[]}
  report={'action':'accept','criteria':[{'id':'c1','status':'met'}],
   'requirement_checks':[{'id':'c1.r1','status':'met','answer_mode':'result','answer_quote':'Result',
     'source_quote':'A'*491,'evidence_ids':['b1'],'gap_type':'none','query':''}]}
  return report,plan,candidate,[{'id':'b1','text':source_text}]
 def test_literal_491_character_basis_is_checked_not_rejected_as_format(self):
  r,p,c,e=self.packet('A'*491)
  value=audit_coverage(r,p,c,e,['Result'],'Result')
  self.assertEqual(value['requirement_checks'][0]['status'],'met')
 def test_long_nonexistent_basis_still_cannot_meet_requirement(self):
  r,p,c,e=self.packet('Different original source.')
  value=audit_coverage(r,p,c,e,['Result'],'Result')
  self.assertNotEqual(value['requirement_checks'][0]['status'],'met')
  self.assertEqual(value['action'],'retrieve')

class RepairHeadroomTests(unittest.TestCase):
 def test_optional_evidence_leaves_input_room_for_a_bounded_correction(self):
  import tempfile
  from pathlib import Path
  from deeper_research.storage import Store
  from deeper_research.engine import Engine
  from deeper_research.contracts import estimate_tokens
  from test_engine import RemoteBoundary,CorpusBoundary,candidate
  class LargeCorpus(CorpusBoundary):
   def search(self,query,source_ids,limit=8,max_tokens=3000):
    return [{'id':f'b{i}','source_id':'s1','text':'source evidence '+'x'*3000,
      'title':'Source','location':{'line_start':i,'line_end':i}} for i in range(1,9)]
  with tempfile.TemporaryDirectory() as tmp:
   store=Store(Path(tmp));remote=RemoteBoundary([candidate('tiny')])
   tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],
    'effort':'low','profile':remote.profile.public_dict()})
   Engine(store,LargeCorpus(),remote).run(tid)
   call=store.calls(tid)[0]
   self.assertLessEqual(estimate_tokens(call['request']),12000-1024)

class NestedQuestionTests(unittest.TestCase):
 def test_changed_question_inside_nested_actual_plan_is_not_ignored(self):
  reply={'question':'Question','response_schema':{'question':'Different task','criteria':[
   {'id':'c1','requirements':[{'kind':'fact','text':'Mechanism.'}]}]}}
  with self.assertRaises(ValueError):validate_plan(reply,['Original requirement'],'Question',assign_anchors=True)
