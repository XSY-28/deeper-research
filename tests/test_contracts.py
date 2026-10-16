import unittest
from deeper_research.contracts import validate_candidate, validate_review, review_accepted


class ContractTests(unittest.TestCase):
    def review(self, issue):
        return {"action":"accept", "criteria":[{"id":"c1","status":"met","reason":"checked","evidence_ids":["b1"]}],
                "issues":[issue], "preferred":"candidate", "comparison_reason":"checked",
                "claim_checks":[{"id":"claim-1","status":"supported","reason":"source support","evidence_ids":["b1"]}]}

    def test_issue_status_alias_keeps_blocking_semantics(self):
        value=self.review({"status":"blocking","reason":"contradiction","evidence_ids":["b1"]})
        result=validate_review(value,{"b1"},1,["claim-1"])
        self.assertEqual(result["issues"][0]["severity"],"blocking")
        self.assertFalse(review_accepted(result))
        self.assertEqual(result["format_corrections"][0]["path"],"issues[0].severity")

    def test_conflicting_issue_fields_are_rejected(self):
        value=self.review({"severity":"later","status":"blocking","reason":"contradiction","evidence_ids":["b1"]})
        with self.assertRaises(ValueError):
            validate_review(value,{"b1"},1,["claim-1"])

    def test_unknown_issue_status_is_not_guessed(self):
        value=self.review({"status":"minor","reason":"unspecified severity","evidence_ids":["b1"]})
        with self.assertRaises(ValueError):
            validate_review(value,{"b1"},1,["claim-1"])

    def test_unknown_secondary_status_cannot_hide_critical_issue(self):
        value=self.review({"severity":"later","status":"critical","reason":"critical mismatch","evidence_ids":["b1"]})
        with self.assertRaises(ValueError):
            validate_review(value,{"b1"},1,["claim-1"])

    def test_provider_cannot_supply_program_owned_correction_metadata(self):
        value=self.review({"status":"blocking","reason":"contradiction","evidence_ids":["b1"]})
        value['format_corrections']={"provider_supplied":"untrusted"}
        result=validate_review(value,{"b1"},1,["claim-1"])
        self.assertFalse(review_accepted(result))
        self.assertEqual(result['format_corrections'],[{'path':'issues[0].severity','original_field':'status','value':'blocking'}])

    def test_scoped_criterion_reference_and_format_example_are_not_sources(self):
        value=validate_candidate({"answer":"满足 [c2]，引用格式为 [来源ID:页码]，依据 [b1]。", "claims":[{"text":"method","evidence_ids":["b1"]}],"open_issues":[]},{"b1"},criterion_count=2)
        self.assertEqual(value["body_evidence_ids"],["b1"])
        self.assertTrue(value["coverage_complete"])

    def test_nonexistent_criterion_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_candidate({"answer":"符合 [c9]。依据 [b1]。", "claims":[{"text":"method","evidence_ids":["b1"]}],"open_issues":[]},{"b1"},criterion_count=2)

    def test_template_cannot_replace_actual_source_citation(self):
        with self.assertRaises(ValueError):
            validate_candidate({"answer":"依据 [来源ID:页码]。", "claims":[{"text":"fact","evidence_ids":["b1"]}],"open_issues":[]},{"b1"},criterion_count=2)
    def test_api_argument_and_citation_placeholder_are_not_source_references(self):
        value=validate_candidate({"answer":"Use search[entity] and a template [E#]; the paper says so [b1].", "claims":[{"text":"method","evidence_ids":["b1"]}],"open_issues":[]},{"b1"})
        self.assertTrue(value["coverage_complete"])

    def test_format_criterion_can_be_checked_without_external_evidence(self):
        value={"action":"accept", "criteria":[{"id":"c1","status":"met","reason":"Answer is written in Chinese","evidence_ids":[]}], "issues":[],"preferred":"candidate","comparison_reason":"check", "claim_checks":[{"id":"claim-1","status":"supported","reason":"source support","evidence_ids":["b1"]}]}
        validate_review(value, {"b1"}, 1, ["claim-1"])
        self.assertTrue(review_accepted(value))
    def test_known_but_unlisted_body_citation_remains_unchecked_gap(self):
        value = validate_candidate({"answer":"Claim [b1] and context [b2]", "claims":[{"text":"Claim", "evidence_ids":["b1"]}],"open_issues":[]}, {"b1","b2"})
        self.assertFalse(value["coverage_complete"])
        self.assertEqual(value["uncovered_evidence_ids"], ["b2"])
    def test_partial_claim_is_valid_but_not_accepted(self):
        value = {"action":"accept", "criteria":[{"id":"c1","status":"met","reason":"evidence","evidence_ids":["b1"]}],
            "issues":[],"preferred":"candidate","comparison_reason":"check",
            "claim_checks":[{"id":"claim-1","status":"partial","evidence_ids":["b1"],"reason":"only one part supported"}]}
        validate_review(value, {"b1"}, 1, ["claim-1"])
        self.assertFalse(review_accepted(value))
    def test_invented_body_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_candidate({"answer":"Claim [invented-block]", "claims":[{"text":"Claim", "evidence_ids":[]}],"open_issues":[]}, {"b1"})
    def test_duplicate_evidence_cannot_inflate_progress(self):
        with self.assertRaises(ValueError):
            validate_candidate({"answer":"Claim [b1]", "claims":[{"text":"Claim", "evidence_ids":["b1","b1"]}],"open_issues":[]}, {"b1"})
    def test_unverified_claim_blocks_acceptance_even_if_criteria_met(self):
        value = {"action":"accept", "criteria":[{"status":"met"}], "issues":[],
                 "claim_checks":[{"id":"claim-1","status":"unverified","evidence_ids":[],"reason":"No supporting evidence"}]}
        self.assertFalse(review_accepted(value))
