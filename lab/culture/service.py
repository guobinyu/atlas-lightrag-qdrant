"""Culture decoding, annotation and retrieval context. SQLite is the source of truth; Qdrant is derived."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid

from lab.ontology.service import chunks, digest
from lab.ontology.validation import locate_quote
from . import prompts as p
from .store import Store

EXPORT_VERSION = '1'


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def card(item):
    """Model-facing summary of one element. Descriptions are model or curator wording, not source quotes."""
    lines = [f"{p.TYPES[item['type']]}：{item['label']}（{p.BASES[item['basis']]}）"]
    if item['description']:
        lines.append(item['description'])
    if item['colors']:
        lines.append('配色：' + '、'.join(f"{c['name']} {c['hex']}（{c['role']}）" for c in item['colors']))
    if item['themes']:
        lines.append('主题：' + '、'.join(item['themes']))
    lines += [f"{r['label']}：{r['other']}" if r['direction'] == 'out' else f"{r['other']} → {r['label']}" for r in item['relations']]
    return '\n'.join(lines)


def index_text(item):
    return card(item) + ''.join('\n原文：' + e['quote'] for e in item['evidence'][:3])


def evidence_entry(item, score):
    # Only located source quotes are citable text; the card stays a separate design summary.
    return {'element_id': item['id'], 'type': item['type'], 'label': item['label'], 'basis': item['basis'],
            'colors': deepcopy(item['colors']), 'themes': list(item['themes']), 'score': round(score, 4),
            'title': f"文化素材·{p.TYPES[item['type']]}：{item['label']}", 'chunk_id': 'culture:' + item['id'],
            'card': card(item), 'text': '\n'.join(e['quote'] for e in item['evidence'])}


def matched_themes(themes, hint):
    return [t for t in themes if t in hint or any(alias in hint for alias in p.THEME_HINTS.get(t, []))]


class CultureService:
    def __init__(self, directory, *, recover=True):
        self.store = Store(directory / 'culture.sqlite3', recover=recover)

    def themes(self):
        with self.store.connect() as db:
            return [r[0] for r in db.execute("SELECT label FROM themes ORDER BY origin='manual', at, rowid")]

    def add_theme(self, label):
        label = label.strip() if isinstance(label, str) else ''
        if not 1 <= len(label) <= 20:
            raise ValueError('主题名称需为 1–20 字')
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM themes WHERE label=?', (label,)).fetchone():
                raise ValueError('主题已存在')
            db.execute("INSERT INTO themes VALUES (?, 'manual', ?)", (label, now()))

    def samples(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT samples.data, COUNT(DISTINCT evidence.element_id) n FROM samples '
                              'LEFT JOIN evidence ON evidence.sample_id=samples.id GROUP BY samples.id ORDER BY samples.rowid').fetchall()
        return [{**json.loads(r['data']), 'elements': r['n']} for r in rows]

    def create_batch(self, sources, model, *, topic=''):
        if not 1 <= len(sources) <= 10 or len({s['id'] for s in sources}) != len(sources):
            raise ValueError('请选择 1–10 份不同资料')
        if any(not s['text'].strip() for s in sources) or sum(len(s['text']) for s in sources) > 100000:
            raise ValueError('正文为空或超过每批 100,000 字符')
        batch_id = uuid.uuid4().hex
        # Theme list is frozen per batch so retries send the same prompt.
        meta = {'id': batch_id, 'model': model, 'prompt': p.VERSION, 'themes': self.themes(), 'topic': topic, 'created_at': now(),
                'sources': [{'id': s['id'], 'title': s.get('title', ''), 'url': s.get('url', ''), 'hash': digest(s['text'])} for s in sources]}
        with self.store.connect() as db:
            db.execute('INSERT INTO batches VALUES (?,?,?)', (batch_id, dumps(meta), 'running'))
            for source in sources:
                db.execute('INSERT OR IGNORE INTO samples VALUES (?,?)', (source['id'], dumps(
                    {'id': source['id'], 'title': source.get('title') or '未命名资料', 'url': source.get('url', '')})))
                for block in chunks(source['text']):
                    block.update(source_id=source['id'], source_hash=digest(source['text']))
                    cid = uuid.uuid5(uuid.NAMESPACE_URL, 'culture:' + batch_id + source['id'] + str(block['start'])).hex
                    db.execute("INSERT INTO chunks(id,batch_id,data,status) VALUES (?,?,?,'pending')", (cid, batch_id, dumps(block)))
        return batch_id

    def batches(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM batches').fetchall()
            counts = {}
            for r in db.execute('SELECT batch_id, status, COUNT(*) n FROM chunks GROUP BY batch_id, status'):
                counts.setdefault(r['batch_id'], {})[r['status']] = r['n']
        values = [{**json.loads(r['data']), 'status': r['status'], 'chunks': counts.get(r['id'], {})} for r in rows]
        return sorted(values, key=lambda b: b['created_at'], reverse=True)

    def batch(self, batch_id):
        found = next((b for b in self.batches() if b['id'] == batch_id), None)
        if not found:
            raise ValueError('未知解码批次')
        return found

    def report(self, batch_id):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM chunks WHERE batch_id=? ORDER BY rowid', (batch_id,)).fetchall()
        return [{'id': r['id'], 'status': r['status'], 'error': r['error'], 'issues': json.loads(r['issues'] or '[]'),
                 **json.loads(r['data'])} for r in rows]

    def summary(self, batch_id):
        blocks = self.report(batch_id)
        query = ('SELECT COUNT(DISTINCT evidence.{0}) FROM evidence JOIN chunks ON chunks.id=evidence.chunk_id '
                 'WHERE chunks.batch_id=? AND evidence.{0} IS NOT NULL')
        with self.store.connect() as db:
            elements = db.execute(query.format('element_id'), (batch_id,)).fetchone()[0]
            relations = db.execute(query.format('relation_id'), (batch_id,)).fetchone()[0]
        return {'batch_id': batch_id, 'blocks': len(blocks), 'failed_blocks': sum(b['status'] != 'success' for b in blocks),
                'elements': elements, 'relations': relations, 'issues': sum(len(b['issues']) for b in blocks)}

    def prepare_retry(self, batch_id, sources, model):
        batch = self.batch(batch_id)
        if batch['status'] not in {'partial', 'failed', 'interrupted'}:
            raise ValueError('该批次没有可重试的失败或中断块')
        if batch['prompt'] != p.VERSION or batch['model'] != model:
            raise ValueError('模型或解码版本已变化，请新建解码批次')
        hashes = {s['id']: digest(s['text']) for s in sources}
        if any(hashes.get(s['id']) != s['hash'] for s in batch['sources']):
            raise ValueError('来源正文已变化，请新建解码批次')
        with self.store.connect() as db:
            db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
        return batch

    async def run(self, batch_id, llm, emit):
        batch = self.batch(batch_id)
        try:
            for block in self.report(batch_id):
                if block['status'] == 'success':
                    continue
                with self.store.connect() as db:
                    db.execute("UPDATE chunks SET status='running' WHERE id=?", (block['id'],))
                emit('culture.chunk.started', {'chunk_id': block['id'], 'source_id': block['source_id']})
                stage = '模型请求'
                try:
                    raw = await llm.decode_culture(block['text'], batch['themes'])
                    stage = 'JSON 协议解析'
                    result = self.save_response(block['id'], raw)
                    emit('culture.chunk.completed', {'chunk_id': block['id'], **result})
                except Exception as exc:
                    # Never persist provider error bodies, credentials, or hidden reasoning.
                    message = stage + '失败（' + type(exc).__name__ + '）。'
                    if stage == '模型请求':
                        status_code = re.search(r'HTTP (\d{3})', str(exc))
                        if status_code:
                            message += status_code.group(0) + '。'
                    if stage == 'JSON 协议解析' and isinstance(exc, (ValueError, TypeError)):
                        message += str(exc)[:200]
                    message += '请检查模型设置或输出协议后重试失败块。'
                    with self.store.connect() as db:
                        db.execute("UPDATE chunks SET status='failed', error=? WHERE id=?", (message, block['id']))
                    emit('culture.chunk.failed', {'chunk_id': block['id'], 'message': message})
        finally:
            with self.store.connect() as db:
                states = [r[0] for r in db.execute('SELECT status FROM chunks WHERE batch_id=?', (batch_id,))]
                state = ('success' if all(s == 'success' for s in states) else 'interrupted' if 'running' in states or 'pending' in states
                         else 'partial' if 'success' in states else 'failed')
                db.execute('UPDATE batches SET status=? WHERE id=?', (state, batch_id))
        return state

    def save_response(self, chunk_id, raw):
        """One transaction per block: located elements, relations and evidence, or nothing."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM chunks WHERE id=?', (chunk_id,)).fetchone()
            if row is None:
                raise ValueError('未知文本块')
            if row['status'] == 'success':
                return {'elements': 0, 'relations': 0, 'issues': 0}
            meta = json.loads(db.execute('SELECT data FROM batches WHERE id=?', (row['batch_id'],)).fetchone()[0])
            elements, relations, issues = p.parse(raw, meta['themes'])
            block = json.loads(row['data'])
            resolved = {}
            for local, element in elements.items():
                anchor = locate_quote(element['quote'], block['text'], offset=block['start'])
                if anchor['match_method'] == 'missing':
                    issues.append(f"{element['label']}：引文无法在原文定位，未入库。")
                    continue
                resolved[local] = self._merge(db, element)
                self._evidence(db, chunk_id, block, 'e:' + local, element['quote'], anchor, element_id=resolved[local])
            saved = 0
            for r in relations:
                name = f"{r['subject']} {r['predicate']} {r['object']}"
                if r['subject'] not in resolved or r['object'] not in resolved:
                    issues.append(f'关系 {name} 的端点素材未入库，已忽略。')
                    continue
                anchor = locate_quote(r['quote'], block['text'], offset=block['start'])
                if anchor['match_method'] == 'missing':
                    issues.append(f'关系 {name}：引文无法在原文定位，未入库。')
                    continue
                rid = self._relation(db, resolved[r['subject']], r['predicate'], resolved[r['object']], 'extracted')
                self._evidence(db, chunk_id, block, 'r:' + name, r['quote'], anchor, relation_id=rid)
                saved += 1
            db.execute("UPDATE chunks SET status='success', raw=?, error=NULL, issues=? WHERE id=?",
                       (dumps({'elements': elements, 'relations': relations}), dumps(issues), chunk_id))
        return {'elements': len(resolved), 'relations': saved, 'issues': len(issues)}

    def _merge(self, db, element):
        key = p.norm(element['label'])
        row = db.execute('SELECT id, data FROM elements WHERE type=? AND norm=?', (element['type'], key)).fetchone()
        if row:
            # Same type and normalized name only. Curated fields stay; themes accumulate, gaps fill.
            data = json.loads(row['data'])
            data['themes'] = list(dict.fromkeys(data['themes'] + element['themes']))
            data['colors'] = data['colors'] or element['colors']
            data['description'] = data['description'] or element['description']
            db.execute('UPDATE elements SET data=? WHERE id=?', (dumps(data), row['id']))
            return row['id']
        eid = uuid.uuid4().hex
        data = {k: element[k] for k in ('type', 'label', 'description', 'basis', 'themes', 'colors')}
        data.update(id=eid, origin='extracted', created_at=now())
        db.execute("INSERT INTO elements VALUES (?,?,?,'published',?)", (eid, element['type'], key, dumps(data)))
        return eid

    def _relation(self, db, subject, predicate, obj, origin):
        rid = uuid.uuid5(uuid.NAMESPACE_URL, 'culture:' + subject + predicate + obj).hex
        db.execute('INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?)', (rid, subject, predicate, obj, dumps({'origin': origin, 'created_at': now()})))
        return rid

    def _evidence(self, db, chunk_id, block, key, quote, anchor, *, element_id=None, relation_id=None):
        eid = uuid.uuid5(uuid.NAMESPACE_URL, 'culture:' + chunk_id + ':' + key).hex
        data = {'quote': quote, 'source_id': block['source_id'], 'source_hash': block['source_hash'],
                **{k: anchor[k] for k in ('start', 'end', 'matched_quote', 'match_method', 'similarity', 'warnings')}}
        db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?)', (eid, element_id, relation_id, chunk_id, block['source_id'], dumps(data)))

    def annotate(self, value, element_id=None):
        """Manual add or edit. Curated wording is not source evidence and adds no quotes."""
        element, unknown = p.clean_element(value, self.themes())
        if unknown:
            raise ValueError('未知主题：' + '、'.join(unknown))
        key = p.norm(element['label'])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            clash = db.execute('SELECT id FROM elements WHERE type=? AND norm=?', (element['type'], key)).fetchone()
            if clash and clash['id'] != element_id:
                raise ValueError('同类型已有同名素材，请编辑原素材')
            if element_id:
                row = db.execute('SELECT data FROM elements WHERE id=?', (element_id,)).fetchone()
                if row is None:
                    raise ValueError('未知素材')
                old = json.loads(row['data'])
                if element['type'] != old['type'] and db.execute('SELECT 1 FROM relations WHERE subject=? OR object=?', (element_id, element_id)).fetchone():
                    raise ValueError('素材已有关系，不能修改类型')
                db.execute('UPDATE elements SET type=?, norm=?, data=? WHERE id=?',
                           (element['type'], key, dumps({**old, **element, 'edited_at': now()}), element_id))
                return element_id
            eid = uuid.uuid4().hex
            db.execute("INSERT INTO elements VALUES (?,?,?,'published',?)",
                       (eid, element['type'], key, dumps({**element, 'id': eid, 'origin': 'manual', 'created_at': now()})))
            return eid

    def relate(self, subject, predicate, obj):
        rule = p.PREDICATES.get(predicate)
        with self.store.connect() as db:
            types = {r['id']: r['type'] for r in db.execute('SELECT id, type FROM elements WHERE id IN (?,?)', (subject, obj))}
            if not rule or subject == obj or types.get(subject) not in rule[1] or types.get(obj) not in rule[2]:
                raise ValueError('关系不符合素材类型约束')
            return self._relation(db, subject, predicate, obj, 'manual')

    def set_status(self, element_id, status):
        if status not in ('published', 'rejected'):
            raise ValueError('素材状态无效')
        with self.store.connect() as db:
            if db.execute('UPDATE elements SET status=? WHERE id=?', (status, element_id)).rowcount == 0:
                raise ValueError('未知素材')

    def _load(self, status=None):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM elements' + (' WHERE status=?' if status else ''), (status,) if status else ()).fetchall()
            names = {r['id']: json.loads(r['data'])['label'] for r in db.execute('SELECT id, data FROM elements')}
            relations = db.execute('SELECT * FROM relations').fetchall()
            evidence = db.execute('SELECT * FROM evidence ORDER BY rowid').fetchall()
            samples = {r['id']: json.loads(r['data']) for r in db.execute('SELECT * FROM samples')}
        items = {r['id']: {**json.loads(r['data']), 'id': r['id'], 'type': r['type'], 'status': r['status'],
                           'relations': [], 'evidence': [], 'samples': []} for r in rows}
        quotes = {}
        for e in evidence:
            data = {**json.loads(e['data']), 'sample_id': e['sample_id'], 'sample': samples.get(e['sample_id'], {}).get('title', '')}
            if e['relation_id']:
                quotes.setdefault(e['relation_id'], []).append(data['quote'])
            elif e['element_id'] in items:
                items[e['element_id']]['evidence'].append(data)
        for r in relations:
            base = {'id': r['id'], 'predicate': r['predicate'], 'label': p.PREDICATES[r['predicate']][0],
                    'origin': json.loads(r['data'])['origin'], 'quotes': quotes.get(r['id'], [])}
            if r['subject'] in items:
                items[r['subject']]['relations'].append({**base, 'direction': 'out', 'other_id': r['object'], 'other': names.get(r['object'], '')})
            if r['object'] in items:
                items[r['object']]['relations'].append({**base, 'direction': 'in', 'other_id': r['subject'], 'other': names.get(r['subject'], '')})
        for item in items.values():
            seen = {}
            for e in item['evidence']:
                seen.setdefault(e['sample_id'], {'id': e['sample_id'], 'title': e['sample']})
            item['samples'] = list(seen.values())
        return items

    def elements(self, *, status='published', kinds=None, theme=None, sample_id=None):
        values = list(self._load(status).values())
        if kinds:
            values = [i for i in values if i['type'] in kinds]
        if theme == '未分主题':
            values = [i for i in values if not i['themes']]
        elif theme:
            values = [i for i in values if theme in i['themes']]
        if sample_id:
            values = [i for i in values if any(s['id'] == sample_id for s in i['samples'])]
        order = list(p.TYPES)
        return sorted(values, key=lambda i: (order.index(i['type']), i['label']))

    def detail(self, element_id):
        return self._load().get(element_id)

    def stats(self):
        items = self._load('published').values()
        with self.store.connect() as db:
            samples = db.execute('SELECT COUNT(DISTINCT sample_id) FROM evidence').fetchone()[0]
        return {'elements': len(items), 'by_type': Counter(i['type'] for i in items), 'samples': samples, 'themes': len(self.themes())}

    async def sync(self, index):
        """Bring the derived vector index in line with published elements; unchanged cards are skipped."""
        items = self._load('published')
        texts = {eid: index_text(item) for eid, item in items.items()}
        payloads = {eid: {'type': i['type'], 'themes': i['themes'], 'basis': i['basis'], 'label': i['label']} for eid, i in items.items()}
        hashes = {eid: digest(dumps([texts[eid], payloads[eid]])) for eid in items}
        with self.store.connect() as db:
            indexed = {r[0]: r[1] for r in db.execute('SELECT element_id, hash FROM indexed')}
        if index.count() != len(indexed):
            # The collection was removed or diverged outside this service: rebuild it from SQLite.
            index.reset()
            with self.store.connect() as db:
                db.execute('DELETE FROM indexed')
            indexed = {}
        removed = [eid for eid in indexed if eid not in items]
        changed = [eid for eid in items if indexed.get(eid) != hashes[eid]]
        if removed:
            index.delete(removed)
            with self.store.connect() as db:
                db.executemany('DELETE FROM indexed WHERE element_id=?', [(e,) for e in removed])
        for start in range(0, len(changed), 32):
            part = changed[start:start + 32]
            await index.upsert([(eid, texts[eid], payloads[eid]) for eid in part])
            with self.store.connect() as db:
                db.executemany('INSERT OR REPLACE INTO indexed VALUES (?,?)', [(eid, hashes[eid]) for eid in part])
        return {'indexed': len(items), 'updated': len(changed), 'removed': len(removed)}

    async def search(self, index, query, *, limit=20, kinds=None, themes=None):
        await self.sync(index)
        items = self._load('published')
        return [(items[eid], score) for eid, score in await index.search(query, limit=limit, kinds=kinds, themes=themes) if eid in items]

    async def context(self, index, query, hint='', limit=8):
        """Task-internal retrieval for demand and design: theme-filtered hits first, then open search."""
        await self.sync(index)
        themes = matched_themes(self.themes(), hint)
        hits = await index.search(query, limit=limit, themes=themes) if themes else []
        if len(hits) < limit:
            seen = {eid for eid, _ in hits}
            hits += [h for h in await index.search(query, limit=limit) if h[0] not in seen][:limit - len(hits)]
        items = self._load('published')
        return [evidence_entry(items[eid], score) for eid, score in hits if eid in items]

    def export(self, format='json'):
        records = [{'id': i['id'], 'type': i['type'], 'label': i['label'], 'description': i['description'], 'basis': i['basis'],
                    'themes': i['themes'], 'colors': i['colors'], 'origin': i['origin'],
                    'relations': [{k: r[k] for k in ('direction', 'predicate', 'label', 'other', 'origin', 'quotes')} for r in i['relations']],
                    'evidence': [{k: e[k] for k in ('quote', 'sample', 'source_id', 'match_method')} for e in i['evidence']]}
                   for i in self.elements()]
        if format == 'jsonl':
            return ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records)
        if format != 'json':
            raise ValueError('不支持的导出格式')
        with self.store.connect() as db:
            samples = [json.loads(r['data']) for r in db.execute('SELECT data FROM samples ORDER BY rowid')]
        metadata = {'export_version': EXPORT_VERSION, 'decode_prompt': p.VERSION, 'exported_at': now(),
                    'types': p.TYPES, 'bases': p.BASES, 'predicates': {k: v[0] for k, v in p.PREDICATES.items()}}
        return json.dumps({'metadata': metadata, 'themes': self.themes(), 'samples': samples, 'elements': records}, ensure_ascii=False, indent=2)
