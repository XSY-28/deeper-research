"""Pre-frozen obligations and complete receipts for legacy control-path fixtures.
These are test-owned task contracts; production still compiles unspecified plans.
Adversarial coverage tests supply their own receipts and bypass this helper.
"""
from copy import deepcopy

def fixture_plan(criteria):
 return {'criteria':[{'id':f'c{i+1}','requirements':[{'text':c,'kind':'fact','request_quote':c}]} for i,c in enumerate(criteria)]}

def with_receipts(reply,payload):
 value=with_semantic_receipts(reply,payload)
 if 'criteria' not in value or 'requirement_checks' in value or 'requirement_plan' not in payload:
  return value
 statuses={c['id']:c['status'] for c in value['criteria']}
 value['requirement_checks']=[]
 for group in payload['requirement_plan']['criteria']:
  status=statuses.get(group['id'],'unmet')
  for r in group['requirements']:
   text=payload['candidate']['answer'][:120] if status=='met' else ''
   block=payload['evidence'][0] if payload['evidence'] else None
   value['requirement_checks'].append({'id':r['id'],'status':status,'answer_quote':text,
    'answer_mode':'result' if status=='met' else 'missing',
    'evidence_ids':[block['id']] if status=='met' and block else [],
    'source_quote':block['text'][:120] if status=='met' and block else '',
    'gap_type':'none' if status=='met' else ('evidence' if value.get('action')=='retrieve' else 'answer'),
    'query':value.get('query','') if value.get('action')=='retrieve' else ''})
 return value


def with_semantic_receipts(reply,payload):
 """Scripted model answers for old control-path tests, not a truth oracle.
 Adversarial semantic tests supply their own checks; production always asks the
 real verifier to judge subject/predicate/scope for every program unit.
 """
 value=deepcopy(reply)
 if 'semantic_units' not in payload or 'semantic_checks' in value:return value
 units=payload['semantic_units'];evidence=payload['evidence']
 value['semantic_checks']=[]
 for u in units:
  block=evidence[0] if evidence else None
  quote=block['text'][:200] if block else ''
  value['semantic_checks'].append({'id':u['id'],'relation':'entailed','source_subject':quote.split()[0] if quote else '',
    'quote':quote,'evidence_ids':[block['id']] if block else [],
    'reason':'Scripted boundary verdict for this control-path fixture.','query':''})
 return value
