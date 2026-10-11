"""Readable demo deliverables, lightweight completeness checks, and exports."""
import math
import re
from copy import deepcopy

PROMPT = '''PLAN：你是营销策划负责人，请直接交付可演示、可执行的完整提案，只输出 JSON。
先在 strategy 中做出选择：话题机会→受众需求→产品价值→行动转化，再展开创意和内容。不要复述检索过程或拼贴资料。
用户需求与本次修改优先；constraints 是从需求提取的执行条件。未提供时周期7天、预算人民币30000元、选择两个合适渠道。
若有 base_version，保留未被要求修改的策略和条件。品牌、产品、人群由已提供资料或策划设定补齐，不要求用户输入或确认。
预算是演示估算，必须给出金额与分配；指标必须有具体示例目标和触发调整的条件，不承诺效果。
必须输出两篇可以直接发布的完整图文（每篇正文150—300字，包含具体场景、产品价值和行动引导），以及一条30秒左右短视频逐镜脚本。
每一天都有执行任务，每条内容作品至少安排发布一次，排期引用对应 content_ids。预算分项之和等于概览总预算。
正文不要出现S1、I3/K3等引用编号、【判断】、date_status、unverified、资料记载等内部处理措辞。来源由界面的推荐依据单独承载。
集中在 assumptions 中说明策划设定，不在各段重复免责声明。未知日期只作趋势参考；无话题时标题明确写常规内容方案，不编造新闻或来源。
不得把合成品牌说成真实品牌，不使用未经支持的认证、功效、销量或虚假背书。
严格按以下结构返回（数组示例是字段说明，必须填满实际内容）：
{
 "content_version":2,"title":"面向客户的方案标题",
 "overview":{"brand":"品牌","brand_basis":"资料已有品牌/策划品牌","product":"主推产品","selling_points":"面向消费者的卖点","audience":"具体人群","goal":"传播目标","duration_days":7,"budget_total":30000,"channels":["渠道一","渠道二"]},
 "strategy":{"opportunity":"为什么借势这个话题","audience_need":"消费者场景与需求","product_value":"产品如何回应需求","conversion":"内容如何推动参与或咨询"},
 "theme":{"name":"创意主题","slogan":"一句传播口号","activity":"核心活动的具体玩法","participation":"用户如何参与，下一步做什么"},
 "posts":[{"id":"P1","channel":"渠道一","title":"可发布标题","body":"完整图文正文，不是写作建议","cta":"行动引导"},{"id":"P2","channel":"渠道二","title":"可发布标题","body":"另一篇完整图文正文","cta":"行动引导"}],
 "video":{"id":"V1","channel":"已选择的渠道","title":"视频主题","scenes":[{"time":"0—5秒","visual":"镜头画面与动作","voiceover":"实际台词","subtitle":"屏幕字幕"}],"cta":"视频结尾行动引导"},
 "schedule":[{"day":1,"channel":"已选择的渠道","action":"具体动作","deliverable":"交付物","owner":"负责角色","content_ids":["P1"]}],
 "budget":[{"category":"费用分类","amount":10000,"purpose":"用途"}],
 "metrics":[{"metric":"指标","target":"具体数字目标（演示）","adjustment":"具体触发条件与调整动作"}],
 "assumptions":["集中说明模型补充的条件"]
}
'''


