"""Semantic extraction and review boundary; no dependency on LightRAG state."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from rdflib import Graph, Namespace, RDF, RDFS, Literal, URIRef, XSD
from . import profile as p
from .store import Store, dumps
from .validation import parse_response, check
from . import verification as v

AT = Namespace('urn:atlas:ontology:')


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def chunks(text, limit=3000, overlap=300):
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = text.rfind('\n\n', start + limit // 2, end)
            if boundary >= 0:
                end = boundary + 2
        yield {'start': start, 'end': end, 'text': text[start:end]}
        if end == len(text):
            break
        start = end - min(overlap, (end - start) // 2)


class SemanticService:
    def __init__(self, directory, workspace=None, *, recover=True):
        self.store = Store(directory / 'semantic.sqlite3', recover=recover)
        self.workspace = workspace

    def start_extraction(self, source_ids, domain='silk'):
        if not self.workspace:
            raise ValueError('未连接工作区')
        return self.workspace.start('semantic', {'source_ids': source_ids, 'domain': domain})

    def retry(self, batch_id):
        return self.workspace.start('semantic_retry', {'batch_id': batch_id})

    def create_batch(self, sources, model, domain, job_id=None, *, topic=''):
        if domain not in p.DOMAINS or not 1 <= len(sources) <= 10 or len({s['id'] for s in sources}) != len(sources):
            raise ValueError('领域无效或请选择 1–10 份不同资料')
        if any(not s['text'].strip() for s in sources) or sum(len(s['text']) for s in sources) > 100000:
            raise ValueError('正文为空或超过每批 100,000 字符')
        batch_id = uuid.uuid4().hex
        meta = {'id': batch_id, 'domain': domain, 'model': model, 'profile': p.VERSION, 'profile_hash': p.fingerprint(),
                'crm': p.CRM_VERSION, 'prompt': p.PROMPT_VERSION, 'verification': v.VERSION, 'created_at': now(), 'job_id': job_id, 'topic': topic,
                'sources': [{'id': s['id'], 'title': s.get('title', ''), 'url': s.get('url', ''), 'hash': digest(s['text']), 'text': s['text']} for s in sources]}
        with self.store.connect() as db:
            db.execute('INSERT INTO batches VALUES (?,?,?)', (batch_id, dumps(meta), 'running'))
            for source in sources:
                for block in chunks(source['text']):
                    block.update(source_id=source['id'], source_hash=digest(source['text']))
                    cid = uuid.uuid5(uuid.NAMESPACE_URL, batch_id + source['id'] + str(block['start'])).hex
                    db.execute('INSERT INTO chunks(id,batch_id,data,status) VALUES (?,?,?,?)', (cid, batch_id, dumps(block), 'pending'))
        return batch_id

    def batches(self):
        with self.store.connect() as db:
            values = [{**json.loads(r['data']), 'status': r['status']} for r in db.execute('SELECT * FROM batches ORDER BY rowid DESC')]
        # Older automatic batches kept their topic in the job journal only.
        if self.workspace:
            for batch in values:
                job_id = batch.get('job_id')
                if not batch.get('topic') and isinstance(job_id, str) and re.fullmatch(r'[0-9a-f]{32}', job_id):
                    try:
                        job = json.loads((self.workspace.job_dir / (job_id + '.json')).read_text(encoding='utf-8'))
                        if job.get('batch_id') == batch['id']:
                            batch['topic'] = job.get('topic', '')
                    except (OSError, ValueError):
                        pass
        return values

    def report(self, batch_id=None):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM chunks' + (' WHERE batch_id=?' if batch_id else ''), (batch_id,) if batch_id else ()).fetchall()
            return [{'id': r['id'], 'batch_id': r['batch_id'], 'status': r['status'], 'error': r['error'],
                     **json.loads(r['data']), 'deferred': json.loads(r['raw']).get('deferred', []) if r['status'] == 'success' else []} for r in rows]

    def prepare_retry(self, batch_id, sources, model):
        batch = next((b for b in self.batches() if b['id'] == batch_id), None)
        if not batch or batch['status'] not in {'partial', 'failed', 'interrupted'}:
            raise ValueError('该批次没有可重试的失败或中断块')
        if not p.compatible_hash(batch['profile_hash']) or batch['prompt'] != p.PROMPT_VERSION or batch['model'] != model:
            raise ValueError('模型或版本已变化，请新建抽取批次')
        hashes = {s['id']: digest(s['text']) for s in sources}
        if any(hashes.get(s['id']) != s['hash'] for s in batch['sources']):
            raise ValueError('来源正文已变化，请新建抽取批次')
        with self.store.connect() as db:
            db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
        return batch

    def save_response(self, chunk_id, raw, *, auto_publish=True, verification=None, verification_error=None):
        # A successful chunk is immutable, including after retries and fresh UI reruns.
        value = parse_response(raw)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM chunks WHERE id=?', (chunk_id,)).fetchone()
            if row is None:
                raise ValueError('未知文本块')
            if row['status'] == 'success':
                if auto_publish:
                    self._auto_publish(db, chunk_id=chunk_id)
                return
            block = json.loads(row['data'])
            meta = json.loads(db.execute('SELECT data FROM batches WHERE id=?', (row['batch_id'],)).fetchone()[0])
            entities = {e['id']: e for e in value['entities']}
            resolved = {}
            for local_id, entity in entities.items():
                eid = entity.get('uri') if entity.get('uri') in p.VOCAB else self.store.base + 'entity:' + uuid.uuid5(uuid.NAMESPACE_URL, chunk_id + ':' + local_id).hex
                resolved[local_id] = {**entity, 'local_id': local_id, 'id': eid}
                db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?)', (eid, chunk_id, dumps(resolved[local_id])))
            seen = set()
            for s in value['statements']:
                validation = check(s, entities, block['text'], block['start'])
                evidence_check = (verification or {}).get(v.statement_id(s))
                validation['errors'].extend(v.errors(evidence_check, s, entities, block['text'], meta['model']))
                validation['valid'] = not validation['errors']
                # Only suppress exact duplicate statements/evidence inside this chunk.
                key = digest(dumps(s))
                if key in seen:
                    continue
                seen.add(key)
                sid = uuid.uuid5(uuid.NAMESPACE_URL, chunk_id + key).hex
                data = {'id': sid, 'batch_id': row['batch_id'], 'chunk_id': chunk_id, 'domain': meta['domain'],
                        'wire': s, 'entities': entities, 'source': next(source for source in meta['sources'] if source['id'] == block['source_id']), 'subject': resolved.get(s['subject']),
                        'object': resolved.get(s['object']) if isinstance(s['object'], str) else s['object'],
                        'predicate': s['predicate'], 'versions': {k: meta[k] for k in ('model', 'profile', 'profile_hash', 'crm', 'prompt')},
                        'verification': evidence_check or {'verdict': 'uncertain', 'reason': verification_error or '缺少模型证据检查；请重新抽取'},
                        'evidence': {'source_id': block['source_id'], 'source_hash': block['source_hash'], 'quote': s['quote'],
                                     **self._anchor_fields(validation)}}
                data['versions']['verification'] = meta.get('verification')
                db.execute("INSERT INTO candidates(id,chunk_id,data,validation) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, validation=excluded.validation WHERE candidates.status='pending'", (sid, chunk_id, dumps(data), dumps(validation)))
            db.execute('UPDATE chunks SET status=?, raw=?, error=? WHERE id=?',
                       ('failed' if verification_error else 'success', dumps(value), verification_error, chunk_id))
            if auto_publish and not verification_error:
                self._auto_publish(db, chunk_id=chunk_id)

    async def run(self, batch_id, llm, emit, *, auto_publish=True):
        batch = next(b for b in self.batches() if b['id'] == batch_id)
        try:
            for block in self.report(batch_id):
                if block['status'] == 'success':
                    if auto_publish:
                        self.auto_publish(batch_id=batch_id)
                    continue
                cid = block['id']
                with self.store.connect() as db:
                    db.execute("UPDATE chunks SET status='running' WHERE id=?", (cid,))
                emit('semantic.chunk.started', {'chunk_id': cid, 'source_id': block['source_id']})
                stage = '模型请求'
                try:
                    with self.store.connect() as db:
                        cached = db.execute('SELECT raw FROM chunks WHERE id=?', (cid,)).fetchone()[0]
                    raw = cached or await llm.extract_semantic(block['text'], batch['domain'])
                    stage = 'JSON 协议解析'
                    value = parse_response(raw)
                    selected = v.requests(value, block['text'])
                    evidence_checks = {}
                    if selected:
                        stage = '模型证据检查'
                        emit('semantic.verification.started', {'chunk_id': cid, 'count': len(selected)})
                        try:
                            response = await llm.verify_semantic(block['text'], selected)
                            evidence_checks = v.parse(response, selected, block['text'], batch['model'])
                        except Exception as exc:
                            message = '模型证据检查失败（' + type(exc).__name__ + '）；未发布，用户可重试失败块。'
                            self.save_response(cid, raw, auto_publish=False, verification_error=message)
                            emit('semantic.chunk.failed', {'chunk_id': cid, 'message': message})
                            continue
                        emit('semantic.verification.completed', {'chunk_id': cid,
                             'supported': sum(r['verdict'] == 'supported' for r in evidence_checks.values())})
                    self.save_response(cid, raw, auto_publish=auto_publish, verification=evidence_checks)
                    emit('semantic.chunk.completed', {'chunk_id': cid, 'auto_publish': auto_publish})
                except Exception as exc:
                    # Never persist provider error bodies, credentials, or hidden reasoning.
                    message = stage + '失败（' + type(exc).__name__ + '）。'
                    if stage == '模型请求':
                        status_code = re.search(r'HTTP (\d{3})', str(exc))
                        if status_code:
                            message += status_code.group(0) + '。'
                    if stage == 'JSON 协议解析' and isinstance(exc, (ValueError, TypeError)):
                        message += str(exc)[:200]
                    message += '请检查模型设置或输出协议后显式重试。'
                    with self.store.connect() as db:
                        db.execute("UPDATE chunks SET status='failed', error=? WHERE id=?", (message, cid))
                    emit('semantic.chunk.failed', {'chunk_id': cid, 'message': message})
        finally:
            with self.store.connect() as db:
                states = [r[0] for r in db.execute('SELECT status FROM chunks WHERE batch_id=?', (batch_id,))]
                state = 'success' if all(s == 'success' for s in states) else 'interrupted' if 'running' in states or 'pending' in states else 'partial' if 'success' in states else 'failed'
                db.execute('UPDATE batches SET status=? WHERE id=?', (state, batch_id))
        return state

    def list_candidates(self, status=None, domain=None, source_id=None, entity_type=None, batch_id=None):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM candidates ORDER BY rowid DESC').fetchall()
            values = [{**json.loads(r['data']), 'status': r['status'], 'validation': json.loads(r['validation'])} for r in rows]
            for v in values:
                v['reviews'] = [dict(r) | {'data': json.loads(r['data'])} for r in db.execute('SELECT * FROM reviews WHERE candidate_id=? ORDER BY id', (v['id'],))]
                v['publications'] = [dict(r) for r in db.execute('SELECT * FROM publications WHERE candidate_id=? ORDER BY id', (v['id'],))]
        return [v for v in values if (not status or v['status'] == status) and (not domain or v['domain'] == domain)
                and (not batch_id or v['batch_id'] == batch_id)
                and (not source_id or v['evidence']['source_id'] == source_id)
                and (not entity_type or any(isinstance(v[k], dict) and v[k].get('type') == entity_type for k in ('subject', 'object')))]

    @staticmethod
    def _anchor_fields(result):
        return {k: result[k] for k in ('start', 'end', 'matched_quote', 'match_method', 'similarity', 'model_start', 'anchor_version')}

    def _validate(self, db, row):
        data = json.loads(row['data'])
        block = json.loads(db.execute('SELECT data FROM chunks WHERE id=?', (row['chunk_id'],)).fetchone()[0])
        result = check(data['wire'], data['entities'], block['text'], block['start'])
        if not p.compatible_hash(data['versions']['profile_hash']) or data['versions']['prompt'] != p.PROMPT_VERSION:
            result['errors'].append('候选版本与当前配置不一致；请重新抽取')
        result['errors'].extend(v.errors(data.get('verification'), data['wire'], data['entities'], block['text'], data['versions']['model']))
        if data.get('source') and digest(data['source']['text']) != data['evidence']['source_hash']:
            result['errors'].append('来源全文哈希不一致')
        if data['evidence']['source_hash'] != block['source_hash'] or data['evidence']['quote'] != data['wire']['quote']:
            result['errors'].append('证据快照不一致')
        result['valid'] = not result['errors']
        updated = {**data['evidence'], **self._anchor_fields(result)}
        if updated != data['evidence']:
            data.setdefault('evidence_revisions', []).append({'at': now(), 'reason': '本地引文重新定位', 'previous': data['evidence']})
            data['evidence'] = updated
        db.execute('UPDATE candidates SET data=?, validation=? WHERE id=?', (dumps(data), dumps(result), row['id']))
        return result

    def validate(self, candidate_ids=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM candidates WHERE status='pending'").fetchall()
            return {r['id']: self._validate(db, r) for r in rows if candidate_ids is None or r['id'] in candidate_ids}

    def _auto_publish(self, db, *, batch_id=None, chunk_id=None):
        query = "SELECT c.* FROM candidates c JOIN chunks k ON c.chunk_id=k.id WHERE c.status='pending' AND k.status='success'"
        params = []
        if batch_id is not None:
            query += ' AND k.batch_id=?'; params.append(batch_id)
        if chunk_id is not None:
            query += ' AND k.id=?'; params.append(chunk_id)
        counts = {'published': 0, 'blocked': 0}
        for row in db.execute(query, params).fetchall():
            if not self._validate(db, row)['valid']:
                counts['blocked'] += 1
                continue
            data = json.loads(row['data'])
            at = now()
            audit = {'batch_id': data['batch_id'], 'confirmed_types': False,
                     'policy': 'automatic-evidence-v2', 'reason': '本体校验和模型证据检查均通过'}
            rid = db.execute('INSERT INTO reviews(candidate_id,action,at,reviewer,data) VALUES (?,?,?,?,?)',
                             (row['id'], 'auto_publish', at, '系统自动发布', dumps(audit))).lastrowid
            db.execute('INSERT INTO publications(candidate_id,review_id,at) VALUES (?,?,?)', (row['id'], rid, at))
            db.execute("UPDATE candidates SET status='published' WHERE id=?", (row['id'],))
            counts['published'] += 1
        return counts

    def auto_publish(self, batch_id=None):
        """Publish only valid, completed extractions; never resurrect rejected/revoked rows."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._auto_publish(db, batch_id=batch_id)

    def approve(self, candidate_ids, *, confirmed_types=False):
        if confirmed_types is not True:
            raise ValueError('请先确认原文证据及相关实体类型')
        return self._review(candidate_ids, 'approve')

    def reject(self, candidate_ids, reason=''):
        return self._review(candidate_ids, 'reject', reason)

    def revoke(self, candidate_ids, reason=''):
        return self._review(candidate_ids, 'revoke', reason)

    def _review(self, ids, action, reason=''):
        ids = list(dict.fromkeys(ids))
        if not ids:
            raise ValueError('请选择陈述')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = []
            for cid in ids:
                row = db.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
                if row is None:
                    raise ValueError('候选不存在')
                if action == 'approve' and row['status'] != 'published':
                    if row['status'] != 'pending' or not self._validate(db, row)['valid']:
                        raise ValueError('只有校验通过且待审核的当前候选可以批准')
                if action == 'reject' and row['status'] not in {'pending', 'rejected'}:
                    raise ValueError('已发布陈述请使用撤回')
                if action == 'revoke' and row['status'] not in {'published', 'revoked'}:
                    raise ValueError('只能撤回已发布陈述')
                rows.append(row)
            target = {'approve': 'published', 'reject': 'rejected', 'revoke': 'revoked'}[action]
            for row in rows:
                if row['status'] == target:
                    continue
                at = now()
                audit = {'reason': reason, 'batch_id': json.loads(row['data'])['batch_id'], 'confirmed_types': action == 'approve'}
                review_id = db.execute('INSERT INTO reviews(candidate_id,action,at,reviewer,data) VALUES (?,?,?,?,?)', (row['id'], action, at, '本机审核', dumps(audit))).lastrowid
                if action == 'approve':
                    db.execute('INSERT INTO publications(candidate_id,review_id,at) VALUES (?,?,?)', (row['id'], review_id, at))
                if action == 'revoke':
                    db.execute('UPDATE publications SET revoked_at=? WHERE candidate_id=? AND revoked_at IS NULL', (at, row['id']))
                db.execute('UPDATE candidates SET status=? WHERE id=?', (target, row['id']))
        return len(ids)

    def get_published_graph(self, **filters):
        values = self.list_candidates(status='published', **filters)
        nodes = {e['id']: e for v in values for e in (v['subject'], v['object'])}
        return {'nodes': list(nodes.values()), 'statements': values}

    def export(self, format='turtle'):
        values = self.get_published_graph()
        metadata = {'project_uri': self.store.base, 'crm': p.CRM_VERSION, 'profile': p.VERSION, 'profile_hash': p.fingerprint(), 'export_version': '1', 'exported_at': now()}
        if format == 'json':
            # Whole-chunk wire entities can include unreviewed, unused candidates.
            # Audit exports contain only the reviewed claim's resolved endpoints.
            def reviewed(value):
                return {k: v for k, v in value.items() if k not in {'entities', 'wire'}}
            return dumps({'metadata': metadata, 'nodes': values['nodes'],
                          'statements': [reviewed(s) for s in values['statements']],
                          'withdrawals': [reviewed(s) for s in self.list_candidates(status='revoked')]})
        if format not in {'turtle', 'json-ld'}:
            raise ValueError('不支持的导出格式')
        g = Graph()
        g.bind('crm', Namespace(p.CRM)); g.bind('atlas', AT)
        project = URIRef(self.store.base)
        for key, value in metadata.items():
            g.add((project, AT[key], Literal(value)))
        for e in values['nodes']:
            node = URIRef(e['id'])
            g.add((node, RDF.type, p.uri(e['type'])))
            g.add((node, RDFS.label, Literal(e['label'], lang='zh')))
        for s in values['statements']:
            subject, predicate, obj = URIRef(s['subject']['id']), URIRef(s['predicate']), URIRef(s['object']['id'])
            claim = URIRef(self.store.base + 'statement:' + s['id'])
            g.add((subject, predicate, obj))
            g.add((claim, RDF.type, RDF.Statement))
            for key, val in [(RDF.subject, subject), (RDF.predicate, predicate), (RDF.object, obj)]:
                g.add((claim, key, val))
            evidence = URIRef(str(claim) + ':evidence')
            g.add((claim, AT.evidence, evidence))
            if s.get('source'):
                for key in ('title', 'url'):
                    g.add((evidence, AT[key], Literal(s['source'].get(key, ''))))
            for key, val in s['evidence'].items():
                if val is None:
                    continue
                g.add((evidence, AT[key], Literal(val)))
            for key in ('batch_id', 'domain'):
                g.add((claim, AT[key], Literal(s[key])))
            g.add((claim, AT.versions, Literal(dumps(s['versions']))))
            if s.get('verification'):
                g.add((claim, AT.verification, Literal(dumps(s['verification']))))
            for review in s['reviews']:
                node = URIRef(self.store.base + 'review:' + str(review['id']))
                g.add((claim, AT.review, node))
                for key in ('action', 'at', 'reviewer'):
                    g.add((node, AT[key], Literal(review[key])))
        return g.serialize(format=format)
