"""Structural scope boundaries must not turn design labels into fact support."""
import unittest
from copy import deepcopy
from deeper_research.semantics import audit_groups, audit_semantics
from deeper_research.protocol import audit_request, expand_audit
from deeper_research.acceptance import validate_plan


class StructuralScopeTests(unittest.TestCase):
    def candidate(self, answer, claim='Method A has been evaluated.'):
        return {'answer':answer,'claims':[{'id':'claim-1','text':claim,'evidence_ids':['b1']}]}

    def test_design_and_negative_paper_fact_are_separate_even_with_shared_citation(self):
        design='同任务、同模型、同资料，预算耗尽就停止。'
        fact='本地收益没有测量；论文评估了其他系统，并未评估该系统本身 [b1]。'
        answer='**四、我的设计**\n'+design+'\n\n**五、边界**\n'+fact
        units=audit_groups(self.candidate(answer))
        fact_units=[u for u in units if '并未评估' in u['text']]
        self.assertEqual(len(fact_units),1)
        self.assertIn(fact,fact_units[0]['text'])
        self.assertFalse(any(design in u['text'] and fact in u['text'] for u in units))

    def test_negative_leadin_cannot_cross_into_a_new_design_heading(self):
        answer='The paper did NOT test the following:\n\n## My design\nRetain every checked answer [b1].'
        units=audit_groups(self.candidate(answer))
        self.assertFalse(any('did NOT test' in u['text'] and 'Retain every' in u['text'] for u in units))
        self.assertTrue(any('did NOT test the following:' in u['text'] for u in units))

    def test_bold_negative_heading_cannot_cross_a_new_design_heading(self):
        answer='**The paper did NOT test:**\n\n## My design\nRetain every checked answer [b1].'
        units=audit_groups(self.candidate(answer))
        self.assertFalse(any('did NOT test' in u['text'] and 'Retain every' in u['text'] for u in units))

    def test_compact_list_items_keep_facts_separate_from_design(self):
        answer='- My design: retain old answers.\n- Paper scope: no controlled study was conducted [b1].'
        units=audit_groups(self.candidate(answer))
        self.assertFalse(any('My design' in u['text'] and 'no controlled study' in u['text'] for u in units))
        self.assertTrue(any('no controlled study was conducted [b1].' in u['text'] for u in units))

    def test_table_rows_are_independent_with_their_scope_header(self):
        answer='| Scope (NOT a local experiment) | Statement |\n| --- | --- |\n| My design | Retain old answers |\n| Paper fact | No controlled comparison [b1] |'
        units=audit_groups(self.candidate(answer))
        body=[u for u in units if u['claim_id'] is None]
        self.assertEqual(len(body),2)
        self.assertTrue(all('NOT a local experiment' in u['text'] for u in body))
        self.assertFalse(any('Retain old answers' in u['text'] and 'No controlled comparison' in u['text'] for u in body))

    def test_single_column_table_cannot_merge_design_and_paper_fact(self):
        text='| Statement |\n| --- |\n| My proposal: retain old answers. |\n| Paper fact: no system was evaluated [b1]. |'
        units=audit_groups(self.candidate(text))
        body=[u for u in units if u['claim_id'] is None]
        self.assertEqual(len(body),2)
        self.assertFalse(any('My proposal' in u['text'] and 'no system was evaluated' in u['text'] for u in body))

    def test_nested_list_keeps_parent_negation_and_all_children(self):
        text='- Not all claims below are established:\n\n  - Method A ran.\n  - Method B did not run.\n- My proposal: keep old answers.'
        units=audit_groups(self.candidate(text))
        self.assertTrue(any('Not all claims' in u['text'] and 'Method A ran' in u['text'] and 'Method B did not run' in u['text'] for u in units))
        self.assertFalse(any('Method B did not run' in u['text'] and 'My proposal' in u['text'] for u in units))

    def test_heading_boundary_inside_one_paragraph_is_respected(self):
        answer='## Proposal\nStore each checked answer.\n## Paper scope\nThe paper did not test every system [b1].'
        units=audit_groups(self.candidate(answer))
        self.assertFalse(any('Store each' in u['text'] and 'did not test' in u['text'] for u in units))
        self.assertTrue(any('The paper did not test every system [b1].' in u['text'] for u in units))

    def test_subject_negation_quantifiers_and_semicolon_scope_stay_whole(self):
        text='Not all methods failed; only Method A was evaluated, whereas Method B was not [b1].'
        units=audit_groups(self.candidate(text,claim='Method A was evaluated.'))
        self.assertTrue(any(u['claim_id'] is None and text in u['text'] for u in units))
        self.assertFalse(any(u['text'].strip()=='Method B was not' for u in units))

    def test_claim_paragraphs_are_not_a_single_design_and_fact_judgment(self):
        claim='My proposal: retain the old answer.\n\nThe paper never evaluated the system.'
        units=audit_groups(self.candidate('Answer [b1].',claim))
        local=[u for u in units if u['claim_id']=='claim-1']
        self.assertEqual(len(local),2)
        self.assertFalse(any('My proposal' in u['text'] and 'never evaluated' in u['text'] for u in local))

    def test_citation_adjacency_never_hides_uncited_tail_or_joins_new_paragraphs(self):
        a='Method A was evaluated [b1].';b='No experiment tested Method B.'
        units=audit_groups(self.candidate(a+'\n\n'+b))
        self.assertTrue(any(u['claim_id'] is None and b in u['text'] for u in units))
        self.assertFalse(any(a in u['text'] and b in u['text'] for u in units))

    def test_fenced_counterexample_and_its_negation_are_not_cut_at_blank_lines(self):
        text='```text\nNot all statements below are facts.\n\n## Still quoted data\nMethod B was not tested.\n```'
        units=audit_groups(self.candidate(text))
        self.assertTrue(any(text in u['text'] for u in units))

    def test_full_answer_keeps_antecedents_without_new_judgment_fields(self):
        answer='Earlier studies are X and Y.\n\nNone of these studies evaluates Method A [b1].'
        plan=validate_plan({'c1':[['fact','State study scope']]},['State study scope'],'Question',assign_anchors=True)
        body,_=audit_request(plan,self.candidate(answer),[],True)
        self.assertEqual(body['candidate']['answer'],answer)
        self.assertEqual(set(body['response_schema']),{'basis','coverage','alignment','feedback','omissions'})
        self.assertTrue(any('None of these studies evaluates Method A' in u['text'] for u in body['semantic_units']))