def number(text):
    try:
        return float(text)
    except ValueError:
        digits = dict(zip('零一二三四五六七八九两', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
        if '十' in text:
            a, b = text.split('十', 1)
            return digits.get(a, 1) * 10 + digits.get(b, 0)
        return digits.get(text)


def extract_conditions(text):
    result = {}
    amount = re.findall(r'(?:预算|经费)[^\d零一二三四五六七八九十两\n，。；]{0,12}([\d.]+|[零一二三四五六七八九十两]+)\s*(万|千)?', text)
    if amount:
        value, unit = amount[-1]
        parsed = number(value)
        if parsed is not None:
            result['budget_total'] = parsed * {'万': 10000, '千': 1000}.get(unit, 1)
    duration = re.findall(r'(?:周期|为期|持续|排期|缩短到|延长到|活动安排)[^\d一二三四五六七八九十两\n，。；]{0,8}([\d]+|[一二三四五六七八九十两]+)\s*(天|周)', text)
    if not duration:
        duration = re.findall(r'([\d]+|[一二三四五六七八九十两]+)\s*(天|周)(?:的)?(?:传播|推广|营销|活动|计划|方案)', text)
    if duration:
        value, unit = duration[-1]
        parsed = number(value)
        if parsed is not None:
            result['duration_days'] = int(parsed) * (7 if unit == '周' else 1)
    return result


def constraints(run, op, base=None):
    values = {'duration_days': 7, 'budget_total': 30000}
    inputs = run['inputs']
    values.update(extract_conditions(inputs.get('request', '')))
    if inputs.get('budget'):
        values.update(extract_conditions('预算' + inputs['budget']))
    if base and base['plan'].get('content_version') == 2:
        overview = base['plan'].get('overview', {})
        values.update({k: overview[k] for k in values if k in overview})
    values.update(extract_conditions(op.get('revision', '')))
    return values


def clean_text(text):
    """Remove internal citation notation; the evidence snapshot remains untouched."""
    text = re.sub(r'【(?:判断|创意|事实)】\s*', '', text)
    return re.sub(r'(?<![A-Za-z0-9])\[?(?:[SIKB]\d+)(?:\s*[/·、]\s*[SIKB]\d+)*\]?(?![A-Za-z0-9])', '', text).strip()


def clean_plan(value):
    if isinstance(value, dict):
        return {key: clean_plan(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_plan(item) for item in value]
    return clean_text(value) if isinstance(value, str) else value


def content_errors(plan, required):
    errors = []
    def texts(obj, keys, label):
        if not isinstance(obj, dict) or any(not isinstance(obj.get(k), str) or not obj[k].strip() or obj[k].strip() in ('待确认', '待补充', '待定') for k in keys):
            errors.append(label + '不完整')
    def rows(key, minimum):
        value = plan.get(key)
        if not isinstance(value, list) or len(value) < minimum or any(not isinstance(r, dict) for r in value):
            errors.append(key + '内容不足')
            return []
        return value
    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    if plan.get('content_version') != 2:
        errors.append('缺少新版方案内容格式')
    texts(plan, ['title'], '标题')
    overview = plan.get('overview') if isinstance(plan.get('overview'), dict) else {}
    texts(overview, ['brand', 'brand_basis', 'product', 'selling_points', 'audience', 'goal'], '策划概览')
    days, total = overview.get('duration_days'), overview.get('budget_total')
    if type(days) is not int or days < 1 or days != required['duration_days']:
        errors.append('周期必须符合执行条件')
    if not numeric(total) or total != required['budget_total']:
        errors.append('预算必须符合执行条件')
    channels = overview.get('channels', [])
    if not isinstance(channels, list) or not channels or any(not isinstance(c, str) or not c.strip() for c in channels):
        errors.append('缺少传播渠道')
        channels = []
    texts(plan.get('strategy'), ['opportunity', 'audience_need', 'product_value', 'conversion'], '核心策略')
    texts(plan.get('theme'), ['name', 'slogan', 'activity', 'participation'], '创意主题')
    posts = rows('posts', 2)
    for post in posts:
        texts(post, ['id', 'channel', 'title', 'body', 'cta'], '图文作品')
        if len(str(post.get('body', ''))) < 100:
            errors.append('图文需要完整正文，不能只给内容方向')
    video = plan.get('video') if isinstance(plan.get('video'), dict) else {}
    texts(video, ['id', 'channel', 'title', 'cta'], '短视频')
    scenes = video.get('scenes')
    if not isinstance(scenes, list) or len(scenes) < 3:
        errors.append('短视频至少包含三个镜头')
    else:
        for scene in scenes:
            texts(scene, ['time', 'visual', 'voiceover', 'subtitle'], '视频镜头')
    assets = posts + [video]
    ids = [a.get('id') for a in assets]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(i for i in ids if isinstance(i, str))) != len(ids):
        errors.append('内容作品编号缺失或重复')
    for asset in assets:
        if asset.get('channel') not in channels:
            errors.append('内容渠道需与概览一致')
    scheduled, covered = set(), set()
    for row in rows('schedule', 1):
        texts(row, ['channel', 'action', 'deliverable', 'owner'], '排期')
        day = row.get('day')
        if type(day) is not int or type(days) is not int or not 1 <= day <= days:
            errors.append('排期超出传播周期')
        else:
            covered.add(day)
        refs = row.get('content_ids')
        if not isinstance(refs, list) or any(not isinstance(i, str) or i not in ids for i in refs):
            errors.append('排期引用的内容作品不存在')
        else:
            scheduled.update(refs)
    if type(days) is int and len(covered) != days:
        errors.append('传播周期内每天需要有执行安排')
    if any(i not in scheduled for i in ids if isinstance(i, str)):
        errors.append('所有内容作品都需要安排发布')
    budget = rows('budget', 1)
    for row in budget:
        texts(row, ['category', 'purpose'], '预算分配')
    if any(not numeric(r.get('amount')) for r in budget) or not numeric(total) or abs(sum(r.get('amount', 0) for r in budget if numeric(r.get('amount'))) - (total if numeric(total) else 0)) > .01:
        errors.append('预算分项金额之和必须等于总预算')
    for row in rows('metrics', 2):
        texts(row, ['metric', 'target', 'adjustment'], '目标与调整')
        if not re.search(r'\d', str(row.get('target', ''))):
            errors.append('演示目标需要具体数字')
    if not isinstance(plan.get('assumptions'), list) or not plan['assumptions'] or any(not isinstance(a, str) for a in plan['assumptions']):
        errors.append('缺少策划假设')
    return list(dict.fromkeys(errors))


def video_text(video):
    lines = [video.get('title', '')]
    for scene in video.get('scenes', []) if isinstance(video.get('scenes'), list) else []:
        if isinstance(scene, dict):
            lines += [f"{scene.get('time', '')}｜画面：{scene.get('visual', '')}", f"台词：{scene.get('voiceover', '')}", f"字幕：{scene.get('subtitle', '')}"]
    return '\n'.join(str(line) for line in [*lines, '行动引导：' + str(video.get('cta', ''))])


def table(headers, rows):
    def cell(value):
        return str(value).replace('|', '／').replace('\n', '<br>')
    return ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |', *['| ' + ' | '.join(cell(c) for c in row) + ' |' for row in rows]]


def markdown(plan):
    lines = ['# ' + plan.get('title', '营销方案'), '', '演示方案 · 以下预算与目标为策划示例。', '', '## 策划概览']
    overview = plan.get('overview', {})
    for key, label in [('brand', '品牌'), ('brand_basis', '品牌设定'), ('product', '主推产品'), ('selling_points', '产品卖点'), ('audience', '目标人群'), ('goal', '传播目标'), ('duration_days', '周期（天）'), ('budget_total', '预算（元）')]:
        lines.append(f"- {label}：{overview.get(key, '')}")
    lines += ['', '## 核心策略']
    for key, label in [('opportunity', '话题机会'), ('audience_need', '人群需求'), ('product_value', '产品价值'), ('conversion', '行动转化')]:
        lines += ['', f"**{label}**", plan.get('strategy', {}).get(key, '')]
    lines += ['', '## 创意主题']
    for key, label in [('name', '主题'), ('slogan', '口号'), ('activity', '活动'), ('participation', '参与方式')]:
        lines += ['', f"**{label}**：{plan.get('theme', {}).get(key, '')}"]
    lines += ['', '## 渠道与内容']
    for post in plan.get('posts', []):
        lines += ['', f"### {post.get('channel', '')} · {post.get('title', '')}", '', post.get('body', ''), '', post.get('cta', '')]
    lines += ['', '### 短视频脚本', '', video_text(plan.get('video', {})), '', '## 执行排期']
    lines += table(['日期', '渠道', '动作', '交付物', '负责角色'], [[f"第{r.get('day')}天", *[r.get(k, '') for k in ('channel', 'action', 'deliverable', 'owner')]] for r in plan.get('schedule', [])])
    lines += ['', '## 预算分配'] + table(['分类', '金额（元）', '用途'], [[r.get(k, '') for k in ('category', 'amount', 'purpose')] for r in plan.get('budget', [])])
    lines += ['', '## 目标与调整（演示目标）'] + table(['指标', '目标', '调整动作'], [[r.get(k, '') for k in ('metric', 'target', 'adjustment')] for r in plan.get('metrics', [])])
    lines += ['', '## 策划假设', *['- ' + a for a in plan.get('assumptions', [])]]
    return '\n'.join(str(line) for line in lines)


def display_plan(plan):
    """Tolerate partial model objects in drafts without hiding valid sections."""
    value = deepcopy(plan)
    value['title'] = str(value.get('title') or '营销方案（待完善）')
    for key in ('overview', 'strategy', 'theme', 'video'):
        if not isinstance(value.get(key), dict):
            value[key] = {}
    for key in ('posts', 'schedule', 'budget', 'metrics'):
        value[key] = [r for r in value.get(key, []) if isinstance(r, dict)] if isinstance(value.get(key), list) else []
    value['assumptions'] = [a for a in value.get('assumptions', []) if isinstance(a, str)] if isinstance(value.get('assumptions'), list) else []
    return value
