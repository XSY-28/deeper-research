import json
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from deeper_research.engine import Engine
from deeper_research.providers import ModelResult, ProviderProfile, ProviderError
from deeper_research.storage import Store
from coverage_fixtures import fixture_plan, with_receipts


def candidate(answer):
    return {"answer": answer + " [b1]", "claims": [{"text": answer, "evidence_ids": ["b1"]}], "open_issues": []}


def review(met=False, preferred="candidate", action=None, blocking=False):
    return {"criteria": [{"id": "c1", "status": "met" if met else "partial", "reason": "Read b1", "evidence_ids": ["b1"]}],
            "claim_checks": [{"id": "claim-1", "status": "supported" if met else "unverified", "reason": "b1 supports the finding", "evidence_ids": ["b1"]}],
            "issues": [{"severity": "blocking", "reason": "Contradicts b1", "evidence_ids": ["b1"]}] if blocking else [],
            "action": action or ("accept" if met else "revise"), "query": "evidence", "strategy": "compare assumptions",
            "preferred": preferred, "comparison_reason": "The cited support resolves the missing condition"}


def verification(met=False, preferred='candidate', action=None, blocking=False):
    value = review(met, preferred, action, blocking)
    value['issue_checks'] = ([{'id':'issue-1','status':'confirmed','reason':'Still contradicted',
                              'evidence_ids':['b1']}] if blocking else [])
    value['source_quotes'] = ([{'claim_id':'claim-1','evidence_id':'b1',
                               'quote':'Evidence says original and revised have different conditions.'}] if met else [])
    return value


class CorpusBoundary:
    def search(self, query, source_ids, limit=8, max_tokens=3000):
        return [{"id": "b1", "source_id": "s1", "text": "Evidence says original and revised have different conditions.",
                 "title": "Evidence", "location": {"line_start": 1, "line_end": 1}}]
    def get_block(self, block_id, source_ids):
        if block_id != "b1" or source_ids != ["s1"]:
            raise ValueError("outside source scope")
        return self.search("", source_ids)[0]


