"""Ark web search: request shape, citation parsing, key reuse, errors and search_web routing. No network."""
import json

import httpx
import pytest

from lab import sources
from lab.search_service import SearchService, SearchSettings, citations
from test_demands import demand, work, INPUT, start

KEY = 'ARK_TEST_SECRET'
REPLY = {'output': [
    {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'query': '双十一 丝巾'}},
    {'type': 'message', 'content': [{'type': 'output_text', 'text': '概括。', 'annotations': [
        {'type': 'url_citation', 'title': '双十一丝巾销售观察', 'url': 'https://news.example.cn/a', 'site_name': '示例新闻',
         'publish_time': '2026-10-20T10:00:00+08:00', 'summary': '丝巾礼盒热销。'},
        {'type': 'url_citation', 'title': '重复链接', 'url': 'https://news.example.cn/a'},
        {'type': 'url_citation', 'title': '非网页', 'url': 'file:///etc/passwd'},
        {'type': 'url_citation', 'title': '国潮围巾', 'url': 'https://www.example.com/b'}]}]}],
    'usage': {'tool_usage': 1}}


def service(tmp_path, handler, image_key=''):
    return SearchService(tmp_path / 'search-settings.json', lambda: image_key, transport=httpx.MockTransport(handler))


def test_request_and_citations(tmp_path):
    seen = {}
    def handler(request):
        seen.update(url=str(request.url), auth=request.headers['Authorization'], body=json.loads(request.content))
        return httpx.Response(200, json=REPLY)
    search = service(tmp_path, handler, image_key=KEY)
    assert search.active
    rows = search.search('双十一 丝巾', 6)
    assert seen['url'] == 'https://ark.cn-beijing.volces.com/api/v3/responses'
    assert seen['auth'] == 'Bearer ' + KEY
    body = seen['body']
    assert body['stream'] is False and body['max_tool_calls'] == 1
    assert body['tools'] == [{'type': 'web_search', 'limit': 6, 'max_keyword': 1}]
    assert '双十一 丝巾' in body['input'][0]['content'][0]['text']
    assert [r['url'] for r in rows] == ['https://news.example.cn/a', 'https://www.example.com/b']
    assert rows[0]['published'].startswith('2026-10-20') and rows[0]['snippet'] == '丝巾礼盒热销。'
    assert citations({'output': []}, 5) == []


def test_key_reuse_separate_key_and_private_file(tmp_path):
    search = service(tmp_path, lambda r: httpx.Response(200, json=REPLY))
    assert not search.active
    search.save(SearchSettings(api_key='SEPARATE', use_image_key=False))
    assert search.active and search.key() == 'SEPARATE'
    assert oct((tmp_path / 'search-settings.json').stat().st_mode & 0o777) == '0o600'
    assert 'SEPARATE' not in json.dumps(search.settings.public())
    reloaded = SearchService(tmp_path / 'search-settings.json', lambda: '')
    assert reloaded.key() == 'SEPARATE'
    search.save(SearchSettings(provider='ddgs'))
    assert not search.active


@pytest.mark.parametrize('value,message', [({'endpoint': 'http://ark.example.com/api/v3'}, 'HTTPS'),
                                           ({'endpoint': 'https://ark.example.com/api/v3/responses'}, '基础地址'),
                                           ({'model': ' '}, '模型'), ({'limit': 0}, '结果数')])
def test_invalid_settings(value, message):
    with pytest.raises(ValueError, match=message):
        SearchSettings(**value).validate()


def test_errors_are_readable_and_redacted(tmp_path):
    def not_found(request):
        return httpx.Response(404, json={'error': {'code': 'InvalidEndpointOrModel.NotFound', 'message': 'model missing ' + KEY}})
    with pytest.raises(ValueError) as error:
        service(tmp_path, not_found, KEY).search('丝巾')
    assert 'HTTP 404' in str(error.value) and '开通' in str(error.value) and KEY not in str(error.value)
    def broken(request):
        raise httpx.ConnectError('boom')
    def not_open(request):
        return httpx.Response(404, json={'error': {'code': 'ToolNotOpen', 'message': 'Your account has not activated web search.'}})
    with pytest.raises(ValueError) as error:
        service(tmp_path, not_open, KEY).search('丝巾')
    assert '联网内容插件' in str(error.value) and '模型 ID' not in str(error.value)
    with pytest.raises(ValueError, match='连接失败'):
        service(tmp_path, broken, KEY).search('丝巾')
    with pytest.raises(ValueError, match='API Key'):
        service(tmp_path, not_found).search('丝巾')


def test_search_web_prefers_ark_and_falls_back(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=REPLY)
    monkeypatch.setattr(sources, '_search_service', service(tmp_path, handler, KEY))
    assert len(sources.search_web('双十一 丝巾', 10)) == 2 and len(calls) == 1
    monkeypatch.setattr(sources, '_search_service', service(tmp_path, handler))  # No key: DDGS path.
    class Fallback:
        def __init__(self, timeout):
            pass
        def text(self, query, max_results, region):
            return [{'href': 'https://example.org/x', 'title': '公开搜索', 'body': ''}]
    import ddgs
    monkeypatch.setattr(ddgs, 'DDGS', Fallback)
    assert [r['title'] for r in sources.search_web('丝巾', 3)] == ['公开搜索'] and len(calls) == 1


def test_search_unavailable_message(demand, monkeypatch):
    import lab.demands as module
    def failing(*args):
        raise ValueError('公开搜索暂时不可用')
    monkeypatch.setattr(module, 'search_web', failing)
    run = demand.demands.create({**INPUT, 'path': 'trend'})
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'search')
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    assert '搜索服务不可用' in op['message'] and '日历节点' in op['message']
    assert '公开搜索暂时不可用' in op['cache']['research']['issues'][0]

