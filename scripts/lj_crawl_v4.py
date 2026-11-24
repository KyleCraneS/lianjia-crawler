# -*- coding: utf-8 -*-
"""链家详情爬虫 v4：调研结论落地的"安全采集"版。

相对 v3 的改动（依据 swarm 风控调研）：
  1. 对数正态间隔（中位8s、长尾偶发>1min）+ 3~5% 概率 60-180s 长停顿
  2. 会话纪律：单段 ≤SESSION_MINUTES 分钟到点自动退出（由调度器安排段间休息）
  3. 每 20 个详情前访问所属区域列表页补路径（Referer 同步替换）
  4. 熔断：连续3次拦截休30min；出现 forbidden(账号封禁) 立即退出
  5. 日配额：QUOTA_PER_DAY 到了自动退出，次日由调度器再启动
"""
import argparse
import csv
import io
import json
import math
import os
import random
import subprocess
import sys
import time

from curl_cffi import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lj_parse_detail import FIELDS, parse_detail
from lj_config import DISTRICT_SLUG, ERSHOU, SITE

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = f'{BASE}/.tmp/lj_detail_state.json'
QUOTA = f'{BASE}/.tmp/lj_quota.json'
LOG = f'{BASE}/.tmp/lj_crawl.log'

SESSION_MINUTES = 80
QUOTA_PER_DAY = 900
PREHEAT_EVERY = 20



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


def today_quota():
    q = json.load(open(QUOTA, encoding='utf-8')) if os.path.exists(QUOTA) else {}
    day = time.strftime('%Y-%m-%d')
    return q.get(day, 0), q


def add_quota(n):
    day = time.strftime('%Y-%m-%d')
    _, q = today_quota()
    q[day] = q.get(day, 0) + n
    json.dump(q, open(QUOTA, 'w', encoding='utf-8'))


def sync_cookies():
    """独立子进程同步 Chrome cookies（带超时，防卡死主进程）"""
    try:
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c',
             "import json,os;from selenium import webdriver;"
             "from selenium.webdriver.chrome.options import Options;"
             "from selenium.webdriver.chrome.service import Service;"
             "o=Options();o.debugger_address='127.0.0.1:9223';"
             "d=webdriver.Chrome(service=Service(os.environ.get('CHROMEDRIVER_PATH','chromedriver')),options=o);"
             "cs=d.get_cookies();d.quit();"
             "open(os.path.join('.tmp','lj_cookies.json'),'w',encoding='utf-8').write(json.dumps(cs,ensure_ascii=False))"],
            capture_output=True, timeout=60, cwd=BASE)
        if r.returncode == 0 and os.path.exists(f'{BASE}/.tmp/lj_cookies.json'):
            return {c['name']: c['value'] for c in json.load(open(f'{BASE}/.tmp/lj_cookies.json', encoding='utf-8'))}
    except Exception as e:
        log(f'cookie同步失败: {e}')
    return None


def browser_fallback(url):
    """浏览器兜底：独立子进程跑验证码求解（崩了/卡了只死子进程）"""
    out_html = f'{BASE}/.tmp/lj_fb_{int(time.time())}.html'
    try:
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', f'{BASE}/scripts/lj_captcha.py', url, out_html],
            capture_output=True, timeout=600, cwd=BASE)
        if r.returncode == 0 and os.path.exists(out_html):
            html = open(out_html, encoding='utf-8').read()
            os.remove(out_html)
            return html or None
    except subprocess.TimeoutExpired:
        log('浏览器兜底超时(600s)')
    except Exception as e:
        log(f'浏览器兜底异常: {e}')
    return None


def is_flagged(r):
    return ('captcha' in r.headers.get('Location', '')
            or '<title>CAPTCHA</title>' in r.text or '人机验证' in r.text[:3000])


def is_forbidden(r):
    return 'forbidden' in r.headers.get('Location', '') or '帐号被临时封禁' in r.text[:3000] \
        or '账号被临时封禁' in r.text[:3000]


