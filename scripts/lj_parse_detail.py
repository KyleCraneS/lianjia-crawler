# -*- coding: utf-8 -*-
"""链家二手房详情页 HTML 解析器（requests 直采版，只依赖服务端渲染内容）。"""
import json
import os
import re


def _clean(s):
    if s is None:
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', s)).strip()


def _one(pattern, html, flags=re.S):
    m = re.search(pattern, html, flags)
    return m.group(1).strip() if m else ''


def parse_detail(html, url):
    d = {'url': url}

    # 标题区
    d['标题'] = _clean(_one(r'<h1[^>]*class="[^"]*main[^"]*"[^>]*>(.*?)</h1>', html)) or _clean(_one(r'<h1[^>]*>(.*?)</h1>', html))
    d['卖点描述'] = _clean(_one(r'<div class="sub"[^>]*>(.*?)</div>', html))

    # 价格
    d['总价_万'] = _one(r'<span class="total"[^>]*>([\d.]+)', html)
    d['单价_元每平'] = _one(r'class="unitPriceValue"[^>]*>([\d,]+)', html).replace(',', '')

    # houseInfo 行
    blk = _one(r'<div class="houseInfo">(.*?)<div class="aroundInfo">', html)
    d['户型'] = _clean(_one(r'class="room"[^>]*>.*?class="mainInfo"[^>]*>(.*?)</div>', blk))
    d['楼层'] = _clean(_one(r'class="room"[^>]*>.*?class="subInfo"[^>]*>(.*?)</div>', blk))
    d['朝向'] = _clean(_one(r'class="type"[^>]*>.*?class="mainInfo"[^>]*title="([^"]+)"', blk)) or _clean(_one(r'class="type"[^>]*>.*?class="mainInfo"[^>]*>(.*?)</div>', blk))
    d['结构_装修'] = _clean(_one(r'class="type"[^>]*>.*?class="subInfo"[^>]*>(.*?)</div>', blk))
    d['建筑面积'] = _clean(_one(r'class="area"[^>]*>.*?class="mainInfo"[^>]*>(.*?)</div>', blk))
    d['年代_楼型'] = _clean(_one(r'class="area"[^>]*>.*?class="subInfo[^>]*>(.*?)</div>', blk))
    d['标签'] = _clean(_one(r'class="haofangInfo[^>]*>(.*?)</a>', blk))

    # aroundInfo
    d['小区名称'] = _clean(_one(r'class="communityName"[^>]*>.*?class="info[^"]*"[^>]*>(.*?)</a>', html))
    d['小区ID'] = _one(r'class="communityName"[^>]*>.*?href="/xiaoqu/(\d+)/"', html)
    d['所在区域'] = _clean(_one(r'class="areaName"[^>]*>.*?class="info"[^>]*>(.*?)</span>', html)).replace('&nbsp;', ' ')
    parts = [p for p in re.split(r'[\s\xa0]+', d['所在区域']) if p]
    d['区域_区'], d['区域_商圈'], d['区域_环线'] = (parts + ['', '', ''])[:3]
    d['地铁'] = _one(r'class="supplement"[^>]*title="([^"]+)"', html)
    d['看房时间'] = _clean(_one(r'class="visitTime"[^>]*>.*?class="info"[^>]*>(.*?)</span>', html))
    d['链家编号'] = _one(r'class="houseRecord"[^>]*>.*?class="info"[^>]*>(\d+)', html)

    # 基本属性 / 交易属性（label -> value 列表；值可能是裸文本或 span 包裹）
    def attr_block(name):
        i = html.find(f'>{name}</div>')
        if i < 0:
            return {}
        seg = html[i:i + 4000]
        seg = seg[:seg.find('</ul>')] if '</ul>' in seg else seg
        pairs = {}
        for m in re.finditer(
                r'<span class="label[^"]*"[^>]*>([^<]+)</span>\s*(?:<span[^>]*?(?:title="([^"]*)")?[^>]*>\s*(.*?)\s*</span>|([^<]+))',
                seg, re.S):
            k = m.group(1).strip()
            v = (m.group(2) or _clean(m.group(3)) or _clean(m.group(4)))
            if k:
                pairs[k] = re.sub(r'\s+', ' ', v).strip()
        return pairs

    base = attr_block('基本属性')
    trade = attr_block('交易属性')
    d['基本属性'] = json.dumps(base, ensure_ascii=False)
    d['交易属性'] = json.dumps(trade, ensure_ascii=False)
    for k in ['挂牌时间', '上次交易', '房屋年限', '交易权属', '房屋用途', '产权所属', '抵押信息']:
        if k in trade:
            d[k] = trade[k]
    if '建筑面积' not in d or not d['建筑面积']:
        d['建筑面积'] = base.get('建筑面积', '')
    d['套内面积'] = base.get('套内面积', '')
    d['梯户比例'] = base.get('梯户比例', '')
    d['配备电梯'] = base.get('配备电梯', '')

    # 房源介绍（业主自荐：核心卖点/周边配套/税费解析/装修描述/交通出行 等 baseattribute 块）
    intro = {}
    for m in re.finditer(
            r'<div class="baseattribute[^"]*"[^>]*>\s*<div class="name"[^>]*>\s*([^<]+?)\s*</div>\s*'
            r'<div class="content"[^>]*>(.*?)</div>\s*</div>',
            html, re.S):
        nm = m.group(1).strip()
        txt = _clean(m.group(2))
        if nm and txt:
            intro[nm] = txt
    d['房源介绍'] = json.dumps(intro, ensure_ascii=False)
    d['核心卖点'] = intro.get('核心卖点', '')

    # 经纪人（data-agent-info JSON）
    m = re.search(r"data-agent-info='(\{.*?\})'", html, re.S)
    if m:
        try:
            aj = json.loads(m.group(1).replace('&quot;', '"'))
        except json.JSONDecodeError:
            aj = {}
        d['经纪人门店'] = aj.get('parentShortName', '')
        d['经纪人机构'] = aj.get('orgName', '')
        d['经纪人电话'] = aj.get('phone400', '')
        d['经纪人司龄'] = aj.get('seniority', '')
        d['经纪人学校'] = aj.get('school', '')
        d['经纪人头像'] = aj.get('avatar', '')
        d['经纪人UCID'] = aj.get('agentUcid', '') or aj.get('ucid', '')
    else:
        for k in ['经纪人门店', '经纪人机构', '经纪人电话', '经纪人司龄', '经纪人学校', '经纪人头像', '经纪人UCID']:
            d[k] = ''

    # 坐标
    pos = _one(r"resblockPosition:'([\d.,-]+)'", html)
    if pos and ',' in pos:
        d['经度'], d['纬度'] = pos.split(',', 1)
    else:
        d['经度'] = d['纬度'] = ''

    d['房源ID'] = _one(r'/ershoufang/(\d+)\.html', url)
    return d


FIELDS = ['房源ID', 'url', '标题', '卖点描述', '总价_万', '单价_元每平', '户型', '楼层', '朝向',
          '结构_装修', '建筑面积', '年代_楼型', '标签', '小区名称', '小区ID', '所在区域', '区域_区',
          '区域_商圈', '区域_环线', '地铁', '看房时间', '链家编号', '挂牌时间', '上次交易', '房屋年限',
          '交易权属', '房屋用途', '产权所属', '抵押信息', '套内面积', '梯户比例', '配备电梯',
          '基本属性', '交易属性', '核心卖点', '房源介绍', '经纪人门店', '经纪人机构', '经纪人电话',
          '经纪人司龄', '经纪人学校', '经纪人头像', '经纪人UCID', '经度', '纬度', '抓取时间']


if __name__ == '__main__':
    import io
    import sys
    import time
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_detail_sample.html'), encoding='utf-8').read()
    d = parse_detail(html, sys.argv[1] if len(sys.argv) > 1 else 'sample')
    d['抓取时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
    for k, v in d.items():
        print(f'{k}: {str(v)[:80]}')
