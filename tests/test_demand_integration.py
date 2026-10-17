"""Full demand flow uses real Engine and empty Qdrant without nested locks."""
from lab.config import ROOT, Settings
from lab.engine import Engine
from test_demands import DemandModel, INPUT


def test_real_engine_empty_index_and_reload(tmp_path):
    (tmp_path / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    engine = Engine(Settings(data_dir=tmp_path, workspace='demand_engine_fixture', seed_demo=False))
    try:
        engine.ready.result(timeout=120)
        work = engine.workbench
        work.llm.settings.model = 'deterministic-demand'
        work.llm = DemandModel(work.llm)
        run = work.demands.create(INPUT)
        work.demands.approve(run['id'])
        result = work.start('demand_candidates', {'run_id': run['id']})[0].result(timeout=30)
        assert result['status'] == 'success'
        run = work.demands.load(run['id'])
        batch = run['candidate_rounds'][0]
        assert batch['context']['knowledge_missing']
        work.demands.select_candidate(run['id'], batch['id'], 'C1')
        result = work.start('demand_brief', {'run_id': run['id']})[0].result(timeout=30)
        assert result['status'] == 'success'
        assert not engine.gate.locked()
        assert work.semantic.get_published_graph()['statements'] == []
        assert work.campaigns.runs() == []
        from lab.demands import DemandService
        reloaded = DemandService(work).load(run['id'])
        assert len(reloaded['briefs']) == 1
        assert reloaded['inputs'] == INPUT
        result = work.start('demand_design_generate', {'run_id': run['id'], 'brief_id': reloaded['briefs'][0]['id']})[0].result(timeout=30)
        assert result['status'] == 'success'
        designed = work.demands.load(run['id'])
        assert designed['design_versions'][0]['brief_snapshot'] == reloaded['briefs'][0]
        assert designed['briefs'] == reloaded['briefs']
        assert work.campaigns.runs() == []
        assert not engine.gate.locked()
    finally:
        engine.close()
