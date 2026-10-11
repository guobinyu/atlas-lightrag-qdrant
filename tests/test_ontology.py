import asyncio
from copy import deepcopy
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from rdflib import Graph, RDF, URIRef
from lab.ontology import profile as p
from lab.ontology.service import SemanticService, chunks, AT
from lab.ontology.validation import parse_response, check
from evidence_helpers import checked, verdicts

# Fixture is executable data so standard URIs cannot drift from the pinned profile.
import importlib.util
spec = importlib.util.spec_from_file_location('culture_samples', Path(__file__).parent / 'fixtures/culture_samples.py')
samples = importlib.util.module_from_spec(spec); spec.loader.exec_module(samples)
CASES = samples.cases()
MODEL = {'provider':'http', 'model':'fixed-test-response', 'endpoint':'http://127.0.0.1:9', 'timeout':180, 'has_key':False}


def ingest(service, payload=None, text=None, domain='silk', source_id='source-1'):
    case = CASES[0]
    bid = service.create_batch([{'id':source_id, 'text':text or case['text'], 'title':'合成样例'}], MODEL, domain)
    block = service.report(bid)[0]
    service.save_response(block['id'], json.dumps(payload or case['response'], ensure_ascii=False), auto_publish=False,
                          verification=checked(payload or case['response'], text or case['text'], MODEL))
    return bid, block, [s for s in service.list_candidates() if s['batch_id'] == bid]


@pytest.mark.parametrize('case', CASES, ids=lambda c:c['id'])
def test_curated_samples(case, tmp_path):
    service = SemanticService(tmp_path)
    _, _, values = ingest(service, case['response'], case['text'], case['domain'])
    assert all(v['validation']['valid'] for v in values) == case['valid']
    assert not service.get_published_graph()['statements']
    if not case['valid']:
        with pytest.raises(ValueError):
            service.approve([v['id'] for v in values], confirmed_types=True)
    if case['id'].endswith('homonyms'):
        assert len({s['subject']['id'] for s in values}) == 2
        assert len({s['object']['id'] for s in values}) == 2
    if case['id'].endswith('general-method'):
        assert not values
    if case['id'].endswith('missing-time-place'):
        assert all(e['type'] not in {'E52','E53'} for e in case['response']['entities'])


def test_profile_inheritance_and_uris():
    assert str(p.uri('P14')) == 'http://www.cidoc-crm.org/cidoc-crm/P14_carried_out_by'
    assert {'E11','E7','E5','E4','E2','E92','E1','E63'} <= p.ancestors('E12')
    assert {'E39','E20','E18'} <= p.ancestors('E21')
    assert len(p.VOCAB) == 12
    assert all(v['type'] == 'E55' for v in p.VOCAB.values())


@pytest.mark.parametrize('raw', ['not JSON', '{}', '```json\n{}\n```', '{"entities":[],"entities":[],"statements":[],"deferred":[]}', '{"entities":NaN,"statements":[],"deferred":[]}'])
def test_invalid_wire(raw):
    with pytest.raises(ValueError):
        parse_response(raw)


@pytest.mark.parametrize('mutation', ['missing-ref','unknown-type','unknown-predicate','literal','empty-quote','fake-uri','wrong-p7'])
def test_invalid_candidates_never_publish(tmp_path, mutation):
    service = SemanticService(tmp_path)
    payload = deepcopy(CASES[0]['response']); text=CASES[0]['text']
    s=payload['statements'][0]
    if mutation=='missing-ref': s['object']='missing'
    if mutation=='unknown-type': payload['entities'][0]['type']='E999'
    if mutation=='unknown-predicate': s['predicate']='http://example.org/invented'
    if mutation=='literal': s['object']={'value':'张甲','datatype':'http://www.w3.org/2001/XMLSchema#string'}
    if mutation=='empty-quote': s['quote']=''
    if mutation=='ambiguous': text *= 2; s.pop('start')
    if mutation=='fake-uri': payload['entities'][0]['uri']='https://example.org/fake'
    if mutation=='wrong-p7': s['predicate']=str(p.uri('P7'))
    _,_,values=ingest(service,payload,text)
    assert not values[0]['validation']['valid']
    with pytest.raises(ValueError): service.approve([values[0]['id']],confirmed_types=True)
    assert not service.get_published_graph()['statements']


