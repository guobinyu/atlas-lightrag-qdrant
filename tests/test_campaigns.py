"""Deterministic workflow tests; model/search doubles are not live quality evidence."""
import asyncio
from concurrent.futures import Future
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest
from campaign_fixture import content_plan

from lab.campaigns import CampaignService, SECTIONS, export_markdown, plan_errors
from lab.campaign_brand import FIELDS
from lab.trends import date_value, metadata_dates, verify_date
from lab.workspace import Workspace

NOW = '2026-10-10T12:00:00+08:00'
BRAND = {'name': '丝绸测试品牌', 'product': '日常丝巾', 'audience': '', 'constraints': ''}
INPUTS = {'mode': 'strict', 'request': '搜索丝绸近期话题并设计传播方案', 'days': 7, 'goal': '品牌曝光', 'channels': '', 'budget': ''}
TEXT = '2026年10月09日举办丝绸展览。织造工艺的展出展示丝绸服饰的日常应用。' * 3


def profile():
    values = {'name': '织光（策划品牌）', 'product': '日常丝巾', 'selling_points': '织造故事与多场景搭配', 'audience': '日常穿搭爱好者',
              'positioning': '东方美学的日常表达', 'scenarios': '通勤、城市漫步', 'constraints': '不宣称未经证实的工艺认证'}
    values['basis'] = [{'field': key, 'kind': '策划推断', 'reason': '由丝绸展览话题推理策划方向', 'citations': []} for key in FIELDS]
    return values


def plan():
    return {'title': '丝绸日常', 'sections': [{'name': n, 'items': [{'kind': '创意', 'text': '用丝巾展示日常搭配', 'refs': []}]} for n in SECTIONS], 'assumptions': ['预算待确认']}


class Model:
    def __init__(self):
        self.settings = SimpleNamespace(validate=lambda: None, api_key='TEST_CAMPAIGN_SECRET')
        self.configured = True
        self.calls = []
        self.fail = None
        self.invalid = False
        self.supported = True
        self.conflicts = []
        self.profile = profile()
        self.brand_supported = []
        self.prompts = []

    async def __call__(self, prompt, **kwargs):
        stage = prompt.split('：')[0]
        self.calls.append(stage)
        self.prompts.append((stage, prompt))
        if self.fail == stage:
            raise ValueError('TEST_CAMPAIGN_SECRET 模型故障')
        if stage == 'SEARCH':
            result = {'queries': ['丝绸展览']}
        elif stage == 'EXTRACT':
            result = {'title': '丝绸展览', 'summary': '展览展示丝绸工艺', 'published': None, 'event': {'date': '2026-10-09', 'quote': '2026年10月09日举办丝绸展览。'}, 'original_publisher': '未知'}
        elif stage == 'GROUP':
            result = {'groups': [{'title': '丝绸展览', 'source_ids': ['S1']}]}
        elif stage == 'INFER_BRAND':
            result = self.profile
        elif stage == 'VERIFY_BRAND':
            result = {'supported_fields': self.brand_supported}
        elif stage == 'MATCH':
            result = {'grade': '推荐', 'brand_fit': '与丝巾工艺相关；品牌方向为策划推断，知识库为空', 'audience_fit': '关注工艺的人群', 'angle': '日常穿搭', 'constraints': '预算未确认', 'conflicts': self.conflicts}
        elif stage == 'PLAN':
            if self.invalid:
                return 'bad json'
            if 'content_version' in prompt:
                data = json.loads(prompt.split('输入数据：\n')[1])
                result = content_plan(data['constraints']['duration_days'], data['constraints']['budget_total'])
            else:
                result = plan()
        elif stage == 'VERIFY':
            result = {'supported': self.supported, 'issues': [] if self.supported else ['事实无依据']}
        else:
            raise AssertionError(stage)
        return json.dumps(result, ensure_ascii=False)


