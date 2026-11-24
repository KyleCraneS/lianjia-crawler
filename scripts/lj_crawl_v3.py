# -*- coding: utf-8 -*-
"""链家详情爬虫 v3：curl_cffi 抓取 + 撞验证自动调 CDP 过验证码 + 温和节奏。

撞验证时的恢复链：
  1. 同步 Chrome cookies 重试一次
  2. 仍被拦 → 浏览器导航到该 URL，若弹验证则自动求解，从浏览器 DOM 提取数据
实测决策参数：--limit 先小样本测量通过率和耗时。
"""
import argparse
import csv
import io
import json
import os
import random
import sys
import time

from curl_cffi import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lj_parse_detail import FIELDS, parse_detail
from lj_config import ERSHOU

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = f'{BASE}/.tmp/lj_detail_state.json'
LOG = f'{BASE}/.tmp/lj_crawl.log'


def log(m):
    line = f'[{time.strftime("%H:%M:%S")}] {m}'
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding='utf-8'))
    return {'done': {}, 'buf': []}


def save_state(st):
    tmp = STATE + '.tmp'
    json.dump(st, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, STATE)


def sync_cookies_from_chrome():
    """从 Chrome 同步最新 cookies（验证通过后调用）"""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        opts = Options()
        opts.debugger_address = '127.0.0.1:9223'
        d = webdriver.Chrome(service=Service(
            os.environ.get('CHROMEDRIVER_PATH', 'chromedriver')), options=opts)
        cs = d.get_cookies()
        d.quit()
        json.dump(cs, open(f'{BASE}/.tmp/lj_cookies.json', 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        return {c['name']: c['value'] for c in cs}
    except Exception as e:
        log(f'cookie 同步失败: {e}')
        return None


class BrowserFallback:
    """浏览器兜底：导航到 URL，弹验证自动求解，从 DOM 提取 HTML"""

    def __init__(self):
        self._solver = None

    def _ensure(self):
        if self._solver is None:
            from lj_captcha import CaptchaSolver
            self._solver = CaptchaSolver(verbose=False)
        return self._solver

    def fetch(self, url, max_solve=3):
        """返回 HTML 字符串；失败返回 None"""
        s = self._ensure()
        cdp = s.cdp
        for attempt in range(max_solve):
            cdp.call('Page.navigate', {'url': url})
            time.sleep(random.uniform(5, 7))
            title = cdp.js('document.title') or ''
            loc = cdp.js('location.href') or ''
            if 'CAPTCHA' not in title and 'captcha' not in loc:
                html = cdp.js('document.documentElement.outerHTML')
                return html
            log('浏览器亦被拦，自动过验证…')
            if s.solve(max_trials=4):
                log('验证通过，重试目标页')
                continue
            else:
                log('自动过验证失败')
                return None
        return None

    def close(self):
        if self._solver:
            self._solver.close()
            self._solver = None


def is_flagged(r):
    return ('captcha' in r.headers.get('Location', '')
            or '<title>CAPTCHA</title>' in r.text or '人机验证' in r.text[:3000])


class AdaptiveThrottle:
    """自适应调速器：根据最近1小时挑战率动态调整请求间隔。

    风控模型（swarm 调研结论）：IP 有信誉分，请求过密/挑战失败会扣分，
    缓慢优雅的请求让分数回血。挑战率越高睡得越久。
    """

    def __init__(self, lo=8.0, hi=14.0):
        self.lo, self.hi = lo, hi
        self.events = []          # (time, challenged)
        self.last_rate = 0.0

    def mark(self, challenged):
        self.events.append((time.time(), challenged))
        if len(self.events) > 200:
            self.events = self.events[-100:]

    def wait(self):
        now = time.time()
        self.events = [e for e in self.events if now - e[0] < 3600]
        n = len(self.events)
        if n >= 8:
            self.last_rate = sum(1 for _, c in self.events if c) / n
        else:
            self.last_rate = 0.0
        r = self.last_rate
        if r > 0.30:
            delay = random.uniform(50, 100)     # 重伤：长休回血
        elif r > 0.15:
            delay = random.uniform(25, 45)
        elif r > 0.05:
            delay = random.uniform(12, 22)
        else:
            delay = random.uniform(self.lo, self.hi)  # 健康：全速
        time.sleep(delay)
        return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--in', dest='in_csv', default=f'{BASE}/输出/ershoufang_urls_sitemap.csv')
    ap.add_argument('--out', dest='out', default=f'{BASE}/输出/ershoufang_details.csv')
    args = ap.parse_args()

    st = load_state()
    rows = list(csv.DictReader(open(args.in_csv, encoding='utf-8-sig')))
    queue = [[r['id'], r['url']] for r in rows if r['id'] not in st['done']]
    if args.limit:
        queue = queue[:args.limit]
    log(f'v3 启动: 待抓 {len(queue)}，已完成 {len(st["done"])}')

    jar = sync_cookies_from_chrome()
    sess = requests.Session(impersonate='chrome')
    sess.headers.update({'Accept-Language': 'zh-CN,zh;q=0.9'})
    if jar:
        sess.cookies.update(jar)

    fb = BrowserFallback()
    fb_broken_until = 0.0
    throttle = AdaptiveThrottle()
    n_ok = n_dead = n_flag = n_fail = 0
    t_start = time.time()

    try:
        while queue:
            hid, url = queue.pop(0)
            try:
                rate = throttle.wait()
                try:
                    r = sess.get(url, headers={'Referer': ERSHOU},
                                 timeout=20, allow_redirects=False)
                except Exception as e:
                    log(f'{hid} 网络异常 {e}')
                    n_fail += 1
                    continue

                challenged = is_flagged(r)
                throttle.mark(challenged)
                html, status = None, None
                if challenged:
                    n_flag += 1
                    log(f'{hid} 被拦(近1h挑战率{rate*100:.0f}%)，自动恢复中…')
                    # 过验证后让分数回血
                    solve_cooldown = random.uniform(45, 90)
                    # 恢复链：先同步 cookie 重试，再浏览器兜底
                    try:
                        jar2 = sync_cookies_from_chrome()
                        if jar2:
                            sess.cookies.clear()
                            sess.cookies.update(jar2)
                        r2 = sess.get(url, headers={'Referer': ERSHOU},
                                      timeout=20, allow_redirects=False)
                        if not is_flagged(r2) and r2.status_code != 404:
                            html, status = r2.text, 'ok'
                    except Exception as e:
                        log(f'{hid} cookie重试异常 {e}')
                    if html is None and time.time() >= fb_broken_until:
                        log(f'{hid} 走浏览器自动过验证')
                        try:
                            html = fb.fetch(url, max_solve=4)
                        except Exception as e:
                            log(f'浏览器兜底异常({type(e).__name__}): {e}')
                            try:
                                fb.close()
                            except Exception:
                                pass
                            fb_broken_until = time.time() + 300
                        status = 'ok' if html else 'fail'
                    time.sleep(solve_cooldown)
                elif r.status_code == 404:
                    status = '已下架'
                    n_dead += 1
                else:
                    html, status = r.text, 'ok'

                if html:
                    d = parse_detail(html, url)
                    if d.get('链家编号'):
                        d['抓取时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
                        st['buf'].append([d.get(k, '') for k in FIELDS] + [status])
                        if status == 'ok':
                            n_ok += 1
                    else:
                        st['done'][hid] = '页面异常'
                        n_fail += 1
                        continue
                st['done'][hid] = status if not html else 'ok'
            except Exception as e:
                log(f'{hid} 未预期异常({type(e).__name__}): {e}')
                st['done'][hid] = '异常'
                n_fail += 1
                time.sleep(10)
                continue

            if len(st['buf']) >= 10:
                newf = not os.path.exists(args.out)
                with open(args.out, 'a', newline='', encoding='utf-8-sig') as f:
                    w = csv.writer(f)
                    if newf:
                        w.writerow(FIELDS + ['状态'])
                    w.writerows(st['buf'])
                st['buf'] = []
                save_state(st)
                el = time.time() - t_start
                spd = (n_ok + n_dead) / el * 3600
                log(f'进度: ok={n_ok} 下架={n_dead} 被拦={n_flag} 异常={n_fail} '
                    f'| 速度 {spd:.0f} 套/小时 | 挑战率 {throttle.last_rate*100:.0f}% | 剩余 {len(queue)}')

    finally:
        if st['buf']:
            newf = not os.path.exists(args.out)
            with open(args.out, 'a', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                if newf:
                    w.writerow(FIELDS + ['状态'])
                w.writerows(st['buf'])
        save_state(st)
        fb.close()
        el = time.time() - t_start
        log(f'结束: ok={n_ok} 下架={n_dead} 被拦={n_flag} 异常={n_fail} 耗时{el/60:.1f}分钟')
        if not queue:
            open(f'{BASE}/.tmp/lj_crawl_done.txt', 'w').write('done')
            log('队列已清空，写入完成标记')


if __name__ == '__main__':
    main()
