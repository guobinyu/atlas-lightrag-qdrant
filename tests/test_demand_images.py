"""Image API contract and isolated task/UI acceptance, without paid calls."""
import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json
import threading

import httpx
from PIL import Image
import pytest

from lab.image_service import ImageService, ImageSettings, png_info
from lab import demand_images
from lab.demands import DemandService
from test_demands import demand, work, start, INPUT
from test_demand_design import designed
from test_demand_ui import page, button, wait


def png(color='green'):
    out = BytesIO()
    Image.new('RGB', (64, 48), color).save(out, format='PNG')
    return out.getvalue()


@pytest.fixture
def images(demand):
    demand.images.save(ImageSettings(endpoint='https://api.openai.com/v1', model='gpt-image-2.5-sunburst', provider='openai', api_key='IMAGE_TEST_SECRET'))
    previous = demand.llm
    class Model:
        configured, settings = True, previous.settings
        calls = 0
        async def __call__(self, prompt, **kwargs):
            if prompt.startswith('DEMAND_IMAGE_PROMPTS'):
                self.calls += 1
                return json.dumps({'variants': [{'title': f'构图{i}', 'prompt': f'平面桥拱纹样，布局{i}'} for i in range(3)]})
            return await previous(prompt, **kwargs)
    demand.llm = Model()
    calls = []
    async def render(prompt, size, source=None, config=None):
        calls.append({'prompt': prompt, 'size': size, 'source': source, 'config': config})
        return png('green' if source is None else 'blue')
    demand.images.render = render
    demand.image_calls = calls
    return demand


def generated(work):
    run = designed(work)
    run, status = start(work, run, 'image_generate', design_id=run['design_versions'][0]['id'], application_index=0)
    assert status['status'] == 'success', status
    return run


def test_generate_edit_sources_and_disk_download(images):
    run = designed(images)
    old = deepcopy(run)
    run, status = start(images, run, 'image_generate', design_id=run['design_versions'][0]['id'], application_index=0, size='1536x1024')
    assert status['status'] == 'success'
    assert len(run['image_assets']) == 3 and images.llm.calls == 1
    original = deepcopy(run['image_assets'])
    asset = original[0]
    raw = demand_images.asset_path(images.demands, run['id'], asset['path']).read_bytes()
    assert png_info(raw)['width'] == 64
    assert asset['size'] == '1536x1024'
    assert 'IMAGE_TEST_SECRET' not in json.dumps(run)
    demand_images.select(images.demands, run['id'], asset['id'])
    run, status = start(images, run, 'image_revise', image_id=asset['id'], instruction='墨绿，减少装饰，保留主体')
    assert status['status'] == 'success'
    assert images.image_calls[-1]['source'] == raw
    assert run['image_assets'][:3] == original
    assert run['image_assets'][-1]['parent_id'] == asset['id']
    assert run['design_versions'] == old['design_versions'] and run['briefs'] == old['briefs']
    assert len(images.image_calls) == 4 and images.llm.calls == 1
    assert '墨绿' in images.image_calls[-1]['prompt']
    assert demand_images.asset_path(images.demands, run['id'], asset['path']).read_bytes() == raw


