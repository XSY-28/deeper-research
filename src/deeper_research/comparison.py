"""Blind quality witnesses and untrusted concerns have different authority."""
from .acceptance import normalized

SCHEMA = {
    'verdict':'A|B|tie|uncertain|needs_check',
    'reason':'Specific substantive comparison, at most600 characters',
    'advantages':[{'candidate':'A|B','dimension':'reasoning|precision|usefulness|clarity|coverage',
                   'span_id':'Program answer span ID, e.g. A.p2','reason':'Why this actual span is better'}],
    'improvement':'One actionable remaining weakness of preferred answer, or empty',
    'concerns':[{'candidate':'A|B','kind':'evidence|answer','span_id':'Program span ID, or empty for a whole-answer concern',
                 'reason':'Possible defect to independently recheck, not a verified fact',
                 'query':'Focused source query, required for evidence'}],
}


def answer_spans(answer: str, label: str) -> list[dict]:
    """Stable literal paragraphs; source text and candidate artifacts stay intact."""
    rows=[];offset=0
    for line in answer.splitlines(keepends=True):
        if line.strip():
            rows.append({'id':f'{label}.p{len(rows)+1}','text':line.rstrip('\r\n'),
                         'start':offset,'end':offset+len(line.rstrip('\r\n'))})
        offset+=len(line)
    return rows


def validate_comparison(value: dict, body: dict) -> dict:
    answers={s['label']:s['candidate']['answer'] for s in body['candidates']}
    span_protocol=all('answer_spans' in s['candidate'] for s in body['candidates'])
    spans={r['id']:(label,r['text']) for label,text in answers.items() for r in answer_spans(text,label)}
    corrections=[]
    for key in ('reason','improvement'):
        if not isinstance(value.get(key),str) or (key=='reason' and not value[key].strip()):
            raise ValueError('Comparison needs concise reason and improvement text')
        if len(value[key])>600:
            corrections.append(f'{key}_shortened:{len(value[key])}->600')
            value[key]=value[key][:600]
    concerns=value.get('concerns')
    if not isinstance(concerns,list) or len(concerns)>6:
        raise ValueError('Comparison concerns must be a list of at most six items')
    def bind(row, *, negative):
        if not isinstance(row,dict) or row.get('candidate') not in answers:
            raise ValueError('Unknown comparison candidate label')
        if not isinstance(row.get('reason'),str) or not row['reason'].strip() or len(row['reason'])>600:
            raise ValueError('A comparison finding needs a specific reason')
        sid=row.get('span_id')
        if span_protocol and not sid and not negative:
            raise ValueError('A new comparison positive witness must use a program span ID')
        if sid:
            if sid in spans and spans[sid][0]==row['candidate']:
                row['answer_quote']=spans[sid][1][:400]
                row['anchor_kind']='program_span';row['quote_located']=True;row['scope']='span'
                return
            if not negative:raise ValueError('Unknown or cross-candidate positive span')
        quote=row.get('answer_quote','')
        if not isinstance(quote,str) or len(quote)>600:
            raise ValueError('Comparison quote must be a concise string')
        located=bool(normalized(quote) and any(c.isalnum() for c in quote)
                     and normalized(quote) in normalized(answers[row['candidate']]))
        if not located and not negative:
            raise ValueError('Positive comparison witness must identify actual answer text')
        row['answer_quote']=quote
        row['quote_located']=located
        row['anchor_kind']='legacy_quote' if located else 'untrusted_advice'
        row['scope']='span' if located else 'whole_answer'
    for row in concerns:
        bind(row,negative=True)
        if row.get('kind')=='coverage':
            row['kind']='answer';corrections.append('coverage_concern_is_answer_recheck')
        if row.get('kind') not in {'evidence','answer'} or not isinstance(row.get('query'),str) or len(row['query'])>300:
            raise ValueError('Invalid comparison concern')
        if row['kind']=='evidence' and not row['query'].strip():
            raise ValueError('Evidence concern needs a focused query')
    value.pop('contract_corrections',None)
    if concerns:
        # Negative advice has no positive evidence authority. A paraphrased
        # concern may trigger a full independent audit, never a winner/acceptance.
        if value.get('verdict')!='needs_check':corrections.append('concerns_override_winner')
        if value.get('advantages'):corrections.append('defer_positive_advantages_until_recheck')
        value['verdict']='needs_check';value['advantages']=[]
        if any(not c['quote_located'] for c in concerns):corrections.append('unlocated_concern_is_whole_answer_advice')
        value['contract_corrections']=corrections
        return value
    if value.get('verdict') not in {'A','B','tie','uncertain'}:
        raise ValueError('Invalid comparison verdict or empty needs_check')
    rows=value.get('advantages')
    if not isinstance(rows,list) or len(rows)>6:
        raise ValueError('Comparison advantages must be a list of at most six items')
    for row in rows:
        bind(row,negative=False)
        if row.get('dimension') not in {'reasoning','precision','usefulness','clarity','coverage'}:
            raise ValueError('Invalid quality dimension')
    if value['verdict'] in answers and not any(x['candidate']==value['verdict'] for x in rows):
        raise ValueError('A strict winner needs an actual positive witness')
    if corrections:value['contract_corrections']=corrections
    return value


def no_regression(old: dict, new: dict) -> bool:
    old_met={x['id'] for x in old.get('requirement_checks',[]) if x['status']=='met'}
    new_met={x['id'] for x in new.get('requirement_checks',[]) if x['status']=='met'}
    if not old_met<=new_met:
        return False
    for status in ('contradicted','partial','unverified'):
        if sum(c['status']==status for c in new['claim_checks'])>sum(c['status']==status for c in old['claim_checks']):
            return False
    return sum(i['severity']=='blocking' for i in new['issues'])<=sum(i['severity']=='blocking' for i in old['issues'])