def human_wait():
    """对数正态间隔 + 偶发长停顿（风控调研：最接近真人的分布）"""
    if random.random() < 0.04:
        time.sleep(random.uniform(60, 180))
    else:
        time.sleep(random.lognormvariate(math.log(8), 0.6))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='in_csv', default=f'{BASE}/输出/ershoufang_urls_sitemap.csv')
    ap.add_argument('--out', dest='out', default=f'{BASE}/输出/ershoufang_details.csv')
    args = ap.parse_args()

    st = load_state()
    rows = list(csv.DictReader(open(args.in_csv, encoding='utf-8-sig')))
    queue = [[r['id'], r['url'], r.get('district', '')] for r in rows if r['id'] not in st['done']]
    used, _ = today_quota()
    remaining_quota = max(0, QUOTA_PER_DAY - used)
    queue = queue[:remaining_quota]
    log(f'v4 会话启动: 待抓 {len(queue)}（今日已用配额 {used}），会话 {SESSION_MINUTES} 分钟')

    jar = sync_cookies()
    if not jar:
        log('Cookie 同步失败，退出')
        return
    sess = requests.Session(impersonate='chrome')
    sess.headers.update({'Accept-Language': 'zh-CN,zh;q=0.9'})
    sess.cookies.update(jar)

    t_end = time.time() + SESSION_MINUTES * 60
    n_ok = n_dead = n_flag = n_fail = 0
    streak = 0          # 连续拦截计数（熔断）
    since_preheat = PREHEAT_EVERY   # 开局先预热
    referer = ERSHOU
    t_start = time.time()

    try:
        while queue and time.time() < t_end:
            hid, url, district = queue.pop(0)
            try:
                # 列表页预热补路径（每 20 个详情一次）
                if since_preheat >= PREHEAT_EVERY:
                    slug = DISTRICT_SLUG.get(district, '')
                    list_url = f'{ERSHOU}{slug}/' if slug else ERSHOU
                    human_wait()
                    try:
                        rl = sess.get(list_url, headers={'Referer': SITE + '/'}, timeout=20)
                        if rl.status_code == 200 and not is_flagged(rl):
                            referer = list_url
                            log(f'预热列表页 OK: {district or "全市"}')
                        else:
                            log('预热被拦（不致命，继续）')
                    except Exception as e:
                        log(f'预热异常 {e}')
                    since_preheat = 0

                human_wait()
                r = sess.get(url, headers={'Referer': referer}, timeout=20, allow_redirects=False)
                since_preheat += 1

                if is_forbidden(r):
                    log('!!! 账号被封禁，立即退出会话')
                    return
                if is_flagged(r):
                    n_flag += 1
                    streak += 1
                    log(f'{hid} 被拦(连续{streak})，自动恢复')
                    jar2 = sync_cookies()
                    if jar2:
                        sess.cookies.clear()
                        sess.cookies.update(jar2)
                    try:
                        r2 = sess.get(url, headers={'Referer': referer}, timeout=20, allow_redirects=False)
                    except Exception:
                        r2 = None
                    html = None
                    if r2 is not None and not is_flagged(r2) and not is_forbidden(r2) and r2.status_code != 404:
                        html = r2.text
                    if html is None:
                        html = browser_fallback(url)
                    if html is None:
                        st['done'][hid] = 'fail'
                        n_fail += 1
                        log(f'{hid} 恢复失败')
                    else:
                        d = parse_detail(html, url)
                        if d.get('链家编号'):
                            d['抓取时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
                            st['buf'].append([d.get(k, '') for k in FIELDS] + ['ok'])
                            st['done'][hid] = 'ok'
                            n_ok += 1
                        else:
                            st['done'][hid] = '页面异常'
                            n_fail += 1
                    # 熔断：连续3次拦截休30分钟
                    if streak >= 3:
                        log('连续3次被拦，熔断休息 30 分钟')
                        time.sleep(1800)
                        streak = 0
                    else:
                        time.sleep(random.uniform(60, 120))
                    continue
                streak = 0

                if r.status_code == 404:
                    st['done'][hid] = '已下架'
                    n_dead += 1
                    continue
                d = parse_detail(r.text, url)
                if not d.get('链家编号'):
                    st['done'][hid] = '页面异常'
                    n_fail += 1
                    continue
                d['抓取时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
                st['buf'].append([d.get(k, '') for k in FIELDS] + ['ok'])
                st['done'][hid] = 'ok'
                n_ok += 1
            except Exception as e:
                log(f'{hid} 未预期异常({type(e).__name__}): {e}')
                st['done'][hid] = '异常'
                n_fail += 1
                time.sleep(10)

            if len(st['buf']) >= 10:
                newf = not os.path.exists(args.out)
                with open(args.out, 'a', newline='', encoding='utf-8-sig') as f:
                    w = csv.writer(f)
                    if newf:
                        w.writerow(FIELDS + ['状态'])
                    w.writerows(st['buf'])
                n = len(st['buf'])
                add_quota(n)
                st['buf'] = []
                save_state(st)
                el = time.time() - t_start
                spd = (n_ok + n_dead) / el * 3600
                log(f'进度: ok={n_ok} 下架={n_dead} 被拦={n_flag} 异常={n_fail} '
                    f'| {spd:.0f} 套/小时 | 剩余{len(queue)}')
    finally:
        if st['buf']:
            newf = not os.path.exists(args.out)
            with open(args.out, 'a', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                if newf:
                    w.writerow(FIELDS + ['状态'])
                w.writerows(st['buf'])
            add_quota(len(st['buf']))
        save_state(st)
        el = time.time() - t_start
        log(f'v4 会话结束: ok={n_ok} 下架={n_dead} 被拦={n_flag} 异常={n_fail} 耗时{el/60:.0f}分钟')


if __name__ == '__main__':
    main()
