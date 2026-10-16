"""Frozen task obligations and source/answer receipts, not a semantic oracle."""
import re
import unicodedata
from .contracts import _evidence, _text

PLAN_SCHEMA={'criteria':[{'id':'c1','requirements':[{
    'text':'One atomic requested result; split EACH named method/item separately',
    'kind':'fact|design|format|abstention'}]}]}
CHECK_SCHEMA={'id':'c1.r1','status':'met|partial|unmet',
    'answer_mode':'result|missing|abstention',
    'answer_quote':'Concise exact answer phrase; empty if missing',
    'evidence_ids':['E1'],'source_quote':'Concise exact source basis; empty if missing',
    'gap_type':'none|answer|evidence','query':'Concise missing evidence topic; empty unless needed'}


def normalized(text):
    return re.sub(r'\s+',' ',re.sub(r'-\s*\n\s*','',unicodedata.normalize('NFKC',text))).strip()


def validate_plan(value, criteria, question, *, assign_anchors=False):
    if 'question' in value and value['question']!=question:
        raise ValueError('Plan changes the frozen question')
    if 'response_schema' in value:
        nested=value['response_schema']
        if not isinstance(nested,dict):raise ValueError('Nested plan must be an object')
        if 'criteria' in value and value['criteria']!=nested.get('criteria'):
            raise ValueError('Conflicting direct and nested plans')
        outer=value.get('acceptance_criteria')
        # Echoed task labels are metadata, not a second plan or authority over
        # the real frozen criteria. Two actual requirement plans must agree.
        if (isinstance(outer,list) and any(isinstance(c,dict) and 'requirements' in c for c in outer)
                and outer!=nested.get('criteria')):
            raise ValueError('Conflicting nested requirement plans')
        result=validate_plan(nested,criteria,question,assign_anchors=assign_anchors)
        result['plan_format_corrections']=['unwrapped_response_schema']
        return result
    if 'acceptance_criteria' in value:
        if 'criteria' in value and value['criteria']!=value['acceptance_criteria']:
            raise ValueError('Conflicting requirement criterion fields')
        value={**value, 'criteria':value['acceptance_criteria']}
    expected={f'c{i+1}' for i in range(len(criteria))}
    if assign_anchors and set(value)==expected:
        groups=[]
        for cid,items in value.items():
            if isinstance(items,dict) and set(items)=={'requirements'}:items=items['requirements']
            if not isinstance(items,list):raise ValueError('Keyed plan needs lists')
            converted=[]
            for item in items:
                if isinstance(item,list) and len(item)==2:
                    item={'kind':item[0],'text':item[1]}
                converted.append(item)
            groups.append({'id':cid,'requirements':converted})
        value={'criteria':groups}
    groups=value.get('criteria')
    if (not isinstance(groups,list) or len(groups)!=len(criteria)
            or any(not isinstance(c,dict) for c in groups)
            or {c.get('id') for c in groups}!={f'c{i+1}' for i in range(len(criteria))}):
        raise ValueError('Requirement plan must cover every frozen criterion')
    result=[]
    for index,text in enumerate(criteria,1):
        group=next(c for c in groups if c['id']==f'c{index}')
        items=group.get('requirements')
        if not isinstance(items,list) or not items:
            raise ValueError('Each criterion needs nonempty atomic requirements')
        requirements=[]
        for n,r in enumerate(items,1):
            if not isinstance(r,dict):raise ValueError('Requirement must be an object')
            description=_text(r.get('text'),'requirement.text')
            quote=text if assign_anchors else _text(r.get('request_quote'),'requirement.request_quote')
            if r.get('kind') not in {'fact','design','format','abstention'}:raise ValueError('Invalid requirement kind')
            anchors=[text] if assign_anchors else [part.strip() for part in re.split(r'\s*(?:\.{3}|…+)\s*',quote) if part.strip()]
            if not anchors or not all(any(normalized(anchor) in normalized(t) for t in (text,question)) for anchor in anchors):
                raise ValueError('Requirement anchor is not in the frozen task')
            requirements.append({'id':f'c{index}.r{n}','text':description,'kind':r['kind'],
                                 'request_quote':quote,'request_quotes':anchors,'anchor_owner':'program' if assign_anchors else 'provided'})
        result.append({'id':f'c{index}','requirements':requirements})
    return {'criteria':result}


