def content_plan(days=7, budget=30000):
    return {
        'content_version': 2, 'title': '把东方仪式感穿进日常',
        'overview': {'brand': '织光', 'brand_basis': '策划品牌', 'product': '日常丝巾', 'selling_points': '一条丝巾搭出三种场景', 'audience': '城市通勤女性', 'goal': '品牌曝光', 'duration_days': days, 'budget_total': budget, 'channels': ['小红书', '视频号']},
        'strategy': {'opportunity': '从文化展览话题切入日常穿搭', 'audience_need': '看展后想把文化带回生活', 'product_value': '丝巾用低门槛搭配表达东方美学', 'conversion': '发布搭配挑战，引导预约门店试穿'},
        'theme': {'name': '把东方仪式感穿进日常', 'slogan': '今天也值得认真打扮', 'activity': '三种场景穿搭挑战', 'participation': '发布自己的穿搭并预约试穿'},
        'posts': [{'id': 'P'+str(i), 'channel': channel, 'title': '今天把展览穿在身上', 'body': '周末看完展览，不妨把喜欢的东方色彩留在身边。一条丝巾可以系在领口，陪你走进办公室；也可以装饰随身包袋，为普通的一天添上特别的心情。' * 3, 'cta': '留言分享你的搭配，预约门店试穿。'} for i, channel in enumerate(['小红书', '视频号'], 1)],
        'video': {'id': 'V1', 'channel': '视频号', 'title': '一条丝巾的三种日常', 'scenes': [{'time': f'{i*10}—{(i+1)*10}秒', 'visual': '模特从通勤穿搭切换到看展造型', 'voiceover': '今天，把喜欢的东方色彩穿在身上。', 'subtitle': '今天也值得认真打扮'} for i in range(3)], 'cta': '点击主页预约试穿'},
        'schedule': [{'day': d, 'channel': '小红书', 'action': '发布主题内容并回复评论', 'deliverable': '图文、短视频及互动记录', 'owner': '内容运营', 'content_ids': ['P1','P2','V1'] if d==1 else []} for d in range(1, days+1)],
        'budget': [{'category': '内容制作', 'amount': budget, 'purpose': '拍摄搭配图文与视频'}],
        'metrics': [{'metric': '互动量', 'target': '100次', 'adjustment': '低于50次则替换首图'}, {'metric': '咨询数', 'target': '20次', 'adjustment': '不足10次时加强评论区预约引导'}],
        'assumptions': ['预算为演示估算', '日期仅作参考，不代表已核实的近期热点。'],
    }
