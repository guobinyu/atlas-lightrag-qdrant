"""Presentation of complete marketing deliverables, with native copy controls."""
from html import escape

import streamlit as st
from lab.campaign_content import display_plan, video_text


def render_content(raw, execution=False):
    plan = display_plan(raw)
    if execution:
        st.markdown('<p class="mobile-table-note">左右滑动表格，查看完整执行安排。</p>', unsafe_allow_html=True)
        st.subheader('执行排期')
        st.table([{'日期': f"第{r.get('day', '')}天", '渠道': r.get('channel', ''), '动作': r.get('action', ''), '交付物': r.get('deliverable', ''), '负责角色': r.get('owner', '')} for r in plan['schedule']])
        st.subheader('预算分配')
        st.caption(f"演示预算合计：¥{plan['overview'].get('budget_total', '')}")
        st.table([{'分类': r.get('category', ''), '金额（元）': r.get('amount', ''), '用途': r.get('purpose', '')} for r in plan['budget']])
        st.subheader('目标与调整')
        st.caption('以下为演示目标，用于安排执行与复盘。')
        st.table([{'指标': r.get('metric', ''), '目标': r.get('target', ''), '调整动作': r.get('adjustment', '')} for r in plan['metrics']])
        return
    st.subheader(plan.get('title', '营销方案'))
    overview = plan['overview']
    def item(label, value, detail=''):
        return ('<div class="summary-item"><small>' + escape(label) + '</small><strong>'
                + escape(str(value)) + '</strong>'
                + ('<p>' + escape(str(detail)) + '</p>' if detail else '') + '</div>')
    channels = overview.get('channels', [])
    channel_text = ' / '.join(str(c) for c in channels if isinstance(c, str)) if isinstance(channels, list) else ''
    st.markdown('<div class="campaign-summary">'
                + item('策划品牌', overview.get('brand', ''), overview.get('brand_basis', ''))
                + item('主推产品', overview.get('product', ''))
                + item('目标人群', overview.get('audience', ''))
                + item('传播周期', str(overview.get('duration_days', '')) + ' 天')
                + item('示例预算', '¥' + str(overview.get('budget_total', '')))
                + item('传播目标', overview.get('goal', ''))
                + item('传播渠道', channel_text) + '</div>', unsafe_allow_html=True)
    st.write(f"**产品卖点**：{overview.get('selling_points', '')}")
    st.subheader('核心策略')
    for key, label in [('opportunity', '话题机会'), ('audience_need', '人群需求'), ('product_value', '产品价值'), ('conversion', '行动转化')]:
        st.markdown('**' + label + '**')
        st.write(plan['strategy'].get(key, ''))
    st.subheader('创意主题')
    st.markdown('<div class="creative-theme"><strong>' + escape(str(plan['theme'].get('name', '')))
                + '</strong><p>' + escape(str(plan['theme'].get('slogan', ''))) + '</p></div>', unsafe_allow_html=True)
    for key, label in [('activity', '核心活动'), ('participation', '参与方式')]:
        st.write(f"**{label}**：{plan['theme'].get(key, '')}")
    st.subheader('可直接使用的内容')
    st.caption('点击每份内容右上角的复制按钮，可复制完整文案或脚本。')
    for post in plan['posts']:
        st.markdown('**' + str(post.get('channel', '')) + ' · ' + str(post.get('title', '')) + '**')
        st.code('\n\n'.join(str(post.get(k, '')) for k in ('title', 'body', 'cta')), language=None, wrap_lines=True)
    st.markdown('**短视频脚本**')
    st.code(video_text(plan['video']), language=None, wrap_lines=True)
    with st.expander('策划假设'):
        for item in plan['assumptions']:
            st.write(item)
