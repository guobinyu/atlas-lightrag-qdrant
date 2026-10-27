"""Design draft gallery: application drafts from every task, grouped by design version. Route stays ?view=pattern."""
from html import escape

import streamlit as st

from lab import demand_images
from lab.campaign_ui import local_time
from lab.demands import stale
from lab.ui_kit import NAV, badges, boundary, go, workbench_for

STATES = {'running': '执行中', 'success': '已完成', 'failed': '失败', 'interrupted': '已中断'}
RETURN_STEP = {'predict': '设计与效果图', 'custom': '设计方案'}


def asset_label(asset):
    if asset.get('mode') == 'pattern':
        return '历史纹样初稿'
    return asset.get('application_name') or '效果图'


def pattern_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('设计图稿', '汇总爆款预测与定制设计生成的应用效果图，按任务与设计版本查看、选用和下载。', 'DESIGN DRAFTS')
    runs = work.demands.runs()
    pipelines = sorted(((r, o) for r in runs for o in r['operations'].values() if o['kind'] == 'demand_pipeline'),
                       key=lambda x: x[1]['created_at'], reverse=True)
    if pipelines:
        with st.expander(f'自动生成记录 · {len(pipelines)} 项', expanded=any(o['status'] == 'running' for _, o in pipelines)):
            for run, op in pipelines[:10]:
                view = workbench_for(run)
                a, b = st.columns([5, 1], vertical_alignment='center')
                a.markdown(f"{local_time(op['created_at'])} · {escape(run['inputs']['request'][:30])}  \n"
                           f"<small>{escape(op['stage'])} · {STATES.get(op['status'], op['status'])}</small>", unsafe_allow_html=True)
                if op.get('message') and op['status'] != 'success':
                    a.caption(op['message'])
                b.button('打开任务', key='pattern-open-' + op['id'], on_click=go, args=(view,),
                         kwargs={'demand': run['id'], 'step': RETURN_STEP[view]})
    found = False
    for run in runs:
        assets = run.get('image_assets', [])
        if not assets:
            continue
        found = True
        view = workbench_for(run)
        designs = {d['id']: d for d in run.get('design_versions', [])}
        selected = set(run.get('image_selections', {}).values())
        for design_id in dict.fromkeys(a['design_id'] for a in reversed(assets)):
            design = designs.get(design_id)
            items = [a for a in assets if a['design_id'] == design_id]
            current = bool(design) and not stale(run, design) and design['selection'] == run['selection']
            with st.container(border=True, key=f'pattern-group-{run["id"]}-{design_id}'):
                title = design['content']['title'] if design else '设计版本已不存在'
                st.markdown(f"**{NAV[view][0]} · {escape(run['inputs']['request'][:24])}**　设计版本 {design['version'] if design else '—'} · {escape(title)}")
                if not current:
                    st.markdown(badges('history'))
                    st.caption('对应历史条件或其他产品方向，可以查看和下载，不能选用。')
                for start in range(0, len(items), 3):
                    for column, asset in zip(st.columns(3), items[start:start + 3]):
                        with column:
                            try:
                                raw = demand_images.asset_path(work.demands, run['id'], asset['path']).read_bytes()
                            except (OSError, ValueError):
                                st.error('图片文件不可读取，请检查工作区备份。')
                                continue
                            chosen = asset['id'] in selected
                            st.image(raw, caption=f"{asset_label(asset)} · {asset['title']}" + (' · 已选用' if chosen else ''), width='stretch')
                            st.download_button('下载 PNG', raw, file_name=f"draft-{asset['id']}.png", mime='image/png',
                                               key='pattern-download-' + asset['id'], width='stretch')
                            # Retired pattern drafts stay viewable but are not selectable any more.
                            if asset.get('mode') != 'pattern' and st.button('已选用' if chosen else '选用此图稿', key='pattern-select-' + asset['id'],
                                                                            disabled=busy or not current or chosen, width='stretch'):
                                try:
                                    demand_images.select(work.demands, run['id'], asset['id'])
                                    st.rerun()
                                except ValueError as exc:
                                    st.error(str(exc))
                st.button('回到设计步骤修改', key=f'pattern-back-{run["id"]}-{design_id}', on_click=go, args=(view,),
                          kwargs={'demand': run['id'], 'step': RETURN_STEP[view]})
    if not found:
        st.info('还没有设计图稿。可以在爆款预测或定制设计的设计步骤中生成应用效果图。')
        a, b = st.columns(2)
        a.button('去爆款预测', on_click=go, args=('predict',), width='stretch')
        b.button('去定制设计', on_click=go, args=('custom',), width='stretch')
    boundary(['效果图是针对具体交付项（如酒标、礼盒盒面、丝巾正面）的平面概念稿，不是实物或场景照片；像素尺寸不代表生产精度。',
              '修改图稿请回到对应任务的设计步骤，基于选用图稿生成新版本；原图保留。纹样初稿已停用，此前生成的仍可查看和下载。'])
