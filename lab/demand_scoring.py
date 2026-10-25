"""Built-in festival and consumption calendar plus a transparent, user-weighted prediction score.

The score combines timeliness, node proximity, model-judged fit and user-entered heat.
It is a planning aid for ranking topics, not measured popularity or a sales forecast.
"""
from datetime import date, timedelta
import json
import threading
import uuid

NOTE = '预测评分，非真实热度'
WEIGHTS = {'timeliness': 0.25, 'proximity': 0.3, 'fit': 0.25, 'heat': 0.2}
LABELS = {'timeliness': '时效', 'proximity': '节点临近', 'fit': '能力关联', 'heat': '人工热度'}
KINDS = ('节日', '消费节点')
WINDOW_DAYS = 120
DEFAULT_LEAD = 30
PEAK_DAYS = 45

# Lunar festivals and solar terms checked against public calendars in 2026-10.
# Extend this table before 2028; rule-based dates are computed below.
LUNAR = [('dongzhi-2026', '冬至', '2026-12-22'), ('laba-2027', '腊八节', '2027-01-15'),
         ('chuxi-2027', '除夕', '2027-02-05'), ('chunjie-2027', '春节', '2027-02-06'),
         ('yuanxiao-2027', '元宵节', '2027-02-20'), ('qingming-2027', '清明节', '2027-04-05'),
         ('duanwu-2027', '端午节', '2027-06-09'), ('qixi-2027', '七夕节', '2027-08-08'),
         ('zhongqiu-2027', '中秋节', '2027-09-15'), ('chongyang-2027', '重阳节', '2027-10-08'),
         ('dongzhi-2027', '冬至', '2027-12-22')]
SOLAR = [('yuandan', '元旦', 1, 1, '节日', ''), ('valentine', '情人节', 2, 14, '节日', ''),
         ('womens', '妇女节', 3, 8, '节日', ''), ('labor', '劳动节', 5, 1, '节日', ''),
         ('wedding-may', '五一婚礼季', 5, 1, '消费节点', '婚礼旺季起点，日期为估算'),
         ('520', '520', 5, 20, '消费节点', '表白与婚恋相关消费'), ('children', '儿童节', 6, 1, '节日', ''),
         ('618', '618 购物节', 6, 18, '消费节点', '电商大促'), ('graduation', '毕业季', 6, 20, '消费节点', '6 月中下旬，日期为估算'),
         ('school', '开学季', 9, 1, '消费节点', '校服、文具与校园用品'), ('teachers', '教师节', 9, 10, '节日', ''),
         ('national', '国庆节', 10, 1, '节日', ''), ('wedding-oct', '国庆婚礼季', 10, 1, '消费节点', '婚礼旺季，日期为估算'),
         ('double11', '双十一', 11, 11, '消费节点', '电商大促'), ('double12', '双十二', 12, 12, '消费节点', '电商大促'),
         ('christmas', '圣诞节', 12, 25, '节日', '')]
FIRST_DAY = '2026-11-01'


def nth_sunday(year, month, n):
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def builtins():
    nodes = [{'key': k, 'name': n, 'date': d, 'kind': '节日', 'note': '', 'origin': 'builtin'} for k, n, d in LUNAR]
    for year in (2026, 2027):
        for key, name, month, day, kind, note in SOLAR:
            nodes.append({'key': f'{key}-{year}', 'name': name, 'date': date(year, month, day).isoformat(),
                          'kind': kind, 'note': note, 'origin': 'builtin'})
        nodes.append({'key': f'mothers-{year}', 'name': '母亲节', 'date': nth_sunday(year, 5, 2).isoformat(), 'kind': '节日', 'note': '5 月第二个星期日', 'origin': 'builtin'})
        nodes.append({'key': f'fathers-{year}', 'name': '父亲节', 'date': nth_sunday(year, 6, 3).isoformat(), 'kind': '节日', 'note': '6 月第三个星期日', 'origin': 'builtin'})
    return sorted((n for n in nodes if n['date'] >= FIRST_DAY), key=lambda n: (n['date'], n['key']))


