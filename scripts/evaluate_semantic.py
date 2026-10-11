"""Explicit opt-in live-model smoke evaluation. Sends ONLY synthetic fixtures.

Run: .venv/bin/python scripts/evaluate_semantic.py --config PATH --output PATH
Never automatically approves or publishes extracted candidates.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.llm import ExtractionLLM
from lab.ontology.service import SemanticService
from lab.ontology.profile import VERSION, PROMPT_VERSION

INPUTS = {
 'silk': '【合成评估，非历史事实】织造是一种传统技艺，不代表某次具体活动。演示记录记载：张甲在甲村实施了本次织造，使用丝线制成这件织锦样品。记录没有说明日期。',
 'tea': '【合成评估，非历史事实】炒青是一种制茶技艺。演示记录记载：李乙实施了本次制茶，采用炒青技艺。记录没有说明时间和地点。不得把一般炒青说明当成另一次事件。',
 'wine': '【合成评估，非历史事实】发酵是一种酿造技艺。演示记录记载：甲村的王丙实施了一次酿造；乙村另一个同名的王丙实施了另一次酿造。两次活动的日期和产品均未记载。',
}

async def main(config, output, domains):
    llm=ExtractionLLM(config, lambda *a:None)
    llm.settings.validate()
    result={'kind':'live-model-synthetic-smoke', 'model':llm.settings.public(), 'profile':VERSION, 'prompt':PROMPT_VERSION,
            'note':'自动校验不等于人工事实核对；人工类型、方向、证据判断单独记载。未发布任何知识。', 'cases':[]}
    with tempfile.TemporaryDirectory(prefix='atlas-semantic-live-') as directory:
        service=SemanticService(Path(directory))
        for domain,text in INPUTS.items():
            if domain not in domains:
                continue
            bid=service.create_batch([{'id':domain,'title':'合成评估','text':text}],llm.settings.public(),domain)
            status=await service.run(bid,llm,lambda *a:None,auto_publish=False)
            candidates=[s for s in service.list_candidates() if s['batch_id']==bid]
            result['cases'].append({'domain':domain,'input':text,'status':status,'chunks':service.report(bid),'candidates':candidates,
                                    'valid':sum(s['validation']['valid'] for s in candidates),'total':len(candidates)})
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(domain,status,len(candidates),'candidates',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--domains', nargs='+', choices=list(INPUTS), default=list(INPUTS))
    args=parser.parse_args()
    asyncio.run(main(args.config,args.output,args.domains))
