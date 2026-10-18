"""Generate three real silk concept images and one edit in an isolated workspace."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.config import ROOT, Settings, load_settings
from lab.engine import Engine
from lab.llm import ExtractionLLM
from lab.image_service import ImageService, image_sizes, png_info
from lab.demand_images import asset_path, select
from verify_design_proposal import seed_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=ROOT / 'docs/design-proposal-evaluation.json')
    args = parser.parse_args()
    cfg = load_settings()
    directory = cfg.data_dir / 'workbench' / cfg.workspace
    images = ImageService(directory / 'image-settings.json')
    text = ExtractionLLM(directory / 'llm-settings.json', lambda *a: None)
    if not images.configured or not text.configured:
        print(json.dumps({'status': 'unavailable', 'reason': '请先在模型设置中配置文字模型与独立图片服务；本脚本未调用模型。'}, ensure_ascii=False))
        return 2
    sources = json.loads(args.sources.read_text(encoding='utf-8'))
    entry = next(e for e in sources['cases'] if e['case'] == 'silk-custom')
    design = deepcopy(entry['latest']['design'])
    output = Path(tempfile.mkdtemp(prefix='atlas-design-images-live-'))
    model_cache = cfg.data_dir / 'models'
    if model_cache.exists():
        (output / 'models').symlink_to(model_cache, target_is_directory=True)
    report = {'directory': str(output), 'status': 'running', 'stages': [], 'visual_review': 'pending'}
    engine = Engine(Settings(data_dir=output, workspace='image_acceptance', seed_demo=False))
    print(json.dumps({'directory': str(output)}, ensure_ascii=False), flush=True)
    try:
        engine.ready.result(timeout=180)
        work = engine.workbench
        work.llm.settings = text.settings
        work.images.settings = images.settings
        run = seed_source(work.demands, {'final_brief': design['brief_snapshot']})
        run['design_versions'] = [design]
        work.demands.save(run)
        original = deepcopy(run)
        for kind in ('demand_image_generate', 'demand_image_revise'):
            if kind.endswith('generate'):
                application_index = next(i for i, a in enumerate(design['content']['applications']) if a['priority'] == 'core')
                params = {'design_id': design['id'], 'application_index': application_index, 'size': image_sizes(images.settings.provider)[1]}
            else:
                first = deepcopy(run['image_assets'])
                select(work.demands, run['id'], first[0]['id'])
                params = {'image_id': first[0]['id'], 'instruction': '改为墨绿，减少装饰，保留主体。'}
            begin = time.monotonic()
            result = work.start(kind, {'run_id': run['id'], **params})[0].result(timeout=2400)
            report['stages'].append({'kind': kind, 'status': result['status'], 'message': result.get('message'), 'seconds': round(time.monotonic()-begin, 2)})
            run = work.demands.load(run['id'])
            report['task_id'] = run['id']
            if result['status'] != 'success':
                report['status'] = 'failed'
                break
        else:
            assert len(run['image_assets']) == 4
            assert run['image_assets'][:3] == first
            assert run['design_versions'] == original['design_versions']
            assert run['briefs'] == original['briefs']
            report['images'] = []
            for a in run['image_assets']:
                path = asset_path(work.demands, run['id'], a['path'])
                raw = path.read_bytes()
                assert hashlib.sha256(raw).hexdigest() == a['sha256']
                report['images'].append({'path': str(path), 'id': a['id'], 'parent_id': a['parent_id'], **png_info(raw)})
            report.update(status='success', originals_preserved=True, source_unchanged=True)
    finally:
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        engine.close()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
