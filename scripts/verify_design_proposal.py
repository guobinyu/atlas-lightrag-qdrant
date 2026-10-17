"""Live text-design acceptance from frozen phase-two sources in an isolated workspace."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.config import ROOT, Settings, load_settings
from lab.engine import Engine
from lab.llm import ExtractionLLM
from lab.demand_design import export_markdown, export_bundle


def seed_source(service, entry):
    """Import a frozen acceptance fixture, never the production task directory."""
    brief = deepcopy(entry['final_brief'])
    run = service.create(brief['summary']['inputs'])
    run.update(revision=brief['binding']['revision'], approved_revision=brief['binding']['revision'],
               inputs=deepcopy(brief['summary']['inputs']), summaries=[deepcopy(brief['summary'])],
               research_id=brief['binding']['research_id'], topic_id=brief['binding']['topic_id'],
               selection=deepcopy(brief['selection']), briefs=[brief])
    run['candidate_rounds'] = [{'id': brief['selection']['round_id'], 'format_version': 3,
        'binding': deepcopy(brief['binding']), 'at': brief['at'], 'candidates': [deepcopy(brief['candidate'])],
        'context': {'summary': deepcopy(brief['summary']), 'topic': deepcopy(brief.get('topic')),
                    'evidence': deepcopy(brief['evidence']), 'knowledge_missing': brief['knowledge_missing']}}]
    service.save(run)
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=ROOT / 'docs/demand-design-evaluation.json')
    parser.add_argument('--case', choices=['custom', 'silk-custom', 'both'], default='both')
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text(encoding='utf-8'))
    cfg = load_settings()
    model = ExtractionLLM(cfg.data_dir / 'workbench' / cfg.workspace / 'llm-settings.json', lambda *a: None)
    if not model.configured:
        print(json.dumps({'status': 'unavailable', 'reason': '未配置真实模型'}, ensure_ascii=False))
        return 2
    directory = Path(tempfile.mkdtemp(prefix='atlas-design-proposal-live-'))
    (directory / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    report = {'directory': str(directory), 'source': str(args.sources), 'mode': '第二阶段真实需求单快照；仅新增真实设计生成和修订调用', 'cases': []}
    print(json.dumps({'directory': str(directory)}, ensure_ascii=False), flush=True)
    engine = Engine(Settings(data_dir=directory, workspace='design_proposal_live', seed_demo=False))
    try:
        engine.ready.result(timeout=180)
        work = engine.workbench
        work.llm.settings = model.settings
        async def forbidden(*a, **kw):
            raise AssertionError('设计阶段不应执行新的检索')
        engine.campaign_context = forbidden
        # Every stage uses an explicit UI-equivalent operation, with no automatic retry.
        for entry in sources['cases']:
            if args.case != 'both' and entry['case'] != args.case:
                continue
            run = seed_source(work.demands, entry)
            original = deepcopy(run['briefs'][0])
            record = {'case': entry['case'], 'task_id': run['id'], 'source_task_id': entry['task_id'],
                      'source_brief_id': original['id'], 'source_brief_version': original['version'], 'stages': [], 'status': 'running'}
            report['cases'].append(record)
            for kind in ('demand_design_generate', 'demand_design_revise'):
                extra = {'brief_id': original['id']} if kind.endswith('generate') else {
                    'design_id': run['design_versions'][0]['id'],
                    'instruction': '主色调整为墨绿体系，保留文化故事、产品方向和核心交付；将配色变化同步到所有产品应用。'}
                started = time.monotonic()
                status = work.start(kind, {'run_id': run['id'], **extra})[0].result(timeout=600)
                stage = {'kind': kind, 'status': status['status'], 'seconds': round(time.monotonic() - started, 2),
                         'message': status.get('message'), 'model_calls': [e['data'] for e in status['events'] if e['event_type'] == 'llm.completed']}
                record['stages'].append(stage)
                print(json.dumps({'case': entry['case'], **stage}, ensure_ascii=False), flush=True)
                run = work.demands.load(run['id'])
                (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                if status['status'] != 'success':
                    record['status'] = 'failed'
                    break
                if kind.endswith('generate'):
                    first = deepcopy(run['design_versions'][0])
            else:
                assert run['briefs'][0] == original
                assert run['design_versions'][0] == first
                assert all(v['brief_snapshot'] == original for v in run['design_versions'])
                assert not any(o['kind'] in ('demand_search', 'demand_candidates') for o in run['operations'].values())
                latest = run['design_versions'][-1]
                record.update(status='success', versions=len(run['design_versions']), source_unchanged=True,
                              no_retrieval=True, first=export_bundle(run, first), latest=export_bundle(run, latest))
                (directory / (entry['case'] + '.md')).write_text(export_markdown(run, latest), encoding='utf-8')
                (directory / (entry['case'] + '.json')).write_text(json.dumps(export_bundle(run, latest), ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        engine.close()
    print(json.dumps({'directory': str(directory), 'cases': [{k: c[k] for k in ('case', 'status')} for c in report['cases']]}, ensure_ascii=False))
    return 0 if report['cases'] and all(c['status'] == 'success' for c in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