def test_review_atomic_idempotent_revoke_and_export(tmp_path):
    service=SemanticService(tmp_path)
    _,_,values=ingest(service); cid=values[0]['id']
    assert not list(Graph().parse(data=service.export(),format='turtle').triples((None,p.uri('P14'),None)))
    with pytest.raises(ValueError): service.approve([cid])
    with pytest.raises(ValueError): service.approve([cid,'unknown'],confirmed_types=True)
    assert not service.get_published_graph()['statements']
    service.approve([cid],confirmed_types=True); service.approve([cid],confirmed_types=True)
    value=service.list_candidates()[0]
    assert len(value['reviews'])==len(value['publications'])==1
    for fmt in ('turtle','json-ld'):
        g=Graph().parse(data=service.export(fmt),format=fmt)
        assert (URIRef(value['subject']['id']),p.uri('P14'),URIRef(value['object']['id'])) in g
        claim=URIRef(service.store.base+'statement:'+cid)
        assert len(list(g.objects(claim,AT.evidence)))==1
        assert len(list(g.objects(claim,AT.review)))==1
    service.revoke([cid]); service.revoke([cid])
    assert not service.get_published_graph()['statements']
    assert not list(Graph().parse(data=service.export(),format='turtle').triples((None,p.uri('P14'),None)))
    audit=json.loads(service.export('json'))
    assert len(audit['withdrawals'])==1 and len(audit['withdrawals'][0]['reviews'])==2
    assert audit['withdrawals'][0]['publications'][0]['revoked_at']


def test_directions_multiedges_evidence_and_no_name_merges(tmp_path):
    service=SemanticService(tmp_path)
    payload={'entities':[{'id':'a','label':'同名文献','type':'E31'},{'id':'b','label':'同名文献','type':'E31'},{'id':'m','label':'材料','type':'E57'},{'id':'e','label':'生产','type':'E12','concrete':True}],
             'statements':[], 'deferred':[]}
    text='合成：两篇文献相互记载；本次生产使用了材料，并具有该材料类型。'
    for a,rel,b in [('a','P70','b'),('b','P70','a'),('e','P126','m'),('e','P2','m')]:
        payload['statements'].append({'subject':a,'predicate':str(p.uri(rel)),'object':b,'quote':text})
    _,block,v1=ingest(service,payload,text)
    _,_,v2=ingest(service,payload,text,source_id='source-2')
    assert len(v1)==len(v2)==4
    assert {v['subject']['id'] for v in v1}.isdisjoint({v['subject']['id'] for v in v2})
    service.save_response(block['id'],json.dumps(payload),auto_publish=False)
    assert len(service.list_candidates())==8
    service.approve([v['id'] for v in v1+v2],confirmed_types=True)
    assert len(service.get_published_graph()['statements'])==8
    assert len({s['evidence']['source_id'] for s in service.list_candidates()})==2


def test_new_batch_does_not_inherit_review_and_version_checks(tmp_path):
    service=SemanticService(tmp_path)
    _,_,a=ingest(service); service.approve([a[0]['id']],confirmed_types=True)
    _,_,b=ingest(service)
    assert b[0]['status']=='pending' and b[0]['id']!=a[0]['id']
    with service.store.connect() as db:
        data=deepcopy(b[0]); data['versions']['profile_hash']='old'
        db.execute('UPDATE candidates SET data=? WHERE id=?',(json.dumps(data),b[0]['id']))
    with pytest.raises(ValueError): service.approve([b[0]['id']],confirmed_types=True)
    assert len(service.get_published_graph()['statements'])==1


