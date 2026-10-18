from copy import deepcopy
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from lab.design_pdf import export_pdf
from test_demands import demand, work
from test_demand_design import designed
from test_demand_images import images, generated
from test_demand_ui import page


def test_text_pdf_chinese_and_version_isolation(demand):
    run = designed(demand)
    design = run['design_versions'][0]
    design['content']['concept'] += '\n<font color="red">示例 & 特殊符号</font>'
    before = deepcopy(run)
    raw = export_pdf(demand.demands, run, design)
    reader = PdfReader(BytesIO(raw))
    text = '\n'.join(p.extract_text() for p in reader.pages)
    for expected in ('整体概念', '视觉设计', '产品应用与交付', '来源与依据', '尚无已保存图稿', '特殊符号'):
        assert expected in text
    assert run == before
    assert raw.startswith(b'%PDF-')
    app = page(demand)
    assert not app.exception
    assert any(b.label == '导出设计方案 PDF' for b in app.get('download_button'))


def test_pdf_saved_images_selection_and_missing_file(images):
    run = generated(images)
    design = run['design_versions'][0]
    asset = run['image_assets'][0]
    run['image_selections'] = {design['id'] + ':0': asset['id']}
    foreign = {**asset, 'design_id': 'other', 'title': '不应导出其他版本'}
    run['image_assets'].append(foreign)
    before = deepcopy(run)
    raw = export_pdf(images.demands, run, design)
    reader = PdfReader(BytesIO(raw))
    text = '\n'.join(p.extract_text() for p in reader.pages)
    assert '已选用' in text and '不应导出其他版本' not in text
    assert sum(len(p.images) for p in reader.pages) == 3
    assert run == before
    Path('/tmp/design-pdf-preview.pdf').write_bytes(raw)
    from lab.demand_images import asset_path
    asset_path(images.demands, run['id'], asset['path']).unlink()
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(export_pdf(images.demands, run, design))).pages)
    assert '图片文件缺失' in text
