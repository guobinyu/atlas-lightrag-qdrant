"""Opt-in live check of culture decoding using saved model settings in an isolated workspace.

Run explicitly: .venv/bin/python scripts/verify_culture.py
Decodes the six bundled examples with the configured model; calls the model service and may incur cost.
The report counts elements, quote location and theme coverage. It does not judge cultural accuracy.
"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.config import ROOT, Settings, load_settings
from lab.engine import Engine
from lab.llm import ExtractionLLM


def main():
    original = load_settings()
    model = ExtractionLLM(original.data_dir / 'workbench' / original.workspace / 'llm-settings.json', lambda *a: None)
    if not model.configured:
        print(json.dumps({'status': 'unavailable', 'reason': '未配置真实模型'}, ensure_ascii=False))
        return 2
    directory = Path(tempfile.mkdtemp(prefix='atlas-culture-live-'))
    (directory / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    engine = Engine(Settings(data_dir=directory, workspace='culture_live', seed_demo=False))
    try:
        engine.ready.result(timeout=180)
        work = engine.workbench
        work.llm.settings = model.settings
        ids = [work.sources.add({'title': p.stem[3:], 'text': p.read_text(encoding='utf-8'), 'url': '', 'filename': p.name})['id']
               for p in sorted((ROOT / 'examples').glob('*.md'))]
        future, job = work.start('culture_decode', {'source_ids': ids})
        result = future.result(timeout=3600)
        service = work.culture
        items = service.elements()
        quotes = [e for i in items for e in i['evidence']]
        blocks = service.report(result['batch_id']) if result.get('batch_id') else []
        report = {'status': result['status'], 'message': result.get('message'), 'workspace': str(directory),
                  'summary': result.get('culture_result'),
                  'by_type': dict(service.stats()['by_type']),
                  'exact_quotes': sum(e['match_method'] == 'exact' for e in quotes), 'approximate_quotes': sum(e['match_method'] != 'exact' for e in quotes),
                  'unplaced_or_ignored': sum(len(b['issues']) for b in blocks),
                  'themes': {t: sum(t in i['themes'] for i in items) for t in service.themes()},
                  'palettes': [{'label': i['label'], 'colors': i['colors']} for i in items if i['type'] == 'palette'],
                  'sample_search': [{'label': item['label'], 'score': round(score, 3)}
                                    for item, score in engine.culture_search('婚礼 丝巾 吉祥纹样').result(timeout=120)[:5]],
                  'events': [{'type': e['event_type'], **{k: e['data'][k] for k in ('duration_ms', 'usage') if k in e['data']}}
                             for e in job.snapshot()['events'] if e['event_type'] == 'llm.completed'],
                  'visual_review': 'pending'}
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if result['status'] in {'success', 'partial'} and items else 1
    finally:
        engine.close()


if __name__ == '__main__':
    raise SystemExit(main())
