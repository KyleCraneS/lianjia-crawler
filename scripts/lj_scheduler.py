# -*- coding: utf-8 -*-
"""调度器：账号封禁监测 + 分段会话调度。

状态机：
  探测（每3h 1次请求）→ 封禁中? 继续等
       → 解封? 启动 v4 会话（80分钟，日配额900）
  会话结束 → 日配额满或到23点 → 休到明早8点
          → 否则段间休息2-4h → 再探测 → 再会话
"""
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from curl_cffi import requests
from lj_config import ERSHOU

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOG = f'{BASE}/.tmp/lj_scheduler.log'
ACTIVE_HOUR_START = 8
ACTIVE_HOUR_END = 23


def log(m):
    line = f'[{time.strftime("%m-%d %H:%M:%S")}] {m}'
    print(line, flush=True)
    with open(SLOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def probe_account(aid, jar, imp):
    """1次请求探测某账号: banned / challenged / ok / error"""
    try:
        try:
            s = requests.Session(impersonate=imp)
        except Exception:
            s = requests.Session(impersonate='chrome120')
        s.headers.update({'Accept-Language': 'zh-CN,zh;q=0.9'})
        s.cookies.update(jar)
        r = s.get(ERSHOU, timeout=15, allow_redirects=True)
        final = r.url
        if 'forbidden' in final or '临时封禁' in r.text[:3000]:
            return 'banned'
        if 'captcha' in final or '<title>CAPTCHA</title>' in r.text or '人机验证' in r.text[:3000]:
            return 'challenged'
        if r.status_code == 200 and len(r.text) > 50000:
            return 'ok'
        return f'unknown({r.status_code})'
    except Exception as e:
        return f'error({type(e).__name__})'


def probe_all():
    """轮询池中全部账号，返回 {aid: state}"""
    from lj_account_pool import AccountPool
    pool = AccountPool()
    results = {}
    for aid, a in pool.accounts.items():
        if a['status'] == 'banned' and time.time() < a['ban_until']:
            results[aid] = 'cooling'
            continue
        results[aid] = probe_account(aid, a['jar'], a['impersonate'])
        time.sleep(8)  # 探测间隔，别太密
    return results


def pool_total_quota():
    from lj_account_pool import AccountPool
    pool = AccountPool()
    return sum(pool.quota_left(aid) for aid in pool.accounts)


def wait_until_morning():
    now = time.localtime()
    if now.tm_hour < ACTIVE_HOUR_START:
        secs = (ACTIVE_HOUR_START - now.tm_hour) * 3600 - now.tm_min * 60
    else:
        secs = (24 - now.tm_hour + ACTIVE_HOUR_START) * 3600 - now.tm_min * 60
    log(f'非活动时间，休到 {ACTIVE_HOUR_START}:00（{secs/3600:.1f} 小时）')
    time.sleep(min(secs, 3600 * 4))


def main():
    log('调度器启动（24h 全速模式）')
    if not glob.glob(f'{BASE}/.tmp/lj_accounts/acc_*.json'):
        log('!!! 账号池为空：先运行 scripts/lj_capture_accounts.py 抓取至少1个账号')
        return
    # 完成检测：队列清空 → 写标记并退出
    try:
        import csv as _csv
        st_file = f'{BASE}/.tmp/lj_detail_state.json'
        done_ids = set(json.load(open(st_file, encoding='utf-8')).get('done', {})) if os.path.exists(st_file) else set()
        with open(f'{BASE}/输出/ershoufang_urls_sitemap.csv', encoding='utf-8-sig') as f:
            total = sum(1 for _ in _csv.DictReader(f))
        if len(done_ids) >= total:
            open(f'{BASE}/.tmp/lj_crawl_done.txt', 'w').write('done')
            log('队列已全部完成，写入标记退出')
            return
    except Exception as e:
        log(f'完成检测异常（忽略）: {e}')
    session_no = 0
    while True:
        left = pool_total_quota()
        if left <= 50:
            log(f'全池今日配额剩 {left}，休 2 小时后跨天继续')
            time.sleep(2 * 3600)
            continue

        results = probe_all()
        log('探测: ' + ', '.join(f'账号{k}={v}' for k, v in results.items()) + f' | 全池剩余配额 {left}')
        usable = [aid for aid, st in results.items() if st == 'ok']
        if not usable:
            log('无可用账号（封禁/验证中），90分钟后复探')
            time.sleep(90 * 60)
            continue

        session_no += 1
        # 估算剩余量，三会话按 0/⅓/⅔ 偏移并行
        try:
            import csv as _csv, json as _json
            st_file = f'{BASE}/.tmp/lj_detail_state.json'
            done_n = len(_json.load(open(st_file, encoding='utf-8')).get('done', {})) if os.path.exists(st_file) else 0
            with open(f'{BASE}/输出/ershoufang_urls_sitemap.csv', encoding='utf-8-sig') as f:
                total_n = sum(1 for _ in _csv.DictReader(f))
            remain = max(0, total_n - done_n)
            offsets = [(0, 'A'), (remain // 3, 'B'), (remain * 2 // 3, 'C')]
        except Exception:
            offsets = [(0, 'A'), (0, 'B'), (0, 'C')]
        log(f'===== 启动第 {session_no} 轮三会话（可用: {usable}，偏移 {[o for o, _ in offsets]}）=====')
        procs = []
        try:
            for off, tag in offsets:
                if tag != 'A' and off < 100:
                    continue    # 剩余太少时减少并行，避免撞车
                cmd = [sys.executable, '-X', 'utf8', f'{BASE}/scripts/lj_crawl_v5.py',
                       '--offset', str(off), '--out', f'{BASE}/输出/ershoufang_details_{tag}.csv']
                p = subprocess.Popen(cmd, cwd=BASE,
                                     stdout=open(f'{BASE}/.tmp/lj_crawl_stdout_{tag}.log', 'a', encoding='utf-8'),
                                     stderr=subprocess.STDOUT)
                procs.append((tag, p))
                time.sleep(20)          # 错峰启动，避免同时刻请求
            for tag, p in procs:
                p.wait()
        except Exception as e:
            log(f'会话启动异常: {e}')
        log(f'会话结束，全池剩余配额 {pool_total_quota()}')
        break_secs = 30 * 60 + (time.time() % 1800)   # 段间休息 30-60 分钟
        log(f'段间休息 {break_secs/60:.0f} 分钟')
        time.sleep(break_secs)


if __name__ == '__main__':
    main()
