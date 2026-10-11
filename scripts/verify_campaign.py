"""Opt-in live smoke test using saved model credentials in an isolated workspace.

Run explicitly: .venv/bin/python scripts/verify_campaign.py
No credentials are printed or copied into the isolated workspace.
"""
import json
from pathlib import Path
import sys
import tempfile
import argparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.config import ROOT, Settings, load_settings
from lab.engine import Engine
from lab.llm import ExtractionLLM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources-only', action='store_true', help='仅验证真实搜索与采集，不调用模型')
    args = parser.parse_args()
    if args.sources_only:
        from lab.sources import search_web, fetch_source
        results = search_web('丝绸 非遗 展览 2026年10月', 3)
        report = {'searched': len(results), 'fetched': [], 'failures': []}
        for item in results:
            try:
                source = fetch_source(item['url'], item['title'])
                report['fetched'].append({'url': source['url'], 'title': source['title'], 'chars': len(source['text']), 'published_metadata': source.get('published_metadata', [])})
            except Exception as exc:
                report['failures'].append({'url': item['url'], 'error_type': type(exc).__name__})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['fetched'] else 1
    original = load_settings()
    model = ExtractionLLM(original.data_dir / 'workbench' / original.workspace / 'llm-settings.json', lambda *a: None)
    if not model.configured:
        print(json.dumps({'status': 'unavailable', 'reason': '未配置真实模型'}, ensure_ascii=False))
        return 2
    directory = Path(tempfile.mkdtemp(prefix='atlas-campaign-live-'))
    (directory / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    engine = Engine(Settings(data_dir=directory, workspace='campaign_live', seed_demo=False))
    try:
        engine.ready.result(timeout=180)
        engine.workbench.llm.settings = model.settings
        future, job = engine.workbench.start('campaign_search', {
            'request': '搜索近七天中国丝绸、非遗展览或东方美学相关的近期新闻，自动推理品牌与产品方向并设计传播初稿。仅使用实际近期报道。',
            'days': 7, 'goal': '品牌曝光', 'channels': '', 'budget': '',
        })
        result = future.result(timeout=900)
        run = engine.workbench.campaigns.load(result['campaign_id'])
        report = {'status': result['status'], 'message': result.get('message'), 'sources': len(run['sources']),
                  'topics': len(run.get('topics', [])), 'verified_topics': sum(t['date_status']=='verified' for t in run.get('topics', [])),
                  'complete_versions': sum(v['status']=='complete' for v in run['versions']), 'issues': run['issues'],
                  'automatic_brand': bool(run.get('brand_profile')),
                  'directory': str(directory), 'job_id': result['id'], 'campaign_id': run['id']}
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if result['status'] in ('success', 'partial') else 1
    finally:
        engine.close()


if __name__ == '__main__':
    raise SystemExit(main())