def test_concurrent_approval(tmp_path):
    service=SemanticService(tmp_path); _,_,values=ingest(service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:service.approve([values[0]['id']],confirmed_types=True),range(2)))
    assert len(service.list_candidates()[0]['reviews'])==1


def test_chunk_bounds_offsets_and_coverage():
    text=('甲'*1400+'\n\n'+'乙'*1800+'\n\n')*4
    blocks=list(chunks(text))
    covered=set()
    for i,b in enumerate(blocks):
        assert len(b['text'])<=3000 and text[b['start']:b['end']]==b['text']
        covered.update(range(b['start'],b['end']))
        if i: assert 0<=blocks[i-1]['end']-b['start']<=300
    assert len(covered)==len(text)


def test_partial_failure_retry_and_restart(tmp_path):
    service=SemanticService(tmp_path)
    source={'id':'a','text':'甲'*6001}; calls=[]
    bid=service.create_batch([source],MODEL,'tea')
    class LLM:
        async def extract_semantic(self,text,domain):
            calls.append(text)
            if len(calls)==2: raise TimeoutError('secret')
            return '{"entities":[],"statements":[],"deferred":[]}'
    assert asyncio.run(service.run(bid,LLM(),lambda *a:None))=='partial'
    count=len(calls)
    service.prepare_retry(bid,[source],MODEL)
    assert asyncio.run(service.run(bid,LLM(),lambda *a:None))=='success'
    assert len(calls)==count+1
    bid2=service.create_batch([source],MODEL,'tea')
    service=SemanticService(tmp_path)
    assert next(b for b in service.batches() if b['id']==bid2)['status']=='interrupted'
    with pytest.raises(ValueError): service.prepare_retry(bid2,[{'id':'a','text':'changed'}],MODEL)
    with pytest.raises(ValueError): service.prepare_retry(bid2,[source],dict(MODEL,model='new'))
    service.prepare_retry(bid2,[source],MODEL)
    assert asyncio.run(service.run(bid2,LLM(),lambda *a:None))=='success'
    assert 'secret' not in service.store.path.read_bytes().decode(errors='ignore')


def test_shared_vocabulary_preserves_each_sources_evidence(tmp_path):
    service=SemanticService(tmp_path)
    payload={'entities':[{'id':'x','type':'E55','label':'织造','uri':p.TERM_BASE+'silk/0'}, {'id':'y','type':'E55','label':'织锦','uri':p.TERM_BASE+'silk/2'}],
             'statements':[{'subject':'x','predicate':str(p.uri('P2')),'object':'y','quote':'仅测试存储，不是语义正确性的声明。'}], 'deferred':[]}
    text='仅测试存储，不是语义正确性的声明。'
    _,_,a=ingest(service,payload,text,source_id='a')
    _,_,b=ingest(service,payload,text,source_id='b')
    assert a[0]['subject']['id']==b[0]['subject']['id']
    assert a[0]['object']['id']==b[0]['object']['id']
    assert a[0]['id']!=b[0]['id'] and a[0]['evidence']['source_id']!=b[0]['evidence']['source_id']
    service.approve([a[0]['id'],b[0]['id']],confirmed_types=True)
    g=Graph().parse(data=service.export(),format='turtle')
    assert len(list(g.triples((None,p.uri('P2'),None))))==1
    assert len(list(g.subjects(RDF.type,RDF.Statement)))==2


def test_batch_limits_and_rejected_review_cannot_be_reused(tmp_path):
    service=SemanticService(tmp_path)
    for sources in ([], [{'id':str(i),'text':'a'} for i in range(11)], [{'id':'x','text':'a'*100001}], [{'id':'x','text':'a'},{'id':'x','text':'b'}]):
        with pytest.raises(ValueError): service.create_batch(sources,MODEL,'silk')
    _,_,values=ingest(service); cid=values[0]['id']
    service.reject([cid]); service.reject([cid])
    with pytest.raises(ValueError): service.approve([cid],confirmed_types=True)
    assert len(service.list_candidates()[0]['reviews'])==1
    assert cid not in service.export('json')


