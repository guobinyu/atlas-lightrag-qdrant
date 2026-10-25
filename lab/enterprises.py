"""Reusable merchant profiles. Selecting one only prefills a task; tasks keep their own copy."""
import json
import threading
import uuid

from lab.sources import utc_now

TEXT_FIELDS = {'name': ('企业名称', 60, True), 'industry': ('行业', 4000, True), 'capabilities': ('生产能力', 4000, True),
               'constraints': ('已知限制', 4000, False), 'brand': ('品牌名称', 4000, False)}


def clean(value):
    from lab.demands import clean_structured
    if not isinstance(value, dict):
        raise ValueError('企业档案格式无效。')
    result = {}
    for key, (label, limit, required) in TEXT_FIELDS.items():
        v = value.get(key, '')
        if not isinstance(v, str) or len(v) > limit or (required and not v.strip()):
            raise ValueError(f'{label}{"为必填项，" if required else ""}需为不超过 {limit} 字的文本。')
        result[key] = v.strip()
    result['capacity'] = clean_structured({'capacity': value.get('capacity') or {}}).get('capacity', {})
    return result


class Enterprises:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    def _read(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []

    def _write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def list(self):
        with self.lock:
            return sorted(self._read(), key=lambda e: e['name'])

    def get(self, identifier):
        return next((e for e in self.list() if e['id'] == identifier), None)

    def save(self, value, identifier=None):
        item = clean(value)
        with self.lock:
            items = self._read()
            if any(e['name'] == item['name'] and e['id'] != identifier for e in items):
                raise ValueError('已有同名企业档案。')
            if identifier:
                old = next((e for e in items if e['id'] == identifier), None)
                if old is None:
                    raise ValueError('企业档案不存在。')
                old.update(item, updated_at=utc_now())
                saved = old
            else:
                saved = {**item, 'id': uuid.uuid4().hex, 'created_at': utc_now()}
                items.append(saved)
            self._write(items)
            return saved

    def remove(self, identifier):
        with self.lock:
            items = self._read()
            if not any(e['id'] == identifier for e in items):
                raise ValueError('企业档案不存在。')
            self._write([e for e in items if e['id'] != identifier])

    @staticmethod
    def task_fields(profile):
        """Fields copied into a demand task. The profile name is not a brand and is not copied."""
        return {k: profile[k] for k in ('industry', 'capabilities', 'constraints', 'brand')} | {'capacity': dict(profile['capacity'])}

    @staticmethod
    def from_task(name, inputs):
        return {'name': name, **{k: inputs.get(k, '') for k in ('industry', 'capabilities', 'constraints', 'brand')},
                'capacity': dict(inputs.get('capacity') or {})}
