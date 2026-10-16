"""Mechanical wire normalization must never manufacture semantic judgments."""
from copy import deepcopy
import unittest
from deeper_research.acceptance import validate_plan, audit_coverage
from deeper_research.contracts import validate_verification, review_accepted
from deeper_research.protocol import expand_audit
from deeper_research.semantics import audit_semantics
import test_named_protocol as named_fixture


class MechanicalProtocolTests(unittest.TestCase):
    setUp = named_fixture.NamedAuditTests.setUp

    def audit(self, value, issues=None):
        issues=issues or []
        result=expand_audit(value,self.plan,self.ca,issues,self.evidence)
        validate_verification(result,{b['id'] for b in self.evidence},1,['claim-1'],[i['id'] for i in issues],self.evidence)
        audit_coverage(result,self.plan,self.ca,self.evidence,['Compare A and B'],'Question')
        return audit_semantics(result,self.ca,self.evidence)

    def long_receipt(self):
        subject='A method with a precisely described subject '+('and scope '*15)
        text=subject+' can use feedback; B updates weights. '+('Full source qualification. '*22)
        self.evidence=[{'id':'b1','text':text}]
        self.ca['answer']=text+' [b1]'
        # Keep same compound claim and body group IDs; the body is still complete.
        value=deepcopy(self.reply)
        # The changed body is no longer an exact duplicate of the claim.
        value['alignment']['body.b1']=deepcopy(value['alignment']['claim-1'])
        value['basis']['Q1']={'source':'b1','quote':text,'subject':subject}
        value['coverage']['c1.r1']['answer']=text
        return value,text

    def test_full_long_exact_receipts_survive_all_audits_without_clipping(self):
        value,text=self.long_receipt()
        result=self.audit(value)
        self.assertTrue(review_accepted(result))
        self.assertEqual(result['source_quotes'][0]['quote'],text)
        self.assertEqual(result['requirement_checks'][0]['answer_quote'],text)
        self.assertEqual(result['semantic_checks'][0]['basis'][0]['quote'],text)

    def test_long_fabricated_quote_downgrades_instead_of_gaining_support(self):
        value,_=self.long_receipt();value['basis']['Q1']['quote']+=' Invented statement.'
        result=self.audit(value)
        self.assertFalse(review_accepted(result));self.assertEqual(result['action'],'retrieve')

    def test_partial_feedback_is_unresolved_and_cannot_vanish_or_accept(self):
        issues=[{'id':'issue-1','severity':'blocking','reason':'Possible harmful edit','evidence_ids':[]}]
        value=deepcopy(self.reply)
        value['feedback']={'issue-1':{'status':'partially_confirmed','reason':'Only part of the concern is established.'}}
        result=self.audit(value,issues)
        self.assertEqual(result['issue_checks'][0]['status'],'unresolved')
        self.assertFalse(review_accepted(result))

    def test_echoed_metadata_never_controls_retrieval_or_adds_missing_judgments(self):
        for flag in (True,False):
            value=deepcopy(self.reply);value['retrieval_available']=flag
            self.assertTrue(review_accepted(self.audit(value)))
            value.pop('omissions')
            with self.assertRaises(ValueError):self.audit(value)
        value=deepcopy(self.reply);value['action']='accept'
        with self.assertRaises(ValueError):self.audit(value)

    def test_more_than_eight_requested_results_are_all_frozen_and_required(self):
        items=[['fact',f'Requested result {i}'] for i in range(1,10)]
        result=validate_plan({'c1':items},['Nine requested results'],'Question',assign_anchors=True)
        self.assertEqual([r['id'] for r in result['criteria'][0]['requirements']], [f'c1.r{i}' for i in range(1,10)])
        self.assertEqual([r['text'] for r in result['criteria'][0]['requirements']],[f'Requested result {i}' for i in range(1,10)])
        value=deepcopy(self.reply)
        with self.assertRaises(ValueError):expand_audit(value,result,self.ca,[],self.evidence)
        with self.assertRaises(ValueError):validate_plan({'c1':[]},['Nine requested results'],'Question',assign_anchors=True)

    def test_unknown_quotes_and_missing_feedback_remain_errors(self):
        value=deepcopy(self.reply);value['alignment']['claim-1']['basis']=['Q99']
        with self.assertRaises(ValueError):self.audit(value)
        value=deepcopy(self.reply)
        with self.assertRaises(ValueError):self.audit(value,[{'id':'issue-1','reason':'Concern','evidence_ids':[]}])
