"""One real-model/public-search evaluation, isolated from the user graph."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.demo import build_demo
from lab.llm import ExtractionLLM
from lab.ontology.service import SemanticService
from lab.sources import SourceStore


async def evaluate(config, output, topic):
    events = []
    def emit(event, data):
        events.append({'event': event, 'data': data})
        print(event, flush=True)
    llm = ExtractionLLM(config, emit)
    with tempfile.TemporaryDirectory(prefix='atlas-auto-live-') as directory:
        directory = Path(directory)
        service = SemanticService(directory / 'ontology')
        result = await build_demo(topic, 1, llm, SourceStore(directory / 'sources'), service, emit)
        report = {'kind': 'live-model-public-source-smoke', 'model': llm.settings.public(), 'result': result,
                  'events': events, 'chunks': service.report(), 'candidates': service.list_candidates(),
                  'note': '仅在临时数据库中发布；不是全面的历史知识准确率评估。'}
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--topic', default='宋朝文化 宋代 点茶 茶具')
    args = parser.parse_args()
    asyncio.run(evaluate(args.config, args.output, args.topic))
