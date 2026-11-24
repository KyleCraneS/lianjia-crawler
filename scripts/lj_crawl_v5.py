# -*- coding: utf-8 -*-
"""链家详情爬虫 v5：账号池轮换版。

相对 v4：Cookie 从账号池取，每个账号独立指纹/配额/冷却；
连续2次被拦自动换号；forbidden 封号自动标记24h冷却并换号。
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
from lj_account_pool import AccountPool
from lj_config import DISTRICT_SLUG, ERSHOU, SITE

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = f'{BASE}/.tmp/lj_detail_state.json'
LOG = f'{BASE}/.tmp/lj_crawl.log'

SESSION_MINUTES = 75
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


def browser_fallback(url, jar=None):
    out_html = f'{BASE}/.tmp/lj_fb_{int(time.time())}.html'
    ck_file = ''
    if jar:
        ck_file = f'{BASE}/.tmp/lj_fb_ck_{int(time.time())}.json'
        json.dump(jar, open(ck_file, 'w', encoding='utf-8'))
    try:
        cmd = [sys.executable, '-X', 'utf8', f'{BASE}/scripts/lj_captcha.py', url, out_html]
        if ck_file:
            cmd.append(ck_file)
        r = subprocess.run(cmd, capture_output=True, timeout=700, cwd=BASE)
        if r.returncode == 0 and os.path.exists(out_html):
            html = open(out_html, encoding='utf-8').read()
            os.remove(out_html)
            return html or None
    except subprocess.TimeoutExpired:
        log('浏览器兜底超时')
    except Exception as e:
        log(f'浏览器兜底异常: {e}')
    finally:
        if ck_file and os.path.exists(ck_file):
            os.remove(ck_file)
    return None


def is_flagged(r):
    return ('captcha' in r.headers.get('Location', '')
            or '<title>CAPTCHA</title>' in r.text or '人机验证' in r.text[:3000])


def is_forbidden(r):
    return 'forbidden' in r.headers.get('Location', '') or '临时封禁' in r.text[:3000]


def human_wait(night=False):
    if random.random() < 0.04:
        time.sleep(random.uniform(60, 180))
    else:
        # 夜间（23-8点）中位间隔 12s，白天 8s（风控夜间更严）
        time.sleep(random.lognormvariate(math.log(12 if night else 8), 0.6))


class Watchdog:
    """看门狗：请求挂死（curl 超时失效）时硬退出，由外层调度重启断点续传。

    feed(seconds) 在每次可能阻塞的操作前更新宽限时间。
    """

    def __init__(self):
        self.deadline = time.time() + 300
        import threading
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def feed(self, seconds):
        self.deadline = time.time() + seconds

    def _run(self):
        while True:
            time.sleep(5)
            if time.time() > self.deadline:
                log('WATCHDOG: 请求挂死超时，硬退出等待重启')
                os._exit(2)


def new_session(pool, aid):
    a = pool.accounts[aid]
    try:
        s = requests.Session(impersonate=a['impersonate'])
    except Exception:
        s = requests.Session(impersonate='chrome120')   # 指纹回退兜底
    s.headers.update({'Accept-Language': 'zh-CN,zh;q=0.9'})
    s.cookies.update(a['jar'])
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='in_csv', default=f'{BASE}/输出/ershoufang_urls_sitemap.csv',
                    help='待抓 URL 队列 CSV（默认 lj_sitemap.py 的输出；双机分工时可传 part1/part2）')
    ap.add_argument('--out', dest='out', default=f'{BASE}/输出/ershoufang_details.csv')
    ap.add_argument('--night', action='store_true')
    ap.add_argument('--offset', type=int, default=0,
                    help='跳过前 N 条未抓队列（并行会话用，错开队列位置）')
    args = ap.parse_args()
    global human_wait_night
    human_wait_night = args.night

    pool = AccountPool()
    log('账号池状态:\n' + pool.info())
    aid = pool.pick()
    if not aid:
        log('无可用账号，退出')
        return
    log(f'选用账号 {aid}（指纹 {pool.accounts[aid]["impersonate"]}，剩余配额 {pool.quota_left(aid)}）')

    st = load_state()
    rows = list(csv.DictReader(open(args.in_csv, encoding='utf-8-sig')))
    queue = [[r['id'], r['url'], r.get('district', '')] for r in rows if r['id'] not in st['done']]
    if args.offset:
        queue = queue[args.offset:]

    sess = new_session(pool, aid)
    wd = Watchdog()
    t_end = time.time() + SESSION_MINUTES * 60
    n_ok = n_dead = n_flag = n_fail = n_rotated = 0
    streak = 0
    since_preheat = PREHEAT_EVERY
    since_refresh = 0
    referer = ERSHOU
    t_start = time.time()

    def rotate(reason):
        nonlocal aid, sess, streak, n_rotated
        old = aid
        pool.mark_hot(aid)
        new_aid = pool.pick()
        if not new_aid or (new_aid == aid and pool.accounts[aid]['status'] == 'banned'):
            log(f'轮换账号（{reason}）：无可用账号')
            return False
        aid = new_aid
        sess = new_session(pool, aid)
        streak = 0
        n_rotated += 1
        log(f'轮换账号（{reason}）：{old} → {aid}（指纹 {pool.accounts[aid]["impersonate"]}）')
        return True

    try:
        while queue and time.time() < t_end:
            hid, url, district = queue.pop(0)
            try:
                if pool.quota_left(aid) <= 0:
                    if not rotate('配额用完'):
                        break
                if since_preheat >= PREHEAT_EVERY:
                    slug = DISTRICT_SLUG.get(district, '')
                    list_url = f'{ERSHOU}{slug}/' if slug else ERSHOU
                    human_wait(args.night)
                    wd.feed(120)
                    try:
                        rl = sess.get(list_url, headers={'Referer': SITE + '/'}, timeout=20)
                        if rl.status_code == 200 and not is_flagged(rl) and not is_forbidden(rl):
                            referer = list_url
                    except Exception:
                        pass
                    wd.feed(300)
                    since_preheat = 0

                human_wait(args.night)
                wd.feed(120)
                r = sess.get(url, headers={'Referer': referer}, timeout=20, allow_redirects=False)
                wd.feed(300)
                since_preheat += 1

                if is_forbidden(r):
                    log(f'账号 {aid} 被封禁，标记冷却 24h')
                    pool.mark_banned(aid)
                    if not rotate('账号封禁'):
                        break
                    continue
                if is_flagged(r):
                    n_flag += 1
                    streak += 1
                    log(f'{hid} 被拦(账号{aid} 连续{streak})')
                    try:
                        r2 = sess.get(url, headers={'Referer': referer}, timeout=20, allow_redirects=False)
                    except Exception:
                        r2 = None
                    wd.feed(300)
                    html = None
                    if r2 is not None and not is_flagged(r2) and not is_forbidden(r2) and r2.status_code != 404:
                        html = r2.text
                    if html is None:
                        wd.feed(700)
                        html = browser_fallback(url, pool.accounts[aid]['jar'])
                        wd.feed(300)
                    if html:
                        d = parse_detail(html, url)
                        if d.get('链家编号'):
                            d['抓取时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
                            st['buf'].append([d.get(k, '') for k in FIELDS] + ['ok'])
                            st['done'][hid] = 'ok'
                            n_ok += 1
                        else:
                            st['done'][hid] = '页面异常'
                            n_fail += 1
                    else:
                        st['done'][hid] = 'fail'
                        n_fail += 1
                    if streak >= 2:
                        if not rotate('连续被拦'):
                            break
                    else:
                        time.sleep(random.uniform(45, 90))
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

            # 并行会话队列刷新：定期重读共享状态，丢弃已被其他会话抓完的条目
            since_refresh += 1
            if since_refresh >= 30:
                since_refresh = 0
                try:
                    fresh = json.load(open(STATE, encoding='utf-8'))
                    st['done'].update(fresh.get('done', {}))   # 合并而非覆盖
                    before = len(queue)
                    queue = [q for q in queue if q[0] not in st['done']]
                    if before - len(queue) > 0:
                        log(f'队列刷新：丢弃已完成 {before - len(queue)} 条')
                except Exception:
                    pass

            if len(st['buf']) >= 10:
                newf = not os.path.exists(args.out)
                with open(args.out, 'a', newline='', encoding='utf-8-sig') as f:
                    w = csv.writer(f)
                    if newf:
                        w.writerow(FIELDS + ['状态'])
                    w.writerows(st['buf'])
                pool.add_quota(aid, len(st['buf']))
                st['buf'] = []
                save_state(st)
                el = time.time() - t_start
                spd = (n_ok + n_dead) / el * 3600
                log(f'进度: ok={n_ok} 下架={n_dead} 被拦={n_flag} 异常={n_fail} 换号={n_rotated} '
                    f'| {spd:.0f} 套/小时 | 账号{aid}剩{pool.quota_left(aid)} | 剩余{len(queue)}')
    finally:
        if st['buf']:
            newf = not os.path.exists(args.out)
            with open(args.out, 'a', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                if newf:
                    w.writerow(FIELDS + ['状态'])
                w.writerows(st['buf'])
            pool.add_quota(aid, len(st['buf']))
        save_state(st)
        log(f'v5 会话结束: ok={n_ok} 下架={n_dead} 被拦={n_flag} 异常={n_fail} 换号={n_rotated}')


if __name__ == '__main__':
    main()