class RemoteBoundary:
    def __init__(self, results):
        self.results = iter(results)
        self.profile = ProviderProfile.from_dict({"protocol": "openai_chat", "base_url": "https://example.com/v1",
            "model": "test", "api_key_env": "TEST_KEY", "input_usd_per_million": "0.3", "output_usd_per_million": "1.2"})
    def validate_credentials(self):
        pass
    def complete(self, messages, *, max_output_tokens, timeout):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        result=with_receipts(result,json.loads(messages[-1]['content']))
        return ModelResult(json.dumps(result), 100, 100, 200, "request", "test", {})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), Decimal("10"))
    def tearDown(self):
        self.temp.cleanup()
    def make(self, effort, results, question="Compare evidence"):
        provider = RemoteBoundary(results)
        task = self.store.create_task({"question": question, "criteria": ["Use evidence"], "source_ids": ["s1"],
                                       "effort": effort, "profile": provider.profile.public_dict(),
                                       "requirements":fixture_plan(['Use evidence'])})
        return task, Engine(self.store, CorpusBoundary(), provider)
    def test_low_exports_initial_draft_as_unchecked_after_one_call(self):
        task, engine = self.make("low", [candidate("initial")])
        result = engine.run(task)
        self.assertEqual((result["state"], result["quality"], result["reason"]), ("finished", "unchecked", "direct_finished"))
        self.assertEqual(len(self.store.calls(task)), 1)
        self.assertIn("initial", (self.store.root/f"tasks/{task}/exports/final.md").read_text())
    def test_high_rechecks_revision_and_feedback_before_accepting(self):
        task, engine = self.make("high", [candidate("original"), review(), verification(),
                                        candidate("improved"), review(True), verification(True)])
        result = engine.run(task)
        self.assertEqual(result["quality"], "criteria_met")
        self.assertEqual(self.store.read_artifact(result["selected"])["answer"], "improved [b1]")
        self.assertEqual(len(self.store.calls(task)), 6)
    def test_rejected_revision_keeps_reviewed_incumbent(self):
        task, engine = self.make("high", [candidate("original"), review(), verification(), candidate("wrong"),
                                        review(blocking=True,preferred="incumbent"),
                                        verification(blocking=True,preferred='incumbent',action='blocked')])
        result = engine.run(task)
        self.assertEqual(result["reason"], "evidence_blocked")
        self.assertEqual(result["quality"], "partial")
        self.assertEqual(self.store.read_artifact(result["selected"])["answer"], "original [b1]")
    def test_unknown_call_requires_explicit_retry_and_counts_both_attempts(self):
        task, engine = self.make("medium", [ProviderError("timeout", outcome_unknown=True), candidate("recovered"),
                                          review(True), verification(True)])
        result = engine.run(task)
        self.assertEqual(result["state"], "interrupted")
        again = engine.run(task)
        self.assertEqual(again["state"], "interrupted")
        self.assertEqual(len(self.store.calls(task)), 1)
        final = engine.run(task, retry_unknown=True)
        self.assertEqual(final["quality"], "criteria_met")
        self.assertEqual(len(self.store.calls(task)), 4)
    def test_input_overflow_dispatches_nothing(self):
        task, engine = self.make("low", [], question="q"*50000)
        result = engine.run(task)
        self.assertEqual(result["reason"], "budget_limit")
        self.assertEqual(self.store.calls(task), [])
    def test_alternative_direction_is_bounded_and_reviewed(self):
        task, engine = self.make("high", [candidate("original"), review(action="alternative"),
                                        verification(action='alternative'), candidate("alternative"),
                                        review(True), verification(True)])
        result = engine.run(task)
        self.assertEqual(result["quality"], "criteria_met")
        self.assertEqual(result["checkpoint"]["directions"], 2)
    def test_model_switch_cannot_mutate_existing_task(self):
        task, engine = self.make("low", [candidate("initial")])
        engine.provider = RemoteBoundary([])
        data = engine.provider.profile.public_dict()
        data["model"] = "different"
        engine.provider.profile = ProviderProfile.from_dict(data)
        with self.assertRaises(ValueError):
            engine.run(task)
        self.assertEqual(self.store.calls(task), [])

    def test_resume_with_changed_controller_version_requires_new_task(self):
        task,engine=self.make("medium",[])
        saved=self.store.task(task)["spec"]
        saved["code_version"]="older-implementation"
        with self.store.db() as db:
            db.execute('UPDATE tasks SET spec=? WHERE id=?',(json.dumps(saved),task))
        with self.assertRaises(ValueError):
            engine.run(task)
        self.assertEqual(self.store.calls(task),[])

    def test_ctrl_c_after_successful_generation_replays_response_not_paid_call(self):
        task, engine = self.make("medium", [candidate("saved"), review(True), verification(True)])
        original = engine._generate
        def interrupt_after(role):
            original(role)
            raise KeyboardInterrupt()
        with patch.object(engine, "_generate", interrupt_after):
            result = engine.run(task)
        self.assertEqual(result["state"], "interrupted")
        result = engine.run(task, retry_unknown=True)
        self.assertEqual(result["quality"], "criteria_met")
        self.assertEqual(len(self.store.calls(task)), 3)

    def test_review_transport_error_preserves_unchecked_draft_in_task_state(self):
        task, engine = self.make("medium", [candidate("saved"), ProviderError("timeout", outcome_unknown=True)])
        result = engine.run(task)
        self.assertEqual(result["quality"], "unchecked")
        self.assertIsNotNone(result["selected"])

    def test_review_without_claim_coverage_is_not_accepted(self):
        bad_review = review(True)
        del bad_review["claim_checks"]
        task, engine = self.make("medium", [candidate("saved"), bad_review, bad_review])
        result = engine.run(task)
        self.assertEqual(result["state"], "failed")
        self.assertNotEqual(result["quality"], "criteria_met")
        self.assertEqual(len(self.store.calls(task)),3)

    def test_one_format_repair_is_paid_and_still_requires_valid_claim_checks(self):
        bad=review(True);del bad['claim_checks']
        task,engine=self.make('medium',[candidate('saved'),bad,review(True),verification(True)])
        result=engine.run(task)
        self.assertEqual(result['quality'],'criteria_met')
        calls=self.store.calls(task)
        self.assertEqual([c['status'] for c in calls],['succeeded','failed','succeeded','succeeded'])
        payload=json.loads(calls[2]['request'][-1]['content'])
        self.assertIn('claim',payload['format_error'])
        self.assertIn('response_to_correct',payload)

    def test_low_cannot_spend_an_extra_call_on_format_repair(self):
        task,engine=self.make('low',[{}])
        result=engine.run(task)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(len(self.store.calls(task)),1)

    def test_unknown_repair_outcome_never_retries_automatically(self):
        bad=review(True);del bad['claim_checks']
        task,engine=self.make('medium',[candidate('saved'),bad,ProviderError('timeout',outcome_unknown=True)])
        result=engine.run(task)
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(len(self.store.calls(task)),3)
        engine.run(task)
        self.assertEqual(len(self.store.calls(task)),3)

    def test_large_format_repair_never_drops_original_evidence(self):
        class LargeCorpus(CorpusBoundary):
            def search(self,query,source_ids,limit=8,max_tokens=3000):
                return [{'id':f'b{i}','source_id':'s1','text':'source evidence '*220,
                         'title':f'Source {i}','location':{'line_start':i,'line_end':i}}
                        for i in range(1,5)]
        bad=review(True);del bad['claim_checks'];bad['notes']='detailed feedback '*450
        good=verification(True);good['source_quotes'][0]['quote']='source evidence'
        task,engine=self.make('medium',[candidate('saved'),bad,review(True),good])
        engine.corpus=LargeCorpus()
        result=engine.run(task)
        calls=self.store.calls(task)
        original=json.loads(calls[1]['request'][-1]['content'])['evidence']
        self.assertEqual({b['canonical_id'] for b in original},{'b1','b2','b3','b4'})
        if len(calls)>=3:
            repaired=json.loads(calls[2]['request'][-1]['content'])['evidence']
            self.assertEqual(repaired,original)
        else:
            self.assertEqual(result['reason'],'budget_limit')
            self.assertEqual(len(calls),2)

    def test_short_prompt_citations_expand_to_frozen_source_ids(self):
        short = {"answer":"A supported answer [E1]", "claims":[{"text":"A supported answer", "evidence_ids":["E1"]}], "open_issues":[]}
        task, engine = self.make("low", [short])
        result = engine.run(task)
        self.assertEqual(result["state"], "finished")
        saved = self.store.read_artifact(result["selected"])
        self.assertEqual(saved["claims"][0]["evidence_ids"], ["b1"])
        self.assertIn("[b1]", saved["answer"])

    def test_citation_aliases_do_not_rewrite_question_or_plain_variables(self):
        short={"answer":"The variable E1 is distinct from a citation [E1].", "claims":[{"text":"variable and citation differ","evidence_ids":["E1"]}],"open_issues":[]}
        task,engine=self.make("low",[short],question="Explain E1 and the literal identifier b1")
        result=engine.run(task)
        saved=self.store.read_artifact(result["selected"])
        self.assertIn("variable E1",saved["answer"])
        request=self.store.calls(task)[0]["request"]
        self.assertEqual(json.loads(request[-1]["content"])["question"],"Explain E1 and the literal identifier b1")

    def test_followup_retrieval_keeps_cited_original_evidence_in_review(self):
        class RotatingCorpus(CorpusBoundary):
            def __init__(self):
                self.reads = 0
            def search(self, query, source_ids, limit=8, max_tokens=3000):
                self.reads += 1
                if self.reads == 1:
                    return super().search(query, source_ids)
                return [{"id":"b2","source_id":"s1","text":"New evidence "*700,"title":"New","location":{"line_start":2,"line_end":2}}]
        task, engine = self.make("medium", [candidate("initial"), review(action="retrieve"), verification(True)])
        engine.corpus = RotatingCorpus()
        result = engine.run(task)
        self.assertEqual(result["quality"], "criteria_met")
        request = self.store.calls(task)[-1]["request"]
        supplied = json.loads(request[-1]["content"])["evidence"]
        self.assertIn("b1", {b["canonical_id"] for b in supplied})


if __name__ == "__main__":
    unittest.main()
