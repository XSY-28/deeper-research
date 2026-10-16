"""Provider-independent task limits and response validation."""
import json
import math
import re
import hashlib
import unicodedata
from pathlib import Path

# Fingerprint at import time: a running process retains its implementation even
# if source files are subsequently edited. Upgrades require a new task version.
CODE_VERSION = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()

LIMITS = {
    "low": {"calls": 1, "rounds": 0, "directions": 1, "reads": 2, "seconds": 120, "tokens": 16000},
    "medium": {"calls": 4, "rounds": 1, "directions": 1, "reads": 8, "seconds": 360, "tokens": 64000},
    "high": {"calls": 10, "rounds": 3, "directions": 2, "reads": 16, "seconds": 720, "tokens": 160000},
}
MAX_INPUT = 12000
MAX_OUTPUT = 3000
PROMPT_VERSION = "2026-10-13.5"


def estimate_tokens(value: str | list | dict) -> int:
    """Conservative byte-based estimate, explicitly not a provider tokenizer."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return math.ceil(len(text.encode("utf-8")) / 2) + 16


def parse_object(content: str) -> dict:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Model response must be a JSON object")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing text: {name}")
    return value


def _evidence(ids, allowed):
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in allowed for i in ids):
        raise ValueError("Response cites evidence outside the supplied evidence packet")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evidence IDs do not constitute additional evidence")


def validate_candidate(value: dict, allowed: set[str], criterion_count: int = 0) -> dict:
    _text(value.get("answer"), "answer")
    claims = value.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("Candidate needs a non-empty claims list")
    for index, claim in enumerate(claims, 1):
        if not isinstance(claim, dict):
            raise ValueError("Claim must be an object")
        _text(claim.get("text"), "claim.text")
        _evidence(claim.get("evidence_ids"), allowed)
        claim["id"] = f"claim-{index}"
    prose = re.sub(r"```.*?```|`[^`]*`", "", value["answer"], flags=re.DOTALL)
    bracket_tokens = set(re.findall(r"(?<![A-Za-z0-9_])\[([^\[\]\n]+)\](?!\()", prose))
    criterion_ids = {f"c{i+1}" for i in range(criterion_count)}
    referenced_criteria = {token for token in bracket_tokens if re.fullmatch(r"c[1-9][0-9]*", token)}
    if not referenced_criteria <= criterion_ids:
        raise ValueError("Body refers to an unknown acceptance criterion")
    # These are explicit example templates, never evidence. Other unknown
    # bracket identifiers remain errors, including invented source IDs.
    citation_templates = {"E#", "来源ID:页码", "source-id:page"}
    body_ids = bracket_tokens - referenced_criteria - citation_templates
    listed_ids = {i for claim in claims for i in claim["evidence_ids"]}
    if not body_ids <= allowed:
        raise ValueError("Body citation refers to unknown evidence")
    if listed_ids and not body_ids:
        raise ValueError("Evidence-backed answers require inline [block-id] citations")
    value["body_evidence_ids"] = sorted(body_ids)
    value["uncovered_evidence_ids"] = sorted(body_ids-listed_ids)
    value["coverage_complete"] = not value["uncovered_evidence_ids"]
    value["criterion_references"] = sorted(referenced_criteria)
    issues = value.get("open_issues")
    if not isinstance(issues, list) or any(not isinstance(i, str) for i in issues):
        raise ValueError("open_issues must be a list of strings")
    return value


def validate_review(value: dict, allowed: set[str], criterion_count: int, claim_ids: list[str]) -> dict:
    criteria = value.get("criteria")
    expected = {f"c{i+1}" for i in range(criterion_count)}
    if not isinstance(criteria, list) or len(criteria) != criterion_count:
        raise ValueError("Review must cover every acceptance criterion")
    if any(not isinstance(c, dict) for c in criteria) or {c.get("id") for c in criteria} != expected:
        raise ValueError("Review criterion IDs do not match the frozen task")
    for criterion in criteria:
        if criterion.get("status") not in {"met", "partial", "unmet"}:
            raise ValueError("Invalid criterion status")
        _text(criterion.get("reason"), "criterion.reason")
        _evidence(criterion.get("evidence_ids"), allowed)
        # Some criteria concern format, scope or honest abstention rather than a
        # sourced fact. Factual support is enforced separately for every claim.
    checks = value.get("claim_checks")
    if (not isinstance(checks, list) or len(checks) != len(claim_ids)
            or any(not isinstance(c, dict) for c in checks)
            or {c.get("id") for c in checks} != set(claim_ids)):
        raise ValueError("Review must independently cover every candidate claim ID")
    for check in checks:
        if check.get("status") not in {"supported", "partial", "contradicted", "unverified", "inference"}:
            raise ValueError("Invalid claim verification status")
        _text(check.get("reason"), "claim_check.reason")
        _evidence(check.get("evidence_ids"), allowed)
        if check["status"] in {"supported", "inference"} and not check["evidence_ids"]:
            raise ValueError("Supported claims and justified inferences need evidence")
    issues = value.get("issues")
    if not isinstance(issues, list):
        raise ValueError("Review needs issues")
    severities = {"blocking", "improvement", "later"}
    corrections = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise ValueError("Invalid review issue")
        if "status" in issue and issue["status"] not in severities:
            raise ValueError("Unknown issue severity alias")
        if "severity" not in issue and issue.get("status") in severities:
            issue["severity"] = issue.pop("status")
            corrections.append({
                "path": f"issues[{index}].severity", "original_field": "status",
                "value": issue["severity"]})
        elif issue.get("status") in severities and issue.get("status") != issue.get("severity"):
            raise ValueError("Conflicting issue severity and status")
        if issue.get("severity") not in severities:
            raise ValueError("Invalid review issue")
        _text(issue.get("reason"), "issue.reason")
        _evidence(issue.get("evidence_ids"), allowed)
    value.pop("format_corrections", None)
    if corrections:
        value["format_corrections"] = corrections
    if value.get("action") not in {"accept", "revise", "alternative", "retrieve", "blocked"}:
        raise ValueError("Invalid review action")
    if value.get("preferred") not in {"candidate", "incumbent", "tie"}:
        raise ValueError("Invalid comparison verdict")
    _text(value.get("comparison_reason"), "comparison_reason")
    if value["action"] == "alternative":
        _text(value.get("strategy"), "strategy")
    if value["action"] == "retrieve":
        _text(value.get("query"), "query")
    return value


def review_accepted(review: dict) -> bool:
    return (review["action"] == "accept"
            and all(c["status"] == "met" for c in review["criteria"])
            and not any(i["severity"] == "blocking" for i in review["issues"])
            and bool(review.get("claim_checks"))
            and all(c["status"] in {"supported", "inference"} for c in review["claim_checks"]))


def validate_verification(value: dict, allowed: set[str], criterion_count: int,
                          claim_ids: list[str], issue_ids: list[str], evidence: list[dict]) -> dict:
    """Validate a second audit and locate its quotes, without proving entailment."""
    validate_review(value, allowed, criterion_count, claim_ids)
    checks = value.get('issue_checks')
    if (not isinstance(checks, list) or len(checks) != len(issue_ids)
            or any(not isinstance(c, dict) for c in checks)
            or {c.get('id') for c in checks} != set(issue_ids)):
        raise ValueError('Verification must cover every original issue ID')
    for check in checks:
        if check.get('status') not in {'confirmed', 'rejected', 'unresolved'}:
            raise ValueError('Invalid issue verification status')
        _text(check.get('reason'), 'issue_check.reason')
        _evidence(check.get('evidence_ids'), allowed)
    for issue in value['issues']:
        if issue.get('original_issue_id') and issue['original_issue_id'] not in issue_ids:
            raise ValueError('Unknown original issue link')
    quotes = value.get('source_quotes')
    if not isinstance(quotes, list):
        raise ValueError('Verification needs source_quotes')
    def normalized(text: str) -> str:
        text = unicodedata.normalize('NFKC', text)
        return re.sub(r'\s+', ' ', re.sub(r'-\s*\n\s*', '', text)).strip()
    located = set()
    checked_sources = {c['id']:set(c['evidence_ids']) for c in value['claim_checks']}
    quote_checks = []
    for quote in quotes:
        if not isinstance(quote, dict) or quote.get('claim_id') not in claim_ids:
            raise ValueError('Unknown quoted claim')
        _evidence([quote.get('evidence_id')], allowed)
        text = _text(quote.get('quote'), 'quote')
        span = normalized(text)
        found = (quote['evidence_id'] in checked_sources[quote['claim_id']]
                 and any(char.isalnum() for char in span) and any(
                    b['id'] == quote['evidence_id'] and span in normalized(b['text'])
                    and (len(span) >= 3 or span == normalized(b['text'])) for b in evidence))
        quote_checks.append({**quote, 'located': found})
        if found:
            located.add(quote['claim_id'])
    unresolved = False
    missing_quotes = []
    for check in value['claim_checks']:
        if check['status'] in {'supported', 'inference'} and check['id'] not in located:
            check['status'] = 'unverified'
            check['reason'] += ' No supporting source quote was located in the supplied text.'
            unresolved = True
            missing_quotes.append(check['id'])
    for check in checks:
        if check['status'] == 'unresolved':
            value['issues'].append({'severity':'blocking', 'reason':check['reason'],
                                    'evidence_ids':check['evidence_ids']})
            unresolved = True
    if unresolved:
        value['issues'].append({'severity':'blocking',
            'reason':'Source-grounded verification remains unresolved; quote occurrence is only a structural check.',
            'evidence_ids':[]})
        if value['action'] != 'retrieve':
            value['action'] = 'blocked'
    value['source_quote_checks'] = quote_checks
    value['missing_quote_claim_ids'] = missing_quotes
    value['verification_unresolved'] = unresolved
    return value


def review_rank(review: dict) -> tuple:
    rank = (-sum(i["severity"] == "blocking" for i in review["issues"]),
            sum(c["status"] == "met" for c in review["criteria"]),
            -sum(c["status"] in {"partial", "contradicted", "unverified"} for c in review["claim_checks"]))
    return rank + (sum(c['status']=='met' for c in review['requirement_checks']),) if 'requirement_checks' in review else rank
