# -*- coding: utf-8 -*-
"""全局配置：要采集哪个城市。

用环境变量 LJ_CITY 指定链家城市代码（就是 https://<代码>.lianjia.com 里的那一段），例如：
  bj 北京  sh 上海  gz 广州  sz 深圳  hz 杭州  cd 成都  nj 南京  cq 重庆 ……
不设置时默认 bj。
"""
import os
import re

CITY = os.environ.get('LJ_CITY', 'bj').strip().lower()
SITE = f'https://{CITY}.lianjia.com'          # 城市站首页
ERSHOU = f'{SITE}/ershoufang/'                # 二手房列表页
SITE_RE = re.escape(f'{CITY}.lianjia.com')    # 正则里用的域名

# 可选：区县中文名 → 链家拼音代码（列表页 URL 里 /ershoufang/<代码>/ 那一段）。
# 只有旧版按区县收集链接（lj_collect_urls.py）和预热时访问区县列表页才用到，留空也能跑。
# 例：{'朝阳': 'chaoyang', '海淀': 'haidian'}
DISTRICT_SLUG = {}


def detail_url(house_id):
    """房源详情页 URL"""
    return f'{ERSHOU}{house_id}.html'