def test_partial_retry_and_restart_preserve_completed(images):
    run = designed(images)
    renderer = images.images.render
    calls = 0
    async def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('IMAGE_TEST_SECRET failure')
        return await renderer(*args, **kwargs)
    images.images.render = broken
    run, status = start(images, run, 'image_generate', design_id=run['design_versions'][0]['id'])
    assert status['status'] == 'failed' and len(run['image_assets']) == 1
    assert 'IMAGE_TEST_SECRET' not in json.dumps(run)
    op = list(run['operations'].values())[-1]
    original = deepcopy(run['image_assets'][0])
    op['status'] = 'running'
    images.demands.save(run)
    DemandService(images).interrupt()
    images.images.render = renderer
    run, status = start(images, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success' and len(run['image_assets']) == 3
    assert run['image_assets'][0] == original
    assert len(images.image_calls) == 3 and images.llm.calls == 1


def test_stale_foreign_source_and_path_rejected(images):
    run = generated(images)
    image = run['image_assets'][0]
    other = images.demands.create(INPUT)
    images.demands.approve(other['id'])
    with pytest.raises(ValueError):
        start(images, other, 'image_revise', image_id=image['id'], instruction='墨绿')
    with pytest.raises(ValueError):
        demand_images.asset_path(images.demands, run['id'], '../private.png')
    images.demands.update(run['id'], {**INPUT, 'purpose': '另一用途'})
    images.demands.approve(run['id'])
    with pytest.raises(ValueError, match='当前条件'):
        start(images, run, 'image_revise', image_id=image['id'], instruction='墨绿')
    with pytest.raises(ValueError):
        demand_images.select(images.demands, run['id'], image['id'])
    assert demand_images.asset_path(images.demands, run['id'], image['path']).exists()


def test_duplicate_submission_and_configuration_lock(images):
    run = designed(images)
    entered, release = threading.Event(), threading.Event()
    renderer = images.images.render
    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return await renderer(*args, **kwargs)
    images.images.render = slow
    try:
        future, _ = images.start('demand_image_generate', {'run_id': run['id'], 'design_id': run['design_versions'][0]['id']})
        assert entered.wait(3)
        with pytest.raises(ValueError, match='执行中'):
            images.start('demand_image_generate', {'run_id': run['id'], 'design_id': run['design_versions'][0]['id']})
        with pytest.raises(ValueError, match='执行中'):
            images.configure_images(ImageSettings(api_key='changed'))
    finally:
        release.set()
    assert future.result(timeout=10)['status'] == 'success'


def test_ui_generate_select_edit_history_and_application(images):
    run = designed(images)
    app = page(images)
    assert not app.exception
    next(b for b in app.button if b.label in ('生成三版效果图', '再生成一批效果图（3 张）')).click().run()
    wait(images); app.run()
    assert not app.exception
    assert len([b for b in app.get('download_button') if b.label == '下载 PNG']) == 3
    button(app, '选用此图稿').click().run()
    next(a for a in app.text_area if a.label == '图稿修改意见').input('减少装饰')
    button(app, '基于选用图稿生成新版本').click().run()
    wait(images); app.run()
    assert not app.exception
    assert len([b for b in app.get('download_button') if b.label == '下载 PNG']) == 1
    history = next(s for s in app.selectbox if s.label == '图稿批次与修订历史')
    history.select_index(1).run()
    assert len([b for b in app.get('download_button') if b.label == '下载 PNG']) == 3
    next(s for s in app.selectbox if s.label == '图稿设计项').select_index(1).run()
    assert not [b for b in app.get('download_button') if b.label == '下载 PNG']
    saved = images.demands.load(run['id'])
    for asset in saved['image_assets']:
        assert png_info(demand_images.asset_path(images.demands, run['id'], asset['path']).read_bytes())


def test_settings_private_independent_and_address_change(tmp_path):
    service = ImageService(tmp_path / 'image-settings.json')
    assert not service.configured
    service.save(ImageSettings(endpoint='https://api.openai.com/v1', model='gpt-image-2.5-sunburst', provider='openai', api_key='secret'))
    assert service.path.stat().st_mode & 0o777 == 0o600
    assert ImageService(service.path).settings.api_key == 'secret'
    assert 'secret' not in json.dumps(service.settings.public())
    config = service.settings.public()
    service.save(ImageSettings(endpoint='https://other.example/v1', api_key='other'))
    with pytest.raises(ValueError, match='地址已变化'):
        asyncio.run(service.render('test', '1024x1024', config=config))


@pytest.mark.parametrize('mode', ['generate', 'edit', '401', '429', 'timeout', 'empty', 'invalid_png'])
def test_api_contract(tmp_path, monkeypatch, mode):
    service = ImageService(tmp_path / 'image-settings.json')
    service.save(ImageSettings(endpoint='https://api.openai.com/v1', model='gpt-image-2.5-sunburst', provider='openai', api_key='secret'))
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer secret'
        if mode == 'timeout':
            raise httpx.ReadTimeout('secret', request=request)
        if mode in ('401', '429'):
            return httpx.Response(int(mode), json={'error': 'secret'})
        if mode == 'empty':
            return httpx.Response(200, json={'data': []})
        raw = b'not png' if mode == 'invalid_png' else png()
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(raw).decode()}]})
    client = httpx.AsyncClient
    monkeypatch.setattr('lab.image_service.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    if mode in ('generate', 'edit'):
        assert asyncio.run(service.render('test', '1024x1024', source=png() if mode == 'edit' else None)) == png()
        request = requests[0]
        assert request.url.path == ('/v1/images/edits' if mode == 'edit' else '/v1/images/generations')
        if mode == 'edit':
            assert b'filename="source.png"' in request.content and b'image/png' in request.content
        else:
            assert json.loads(request.content)['output_format'] == 'png'
            assert json.loads(request.content)['quality'] == 'medium'
    else:
        with pytest.raises(ValueError) as e:
            asyncio.run(service.render('test', '1024x1024'))
        assert 'secret' not in str(e.value)


def test_image_test_job_is_independent(images):
    future, _ = images.start('image_test', {})
    result = future.result(timeout=10)
    assert result['status'] == 'success'
    assert png_info((images.directory / result['image_path']).read_bytes())
    assert images.demands.runs() == []


def test_edit_does_not_require_text_connection(images):
    run = generated(images)
    def unavailable():
        raise ValueError('文字连接不可用')
    images.llm.settings.validate = unavailable
    run, status = start(images, run, 'image_revise', image_id=run['image_assets'][0]['id'], instruction='保留主体')
    assert status['status'] == 'success'


def test_invalid_prompts_retry_without_image_call(images):
    run = designed(images)
    original = images.llm
    class Invalid:
        configured, settings = True, original.settings
        async def __call__(self, *args, **kwargs):
            return json.dumps({'variants': [{'title': '相同', 'prompt': '相同'}] * 3})
    images.llm = Invalid()
    run, status = start(images, run, 'image_generate', design_id=run['design_versions'][0]['id'])
    assert status['status'] == 'failed' and images.image_calls == []
    images.llm = original
    op = list(run['operations'].values())[-1]
    run, status = start(images, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success' and len(run['image_assets']) == 3


def test_settings_ui_save_without_call_and_endpoint_key_isolation(images):
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
from lab.workbench_ui import image_settings_panel
image_settings_panel(st.session_state.work)
''')
    app.session_state['work'] = images
    app.run()
    assert not app.exception
    button(app, '保存图片配置').click().run()
    assert images.image_calls == []
    assert images.images.settings.api_key == 'IMAGE_TEST_SECRET'
    button(app, '生成测试图').click().run()
    wait(images); app.run()
    assert not app.exception
    assert len(images.image_calls) == 1
    next(t for t in app.text_input if t.label == '图片 API 基础地址').input('https://new.example/v1')
    button(app, '保存图片配置').click().run()
    assert images.images.settings.api_key == ''
    assert button(app, '生成测试图').disabled


def test_new_text_version_and_changed_source_keep_image_history(images):
    run = generated(images)
    old = deepcopy(run['image_assets'])
    run, status = start(images, run, 'design_revise', design_id=run['design_versions'][0]['id'], instruction='简化装饰')
    assert status['status'] == 'success'
    app = page(images)
    assert not [b for b in app.get('download_button') if b.label == '下载 PNG']
    next(s for s in app.selectbox if s.label == '设计方案历史版本').select_index(1).run()
    assert len([b for b in app.get('download_button') if b.label == '下载 PNG']) == 3
    assert images.demands.load(run['id'])['image_assets'] == old
    images.demands.update(run['id'], {**INPUT, 'purpose': '其他用途'})
    app.run()
    assert not app.exception
    assert next(b for b in app.button if b.label in ('生成三版效果图', '再生成一批效果图（3 张）')).disabled


@pytest.mark.parametrize('size', ['2048x2048', '2496x1664', '1664x2496'])
@pytest.mark.parametrize('edit', [False, True])
def test_seedream_generate_and_edit_json_contract(tmp_path, monkeypatch, size, edit):
    service = ImageService(tmp_path / 'image-settings.json')
    assert service.settings.provider == 'seedream'
    service.save(ImageSettings(api_key='ark-test-key'))
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
        assert request.headers['authorization'] == 'Bearer ark-test-key'
        assert 'application/json' in request.headers['content-type']
        payload = json.loads(request.content)
        assert payload['model'] == 'doubao-seedream-5-0-lite-260128'
        assert payload['size'] == size
        assert payload['response_format'] == 'b64_json'
        assert payload['output_format'] == 'png'
        assert payload['watermark'] is True
        assert 'quality' not in payload and 'n' not in payload
        if edit:
            assert base64.b64decode(payload['image'].split(',', 1)[1]) == png()
            assert payload['image'].startswith('data:image/png;base64,')
        else:
            assert 'image' not in payload
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    client = httpx.AsyncClient
    monkeypatch.setattr('lab.image_service.httpx.AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(service.render('平面江南桥拱纹样', size, source=png() if edit else None)) == png()
    assert len(requests) == 1


def test_legacy_config_and_snapshot_keep_openai_protocol(tmp_path, monkeypatch):
    path = tmp_path / 'image-settings.json'
    legacy = {'endpoint': 'https://api.openai.com/v1', 'model': 'gpt-image-2.5-sunburst', 'api_key': 'old-key', 'timeout': 300}
    path.write_text(json.dumps(legacy))
    service = ImageService(path)
    assert service.settings.provider == 'openai'
    snapshot = {k: v for k, v in legacy.items() if k != 'api_key'}
    def handler(request):
        assert request.url.path == '/v1/images/edits'
        assert 'multipart/form-data' in request.headers['content-type']
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    client = httpx.AsyncClient
    monkeypatch.setattr('lab.image_service.httpx.AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(service.render('old edit', '1024x1024', source=png(), config=snapshot)) == png()
    service.save(ImageSettings(api_key='new-ark-key'))
    with pytest.raises(ValueError, match='服务商已切换'):
        asyncio.run(service.render('old retry', '1024x1024', config=snapshot))


def test_seedream_workflow_and_cross_provider_revision(images):
    run = generated(images)  # Previously saved OpenAI images remain usable as reference images.
    original = deepcopy(run['image_assets'])
    images.images.save(ImageSettings(api_key='ark-key'))
    run, status = start(images, run, 'image_revise', image_id=original[0]['id'], instruction='保留主体')
    assert status['status'] == 'success'
    assert run['image_assets'][-1]['size'] == '2048x2048'
    assert run['image_assets'][-1]['model_config']['provider'] == 'seedream'
    assert run['image_assets'][-1]['quality'] == 'default'
    assert run['image_assets'][:3] == original
    run, status = start(images, run, 'image_generate', design_id=run['design_versions'][0]['id'], size='2496x1664')
    assert status['status'] == 'success'
    assert all(a['size'] == '2496x1664' for a in run['image_assets'][-3:])
    app = page(images)
    assert not app.exception
    sizes = next(s for s in app.selectbox if s.label == '图稿像素规格')
    assert '2496×1664' in ' '.join(sizes.options)


def test_provider_switch_ui_clears_key_and_cannot_test_unsaved_provider(images):
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
from lab.workbench_ui import image_settings_panel
image_settings_panel(st.session_state.work)
''')
    app.session_state['work'] = images
    app.run()
    next(s for s in app.selectbox if s.label == '图片服务商').select('seedream').run()
    assert not app.exception
    assert button(app, '生成测试图').disabled
    assert next(t for t in app.text_input if t.label == '图片 API 基础地址').value == 'https://ark.cn-beijing.volces.com/api/v3'
    button(app, '保存图片配置').click().run()
    assert images.images.settings.provider == 'seedream'
    assert images.images.settings.api_key == ''
    assert button(app, '生成测试图').disabled
    assert images.image_calls == []


@pytest.mark.parametrize('code,message', [
    ('QuotaExceeded', 'Free trial quota exhausted. secret'),
    ('RateLimitExceeded.EndpointRPMExceeded', 'RPM limit exceeded'),
    ('ServerOverloaded', 'Please retry later'),
])
def test_provider_error_details_are_preserved_and_redacted(code, message):
    from lab.image_service import image_error
    response = httpx.Response(429, headers={'x-request-id': 'test-request-id'}, json={'error': {'code': code, 'message': message}})
    result = image_error(response, 'secret')
    assert code in result and 'test-request-id' in result
    assert 'secret' not in result
    assert 'HTTP 429' in result


def test_error_non_json_does_not_expose_raw_response():
    from lab.image_service import image_error
    assert 'private raw body' not in image_error(httpx.Response(502, text='private raw body'), 'secret')


def test_combined_design_images_retry_keeps_text_and_saved_images(images):
    from test_demand_design import briefed
    run = briefed(images)
    renderer = images.images.render
    count = 0
    async def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError('temporary interruption')
        return await renderer(*args, **kwargs)
    images.images.render = interrupted
    run, status = start(images, run, 'design_generate', brief_id=run['briefs'][0]['id'], include_images=True)
    assert status['status'] == 'failed'
    assert len(run['design_versions']) == 1 and len(run['image_assets']) == 1
    design = deepcopy(run['design_versions'][0])
    asset = deepcopy(run['image_assets'][0])
    images.images.render = renderer
    run, status = start(images, run, 'retry', operation_id=design['id'])
    assert status['status'] == 'success'
    assert run['design_versions'] == [design]
    assert run['image_assets'][0] == asset
    assert len(run['image_assets']) == 3 and len(images.image_calls) == 3
    assert images.llm.calls == 1
    run, status = start(images, run, 'design_revise', design_id=design['id'], instruction='主色改为墨绿', include_images=True)
    assert status['status'] == 'success'
    assert len(run['design_versions']) == 2 and len(run['image_assets']) == 6
    assert run['design_versions'][0] == design
    assert all(a['design_id'] == run['design_versions'][1]['id'] for a in run['image_assets'][3:])


def test_page_design_generates_images_in_step_five(images):
    from test_demand_design import briefed
    run = briefed(images)
    app = page(images)
    button(app, '生成设计方案').click().run()
    wait(images)
    app.run()
    assert not app.exception
    saved = images.demands.load(run['id'])
    assert len(saved['design_versions']) == 1 and len(saved['image_assets']) == 3
    assert not any('6 ·' in h.value for h in app.subheader)
    assert len([b for b in app.get('download_button') if b.label == '下载 PNG']) == 3