def audit_coverage(report,plan,candidate,evidence,criteria,question):
    """Downgrade unsupported met receipts; truth/entailment still needs a model."""
    required={r['id']:(c['id'],r) for c in plan['criteria'] for r in c['requirements']}
    checks=report.get('requirement_checks')
    if (not isinstance(checks,list) or len(checks)!=len(required)
            or any(not isinstance(c,dict) for c in checks)
            or {c.get('id') for c in checks}!=set(required)):
        raise ValueError('Audit must cover every frozen requirement ID')
    allowed={b['id'] for b in evidence};gaps=[]
    for c in checks:
        criterion,r=required[c['id']]
        mode=c.get('answer_mode')
        if mode in {'partial','inference'}:
            c['answer_mode_correction']={'original':mode,'rule':'No factual completion inferred from a cross-field label'}
            c['answer_mode']='result'
            if mode=='partial' or r['kind']!='design':
                if c.get('status')=='met':c['status']='partial'
                if c.get('gap_type')=='none':c['gap_type']='answer'
        if c.get('status') not in {'met','partial','unmet'}:raise ValueError('Invalid requirement status')
        if c.get('answer_mode') not in {'result','missing','abstention'}:raise ValueError('Invalid answer coverage mode')
        if c.get('gap_type') not in {'none','answer','evidence'}:raise ValueError('Invalid requirement gap type')
        for k in ('answer_quote','source_quote','query'):
            if not isinstance(c.get(k),str):raise ValueError('Missing requirement receipt field: '+k)
        _evidence(c.get('evidence_ids'),allowed)
        answer=normalized(c['answer_quote'])
        answer_found=bool(answer and any(ch.isalnum() for ch in answer) and answer in normalized(candidate['answer']))
        not_delivered = (c['answer_mode']=='missing' or
                         (c['answer_mode']=='abstention' and r['kind']!='abstention'))
        source=normalized(c['source_quote'])
        source_found=bool(source and any(ch.isalnum() for ch in source) and any(
            b['id'] in c['evidence_ids'] and source in normalized(b['text'])
            and (len(source)>=3 or source==normalized(b['text'])) for b in evidence))
        c['answer_located']=answer_found;c['source_located']=source_found
        if c['status']=='met':
            if not answer_found or not_delivered:
                c['status']='unmet';c['gap_type']='answer'
            elif r['kind']=='fact' and not source_found:
                c['status']='partial';c['gap_type']='evidence'
            elif c['gap_type']!='none':
                c['status']='partial'
        if c['status']!='met':
            gap=c['gap_type'] if c['gap_type']!='none' else 'answer'
            gaps.append({'id':c['id'],'criterion_id':criterion,'kind':gap,
                         'query':c['query'] or r['text'],'reason':c.get('reason','')})
    omissions=report.get('plan_omissions',[])
    if not isinstance(omissions,list):raise ValueError('plan_omissions must be a list')
    for item in omissions:
        if not isinstance(item,dict) or item.get('criterion_id') not in {c['id'] for c in plan['criteria']}:
            raise ValueError('Unknown criterion for omitted request')
        quote=_text(item.get('request_quote'),'omission.request_quote')
        text=criteria[int(item['criterion_id'][1:])-1]
        if not any(normalized(quote) in normalized(t) for t in (text,question)):
            raise ValueError('Omitted request must be in the frozen task')
        gaps.append({'id':'plan-omission','criterion_id':item['criterion_id'],'kind':'answer','query':quote})
    for c in report['criteria']:
        local=[check['status'] for check in checks if required[check['id']][0]==c['id']]
        if any(g['criterion_id']==c['id'] for g in gaps):
            derived='partial' if 'met' in local or 'partial' in local else 'unmet'
            if c['status']=='met' or derived=='unmet':c['status']=derived
    report['coverage_gaps']=gaps;report['requirements_checked']=True
    if gaps and report['action'] in {'accept','revise','alternative'}:
        missing_evidence=next((g for g in gaps if g['kind']=='evidence'),None)
        if missing_evidence:report['action']='retrieve'
        elif report['action']=='accept':report['action']='revise'
        if missing_evidence:report['query']=missing_evidence['query']
    return report
