"""Clause/source alignment receipts, not a deterministic entailment oracle.

The model must check each declared clause and each cited body span; a quote for
one half of a compound claim cannot count as coverage of its other half.
"""
import re
from .acceptance import normalized
from .contracts import _evidence

CHECK={'id':'program unit ID','relation':'entailed|contradicted|different_subject|weaker_scope|unclear|proposal_basis',
       'source_subject':'Exact source subject phrase INSIDE your quote, resolving antecedents in reason',
       'quote':'Concise contiguous source text containing subject and relevant predicate',
       'evidence_ids':[],'reason':'Explain subject, polarity, comparison direction and scope concisely',
       'query':'Focused missing-source query, empty unless needed'}


def semantic_units(candidate: dict) -> list[dict]:
    rows=[];seen=set()
    for claim in candidate['claims']:
        chunks=re.split(r'(?<=[，；;。!?！？])|(?<!\d)[,.](?!\d)',claim['text'])
        for part in chunks:
            if not any(c.isalnum() for c in part):continue
            text=part.strip();rows.append({'id':f"{claim['id']}.p{sum(r['claim_id']==claim['id'] for r in rows)+1}",
                'claim_id':claim['id'],'text':text,'context':claim['text']})
            seen.add(normalized(text).strip(' .。;；，,'))
    # Each adjacent citation cluster binds the preceding literal answer span.
    ids=set(candidate.get('body_evidence_ids',[])) | {i for c in candidate['claims'] for i in c['evidence_ids']}
    marks=list(re.finditer(r'(?:\[[^\[\]\n]+\]\s*)+',candidate['answer']))
    cursor=0;n=0
    for mark in marks:
        cited=set(re.findall(r'\[([^\[\]\n]+)\]',mark.group()))
        if not cited & ids:continue
        text=candidate['answer'][cursor:mark.start()].strip();cursor=mark.end()
        if not any(c.isalnum() for c in text):continue
        if normalized(text).strip(' .。;；，,') in seen:continue
        n+=1;rows.append({'id':f'body.p{n}','claim_id':None,'text':text,'context':'cited answer span'})
    # Uncited tail (or the whole uncited answer) is still part of the delivered
    # answer. A true cited assertion must not conceal a false following sentence.
    tail=candidate['answer'][cursor:].strip()
    if any(c.isalnum() for c in tail) and normalized(tail).strip(' .。;；，,') not in seen:
        rows.append({'id':f'body.p{n+1}','claim_id':None,'text':tail,'context':'remaining answer text'})
    return rows


def _paragraph_scopes(text: str) -> list[str]:
    """Keep complete paragraphs; headings fence scope, citations do not.

    Never split punctuation inside propositions. Blank lines or heading-like
    data inside fenced examples/math remain in their original quoted scope.
    """
    blocks=[];lines=[];fence=None;math=None;list_level=None
    def flush():
        nonlocal list_level
        if lines:
            value=''.join(lines).strip('\r\n')
            if value.strip():blocks.append(value)
            lines.clear()
        list_level=None
    def heading(line):
        return bool(re.fullmatch(r' {0,3}(?:#{1,6}[ \t]+.+|\*\*[^\n]+\*\*)\s*',line))
    for line in text.splitlines(keepends=True):
        stripped=line.strip();indent=len(line)-len(line.lstrip(' '))
        if fence:
            lines.append(line)
            if re.fullmatch(re.escape(fence[0])+r'{'+str(fence[1])+r',}\s*',stripped):fence=None
            continue
        if math:
            lines.append(line)
            if stripped==math:math=None
            continue
        marker=re.match(r'^\s*(`{3,}|~{3,})',line)
        if marker:
            lines.append(line);fence=(marker[1][0],len(marker[1]));continue
        if stripped in {'$$',r'\['}:
            lines.append(line);math='$$' if stripped=='$$' else r'\]';continue
        if not stripped:
            if list_level is not None:lines.append(line)
            else:flush()
            continue
        item=re.match(r'^ *(?:[-+*]|[0-9]+[.)])[ \t]+',line)
        if item and ((list_level is None and indent<=3) or (list_level is not None and indent<=list_level)):
            flush();list_level=indent
        elif list_level is not None and lines and not lines[-1].strip() and indent<=list_level:
            flush()
        if heading(line) and lines and not (list_level is not None and indent>list_level):flush()
        lines.append(line)
    flush()

    def table_rows(block):
        rows=block.splitlines()
        def cells(row):return [c.strip() for c in re.split(r'(?<!\\)\|',row.strip().strip('|'))]
        if len(rows)<3:return [block]
        header=cells(rows[0]);separator=cells(rows[1])
        if ('|' not in rows[0] or len(separator)!=len(header)
                or not all(re.fullmatch(r':?-{3,}:?',c) for c in separator)
                or not all(len(cells(row))==len(header) for row in rows[2:])):
            return [block]
        # Keep the original column headers/qualifiers on EACH independent row.
        # Repetition supplies context; no fact/proposal label is inferred.
        return ['\n'.join([rows[0],rows[1],row]) for row in rows[2:]]

    scopes=[];prefix=None
    for block in blocks:
        if prefix is not None and heading(block.splitlines()[0]):
            scopes.append(prefix);prefix=None
        if heading(block):
            # A preceding heading may itself contain a factual assertion.
            # Never accumulate it across a new section's heading.
            if prefix is not None:scopes.append(prefix)
            prefix=block;continue
        for part in table_rows(block):
            scopes.append(prefix+'\n\n'+part if prefix is not None else part)
        prefix=None
    if prefix is not None:scopes.append(prefix)
    return scopes


