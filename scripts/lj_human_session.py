# -*- coding: utf-8 -*-
"""拟人化请求层：curl_cffi Chrome 指纹 + 会话预热 + Referer 链 + 随机延迟。

设计目标：让链路家在指纹层(IP/TLS/HTTP2)和行为层(时序/Referer)都难以区分于真实浏览。
"""
import io
import json
import os
import random
import re
import sys
import time

from curl_cffi import requests
from lj_config import ERSHOU, SITE

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = open(f'{BASE}/.tmp/lj_ua.txt', encoding='utf-8').read().strip()
COOKIES = {c['name']: c['value'] for c in json.load(open(f'{BASE}/.tmp/lj_cookies.json', encoding='utf-8'))}


class HumanSession:
    """拟人化 HTTP 会话"""

    def __init__(self):
        # impersonate='chrome'：TLS/JA3/JA4/HTTP2 指纹与真实 Chrome 完全一致
        self.s = requests.Session(impersonate='chrome')
        self.s.headers.update({'Accept-Language': 'zh-CN,zh;q=0.9', 'UA': UA})
        self.s.cookies.update(COOKIES)
        self.last_url = SITE + '/'
        self.visited = []

    def _think(self, lo=2.5, hi=6.0):
        """模拟人类阅读/思考间隔，偶发长停顿"""
        r = random.random()
        if r < 0.08:
            wait = random.uniform(12, 25)   # 8% 概率长时间停留（阅读/切窗口）
        elif r < 0.25:
            wait = random.uniform(6, 10)    # 17% 概率中等停留
        else:
            wait = random.uniform(lo, hi)   # 常规 2.5-6s
        time.sleep(wait)

    def get(self, url, referer=None):
        """带 Referer 链和拟人延迟的请求"""
        self._think()
        headers = {}
        ref = referer or self.last_url
        if ref and ref != url:
            headers['Referer'] = ref
        r = self.s.get(url, headers=headers, timeout=25, allow_redirects=True)
        self.last_url = url
        self.visited.append(url)
        return r

    def is_flagged(self, r):
        """检测是否被人机验证拦截"""
        loc = r.headers.get('Location', '')
        return ('captcha' in loc or '<title>CAPTCHA</title>' in r.text
                or '人机验证' in r.text[:3000] or r.status_code in (301, 302))

    def warmup(self):
        """会话预热：访问首页，让服务端刷新 token/指纹关联"""
        print('[warmup] 访问首页预热会话…', flush=True)
        r = self.get(SITE + '/')
        print(f'[warmup] http={r.status_code} len={len(r.text)}', flush=True)
        return not self.is_flagged(r)


def smoke():
    hs = HumanSession()
    if not hs.warmup():
        print('[SMOKE] 预热即被拦截，IP/会话仍被标记', flush=True)
        return False

    base = SITE
    tests = [
        ('列表首页', f'{base}/ershoufang/'),
        ('列表pg2', f'{base}/ershoufang/pg2/'),
        ('列表pg3', f'{base}/ershoufang/pg3/'),
        ('列表pg4', f'{base}/ershoufang/pg4/'),
    ]
    ok = 0
    for name, url in tests:
        r = hs.get(url)
        flagged = hs.is_flagged(r)
        ids = len(set(re.findall(r'/ershoufang/(\d+)\.html', r.text)))
        m = re.search(r"page-data='(\{[^']+\})'", r.text)
        title = re.search(r'<title>(.*?)</title>', r.text)
        print(f'[{name}] http={r.status_code} flagged={flagged} ids={ids} '
              f'page={m.group(1) if m else "-"} title={title.group(1)[:30] if title else "-"}', flush=True)
        if not flagged:
            ok += 1
    print(f'[SMOKE] 通过 {ok}/{len(tests)}', flush=True)
    return ok >= len(tests) - 1  # 允许1个失败


if __name__ == '__main__':
    smoke()