def test_explicit_offset_resolves_repeated_quote_and_tamper_rejected(tmp_path):
    service=SemanticService(tmp_path)
    payload=deepcopy(CASES[0]['response']); text=CASES[0]['text']*2
    payload['statements'][0]['start']=len(CASES[0]['text'])
    _,_,values=ingest(service,payload,text)
    assert values[0]['validation']['valid']
    cid=values[0]['id']
    with service.store.connect() as db:
        data=json.loads(db.execute('SELECT data FROM candidates WHERE id=?',(cid,)).fetchone()[0])
        data['source']['text']='changed source'
        db.execute('UPDATE candidates SET data=? WHERE id=?',(json.dumps(data),cid))
    with pytest.raises(ValueError): service.approve([cid],confirmed_types=True)


def test_invalid_json_block_failure_keeps_successful_blocks(tmp_path):
    service=SemanticService(tmp_path)
    bid=service.create_batch([{'id':'x','text':'a'*6001}],MODEL,'silk')
    calls=[]
    class Model:
        async def extract_semantic(self,*args):
            calls.append(1)
            return 'not-json' if len(calls)==1 else '{"entities":[],"statements":[],"deferred":[]}'
    assert asyncio.run(service.run(bid,Model(),lambda *a:None))=='partial'
    assert sum(b['status']=='failed' for b in service.report(bid))==1
    assert len(calls)==3
    assert not service.get_published_graph()['statements']


def test_audit_export_excludes_unreviewed_chunk_entities(tmp_path):
    service=SemanticService(tmp_path)
    payload=deepcopy(CASES[0]['response'])
    payload['entities'].append({'id':'unused','label':'UNREVIEWED_ORPHAN','type':'E21'})
    _,_,values=ingest(service,payload)
    service.approve([values[0]['id']],confirmed_types=True)
    assert 'UNREVIEWED_ORPHAN' not in service.export('json')


def test_lenient_anchors_and_actual_source_spans():
    from lab.ontology.validation import locate_quote
    text='前文。张甲在甲村实施了本次织造，使用丝线制成这件织锦样品。后文。'
    quote='张甲在甲村实施了本次织造，使用丝线制成这件织锦样品。'
    for supplied, method in [(quote,'exact'), (quote.replace('，',' ,  '),'normalized'), (quote.replace('实施','开展'),'approximate')]:
        r=locate_quote(supplied,text,999,offset=100)
        assert r['match_method']==method
        assert text[r['start']-100:r['end']-100]==r['matched_quote']
        assert r['warnings']
        assert r['model_start']==999
    r=locate_quote(quote,quote+quote,None)
    assert r['match_method']=='exact' and any('多处' in w for w in r['warnings'])
    assert locate_quote('完全不存在的文化人物参加了海外的另外一种活动。',text)['match_method']=='missing'
    assert locate_quote('虚构人物',text)['match_method']=='missing'


def test_legacy_pending_reanchor_audit_and_approval(tmp_path):
    service=SemanticService(tmp_path)
    payload=deepcopy(CASES[0]['response']);payload['statements'][0]['start']=2
    _,_,values=ingest(service,payload)
    cid=values[0]['id']
    with service.store.connect() as db:
        row=db.execute('SELECT data FROM candidates WHERE id=?',(cid,)).fetchone()
        data=json.loads(row[0])
        data['evidence']={k:v for k,v in data['evidence'].items() if k in {'source_id','source_hash','quote'}}
        data['evidence'].update(start=2,end=2+len(payload['statements'][0]['quote']))
        db.execute('UPDATE candidates SET data=?,validation=? WHERE id=?',(json.dumps(data),json.dumps({'valid':False,'errors':['引文与指定偏移不匹配']}),cid))
    assert service.validate([cid])[cid]['valid']
    result=service.list_candidates()[0]
    assert result['status']=='pending' and result['evidence']['start']==0
    assert result['wire']['start']==2 and result['evidence_revisions'][0]['previous']['start']==2
    service.validate([cid])
    assert len(service.list_candidates()[0]['evidence_revisions'])==1
    service.approve([cid],confirmed_types=True)
    assert service.validate()=={}  # Published evidence is not rewritten by maintenance.
    assert service.list_candidates()[0]['evidence']['start']==0


