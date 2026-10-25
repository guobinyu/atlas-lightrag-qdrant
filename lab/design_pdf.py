"""On-demand PDF export of a frozen design and its saved concept artwork."""
from html import escape
from io import BytesIO
from pathlib import Path
import re
from threading import Lock

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image

from lab.demand_design import sections
from lab.demand_images import asset_path, selection_key

_FONT_LOCK = Lock()


def chinese_font():
    with _FONT_LOCK:
        name = 'DesignCJK'
        if name in pdfmetrics.getRegisteredFontNames():
            return name
        # Embed a Chinese font when present; standard CJK font is portable fallback.
        for path in ('/System/Library/Fonts/STHeiti Light.ttc',
                     '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
                     'C:/Windows/Fonts/msyh.ttc'):
            if Path(path).is_file():
                try:
                    pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
                    return name
                except Exception:
                    continue
        name = 'STSong-Light'
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(name))
        return name


def export_pdf(service, run, design):
    """Return bytes. Never retrieves remote assets or changes saved records."""
    font = chinese_font()
    styles = {
        'body': ParagraphStyle('body', fontName=font, fontSize=10, leading=15,
                               wordWrap='CJK', spaceAfter=6, textColor=colors.HexColor('#233F43')),
        'title': ParagraphStyle('title', fontName=font, fontSize=22, leading=30,
                                wordWrap='CJK', spaceAfter=16, textColor=colors.HexColor('#11675C')),
        'heading': ParagraphStyle('heading', fontName=font, fontSize=15, leading=23,
                                  wordWrap='CJK', spaceBefore=16, spaceAfter=10, keepWithNext=True),
        'sub': ParagraphStyle('sub', fontName=font, fontSize=11, leading=18,
                              wordWrap='CJK', spaceBefore=8, spaceAfter=6, keepWithNext=True),
    }
    def para(text, style='body'):
        # Model/user text is data, never ReportLab markup or a resource URL.
        return Paragraph(escape(str(text)).replace('\n', '<br/>'), styles[style])

    story = []
    for i, (title, body, _) in enumerate(sections(run, design)):
        story.append(para(title, 'title' if i == 0 else 'heading'))
        for block in re.split(r'\n\s*\n', body):
            style = 'body'
            if block.startswith('### ') or (block.startswith('**') and block.endswith('**') and '\n' not in block):
                style = 'sub'
            text = re.sub(r'^###\s*', '', block)
            text = text.replace('**', '').replace('`', '')
            story.append(para(text, style))
    assets = [a for a in run.get('image_assets', []) if a['design_id'] == design['id']]
    # Application drafts first, then tileable pattern drafts as their own section.
    assets.sort(key=lambda a: a.get('mode') == 'pattern')
    if not assets:
        story += [para('设计图稿', 'heading'), para('此设计版本尚无已保存图稿。')]
    for asset in assets:
        pattern = asset.get('mode') == 'pattern'
        story.append(PageBreak())
        story.append(para('纹样初稿' if pattern else '设计图稿 · ' + asset['application_name'], 'heading'))
        chosen = run.get('image_selections', {}).get(selection_key(asset)) == asset['id']
        story.append(para(f"图稿版本 {asset['version']} · {asset['title']}" + (' · 已选用' if chosen else '')))
        story.append(para('可平铺纹样单元初稿，供图案设计参考；未经接缝与分色检查。' if pattern else '平面概念稿，供设计讨论；像素尺寸不代表生产尺寸。'))
        try:
            raw = asset_path(service, run['id'], asset['path']).read_bytes()
            image = Image(BytesIO(raw))
            scale = min(475 / image.imageWidth, 440 / image.imageHeight)
            image.drawWidth = image.imageWidth * scale
            image.drawHeight = image.imageHeight * scale
            image.hAlign = 'CENTER'
            story.extend([Spacer(1, 10), image, Spacer(1, 10)])
        except (OSError, ValueError):
            story.append(para('图片文件缺失或无法读取，本页保留图稿记录。'))
        config = asset.get('model_config', {})
        story.append(para(f"模型：{config.get('model', '未记录')} · 像素：{asset['size']}"))
        story.append(para(f"图稿 ID：{asset['id']}\n来源设计版本：{design['version']} · 原图 ID：{asset.get('parent_id') or '初始生成'}"))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#D5E3DF'))
        canvas.line(48, 42, A4[0] - 48, 42)
        canvas.setFont(font, 9)
        canvas.setFillColor(colors.HexColor('#647A77'))
        canvas.drawString(48, 27, f"设计方案 · 版本 {design['version']} · 概念设计")
        canvas.drawRightString(A4[0] - 48, 27, str(doc.page))
        canvas.restoreState()

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=54, rightMargin=54,
                            topMargin=48, bottomMargin=58, title=design['content']['title'], author='ATLAS')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
