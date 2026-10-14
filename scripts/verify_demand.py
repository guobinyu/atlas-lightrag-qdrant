"""Explicit live acceptance in an isolated directory; never changes production tasks."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.config import ROOT, Settings, load_settings
from lab.engine import Engine
from lab.llm import ExtractionLLM
from lab.demands import export_markdown, export_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['custom', 'trend', 'both'], default='both')
    args = parser.parse_args()
    cfg = load_settings()
    model = ExtractionLLM(cfg.data_dir / 'workbench' / cfg.workspace / 'llm-settings.json', lambda *a: None)
    if not model.configured:
        print(json.dumps({'status': 'unavailable', 'reason': '未配置真实模型'}, ensure_ascii=False))
        return 2
    directory = Path(tempfile.mkdtemp(prefix='atlas-demand-live-'))
    (directory / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    print(json.dumps({'directory': str(directory)}, ensure_ascii=False), flush=True)
    engine = Engine(Settings(data_dir=directory, workspace='demand_live', seed_demo=False))
    report = {'directory': str(directory), 'knowledge': '隔离的空知识库，验证无命中行为', 'cases': []}
    try:
        engine.ready.result(timeout=180)
        work = engine.workbench
        work.llm.settings = model.settings
        cases = ['custom', 'trend'] if args.case == 'both' else [args.case]
        for case in cases:
            inputs = {'request': '为校友聚会提出黄酒纪念礼品建议，主题为重逢，预算和数量尚未确定。', 'industry': '黄酒',
                      'capabilities': '能生产黄酒、定制标签和礼盒包装', 'brand': '', 'constraints': '不生产服饰，不宣称保健功效',
                      'audience': '参加校友聚会的成年人', 'scene': '校友聚会赠礼', 'theme': '重逢', 'path': case, 'days': 7}
            if case == 'trend':
                inputs.update(request='结合近期中国文化、非遗、丝绸展览话题，探索日常丝绸产品。', industry='丝绸',
                              capabilities='可生产真丝丝巾和家居服，支持数码印花', constraints='仅开发丝巾或家居服，不生产酒类',
                              audience='成年消费者', scene='日常穿搭', theme='')
            run = work.demands.create(inputs)
            entry = {'case': case, 'task_id': run['id'], 'stages': [], 'status': 'running'}
            report['cases'].append(entry)

            def stage(kind, **extra):
                begun = time.monotonic()
                future, _ = work.start('demand_' + kind, {'run_id': run['id'], **extra})
                result = future.result(timeout=1200)
                record = {'kind': kind, 'status': result['status'], 'seconds': round(time.monotonic() - begun, 2),
                          'message': result.get('message'), 'model_calls': [e['data'] for e in result['events'] if e['event_type'] == 'llm.completed']}
                entry['stages'].append(record)
                (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'case': case, **record}, ensure_ascii=False), flush=True)
                return result['status'] == 'success'

            if not stage('analyze'):
                entry['status'] = 'failed'
                continue
            work.demands.approve(run['id'])
            if case == 'trend':
                if not stage('search'):
                    entry['status'] = 'failed'
                    continue
                current = work.demands.load(run['id'])
                topics = current['research'][-1]['topics']
                topic = next((t for t in topics if t['date_status'] == 'verified'), topics[0])
                # Explicit selection by this acceptance scenario, not automatic product behavior.
                work.demands.select_topic(run['id'], topic['id'])
                entry['selected_topic'] = topic
            if not stage('candidates'):
                entry['status'] = 'failed'
                continue
            current = work.demands.load(run['id'])
            batch = current['candidate_rounds'][-1]
            entry['candidate_names'] = [c['name'] for c in batch['candidates']]
            work.demands.select_candidate(run['id'], batch['id'], batch['candidates'][0]['id'])
            if not stage('brief'):
                entry['status'] = 'failed'
                continue
            current = work.demands.load(run['id'])
            original = current['briefs'][-1]
            if not stage('revise', brief_id=original['id'], instruction='保留产品方向，简化包装或装饰建议，明确优先交付的设计稿。'):
                entry['status'] = 'failed'
                continue
            current = work.demands.load(run['id'])
            assert current['briefs'][0] == original
            assert current['inputs'] == inputs
            entry.update(status='success', brief_versions=len(current['briefs']), brand_unchanged=current['inputs']['brand'] == '')
            latest = current['briefs'][-1]
            (directory / (case + '.md')).write_text(export_markdown(current, latest), encoding='utf-8')
            (directory / (case + '.json')).write_text(json.dumps(export_bundle(current, latest), ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        engine.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(c['status'] == 'success' for c in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
