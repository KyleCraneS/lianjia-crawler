# -*- coding: utf-8 -*-
"""链家二手房（旧版）：阶段1 —— 按区域(必要时按价格/面积/房型细分)收集全部房源链接。

断点续传：状态存 .tmp/lj_url_state.json，结果增量写入 输出/ershoufang_urls.csv
"""
import csv
import io
import json
import os
import random
import re
import sys

from lj_config import DISTRICT_SLUG, ERSHOU
import time

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = open(f'{BASE}/.tmp/lj_ua.txt', encoding='utf-8').read().strip()
COOKIES = {c['name']: c['value'] for c in json.load(open(f'{BASE}/.tmp/lj_cookies.json', encoding='utf-8'))}
OUT_CSV = f'{BASE}/输出/ershoufang_urls.csv'
STATE = f'{BASE}/.tmp/lj_url_state.json'
LOG = f'{BASE}/.tmp/lj_collect.log'

# 区县列表取自 lj_config.DISTRICT_SLUG，用这个旧脚本前先在那里填好你所在城市的区县
DISTRICTS = [(slug, name) for name, slug in DISTRICT_SLUG.items()]
PRICE_BANDS = [(0, 50), (50, 80), (80, 100), (100, 120), (120, 150),
               (150, 200), (200, 300), (300, 500), (500, None)]
AREA_BANDS = [(0, 50), (50, 70), (70, 90), (90, 110), (110, 130),
              (130, 150), (150, 200), (200, 300), (300, None)]
ROOMS = ['l1', 'l2', 'l3', 'l4', 'l5', 'l6']

CAP = 100  # 链家列表页硬上限


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def build_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': ERSHOU,
    })
    s.cookies.update(COOKIES)
    return s


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    return {'done_leaves': [], 'ids': {}}


def save_state(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def append_rows(rows):
    if not rows:
        return
    new = not os.path.exists(OUT_CSV)
    with open(OUT_CSV, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['id', 'url', 'district', 'filter_path'])
        w.writerows(rows)


def is_captcha(text, status, loc=''):
    return (status in (301, 302) and 'captcha' in (loc or '')) \
        or '<title>CAPTCHA</title>' in text or '人机验证' in text


def fetch(sess, url, captcha_cnt):
    for attempt in range(5):
        time.sleep(random.uniform(4.5, 7.0))
        try:
            r = sess.get(url, timeout=20, allow_redirects=False)
        except requests.RequestException as e:
            log(f'网络异常 {url}: {e}, 20s 后重试')
            time.sleep(20)
            continue
        if is_captcha(r.text, r.status_code, r.headers.get('Location', '')):
            captcha_cnt[0] += 1
            wait = min(600, 120 * captcha_cnt[0])
            log(f'触发人机验证({captcha_cnt[0]}): {url}, 休眠 {wait}s')
            time.sleep(wait)
            continue
        captcha_cnt[0] = 0
        return r.text
    raise RuntimeError(f'连续验证码，放弃: {url}')


def parse_total_page(html):
    m = re.search(r"page-data='(\{[^']+\})'", html)
    if m:
        try:
            return json.loads(m.group(1)).get('totalPage', 0)
        except json.JSONDecodeError:
            pass
    return None


def fmt_band(band, plo, phi):
    lo, hi = band
    if lo is None:
        return f'{phi}{hi}'
    if hi is None:
        return f'{plo}{lo}'
    return f'{plo}{lo}{phi}{hi}'


def leaf_key(district, extra):
    return f'{district}|{extra or "-"}'


def collect_leaf(sess, st, captcha_cnt, district, dname, extra, path):
    key = leaf_key(district, extra)
    if key in st['done_leaves']:
        return
    first_url = f'{ERSHOU}{district}/{extra}/'
    first = fetch(sess, first_url, captcha_cnt)
    total = parse_total_page(first)
    ids = re.findall(r'/ershoufang/(\d+)\.html', first)
    if total is None:
        total = 1 if ids else 0
    if total > CAP:
        subdivide(sess, st, captcha_cnt, district, dname, extra, path)
        return
    rows = []
    for hid in ids:
        if hid not in st['ids']:
            st['ids'][hid] = 1
            rows.append([hid, f'{ERSHOU}{hid}.html', dname, path])
    for pg in range(2, total + 1):
        url = f'{ERSHOU}{district}/{extra}/pg{pg}/'
        html = fetch(sess, url, captcha_cnt)
        ids = re.findall(r'/ershoufang/(\d+)\.html', html)
        for hid in ids:
            if hid not in st['ids']:
                st['ids'][hid] = 1
                rows.append([hid, f'{ERSHOU}{hid}.html', dname, path])
        if pg % 20 == 0 or pg == total:
            log(f'{path}: {pg}/{total} 页')
        if pg % 20 == 0:
            append_rows(rows)
            rows = []
            save_state(st)
            time.sleep(random.uniform(20, 40))
    append_rows(rows)
    st['done_leaves'].append(key)
    save_state(st)
    log(f'{path}: 完成，全库累计 {len(st["ids"])}')


def subdivide(sess, st, captcha_cnt, district, dname, extra, path):
    if 'bp' not in extra:
        log(f'{path}: 超上限，按价格细分')
        for band in PRICE_BANDS:
            ex = extra + fmt_band(band, 'bp', 'ep')
            collect_leaf(sess, st, captcha_cnt, district, dname, ex, f'{path}>{ex}')
    elif 'ba' not in extra:
        log(f'{path}: 仍超上限，按面积细分')
        for band in AREA_BANDS:
            ex = extra + fmt_band(band, 'ba', 'ea')
            collect_leaf(sess, st, captcha_cnt, district, dname, ex, f'{path}>{ex}')
    elif not re.search(r'l[1-6]$', extra):
        log(f'{path}: 仍超上限，按房型细分')
        for r in ROOMS:
            ex = extra + r
            collect_leaf(sess, st, captcha_cnt, district, dname, ex, f'{path}>{r}')
    else:
        log(f'{path}: 无法再细分，放弃剩余部分')


def main():
    st = load_state()
    log(f'开始收集，已完成叶片 {len(st["done_leaves"])}，已有 id {len(st["ids"])}')
    sess = build_session()
    captcha_cnt = [0]
    for slug, dname in DISTRICTS:
        collect_leaf(sess, st, captcha_cnt, slug, dname, '', dname)
        log(f'===== {dname} 完成，全库 {len(st["ids"])} =====')
    log(f'全部完成，共 {len(st["ids"])} 套房源链接 -> {OUT_CSV}')


if __name__ == '__main__':
    main()
