# -*- coding: utf-8 -*-
"""链家 sitemap 全量 URL 收集：索引 → 分片 → 解析房源 URL。

sitemap 每日更新（约上午 10:30-11:15 重新生成），每个分片约 1 万条。
用法: python scripts/lj_sitemap.py [--smoke]   # --smoke 只抓索引和第一个分片验证结构
"""
import argparse
import csv
import io
import os
import random
import re
import sys
import time

from curl_cffi import requests
from lj_config import SITE, SITE_RE

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(BASE, '.tmp'), exist_ok=True)
os.makedirs(os.path.join(BASE, '输出'), exist_ok=True)
# 可选：.tmp/lj_ua.txt 里放自定义 User-Agent；没有就用 curl_cffi 伪装 Chrome 自带的
_UA_FILE = os.path.join(BASE, '.tmp', 'lj_ua.txt')
UA = open(_UA_FILE, encoding='utf-8').read().strip() if os.path.exists(_UA_FILE) else ''
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '输出', 'ershoufang_urls_sitemap.csv')
IDX_URL = f'{SITE}/sitemap.xml'


def log(m):
    print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)


def fetch(url, referer=None):
    headers = {'Accept-Language': 'zh-CN,zh;q=0.9'}
    if referer:
        headers['Referer'] = referer
    r = requests.get(url, headers=headers, impersonate='chrome', timeout=30)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--smoke', action='store_true')
    args = ap.parse_args()

    log(f'抓取索引 {IDX_URL}')
    r = fetch(IDX_URL)
    log(f'http={r.status_code} len={len(r.text)}')
    if r.status_code != 200:
        log(f'索引抓取失败: {r.text[:300]}')
        return
    open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_sitemap_index.xml'), 'w', encoding='utf-8').write(r.text)

    # 解析所有子 sitemap URL（支持 /sitemap-xxx.xml 和 /sitemap/xxx 两种形态）
    subs = re.findall(rf'<loc>(https://{SITE_RE}/sitemap-[^<]+\.xml)</loc>', r.text)
    log(f'子 sitemap 共 {len(subs)} 个')
    for s in subs:
        log(f'  {s}')

    # 房源分片索引（fangyuan.xml 是二级索引，指向若干个万条分片）
    shard_idx = [u for u in subs if 'fangyuan' in u]
    shards = []
    for si in shard_idx:
        time.sleep(random.uniform(3, 6))
        try:
            ri = fetch(si, referer=IDX_URL)
            found = re.findall(rf'<loc>(https://{SITE_RE}/sitemap-[^<]+\.xml)</loc>', ri.text)
            shards.extend(found)
            log(f'{si.split("com/")[1]} -> {len(found)} 个分片')
        except Exception as e:
            log(f'分片索引异常 {si}: {e}')
    log(f'房源分片共 {len(shards)} 个')
    subs = shards

    if args.smoke:
        subs = subs[:1]

    total = 0
    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        csv_w = csv.writer(f)
        csv_w.writerow(['id', 'url', 'source'])
        for si, sub in enumerate(subs):
            time.sleep(3)
            try:
                r2 = fetch(sub, referer=IDX_URL)
            except Exception as e:
                log(f'分片{si} 异常: {e}')
                continue
            if r2.status_code != 200:
                log(f'分片{si} http={r2.status_code}')
                continue
            urls = re.findall(rf'<loc>(https://{SITE_RE}/ershoufang/\d+\.html)</loc>', r2.text)
            xiaoqu = re.findall(rf'<loc>(https://{SITE_RE}/xiaoqu/\d+/)</loc>', r2.text)
            log(f'分片{si}: 房源 {len(urls)} 小区 {len(xiaoqu)}')
            for u in urls:
                hid = re.search(r'/ershoufang/(\d+)\.html', u).group(1)
                csv_w.writerow([hid, u, 'sitemap'])
            total += len(urls)
    log(f'完成：共 {total} 条房源 URL -> {OUT}')


if __name__ == '__main__':
    main()