@pytest.fixture
def work(tmp_path, monkeypatch):
    import lab.campaigns as campaigns
    monkeypatch.setattr(campaigns, 'utc_now', lambda: NOW)
    monkeypatch.setattr(campaigns, 'search_web', lambda *a: [{'title': '展览', 'url': 'https://example.com/news'}])
    monkeypatch.setattr(campaigns, 'fetch_source', lambda url, title: {'text': TEXT, 'url': url, 'title': title, 'collected_at': NOW})
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ready = Future()
    ready.set_result(True)
    async def context(question):
        return []
    engine = SimpleNamespace(loop=loop, gate=threading.Lock(), ready=ready, campaign_context=context,
                             settings=SimpleNamespace(workspace='fixture', embedding_api_key='', qdrant_api_key=''))
    instance = Workspace.__new__(Workspace)
    instance.engine = engine
    instance.directory = tmp_path
    instance.job_dir = tmp_path / 'jobs'
    instance.job_dir.mkdir()
    instance.active = None
    instance.llm = Model()
    from lab.image_service import ImageService
    instance.images = ImageService(tmp_path / "image-settings.json")
    instance.campaigns = CampaignService(instance)
    engine.workbench = instance
    yield instance
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def start(work, kind='campaign_search', inputs=None):
    future, job = work.start(kind, deepcopy(inputs if inputs is not None else INPUTS))
    result = future.result(timeout=10)
    return result, work.campaigns.load(result['campaign_id'])


def test_full_flow_revision_exports_and_refresh_are_read_only(work):
    result, run = start(work)
    assert result['status'] == 'success', result
    assert len(run['sources']) == 1 and len(run['versions']) == 1
    assert run['topics'][0]['date_status'] == 'verified'
    old = deepcopy(run['versions'][0])
    before = len(work.llm.calls)
    assert work.campaigns.runs()[0]['versions'][0] == old
    assert len(work.llm.calls) == before
    result, revised = start(work, 'campaign_revise', {'run_id': run['id'], 'topic_id': 'T1', 'version_id': old['id'], 'revision': '增加小红书内容'})
    assert result['status'] == 'success'
    assert revised['versions'][0] == old
    assert revised['versions'][1]['base_version'] == old['id']
    assert work.llm.calls.count('SEARCH') == 1
    exported = export_markdown(revised, revised['versions'][-1])
    assert '增加小红书' in exported and '预算待确认' in exported and 'https://example.com/news' in exported


def test_failure_retry_resumes_saved_stages_and_redacts(work):
    work.llm.fail = 'MATCH'
    result, run = start(work)
    assert result['status'] == 'failed'
    assert 'TEST_CAMPAIGN_SECRET' not in json.dumps(result)
    assert 'TEST_CAMPAIGN_SECRET' not in json.dumps(run)
    assert not work.engine.gate.locked()
    work.llm.fail = None
    result, retried = start(work, 'campaign_retry', {'run_id': run['id'], 'operation_id': result['operation_id']})
    assert result['status'] == 'success'
    assert work.llm.calls.count('SEARCH') == 1
    assert work.llm.calls.count('EXTRACT') == 1
    assert len(retried['versions']) == 1


def test_verification_network_failure_does_not_repeat_generation(work):
    work.llm.fail = 'VERIFY'
    result, run = start(work)
    assert result['status'] == 'failed'
    work.llm.fail = None
    result, run = start(work, 'campaign_retry', {'run_id': run['id'], 'operation_id': result['operation_id']})
    assert result['status'] == 'success'
    assert work.llm.calls.count('PLAN') == 1


@pytest.mark.parametrize('invalid', [False, True])
def test_repair_limit_preserves_draft(work, invalid):
    work.llm.invalid = invalid
    work.llm.supported = False
    result, run = start(work)
    assert result['status'] == 'failed'
    assert work.llm.calls.count('PLAN') == 2
    assert run['versions'][0]['status'] == 'draft'
    assert run['versions'][0]['errors']
    assert '未完成草稿' in export_markdown(run, run['versions'][0]) or not invalid
    work.llm.invalid = False
    work.llm.supported = True
    result, revised = start(work, 'campaign_revise', {'run_id': run['id'], 'topic_id': 'T1', 'version_id': run['versions'][0]['id'], 'revision': '删除无依据事实'})
    assert result['status'] == 'success' and len(revised['versions']) == 2


def test_gate_has_no_phantom_jobs(work):
    work.engine.gate.acquire()
    try:
        with pytest.raises(ValueError, match='已有'):
            work.start('campaign_search', INPUTS)
        assert not work.jobs()
        assert not work.campaigns.runs()
    finally:
        work.engine.gate.release()