class Calendar:
    """Workspace edits to the built-in calendar. Tasks snapshot a node when it is selected."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    def _data(self):
        if not self.path.exists():
            return {'added': [], 'removed': []}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def nodes(self):
        with self.lock:
            data = self._data()
        removed = set(data['removed'])
        return sorted([n for n in builtins() if n['key'] not in removed] + data['added'], key=lambda n: (n['date'], n['key']))

    def get(self, key):
        return next((n for n in self.nodes() if n['key'] == key), None)

    def upcoming(self, today, days=WINDOW_DAYS):
        start, end = today.isoformat(), (today + timedelta(days=days)).isoformat()
        return [n for n in self.nodes() if start <= n['date'] <= end]

    def add(self, name, day, kind, note=''):
        name = name.strip() if isinstance(name, str) else ''
        note = note.strip() if isinstance(note, str) else ''
        if not 1 <= len(name) <= 30 or len(note) > 200 or kind not in KINDS:
            raise ValueError('节点名称需为 1–30 字，说明不超过 200 字，类型为节日或消费节点。')
        try:
            day = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError):
            raise ValueError('请填写有效日期。') from None
        node = {'key': 'custom-' + uuid.uuid4().hex[:8], 'name': name, 'date': day, 'kind': kind, 'note': note, 'origin': 'custom'}
        with self.lock:
            data = self._data()
            data['added'].append(node)
            self._save(data)
        return node

    def remove(self, key):
        with self.lock:
            data = self._data()
            if any(n['key'] == key for n in data['added']):
                data['added'] = [n for n in data['added'] if n['key'] != key]
            elif any(n['key'] == key for n in builtins()) and key not in data['removed']:
                data['removed'].append(key)
            else:
                raise ValueError('节点不存在。')
            self._save(data)


def calendar_topic(node):
    return {'id': 'N-' + node['key'], 'kind': 'calendar', 'title': node['name'], 'date': node['date'],
            'date_status': 'calendar', 'node_kind': node['kind'],
            'fit': node['note'] or f"{node['kind']}：{node['name']}", 'heat': '热度未验证'}


def components(topic, lead_days, heat, today):
    """Each present component is in [0, 1]; missing components are omitted, not zero.
    heat is None when the user has rated no topic, so manual heat then stays out of every score."""
    parts, flags = {}, []
    kind = topic.get('kind', 'search')
    if kind == 'search':
        parts['timeliness'] = {'verified': 1.0, 'unverified': 0.5}.get(topic.get('date_status'), 0.0)
        if type(topic.get('fit_score')) is int and 1 <= topic['fit_score'] <= 5:
            parts['fit'] = (topic['fit_score'] - 1) / 4
    elif topic.get('date'):
        lead = lead_days or DEFAULT_LEAD
        if not lead_days:
            flags.append(f'未设置交期，按 {DEFAULT_LEAD} 天估算')
        days = (date.fromisoformat(topic['date'][:10]) - today).days
        if days < 0:
            parts['proximity'] = 0.0
            flags.append('节点已过')
        elif days < lead:
            parts['proximity'] = 0.0
            flags.append('交期不足')
        elif days <= lead + PEAK_DAYS:
            parts['proximity'] = 1.0
        else:
            parts['proximity'] = max(0.0, 1 - (days - lead - PEAK_DAYS) / 90)
    if heat is not None:
        parts['heat'] = heat / 5
    return parts, flags


def score(topic, run, today):
    scoring = run.get('scoring', {})
    weights = {**WEIGHTS, **scoring.get('weights', {})}
    capacity = run['inputs'].get('capacity') or {}
    rated = scoring.get('heat', {})
    # Once any topic is rated, unrated topics count as 0 so a rating can actually promote a topic.
    parts, flags = components(topic, capacity.get('lead_days'), rated.get(topic['id'], 0) if rated else None, today)
    total = sum(weights[k] for k in parts)
    value = round(sum(weights[k] * v for k, v in parts.items()) / total, 3) if total else None
    return {'score': value, 'components': {k: round(v, 3) for k, v in parts.items()},
            'weights': {k: weights[k] for k in WEIGHTS}, 'flags': flags, 'note': NOTE}


def check(weights, heat):
    if not isinstance(weights, dict) or set(weights) != set(WEIGHTS):
        raise ValueError('请为四个评分分量提供权重。')
    for v in weights.values():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ValueError('权重需在 0–1 之间。')
    if sum(weights.values()) <= 0:
        raise ValueError('权重不能全为 0。')
    if not isinstance(heat, dict) or any(not isinstance(k, str) or type(v) is not int or not 1 <= v <= 5 for k, v in heat.items()):
        raise ValueError('人工热度需为 1–5 的整数；0 表示未填写。')
    return {k: float(v) for k, v in weights.items()}, dict(heat)