def test_approximate_quote_review_and_export(tmp_path):
    service=SemanticService(tmp_path)
    payload=deepcopy(CASES[0]['response'])
    payload['statements'][0]['quote']=payload['statements'][0]['quote'].replace('实施','开展')
    _,_,values=ingest(service,payload)
    s=values[0]
    assert s['validation']['valid'] and s['evidence']['match_method']=='approximate'
    assert not service.get_published_graph()['statements']
    service.approve([s['id']],confirmed_types=True)
    for fmt in ('turtle','json-ld'):
        graph=Graph().parse(data=service.export(fmt),format=fmt)
        assert list(graph.objects(None,AT.matched_quote))
        assert list(graph.objects(None,AT.match_method))
    assert '开展' in s['evidence']['quote'] and '实施' in s['evidence']['matched_quote']


def test_maintenance_open_does_not_interrupt_running_batch(tmp_path):
    service=SemanticService(tmp_path)
    service.create_batch([{'id':'a','text':'test'}],MODEL,'silk')
    other=SemanticService(tmp_path,recover=False)
    assert other.batches()[0]['status']=='running'


def test_automatic_publication_is_validated_atomic_and_idempotent(tmp_path):
    service=SemanticService(tmp_path)
    payload=deepcopy(CASES[0]['response'])
    invalid=deepcopy(payload['statements'][0]); invalid['predicate']=str(p.uri('P7'))
    payload['statements'].append(invalid)
    bid=service.create_batch([{'id':'a','text':CASES[0]['text']}],MODEL,'silk')
    block=service.report(bid)[0]
    service.save_response(block['id'],json.dumps(payload), verification=checked(payload, CASES[0]['text'], MODEL))
    published=service.get_published_graph()['statements']
    assert len(published)==1
    s=published[0]
    assert s['reviews'][0]['action']=='auto_publish'
    assert s['reviews'][0]['reviewer']=='系统自动发布'
    assert s['reviews'][0]['data']['confirmed_types'] is False
    assert len(service.list_candidates(status='pending'))==1
    service.save_response(block['id'],json.dumps(payload))
    service.auto_publish()
    assert len(service.get_published_graph()['statements'][0]['reviews'])==1
    service.revoke([s['id']])
    service.auto_publish()
    assert not service.get_published_graph()['statements']


def test_automatic_publish_recovers_legacy_and_keeps_rejections(tmp_path):
    service=SemanticService(tmp_path)
    _,_,a=ingest(service)
    _,_,b=ingest(service)
    service.reject([b[0]['id']])
    report=service.auto_publish()
    assert report=={'published':1,'blocked':0}
    assert service.list_candidates(status='rejected')[0]['id']==b[0]['id']
    assert service.auto_publish()=={'published':0,'blocked':0}


def test_auto_run_partial_failure_and_retry_no_duplicate_publish(tmp_path):
    service=SemanticService(tmp_path)
    text=CASES[0]['text']
    bid=service.create_batch([{'id':'a','text':text},{'id':'b','text':text}],MODEL,'silk')
    class Model:
        calls=0
        async def verify_semantic(self,text,claims):
            return verdicts(claims)
        async def extract_semantic(self,*args):
            self.calls+=1
            if self.calls==2: raise TimeoutError()
            return json.dumps(CASES[0]['response'])
    model=Model()
    assert asyncio.run(service.run(bid,model,lambda *a:None))=='partial'
    assert len(service.get_published_graph()['statements'])==1
    service.prepare_retry(bid,[{'id':'a','text':text},{'id':'b','text':text}],MODEL)
    assert asyncio.run(service.run(bid,model,lambda *a:None))=='success'
    assert model.calls==3
    values=service.get_published_graph()['statements']
    assert len(values)==2 and all(len(s['publications'])==1 for s in values)