def test_interrupt_and_workspace_isolation(work, tmp_path):
    run, op = work.campaigns.prepare('campaign_search', INPUTS)
    work.campaigns.interrupt()
    assert work.campaigns.load(run['id'])['operations'][op['id']]['status'] == 'interrupted'
    with pytest.raises(ValueError):
        work.campaigns.load('../brand')
    work.engine.settings.workspace = 'other'
    with pytest.raises(ValueError, match='当前工作区'):
        work.campaigns.load(run['id'])


def test_all_search_failures_never_invent_topics(work, monkeypatch):
    import lab.campaigns as campaigns
    def fail(*args):
        raise ValueError('network')
    monkeypatch.setattr(campaigns, 'search_web', fail)
    result, run = start(work)
    assert result['status'] == 'failed' and not run['versions']
    assert 'EXTRACT' not in work.llm.calls


def test_duplicate_and_partial_fetch(work, monkeypatch):
    import lab.campaigns as campaigns
    monkeypatch.setattr(campaigns, 'search_web', lambda *a: [{'title': '展览', 'url': 'https://example.com/' + p} for p in ('one', 'copy', 'bad')])
    def fetch(url, title):
        if url.endswith('bad'):
            raise ValueError('unavailable')
        return {'text': TEXT, 'url': url, 'title': title, 'collected_at': NOW}
    monkeypatch.setattr(campaigns, 'fetch_source', fetch)
    result, run = start(work)
    assert result['status'] == 'partial'
    assert len(run['sources']) == 1 and len(run['sources'][0]['aliases']) == 1
    assert run['topics'][0]['source_ids'] == ['S1']


def test_old_and_missing_dates_do_not_generate(work, monkeypatch):
    import lab.campaigns as campaigns
    monkeypatch.setattr(campaigns, 'fetch_source', lambda url, title: {'text': '没有明确日期的丝绸展览信息' * 10, 'url': url, 'title': title, 'collected_at': NOW})
    result, run = start(work)
    assert result['status'] == 'success'
    assert run['topics'][0]['date_status'] == 'unverified' and not run['versions']
    assert 'PLAN' not in work.llm.calls


def test_conflict_retained_and_invalid_refs_rejected(work):
    work.llm.conflicts = ['产品工艺与知识库记录有冲突']
    _, run = start(work)
    assert run['matches']['T1']['conflicts'] == work.llm.conflicts
    draft = plan()
    draft['sections'][0]['items'][0] = {'kind': '事实', 'text': '不存在的事实', 'refs': ['S99']}
    assert '引用不存在或格式错误' in plan_errors(draft, {'B1'})


def test_evergreen_versions_can_be_revised(work):
    _, run = start(work)
    _, run = start(work, 'campaign_evergreen', {'run_id': run['id']})
    version = run['versions'][-1]
    result, run = start(work, 'campaign_revise', {'run_id': run['id'], 'topic_id': 'evergreen', 'version_id': version['id'], 'revision': '聚焦工艺'})
    assert result['status'] == 'success'


@pytest.mark.parametrize('date,days,status', [('2026-10-09',7,'verified'), ('2026-09-01',7,'excluded'), ('2026-10-11',7,'excluded'), ('2026-10-09',1,'unverified'), ('2026-10-09T12:00:00+08:00',1,'verified'), ('2026-10-09T11:59:59+08:00',1,'excluded')])
def test_date_precision_and_bounds(date, days, status):
    source = {'text': date}
    extracted = {'event': {'date': date, 'quote': date}}
    assert verify_date(source, extracted, NOW, days)['status'] == status


def test_dates_conflict_old_event_and_fabricated_evidence():
    source = {'text': '事件发生于2020年01月01日。发布时间2026年10月09日。', 'published_metadata': [{'value': '2026-10-08', 'evidence': 'meta'}]}
    extracted = {'published': {'date': '2026-10-09', 'quote': '发布时间2026年10月09日。'}}
    assert verify_date(source, extracted, NOW, 7)['status'] == 'unverified'
    extracted = {'event': {'date': '2020-01-01', 'quote': '事件发生于2020年01月01日。'}}
    assert verify_date(source, extracted, NOW, 7)['status'] == 'excluded'
    assert verify_date({'text': '2020年01月01日'}, {'event': {'date': '2026-10-09', 'quote': '2020年01月01日'}}, NOW, 7)['status'] == 'unverified'
    assert date_value('2026-13-99') is None


