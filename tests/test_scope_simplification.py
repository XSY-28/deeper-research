"""Offline regressions for lost context and redundant wire work."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from deeper_research.corpus import Corpus
from deeper_research.contracts import estimate_tokens,validate_candidate
from deeper_research.semantics import audit_groups
from deeper_research.protocol import expand_audit
from deeper_research.engine import Engine
from deeper_research.storage import Store
import test_named_protocol as named_fixture
from test_engine import RemoteBoundary,CorpusBoundary,candidate,review
from test_corpus import make_pdf
from coverage_fixtures import fixture_plan


class ScopeSimplificationTests(unittest.TestCase):
    def test_exact_complete_claim_and_body_are_requested_once(self):
        text='Not all methods failed; only A was tested, whereas B was not.'
        ca=validate_candidate({'answer':text+' [b1]',
            'claims':[{'text':text,'evidence_ids':['b1']}],'open_issues':[]},{'b1'})
        groups=audit_groups(ca)
        self.assertEqual(len(groups),1)
        self.assertIn(text,groups[0]['text'])
        self.assertIn('[b1]',groups[0]['text'])
        self.assertEqual(groups[0]['claim_id'],'claim-1')

    def test_paraphrases_substrings_and_different_polarity_are_not_deduplicated(self):
        for text in ('A was tested.','Not all methods failed.','Every method failed.'):
            ca=validate_candidate({'answer':'Not all methods failed; A was tested, B was not. [b1]',
                'claims':[{'text':text,'evidence_ids':['b1']}],'open_issues':[]},{'b1'})
            self.assertEqual(len(audit_groups(ca)),2)

    def test_long_reason_survives_in_full_without_a_format_retry(self):
        fixture=named_fixture.NamedAuditTests();fixture.setUp();raw=deepcopy(fixture.reply)
        raw['alignment']={u['id']:deepcopy(next(iter(raw['alignment'].values()))) for u in audit_groups(fixture.ca)}
        reason='Not all methods failed; only A was tested, whereas B was not. '*9
        for row in raw['alignment'].values():row['reason']=reason
        try:result=expand_audit(raw,fixture.plan,fixture.ca,[],fixture.evidence)
        except ValueError as ex:self.fail('A complete reason must remain usable: '+str(ex))
        self.assertTrue(all(c['reason']==reason.strip() for c in result['semantic_checks']))

    def test_lookup_evidence_used_by_verified_findings_is_pinned_for_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp));remote=RemoteBoundary([])
            tid=store.create_task({'question':'Question','criteria':['Use evidence'],'source_ids':['s1'],
                'effort':'high','profile':remote.profile.public_dict(),'requirements':fixture_plan(['Use evidence'])})
            ca=store.save_artifact(tid,'candidate',validate_candidate(candidate('Initial'),{'b1'}))
            initial=[{'id':'b1','text':'Initial evidence.','source_id':'s1','location':{},'title':'Source'}]
            extra={'id':'b2','text':'Only Group A was tested; Group B was not.','source_id':'s1','location':{},'title':'Source'}
            solve=store.reserve_call(tid,'solve',Engine._render_messages('Answer',{'evidence':initial}),10,10,remote.profile.input_usd_per_million,remote.profile.output_usd_per_million)
            verify=store.reserve_call(tid,'verify',Engine._render_messages('Audit',{'evidence':initial+[extra]}),10,10,remote.profile.input_usd_per_million,remote.profile.output_usd_per_million)
            findings=review();findings['issues']=[{'severity':'blocking','reason':'Apply the restricted evidence.','evidence_ids':['b2']}]
            path=store.save_artifact(tid,'verification',findings)
            engine=Engine(store,CorpusBoundary(),None);engine.task_id=tid;engine.spec=store.task(tid)['spec']
            engine.cp={'target':ca,'best':ca,'packet':store.save_artifact(tid,'packet',initial),
                'requirements':store.save_artifact(tid,'requirements',store.task(tid)['spec']['requirements']),
                'candidate_meta':{ca:{'source_call':solve}},'last_review':path,
                'review_verifications':[{'verification':path,'source_call':verify}]}
            for role in ('revise','alternative'):
                with self.subTest(role=role):
                    payload=json.loads(engine._messages(role)[-1]['content'])
                    self.assertTrue(any(x.get('canonical_id')=='b2' and x['text']==extra['text'] for x in payload['evidence']))


class SourceBoundaryTests(unittest.TestCase):
    def search(self,text,query,max_tokens=1600):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.pdf';make_pdf(source,[text])
            corpus=Corpus(root/'workspace')
            sid=corpus.ingest(source)
            text=corpus.get_block(sid+':b1',[sid])['text']
            packet=corpus.search(query,[sid],max_tokens=max_tokens)
            self.assertLessEqual(estimate_tokens(packet),max_tokens)
            for b in packet:self.assertEqual(b['text'],text[b.get('text_start',0):b.get('text_end',len(text))])
            return packet

    def test_whole_structural_group_retains_title_and_excludes_next_group(self):
        scope='Group Delta: Restricted comparison\nOnly the pilot group was evaluated.\nQuestion: Which group?\nAnswer: Delta\n'
        following='Group Omega: Other comparison\nDifferent subjects were evaluated.\n'
        text='Background. '*170+'\n\n'+scope+'\n'+following+'\n'+'Background. '*170
        packet=self.search(text,'Delta pilot group')
        hit=next(b for b in packet if 'Answer: Delta' in b['text'])
        self.assertIn(scope.strip(),hit['text'])
        self.assertNotIn('Group Omega',hit['text'])

    def test_oversized_unbroken_proposition_is_not_returned_as_a_subjectless_tail(self):
        text='Only under the excluded condition '+('without new evidence '*250)+'may this rarekeyword hold.'
        self.assertEqual(self.search(text,'rarekeyword',max_tokens=350),[])

    def test_complete_negative_sentence_is_never_cut_before_its_qualifier(self):
        text='Background detail. '*200+'\n\nNot all groups failed; only Alpha was evaluated, whereas Beta was not.\n\n'+'Other detail. '*200
        packet=self.search(text,'Alpha Beta',max_tokens=500)
        self.assertTrue(any('Not all groups failed; only Alpha was evaluated, whereas Beta was not.' in b['text'] for b in packet))

    def test_real_text_ingestion_keeps_distant_paragraph_range_or_returns_no_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'scope.txt'
            source.write_text('These results apply only to simulated environments. '+
                'Background context. '*200+'The system achieved raremetric accuracy on all evaluated tasks.\n')
            corpus=Corpus(root/'workspace');sid=corpus.ingest(source)
            packet=corpus.search('raremetric accuracy',[sid],max_tokens=500)
            self.assertEqual(packet,[])

    def test_auxiliary_pdf_layout_failure_does_not_abort_other_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'imperfect.pdf'
            stream=('BT (preface) Tj /F1 12 Tf 50 700 Td ('+'raremetric context '*250+') Tj ET').encode('ascii')
            make_pdf(source,[stream]);good=root/'good.txt';good.write_text('Only the pilot has raremetric evidence.')
            corpus=Corpus(root/'workspace');bad_id=corpus.ingest(source);good_id=corpus.ingest(good)
            # Default extraction/import succeeds; optional layout extraction
            # rejects text shown before Tf. It must not abort scoped retrieval.
            packet=corpus.search('raremetric',[bad_id,good_id],max_tokens=500)
            self.assertEqual([b['source_id'] for b in packet],[good_id])


if __name__=='__main__':unittest.main()
