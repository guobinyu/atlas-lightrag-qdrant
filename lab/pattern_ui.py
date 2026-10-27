"""Pattern gallery: tileable pattern drafts from every task, grouped by design version."""
from html import escape

import streamlit as st

from lab import demand_images
from lab.campaign_ui import local_time
from lab.demands import stale
from lab.ui_kit import NAV, badges, boundary, go, workbench_for

STATES = {'running': '执行中', 'success': '已完成', 'failed': '失败', 'interrupted': '已中断'}
RETURN_STEP = {'predict': '设计与纹样', 'custom': '设计方案'}


def pattern_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('纹样设计', '汇总爆款预测与定制设计生成的可平铺纹样初稿，按任务与设计版本查看、选用和下载。', 'PATTERN DESIGN')
    runs = work.demands.runs()
    pipelines = sorted(((r, o) for r in runs for o in r['operations'].values() if o['kind'] == 'demand_pipeline'),
                       key=lambda x: x[1]['created_at'], reverse=True)
    if pipelines:
        with st.expander(f'纹样任务记录 · {len(pipelines)} 项', expanded=any(o['status'] == 'running' for _, o in pipelines)):
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
        assets = [a for a in run.get('image_assets', []) if a.get('mode') == 'pattern']
        if not assets:
            continue
        found = True
        view = workbench_for(run)
        designs = {d['id']: d for d in run.get('design_versions', [])}
        for design_id in dict.fromkeys(a['design_id'] for a in reversed(assets)):
            design = designs.get(design_id)
            items = [a for a in assets if a['design_id'] == design_id]
            current = bool(design) and not stale(run, design) and design['selection'] == run['selection']
            chosen = run.get('image_selections', {}).get(demand_images.selection_key(design_id, 'pattern'))
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
                            st.image(raw, caption=f"纹样 · {asset['title']}" + (' · 已选用' if chosen == asset['id'] else ''), width='stretch')
                            st.download_button('下载 PNG', raw, file_name=f"pattern-{asset['id']}.png", mime='image/png',
                                               key='pattern-download-' + asset['id'], width='stretch')
                            if st.button('已选用' if chosen == asset['id'] else '选用此纹样', key='pattern-select-' + asset['id'],
                                         disabled=busy or not current or chosen == asset['id'], width='stretch'):
                                ok = True
                                try:
                                    demand_images.select(work.demands, run['id'], asset['id'])
                                except ValueError as exc:
                                    ok = False
                                    st.error(str(exc))
                                if ok:
                                    st.rerun()
                st.button('回到设计步骤修改', key=f'pattern-back-{run["id"]}-{design_id}', on_click=go, args=(view,),
                          kwargs={'demand': run['id'], 'step': RETURN_STEP[view]})
    if not found:
        st.info('还没有纹样初稿。可以在爆款预测中「下发纹样任务」，或在设计方案的图稿区选择「纹样初稿」。')
        a, b = st.columns(2)
        a.button('去爆款预测', on_click=go, args=('predict',), width='stretch')
        b.button('去定制设计', on_click=go, args=('custom',), width='stretch')
    boundary(['纹样初稿是可平铺单元的概念图，未经接缝、分色和印花工艺检查；像素尺寸不代表生产精度。',
              '修改纹样请回到对应任务的设计步骤，基于选用图稿生成新版本；原图保留。'])