def test_metadata_ignores_modified_and_nested_unrelated_dates():
    body = b'<meta property="article:published_time" content="2026-10-09"><meta property="article:modified_time" content="2026-10-10"><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-10-08"}</script>'
    assert len(metadata_dates(body)) == 2


def test_campaign_ui_history_revision_and_export(work):
    from streamlit.testing.v1 import AppTest
    result, run = start(work)
    app = AppTest.from_string('''
import streamlit as st
from lab.campaign_ui import campaign_page
campaign_page(st.session_state.engine)
''')
    app.session_state.engine = work.engine
    app.run(timeout=20)
    assert not app.exception
    assert len(app.tabs) == 3
    assert len(app.get('download_button')) == 2
    before = len(work.llm.calls)
    app.run(timeout=20)
    assert len(work.llm.calls) == before
    next(t for t in app.text_area if t.label == '继续调整这份方案').set_value('增加小红书内容')
    next(b for b in app.button if b.label == '生成新版本').click().run(timeout=20)
    assert not app.exception
    # Observe background completion without sending another request.
    import time
    deadline = time.monotonic() + 5
    while work.active and time.monotonic() < deadline:
        time.sleep(.01)
    app.run(timeout=20)
    assert not app.exception
    assert len(work.campaigns.load(run['id'])['versions']) == 2
    assert next(s for s in app.selectbox if s.label == '方案版本').value == work.campaigns.load(run['id'])['versions'][-1]['id']


