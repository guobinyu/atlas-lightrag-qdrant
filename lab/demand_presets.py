"""Selectable capacity options, scenario templates and style presets. Prefills are drafts, never confirmed facts."""
CATEGORIES = ['丝巾', '围巾', '领带', '家居服', '旗袍', '校服', '抱枕', '团扇', '书签', '礼盒包装', '面料']
CRAFTS = ['数码印花', '丝网印花', '提花织造', '刺绣', '缂丝', '扎染', '手绘']
STYLES = ['简约', '典雅', '喜庆', '国潮', '清新']
TEMPLATE_FIELDS = ('audience', 'scene', 'purpose', 'theme')
TEMPLATES = {
    'wedding': {'label': '婚礼丝绸用品', 'audience': '新人及双方亲友', 'scene': '婚礼仪式、答谢宴与回门',
                'purpose': '婚礼自用与宾客回礼', 'theme': '喜庆、吉祥与相守',
                'focus': '区分新人自用与宾客回礼的数量和单价；吉祥寓意避免陈旧刻板；可预留姓名、日期等个性化位置；按婚期倒排交付时间。'},
    'uniform': {'label': '校服', 'audience': '在校学生及家长', 'scene': '日常上学、典礼与运动',
                'purpose': '学校统一着装', 'theme': '校园文化与学校标识',
                'focus': '尺码段与版型覆盖身高跨度；关注耐洗、耐磨、透气等面料表现；穿着安全及相关标准由商家确认；校徽、校名和配色需学校授权。'},
    'alumni': {'label': '校友纪念品', 'audience': '返校校友', 'scene': '校庆、返校日与校友聚会',
               'purpose': '纪念与赠礼', 'theme': '母校记忆与重逢',
               'focus': '可加入届别、年份等个性化信息；校名校徽需学校授权；兼顾携带与长期收藏。'},
    'souvenir': {'label': '文旅伴手礼', 'audience': '游客及商务访客', 'scene': '景区、酒店与城市礼赠',
                 'purpose': '旅行纪念与城市名片', 'theme': '地域文化与城市地标',
                 'focus': '体积轻、便于携带；地域符号需有出处，避免泛化；考虑多个价格档位和批量补货。'},
}