class ScopeResponseBindingTests(unittest.TestCase):
    def test_source_aliases_cannot_rewrite_program_scope_identifiers(self):
        import json
        from deeper_research.engine import Engine
        payload={'evidence':[{'id':'b1','text':'Evidence.'}],
                 'semantic_units':[{'id':'body.b1','claim_id':None,'text':'Statement [b1].'}]}
        body=json.loads(Engine._render_messages('Instruction',payload)[-1]['content'])
        self.assertEqual(body['semantic_units'][0]['id'],'body.b1')
        self.assertEqual(body['semantic_units'][0]['text'],'Statement [E1].')

    def test_new_request_cannot_accept_old_citation_group_ids_in_legacy_report(self):
        import json,tempfile
        from pathlib import Path
        from deeper_research.storage import Store
        from deeper_research.engine import Engine
        from test_engine import CorpusBoundary,RemoteBoundary,candidate,review
        from unittest.mock import patch
        from coverage_fixtures import fixture_plan
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp));remote=RemoteBoundary([candidate('Initial'),review()]);engine=Engine(store,CorpusBoundary(),remote)
            tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],'effort':'high',
                                  'profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
            with patch.object(engine,'_verify',side_effect=KeyboardInterrupt):engine.run(tid)
            call=store.calls(tid)[1];raw=json.loads(call['response']['content'])
            raw['semantic_checks']=[{'id':'claim-1.p1','relation':'entailed','source_subject':'Evidence',
                 'quote':'Evidence says original and revised have different conditions.','evidence_ids':['E1'],
                 'reason':'Old clause receipt.','query':''}]
            with self.assertRaises(ValueError):engine._response_object(json.dumps(raw),call['id'])

class ScopedVerdictTests(unittest.TestCase):
    def audit(self, fact, relation):
        from deeper_research.contracts import validate_candidate,validate_verification
        from deeper_research.acceptance import audit_coverage
        ca=validate_candidate({'answer':'## My design\nKeep the checked answer.\n\n## Paper boundary\n'+fact+' [b1]',
            'claims':[{'text':'The authors evaluated System A observationally.','evidence_ids':['b1']}],'open_issues':[]},{'b1'},1)
        text='The authors evaluated System A observationally. No controlled comparison was conducted.'
        evidence=[{'id':'b1','text':text}]
        plan=validate_plan({'c1':[['fact','State evaluation scope']]},['State evaluation scope'],'Question',assign_anchors=True)
        scopes=audit_groups(ca)
        value={'basis':{'Q1':{'source':'b1','quote':text,'subject':'The authors'}},
            'coverage':{'c1.r1':{'status':'met','answer':fact,'basis':['Q1'],'gap':''}},
            'alignment':{u['id']:{'relation':relation if fact in u['text'] else 'entailed' if u['claim_id'] else 'proposal_basis',
                'basis':['Q1'],'reason':'Source-inspected reference: distinguish no evaluation from no controlled comparison.'}
                for u in scopes},'feedback':{},'omissions':[]}
        result=expand_audit(value,plan,ca,[],evidence)
        validate_verification(result,{'b1'},1,['claim-1'],[],evidence)
        audit_coverage(result,plan,ca,evidence,['State evaluation scope'],'Question')
        return audit_semantics(result,ca,evidence)

    def test_known_false_negative_cannot_hide_under_positive_design_scope(self):
        from deeper_research.contracts import review_accepted
        result=self.audit('The authors never evaluated System A.','contradicted')
        self.assertFalse(review_accepted(result));self.assertEqual(result['action'],'revise')
        self.assertTrue(any(x.get('semantic_unit_id')=='body.b2' for x in result['issues']))

    def test_correct_negative_and_limited_scope_can_pass(self):
        from deeper_research.contracts import review_accepted
        result=self.audit('The authors evaluated System A, but did not conduct a controlled comparison.','entailed')
        self.assertTrue(review_accepted(result))