def test_campaign_ui_only_requires_request(work):
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
from lab.campaign_ui import campaign_page
campaign_page(st.session_state.engine)
''')
    app.session_state.engine = work.engine
    app.run(timeout=20)
    assert not any(t.label in ('品牌名称（必填）', '产品与卖点（必填）') for t in [*app.text_area, *app.text_input])
    next(b for b in app.button if b.label == '搜索并生成方案').click().run(timeout=20)
    assert not app.exception and app.error
    assert not work.campaigns.runs()
    next(t for t in app.text_area if t.label == '营销需求').set_value('帮我搜索一下近期的热点信息并设计营销传播方案')
    next(b for b in app.button if b.label == '搜索并生成方案').click().run(timeout=20)
    assert not app.exception
    assert len(work.campaigns.runs()) == 1
    import time
    deadline = time.monotonic() + 5
    while work.active and time.monotonic() < deadline:
        time.sleep(.01)
    app.run(timeout=20)
    assert not app.exception
    assert not next(b for b in app.button if b.label == '搜索并生成方案').disabled
    run = work.campaigns.runs()[0]
    assert run['brand_profile']['name'] == '织光（策划品牌）'
    assert len(run['versions']) == 1
    assert any(e.label == '自动推理的品牌与产品' for e in app.expander)
    assert 'brand' not in run['inputs']


def test_unsupported_event_cannot_fall_back_to_recent_publication():
    source = {'text': '2020年01月01日', 'published_metadata': [{'value': '2026-10-09', 'evidence': 'meta'}]}
    value = verify_date(source, {'event': {'date': '2026-10-09', 'quote': '2020年01月01日'}}, NOW, 7)
    assert value['status'] == 'unverified'


def test_date_precision_is_preserved_for_display():
    from lab.campaign_ui import local_time
    value = verify_date({'text': '2026年10月09日'}, {'event': {'date': '2026-10-09', 'quote': '2026年10月09日'}}, NOW, 7)
    assert value['precision'] == 'day'
    assert local_time(value['event_at'], value['event_precision']) == '2026-10-09'


def test_empty_search_and_no_evidence_never_calls_generation(work, monkeypatch):
    import lab.campaigns as campaigns
    monkeypatch.setattr(campaigns, 'search_web', lambda *args: [])
    result, run = start(work)
    assert result['status'] == 'success'
    assert run['topics'] == [] and run['versions'] == []
    assert work.llm.calls == ['SEARCH']


def test_brand_is_inferred_after_topics_and_flows_into_plan(work):
    result, run = start(work)
    assert result['status'] == 'success'
    assert run['schema_version'] == 2 and 'brand' not in run['inputs']
    assert work.llm.calls.index('GROUP') < work.llm.calls.index('INFER_BRAND') < work.llm.calls.index('MATCH') < work.llm.calls.index('PLAN')
    inference = next(p for stage, p in work.llm.prompts if stage == 'INFER_BRAND')
    assert TEXT in inference and 'S1' in inference
    generation = next(p for stage, p in work.llm.prompts if stage == 'PLAN')
    assert '织光（策划品牌）' in generation and '织造故事与多场景搭配' in generation
    assert run['versions'][0]['evidence']['B1']['inferred']
    assert '自动推理的品牌与产品' in export_markdown(run, run['versions'][0])
    assert not (work.campaigns.directory / 'brand.json').exists()


def test_stale_brand_file_not_used_for_new_tasks(work):
    work.campaigns.write(work.campaigns.directory / 'brand.json', BRAND)
    _, run = start(work)
    assert run['brand_profile']['name'] != BRAND['name']
    assert all(BRAND['name'] not in prompt for _, prompt in work.llm.prompts)


def test_brand_inference_retry_reuses_search_and_extraction(work):
    work.llm.fail = 'INFER_BRAND'
    result, run = start(work)
    assert result['status'] == 'failed' and result['phase'] == '推理品牌与产品'
    assert 'brand_profile' not in run and not run['versions']
    work.llm.fail = None
    result, run = start(work, 'campaign_retry', {'run_id': run['id'], 'operation_id': result['operation_id']})
    assert result['status'] == 'success'
    assert work.llm.calls.count('SEARCH') == 1 and work.llm.calls.count('EXTRACT') == 1
    assert work.llm.calls.count('GROUP') == 1


def test_completed_inference_not_repeated_on_downstream_retry(work):
    work.llm.fail = 'PLAN'
    result, run = start(work)
    original = deepcopy(run['brand_profile'])
    work.llm.fail = None
    result, run = start(work, 'campaign_retry', {'run_id': run['id'], 'operation_id': result['operation_id']})
    assert result['status'] == 'success' and run['brand_profile'] == original
    assert work.llm.calls.count('INFER_BRAND') == 1


@pytest.mark.parametrize('supported', [True, False])
def test_known_product_fact_verified_or_automatically_downgraded(work, supported):
    work.llm.profile['product'] = '丝绸服饰'
    item = next(i for i in work.llm.profile['basis'] if i['field'] == 'product')
    item.update(kind='资料提取', citations=[{'ref': 'S1', 'quote': '织造工艺的展出展示丝绸服饰的日常应用。'}])
    work.llm.brand_supported = ['product'] if supported else []
    result, run = start(work)
    assert result['status'] == 'success'
    actual = next(i for i in run['brand_profile']['basis'] if i['field'] == 'product')
    assert actual['kind'] == ('资料提取' if supported else '策划推断')
    assert len(run['versions']) == 1  # No human confirmation stops either path.


def test_invented_brand_citation_never_becomes_a_fact(work):
    item = work.llm.profile['basis'][0]
    item.update(kind='资料提取', citations=[{'ref': 'S99', 'quote': '不存在的品牌'}])
    result, run = start(work)
    assert result['status'] == 'success'
    basis = run['brand_profile']['basis'][0]
    assert basis['kind'] == '策划推断' and basis['citations'] == []
    assert 'VERIFY_BRAND' not in work.llm.calls


def test_brand_verification_retry_does_not_repeat_inference(work):
    work.llm.profile['basis'][0].update(kind='资料提取', citations=[{'ref': 'S1', 'quote': '丝绸展览'}])
    work.llm.fail = 'VERIFY_BRAND'
    result, run = start(work)
    assert result['status'] == 'failed'
    work.llm.fail = None
    result, run = start(work, 'campaign_retry', {'run_id': run['id'], 'operation_id': result['operation_id']})
    assert result['status'] == 'success' and work.llm.calls.count('INFER_BRAND') == 1


def test_generated_brand_cannot_self_cite_as_confirmed_fact(work):
    _, run = start(work)
    value = plan()
    value['sections'][0]['items'][0].update(kind='事实', refs=['B1'])
    errors = plan_errors(value, run['versions'][0]['evidence'])
    assert any('品牌设定' in error for error in errors)
    value['sections'][0]['items'][0]['kind'] = '创意'
    assert not plan_errors(value, run['versions'][0]['evidence'])


def test_brand_inference_retrieves_and_preserves_knowledge(work):
    async def context(question):
        return [{'chunk_id': 'test-chunk', 'file_path': 'brand.txt', 'content': '品牌关注丝绸日常搭配。'}]
    work.engine.campaign_context = context
    work.llm.profile['basis'][0]['citations'] = [{'ref': 'I1', 'quote': '品牌关注丝绸日常搭配。'}]
    _, run = start(work)
    assert run['brand_knowledge'][0]['file_path'] == 'brand.txt'
    assert run['versions'][0]['evidence']['I1']['chunk_id'] == 'test-chunk'
    assert 'brand.txt' in export_markdown(run, run['versions'][0])


def test_no_recent_sources_cannot_trigger_hotspot_brand_inference(work, monkeypatch):
    import lab.campaigns as campaigns
    monkeypatch.setattr(campaigns, 'search_web', lambda *args: [])
    _, run = start(work)
    assert 'INFER_BRAND' not in work.llm.calls
    result, run = start(work, 'campaign_evergreen', {'run_id': run['id']})
    assert result['status'] == 'success' and run['brand_profile']
    assert run['versions'][0]['topic_id'] == 'evergreen'


def test_legacy_brand_tasks_remain_readable_without_reinference(work):
    run, op = work.campaigns.prepare('campaign_search', INPUTS)
    run['schema_version'] = 1
    run['inputs']['brand'] = BRAND
    work.campaigns.save(run)
    class Job:
        def update(self, **kwargs):
            pass
        def emit(self, *args):
            pass
    asyncio.run(work.campaigns.execute(run, op, Job()))
    assert 'INFER_BRAND' not in work.llm.calls
    assert not run['versions'][0]['evidence']['B1']['inferred']
    assert '用户品牌简档' in run['versions'][0]['evidence']['B1']['kind']


@pytest.mark.parametrize('missing', [None, {'date': None, 'quote': None}, {'date': '', 'quote': ''}])
def test_absent_event_date_does_not_invalidate_publication(missing):
    source = {'text': '来源：央视网 | 2026年10月05日 15:53:19'}
    extracted = {'published': {'date': '2026年10月05日', 'quote': source['text']}, 'event': missing}
    result = verify_date(source, extracted, NOW, 7)
    assert result['status'] == 'verified'
    assert result['published_at'] == '2026-10-05T15:53:19+08:00'
    assert result['published_precision'] == 'time'


@pytest.mark.parametrize('value,expected', [('2026-10-09', 'verified'), ('2026-07-02', 'excluded')])
def test_published_quote_can_reference_saved_page_metadata(value, expected):
    meta = f'<meta property="article:published_time" content="{value}T14:49:33+08:00" />'
    source = {'text': '正文没有显示发布时间', 'published_metadata': [{'value': value + 'T14:49:33+08:00', 'evidence': meta}]}
    extracted = {'published': {'date': value, 'quote': meta}, 'event': {'date': None, 'quote': None}}
    assert verify_date(source, extracted, NOW, 7)['status'] == expected
    extracted['published']['quote'] = meta.replace('14:49', '18:49')
    assert verify_date(source, extracted, NOW, 7)['status'] == 'unverified'


def test_model_cannot_invent_time_from_date_only_evidence():
    source = {'text': '2026-10-09'}
    extracted = {'event': {'date': '2026-10-09T13:00:00+08:00', 'quote': source['text']}}
    assert verify_date(source, extracted, NOW, 1)['status'] == 'unverified'


def legacy_unverified_run(work):
    _, run = start(work)
    run.pop('date_check_version', None)
    run['versions'] = []
    run['matches'] = {}
    run.pop('brand_profile', None)
    run.pop('brand_candidate', None)
    run.pop('brand_knowledge', None)
    source = run['sources'][0]
    source['text'] = '来源：新闻发布 | 2026年10月05日 15:53:19。这是一篇关于丝绸日常应用的报道。'
    extracted = run['extractions']['S1']
    extracted.update(published={'date': '2026年10月05日', 'quote': '来源：新闻发布 | 2026年10月05日 15:53:19'}, event={'date': None, 'quote': None})
    extracted['date'] = {'status': 'unverified', 'published_at': None, 'event_at': None, 'reason': '提取日期未能与原文证据对应，需要核实'}
    run['topics'][0].update(date_status='unverified', date='')
    next(iter(run['operations'].values())).update(stage='品牌匹配', status='partial')
    work.campaigns.save(run)
    return run


def test_recheck_reuses_sources_then_automatically_generates_without_changing_original(work, monkeypatch):
    import lab.campaigns as campaigns
    original = legacy_unverified_run(work)
    before = deepcopy(original)
    calls = len(work.llm.calls)
    monkeypatch.setattr(campaigns, 'utc_now', lambda: '2026-10-20T12:00:00+08:00')
    def no_network(*args):
        raise AssertionError('重新核验不应重新搜索或采集')
    monkeypatch.setattr(campaigns, 'search_web', no_network)
    monkeypatch.setattr(campaigns, 'fetch_source', no_network)
    result, run = start(work, 'campaign_recheck', {'run_id': original['id']})
    assert result['status'] == 'success', result
    assert run['id'] != original['id'] and run['recheck_of'] == original['id']
    assert run['snapshot_at'] == original['created_at']  # Original seven-day window.
    assert run['sources'] == original['sources']
    assert work.campaigns.load(original['id']) == before
    assert run['topics'][0]['date_status'] == 'verified'
    assert len(run['versions']) == 1
    assert 'SEARCH' not in work.llm.calls[calls:]
    assert 'EXTRACT' not in work.llm.calls[calls:]
    assert 'GROUP' not in work.llm.calls[calls:]
    assert 'INFER_BRAND' in work.llm.calls[calls:]


def test_button_explains_every_blocker_and_reading_is_not_a_requirement():
    from lab.campaign_ui import generation_blockers
    topic = {'date_status': 'unverified'}
    assert any('日期' in r for r in generation_blockers(topic, {}, True, False, strict=True))
    topic['date_status'] = 'verified'
    assert any('品牌匹配' in r for r in generation_blockers(topic, {}, True, False, strict=True))
    assert any('不建议' in r for r in generation_blockers(topic, {'grade': '不建议'}, True, False, strict=True))
    assert any('配置模型' in r for r in generation_blockers(topic, {'grade': '推荐'}, False, False, strict=True))
    assert any('执行中' in r for r in generation_blockers(topic, {'grade': '推荐'}, True, True, strict=True))
    assert not generation_blockers(topic, {'grade': '备选'}, True, False, strict=True)


def test_ui_date_blocker_offers_recheck_and_recovery(work):
    from streamlit.testing.v1 import AppTest
    run = legacy_unverified_run(work)
    app = AppTest.from_string('''
