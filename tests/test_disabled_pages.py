"""DISABLED_PAGES hides a page for a focused demo without stopping the features behind it."""
from types import SimpleNamespace

import pytest

from lab.config import Settings, load_settings
from test_demands import demand, work, INPUT, start
from test_ui_shell import shell


def disable(work, *pages):
    work.engine.settings = SimpleNamespace(**vars(work.engine.settings), disabled_pages=pages)


def test_setting_is_parsed_and_validated(monkeypatch):
    monkeypatch.setenv('DISABLED_PAGES', 'culture, campaign')
    assert load_settings().disabled_pages == ('culture', 'campaign')
    with pytest.raises(ValueError, match='DISABLED_PAGES'):
        Settings(disabled_pages=('../x',)).validate()
    assert Settings().disabled_pages == ()


def test_disabled_page_is_locked_everywhere(demand):
    disable(demand, 'culture')
    app = shell(demand)
    assert not app.exception
    entry = app.button(key='home-go-culture')
    assert entry.disabled and entry.label == '即将上线'
    assert not app.button(key='home-go-campaign').disabled  # Marketing stays available.
    app.segmented_control(key='top-nav').set_value('culture').run()
    assert not app.exception
    assert app.query_params.get('view', ['home']) != ['culture']
    assert any('即将上线' in t.value for t in app.toast)
    # A direct link lands on the home page instead of the disabled page.
    app = shell(demand, view='culture')
    assert not app.exception and app.query_params['view'] == ['home']
    assert any('即将上线' in t.value for t in app.toast)


def test_features_behind_the_page_keep_working(demand):
    disable(demand, 'culture')
    calls = []
    async def culture(question, hint=''):
        calls.append(question)
        return []
    demand.engine.culture_context = culture
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'success' and calls
