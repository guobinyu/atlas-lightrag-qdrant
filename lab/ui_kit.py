"""Shared platform chrome: top navigation, badges, swatches and category icons."""
from html import escape

import streamlit as st

BRAND = '智能设计平台'
NAV = {'home': ('首页', 'home'), 'predict': ('爆款预测', 'trending_up'), 'custom': ('定制设计', 'design_services'),
       'culture': ('知识库', 'menu_book'), 'pattern': ('纹样设计', 'texture'), 'campaign': ('营销生成', 'campaign')}
SYSTEM = {'collect': '资料工作台', 'graph': '知识图谱', 'semantic': '标准语义图谱', 'vectors': '向量数据库',
          'retrieve': '检索观察台', 'settings': '模型、企业与日历设置'}
# One wording per boundary. Details live in each page's 「依据与边界」 note, not repeated per item.
BADGES = {'heat': ('热度未验证', 'gray'), 'score': ('预测评分', 'blue'), 'model': ('模型判断', 'violet'),
          'interpretation': ('设计解读', 'orange'), 'documented': ('资料记载', 'green'), 'user': ('用户提供', 'gray'),
          'calendar': ('日历节点', 'blue'), 'pass': ('产能通过', 'green'), 'gap': ('需新增能力', 'orange'),
          'unset': ('未设产能', 'gray'), 'history': ('历史条件', 'gray')}
ROLE = {'main': '主色', 'secondary': '辅助色', 'accent': '点缀色'}
# Checked in order: 校服 before the generic clothing words.
ICONS = [(('礼盒', '包装', '礼品', '套装'), 'redeem'), (('校服', '校徽', '校园'), 'school'), (('书签',), 'bookmark'),
         (('丝巾', '围巾', '领带', '家居服', '旗袍', '服', '衣', '巾'), 'checkroom'), (('抱枕', '靠垫', '家居'), 'weekend'),
         (('团扇', '扇'), 'air'), (('面料', '布料'), 'texture')]


def go(view, **params):
    """Use as an on_click callback; outside callbacks call st.rerun() afterwards."""
    st.query_params.clear()
    st.query_params.update({'view': view, **{k: v for k, v in params.items() if v}})


def top_nav(engine, route):
    # Mirror the URL into the control once per change so browser back/forward and links stay in sync.
    if st.session_state.get('top-nav-url') != route:
        st.session_state['top-nav'] = route if route in NAV else None
        st.session_state['top-nav-url'] = route

    def changed():
        choice = st.session_state['top-nav']
        if choice is None:
            st.session_state['top-nav'] = route if route in NAV else None
            return
        go(choice)
        st.session_state['top-nav-url'] = choice

    with st.container(key='top-bar'):
        brand, nav, system = st.columns([1.35, 5, 0.75], vertical_alignment='center')
        brand.markdown(f'<div class="top-brand">{BRAND}</div>', unsafe_allow_html=True)
        nav.segmented_control('主导航', list(NAV), format_func=lambda k: f':material/{NAV[k][1]}: {NAV[k][0]}',
                              key='top-nav', on_change=changed, label_visibility='collapsed')
        with system.popover('系统', icon=':material/settings:', width='stretch'):
            for key, label in SYSTEM.items():
                st.button(label, key='system-' + key, on_click=go, args=(key,), width='stretch',
                          type='primary' if key == route else 'secondary')
            work = engine.workbench
            settings = engine.settings
            st.caption(f"工作区：{settings.workspace}")
            st.caption(('● 文字模型已配置' if work.llm.configured else '○ 文字模型待配置') + ' · '
                       + ('● 图片服务已配置' if work.images.configured else '○ 图片服务待配置'))
            st.caption('● Qdrant 本地引擎' if getattr(settings, 'qdrant_mode', 'local') == 'local' else '● Qdrant Server')


def badges(*kinds):
    """Inline markdown badges; text always accompanies the color."""
    return ' '.join(f':{BADGES[k][1]}-badge[{BADGES[k][0]}]' for k in kinds)


def boundary(lines):
    with st.expander('依据与边界', icon=':material/info:'):
        for line in lines:
            st.caption(line)


def swatches(palette, roles=False):
    box = 'display:inline-block;width:14px;height:14px;border-radius:3px;border:1px solid #c9d2cc;vertical-align:-2px;margin-right:4px;background:'
    return '　'.join(f'<span style="{box}{escape(c["hex"])}" aria-hidden="true"></span>{escape(c["name"])} <code>{escape(c["hex"])}</code>'
                    + (f'（{ROLE[c["role"]]}）' if roles and c.get('role') in ROLE else '') for c in palette)


def category_icon(text):
    text = text or ''
    name = next((icon for words, icon in ICONS if any(w in text for w in words)), 'category')
    return f':material/{name}:'


def panel_title(text, anchor=None):
    ident = f' id="{escape(anchor)}"' if anchor else ''
    st.markdown(f'<div class="panel-title"{ident}>{escape(text)}</div>', unsafe_allow_html=True)


def workbench_for(run):
    return 'predict' if run['inputs']['path'] == 'trend' else 'custom'


def goto_step(key, step):
    """Open another step on the next run: the tab state is dropped so ?step= decides again."""
    st.query_params['step'] = step
    st.session_state.pop(key, None)


def step_tabs(steps, key):
    """Tabs whose state follows ?step=, so a refresh or shared link opens the same step."""
    def changed():
        st.query_params['step'] = st.session_state[key]
    requested = st.query_params.get('step')
    return st.tabs(steps, key=key, default=requested if requested in steps else steps[0], on_change=changed)