import streamlit as st
from lab.campaign_ui import campaign_page
campaign_page(st.session_state.engine)
''')
    app.session_state.engine = work.engine
    app.run(timeout=20)
    assert not app.exception
    assert not next(b for b in app.button if b.label == '为此话题生成方案').disabled
    assert any('不影响生成' in caption.value for caption in app.caption)
    assert not any('先查看推荐依据' in item.value for item in app.info)
    next(b for b in app.button if b.label == '重新核验日期并继续').click().run(timeout=20)
    import time
    deadline = time.monotonic() + 5
    while work.active and time.monotonic() < deadline:
        time.sleep(.01)
    app.run(timeout=20)
    assert not app.exception
    assert len(work.campaigns.runs()) == 2
    derived = next(r for r in work.campaigns.runs() if r['id'] != run['id'])
    assert len(derived['versions']) == 1
    assert app.selectbox(key='campaign-history').value == derived['id']


@pytest.mark.parametrize('date_status', ['unverified', 'excluded'])
def test_demo_generates_from_legacy_topic_without_recheck(work, date_status):
    run = legacy_unverified_run(work)
    run['topics'][0]['date_status'] = date_status
    work.campaigns.save(run)
    before = len(work.llm.calls)
    work.llm.fail = 'VERIFY'
    result, updated = start(work, 'campaign_generate', {'run_id': run['id'], 'topic_id': 'T1', 'mode': 'demo'})
    assert result['status'] == 'success', result
    assert updated['topics'][0]['date_status'] == date_status
    assert updated['brand_profile'] and updated['matches']['T1']
    version = updated['versions'][-1]
    assert version['status'] == 'complete' and version['mode'] == 'demo'
    assert version['plan']['content_version'] == 2 and len(version['plan']['posts']) == 2
    assert '演示方案' in export_markdown(updated, version)
    assert 'SEARCH' not in work.llm.calls[before:]
    assert 'EXTRACT' not in work.llm.calls[before:]
    assert 'VERIFY' not in work.llm.calls[before:]
    assert any('不代表已核实' in a for a in version['plan']['assumptions'])


def test_demo_default_automatically_generates_despite_missing_dates(work, monkeypatch):
    import lab.campaigns as campaigns
    monkeypatch.setattr(campaigns, 'fetch_source', lambda url, title: {'text': '展览信息没有日期。' * 10, 'url': url, 'title': title, 'collected_at': NOW})
    inputs = {k: v for k, v in INPUTS.items() if k != 'mode'}
    result, run = start(work, inputs=inputs)
    assert result['status'] == 'success'
    assert run['topics'][0]['date_status'] == 'unverified'
    assert len(run['versions']) == 1 and run['versions'][0]['mode'] == 'demo'


def test_demo_search_failure_produces_explicit_evergreen_not_fake_hotspots(work, monkeypatch):
    import lab.campaigns as campaigns
    def fail(*args):
        raise RuntimeError('offline')
    monkeypatch.setattr(campaigns, 'search_web', fail)
    result, run = start(work, inputs={**INPUTS, 'mode': 'demo'})
    assert result['status'] == 'partial'
    assert not run['topics'] and not run['sources']
    assert run['versions'][0]['topic_id'] == 'evergreen'
    assert any('不代表近期热点' in i for i in run['issues'])


def test_demo_matching_grade_is_advisory_and_revision_keeps_original(work):
    run = legacy_unverified_run(work)
    run['matches']['T1'] = {'grade': '不建议', 'brand_fit': '关联较弱', 'audience_fit': '', 'angle': '', 'constraints': '', 'conflicts': []}
    work.campaigns.save(run)
    result, run = start(work, 'campaign_generate', {'run_id': run['id'], 'topic_id': 'T1', 'mode': 'demo'})
    assert result['status'] == 'success'
    original = deepcopy(run['versions'][0])
    result, run = start(work, 'campaign_revise', {'run_id': run['id'], 'topic_id': 'T1', 'version_id': original['id'], 'revision': '增加短视频方案', 'mode': 'demo'})
    assert result['status'] == 'success' and len(run['versions']) == 2
    assert run['versions'][0] == original


def test_demo_unknown_references_removed_and_content_not_labeled_verified():
    from lab.campaigns import demo_plan
    draft = plan()
    draft['sections'][0]['items'][0].update(kind='事实', refs=['S99', 'B1'])
    result = demo_plan(draft, {'B1': {'inferred': True}}, {'date_status': 'unverified'})
    assert result['sections'][0]['items'][0]['refs'] == ['B1']
    assert result['sections'][0]['items'][0]['kind'] == '判断'
    assert not plan_errors(result, {'B1': {'inferred': True}})


def test_ui_demo_click_completes_legacy_blocked_topic(work):
    from streamlit.testing.v1 import AppTest
    from lab.campaign_ui import generation_blockers
    run = legacy_unverified_run(work)
    app = AppTest.from_string('''
import streamlit as st
from lab.campaign_ui import campaign_page
campaign_page(st.session_state.engine)
''')
    app.session_state.engine = work.engine
    app.run(timeout=20)
    button = next(b for b in app.button if b.label == '为此话题生成方案')
    assert not button.disabled
    assert not app.warning
    button.click().run(timeout=20)
    import time
    deadline = time.monotonic() + 5
    while work.active and time.monotonic() < deadline:
        time.sleep(.01)
    app.run(timeout=20)
    assert not app.exception
    assert len(work.campaigns.load(run['id'])['versions']) == 1
    assert len(app.get('download_button')) == 2
    before = len(work.llm.calls)
    app.run(timeout=20)
    assert len(work.llm.calls) == before
    assert generation_blockers({'date_status': 'unverified'}, {}, False, False)
    assert generation_blockers({'date_status': 'unverified'}, {}, True, True)
    assert not generation_blockers({'date_status': 'unverified'}, {}, True, False)
