"""Platform home: entries to the five workbenches, recent tasks, upcoming nodes and library status."""
from html import escape

import streamlit as st

from lab.campaign_ui import local_time
from lab.demands import current_day
from lab.ui_kit import NAV, go, workbench_for

ENTRIES = [('predict', '结合节日节点、热点与企业产能，研判并推荐细分产品方向。', '个研判任务'),
           ('custom', '面向婚礼、校服、校友纪念品等场景，对话式拆解需求并形成设计说明。', '个定制任务'),
           ('culture', '按主题、样本、素材三层解码文化资料，提供纹样、配色与叙事。', '个已发布素材'),
           ('pattern', '汇总各任务的应用效果图，查看、选用和下载。', '张设计图稿'),
           ('campaign', '结合热点与知识库生成传播方案、图文与短视频脚本。', '个营销任务')]
STATES = {'running': '执行中', 'success': '已完成', 'failed': '失败', 'interrupted': '已中断'}


def run_state(run):
    ops = list(run['operations'].values())
    if ops:
        return STATES.get(ops[-1]['status'], ops[-1]['status'])
    return '需求已确认' if run['approved_revision'] == run['revision'] else '待开始'


def home_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    header('从文化热点到可落地的设计', '结合企业产能研判爆款方向，按场景定制设计，生成设计图稿与营销素材。', 'SMART DESIGN · DIGITAL CULTURE')
    demands, campaigns, stats = work.demands.runs(), work.campaigns.runs(), work.culture.stats()
    counts = {'predict': sum(r['inputs']['path'] == 'trend' for r in demands),
              'custom': sum(r['inputs']['path'] == 'custom' for r in demands),
              'culture': stats['elements'],
              'pattern': sum(len(r.get('image_assets', [])) for r in demands),
              'campaign': len(campaigns)}
    for column, (view, copy, unit) in zip(st.columns(len(ENTRIES)), ENTRIES):
        with column, st.container(border=True, key='home-entry-' + view):
            label, icon = NAV[view]
            st.markdown(f'**:material/{icon}: {label}**')
            st.caption(copy)
            st.caption(f'{counts[view]} {unit}')
            st.button('进入' + label, key='home-go-' + view, on_click=go, args=(view,), width='stretch')
    recent, upcoming, status = st.columns([1.5, 1, 1])
    with recent, st.container(border=True, key='home-recent'):
        st.markdown('**最近任务**')
        if not demands and not campaigns:
            st.caption('还没有任务。可以从爆款预测或定制设计开始。')
        for run in demands[:6]:
            view = workbench_for(run)
            a, b = st.columns([4, 1], vertical_alignment='center')
            a.markdown(f"{NAV[view][0]} · {escape(run['inputs']['request'][:26])}  \n"
                       f"<small>{local_time(run['created_at'])} · {run_state(run)}</small>", unsafe_allow_html=True)
            b.button('打开', key='home-open-' + run['id'], on_click=go, args=(view,), kwargs={'demand': run['id']})
        for run in campaigns[:3]:
            a, b = st.columns([4, 1], vertical_alignment='center')
            a.markdown(f"营销生成 · {escape(run['inputs']['request'][:26])}  \n<small>{local_time(run['created_at'])}</small>", unsafe_allow_html=True)
            b.button('打开', key='home-open-' + run['id'], on_click=go, args=('campaign',), kwargs={'campaign': run['id']})
    with upcoming, st.container(border=True, key='home-calendar'):
        st.markdown('**即将到来的节点**')
        nodes = work.demands.calendar.upcoming(current_day())[:5]
        for node in nodes:
            st.markdown(f"{node['date']} · **{escape(node['name'])}**  \n<small>{node['kind']}{' · ' + escape(node['note']) if node['note'] else ''}</small>", unsafe_allow_html=True)
        if not nodes:
            st.caption('未来 120 天内没有日历节点，可在系统设置中添加。')
        st.caption('日历节点用于安排上市时间，热度未验证。')
    with status, st.container(border=True, key='home-status'):
        st.markdown('**平台状态**')
        st.write(('● ' if work.llm.configured else '○ ') + '文字模型' + ('已配置' if work.llm.configured else '待配置'))
        st.write(('● ' if work.images.configured else '○ ') + '图片服务' + ('已配置' if work.images.configured else '待配置'))
        a, b = st.columns(2)
        a.metric('文化素材', stats['elements'])
        b.metric('样本资料', stats['samples'])
        st.button('打开系统设置', key='home-settings', on_click=go, args=('settings',), width='stretch')
