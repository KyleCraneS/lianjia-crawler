# -*- coding: utf-8 -*-
"""顶层看门狗：监督调度器进程，崩溃/被杀自动拉起；任务完成自动退出。

链路: 看门狗 → 调度器 → v5 采集会话
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONE = f'{BASE}/.tmp/lj_crawl_done.txt'
SLOG = f'{BASE}/.tmp/lj_supervisor.log'
os.makedirs(f'{BASE}/.tmp', exist_ok=True)
os.makedirs(f'{BASE}/输出', exist_ok=True)


def log(m):
    line = f'[{time.strftime("%m-%d %H:%M:%S")}] {m}'
    print(line, flush=True)
    with open(SLOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def main():
    if os.path.exists(DONE):
        os.remove(DONE)
    restart_delay = 60
    round_no = 0
    while True:
        if os.path.exists(DONE):
            log('检测到完成标记，看门狗退出')
            return
        round_no += 1
        log(f'===== 启动调度器（第 {round_no} 轮）=====')
        t0 = time.time()
        try:
            p = subprocess.Popen(
                [sys.executable, '-X', 'utf8', f'{BASE}/scripts/lj_scheduler.py'],
                cwd=BASE,
                stdout=open(f'{BASE}/.tmp/lj_scheduler_stdout.log', 'a', encoding='utf-8'),
                stderr=subprocess.STDOUT)
            rc = p.wait()
        except Exception as e:
            log(f'调度器启动异常: {e}')
            time.sleep(restart_delay)
            continue
        elapsed = (time.time() - t0) / 60
        if os.path.exists(DONE) or rc == 0:
            log(f'调度器正常结束（rc={rc}，{elapsed:.0f} 分钟），看门狗退出')
            return
        log(f'调度器退出 rc={rc}，运行 {elapsed:.1f} 分钟，{restart_delay}s 后重启')
        time.sleep(restart_delay)


if __name__ == '__main__':
    main()