def audit_groups(candidate: dict) -> list[dict]:
    """One judgment per structural scope, without crossing paragraph roles.

    Full answer/claims remain in the request for antecedents and logical scope.
    All text, including uncited facts, is retained; no semantic role is guessed.
    New body IDs prevent a former citation-group verdict from filling new rows.
    """
    groups=[]
    for claim in candidate['claims']:
        scopes=_paragraph_scopes(claim['text'])
        for n,text in enumerate(scopes,1):
            groups.append({'id':claim['id'] if len(scopes)==1 else f"{claim['id']}.b{n}",
                           'claim_id':claim['id'],'text':text})
    ids=set(candidate.get('body_evidence_ids',[])) | {i for c in candidate['claims'] for i in c['evidence_ids']}
    def literal(text):
        # Remove only known inline citation tokens. No punctuation, negation,
        # heading or wording normalization may turn a broader scope into a copy.
        text=re.sub(r'\[([^\[\]\n]+)\]',lambda m:'' if m[1] in ids else m[0],text)
        return re.sub(r'\s+',' ',text).strip()
    for n,text in enumerate(_paragraph_scopes(candidate['answer']),1):
        copies=[g for g in groups if g['claim_id'] is not None and literal(g['text'])==literal(text)]
        if len(copies)==1:
            copies[0]['text']=text
        else:
            groups.append({'id':f'body.b{n}','claim_id':None,'text':text})
    return groups


def audit_semantics(report: dict, candidate: dict, evidence: list[dict]) -> dict:
    grouped=report.get('wire_protocol')=='compact-v2'
    checks=report.get('semantic_checks')
    scopes=audit_groups(candidate)
    # Legacy report shapes may answer the NEW request IDs directly; never
    # expand one broad verdict into separately authored narrower judgments.
    scoped=grouped or (isinstance(checks,list) and all(isinstance(c,dict) for c in checks)
                      and {c.get('id') for c in checks}=={g['id'] for g in scopes})
    units={u['id']:u for u in (scopes if scoped else semantic_units(candidate))}
    report['semantic_checks']=checks
    if (not isinstance(checks,list) or len(checks)!=len(units) or any(not isinstance(c,dict) for c in checks)
            or {c.get('id') for c in checks}!=set(units)):
        raise ValueError('Semantic verification must cover every program clause/body unit ID')
    allowed={b['id'] for b in evidence};failures=[]
    for check in checks:
        if check.get('relation') not in {'entailed','contradicted','different_subject','weaker_scope','unclear','proposal_basis'}:
            raise ValueError('Invalid semantic relation')
        for key in ('source_subject','quote','reason','query'):
            if not isinstance(check.get(key),str):raise ValueError('Invalid semantic field: '+key)
        if not check['reason'].strip():raise ValueError('Semantic check needs a reason')
        _evidence(check.get('evidence_ids'),allowed)
        quote=normalized(check['quote']);subject=normalized(check['source_subject'])
        located=bool(quote and any(c.isalnum() for c in quote) and any(
            b['id'] in check['evidence_ids'] and quote in normalized(b['text'])
            and (len(quote)>=3 or quote==normalized(b['text'])) for b in evidence))
        subject_located=bool(subject and subject in quote)
        check['quote_located']=located;check['subject_located']=subject_located
        if grouped:
            basis=check.get('basis')
            if not isinstance(basis,list):raise ValueError('Grouped semantic check needs complete source basis')
            located=bool(basis)
            subject_located=bool(basis)
            for b in basis:
                _evidence([b.get('source')],allowed)
                q=normalized(b['quote']);sub=normalized(b['subject'])
                located &= bool(q and any(c.isalnum() for c in q) and any(
                    e['id']==b['source'] and q in normalized(e['text']) and (len(q)>=3 or q==normalized(e['text'])) for e in evidence))
                subject_located &= bool(sub and sub in q)
            check['quote_located']=located;check['subject_located']=subject_located
        good=check['relation'] in {'entailed','proposal_basis'} and located and subject_located
        if good:continue
        unit=units[check['id']]
        missing=not located or not subject_located or check['relation']=='unclear'
        status=('contradicted' if check['relation']=='contradicted' else
                ('partial' if check['relation']=='weaker_scope' else 'unverified')) if not missing else 'unverified'
        for parent in report['claim_checks']:
            if parent['id']==unit['claim_id'] and parent['status']!='contradicted':
                parent['status']=status
                parent['reason']+=' Clause/source alignment is not established: '+check['id']
        reason=f"Semantic unit {check['id']}: {check['reason']}"
        if missing:reason+=' Source subject/predicate basis is not located or remains unclear.'
        report['issues'].append({'severity':'blocking','reason':reason,'evidence_ids':check['evidence_ids'],
                                'semantic_unit_id':check['id']})
        failures.append({'id':check['id'],'kind':'evidence' if missing else 'answer',
                         'query':check['query'] or unit['text'][:300]})
    report['semantics_checked']=True;report['semantic_gaps']=failures
    if failures and report['action']!='blocked':
        need=next((x for x in failures if x['kind']=='evidence'),None)
        if need or report['action']=='retrieve':
            report['action']='retrieve'
            if need:report['query']=need['query']
        else:report['action']='revise'
    return report
