# -*- coding: utf-8 -*-
"""账号状态抓取：循环开关 Chrome，每次用户登录一个账号，自动导出状态。

用法: python scripts/lj_capture_accounts.py [起始编号]
流程（每个账号）：
  1. 杀掉旧 Chrome → 全新临时 profile 启动（9223 调试端口）
  2. 打开链家城市站首页，等用户扫码/短信登录
  3. 检测到登录（cookie 出现 login_ucid）→ 导出 cookies + localStorage
  4. 存为 .tmp/lj_accounts/acc_N.json，杀掉 Chrome，进入下一个
按 Ctrl+C 结束当前等待。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from lj_config import ERSHOU, SITE

# Chrome 路径：用环境变量 CHROME_PATH 指定；未设置时用 Windows 默认安装位置
CHROME = os.environ.get('CHROME_PATH') or os.path.expandvars(
    r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe')
PROFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_capture_profile')
CDP_PORT = 9224
ACCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_accounts')
SLOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_capture.log')


def log(m):
    line = f'[{time.strftime("%H:%M:%S")}] {m}'
    print(line, flush=True)
    with open(SLOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def kill_chrome():
    """只杀采集 profile 的 Chrome，不动用户日常浏览器"""
    subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
         "Where-Object { $_.CommandLine -match 'lj_capture_profile' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
        capture_output=True, timeout=30)
    time.sleep(2)


def clean_profile():
    """删除 profile 目录 → 下次启动零登录状态（关键修复）"""
    import shutil
    kill_chrome()
    if os.path.exists(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)
        log('已清空浏览器档案（零登录状态）')


def launch_chrome():
    subprocess.Popen([
        CHROME, f'--user-data-dir={PROFILE}', f'--remote-debugging-port={CDP_PORT}',
        '--no-first-run', '--no-default-browser-check', '--window-size=1200,900',
        ERSHOU])
    for _ in range(30):
        time.sleep(1)
        try:
            json.load(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/version', timeout=2))
            return True
        except Exception:
            pass
    return False


def get_cookies_via_cdp():
    """用 CDP Network.getCookies 拿全站 cookies（含 HttpOnly）"""
    import websocket
    tabs = json.load(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/list', timeout=5))
    page = next(t for t in tabs if t.get('type') == 'page' and 'lianjia' in t.get('url', ''))
    ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=20, suppress_origin=True)
    mid = [0]

    def call(method, params=None):
        mid[0] += 1
        ws.send(json.dumps({'id': mid[0], 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get('id') == mid[0]:
                return msg.get('result', {})

    call('Network.enable')
    r = call('Network.getCookies', {'urls': [SITE + '/', 'https://lianjia.com/', 'https://ke.com']})
    cookies = r.get('cookies', [])
    # localStorage
    ls = call('Runtime.evaluate', {'expression':
               "JSON.stringify(Object.fromEntries(Object.entries(localStorage)))", 'returnByValue': True})
    ls_data = json.loads(ls.get('result', {}).get('value') or '{}')
    ws.close()
    keep = [c for c in cookies if 'lianjia' in c.get('domain', '') or 'ke.com' in c.get('domain', '')]
    return keep, ls_data


def wait_login(timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        try:
            tabs = json.load(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/list', timeout=3))
            page = next((t for t in tabs if t.get('type') == 'page' and 'lianjia' in t.get('url', '')), None)
            if not page:
                continue
            import websocket
            ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10, suppress_origin=True)
            ws.send(json.dumps({'id': 1, 'method': 'Network.enable', 'params': {}}))
            ws.send(json.dumps({'id': 2, 'method': 'Network.getCookies',
                                'params': {'urls': [SITE + '/']}}))
            names = set()
            while True:
                msg = json.loads(ws.recv())
                if msg.get('id') == 2:
                    names = {c['name'] for c in msg.get('result', {}).get('cookies', [])}
                    break
            ws.close()
            if 'login_ucid' in names:
                return True
        except Exception:
            pass
    return False


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    os.makedirs(ACCS_DIR, exist_ok=True)
    for i in range(start, end + 1):
        out = f'{ACCS_DIR}/acc_{i}.json'
        log(f'===== 账号 {i}：启动全新 Chrome，请登录 =====')
        clean_profile()
        if not launch_chrome():
            log('Chrome 启动失败')
            continue
        if wait_login():
            time.sleep(2)
            cookies, ls = get_cookies_via_cdp()
            json.dump({'cookies': cookies, 'localStorage': ls,
                       'captured_at': time.strftime('%Y-%m-%d %H:%M:%S')},
                      open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            log(f'账号 {i} 捕获成功: {len(cookies)} cookies -> {out}')
        else:
            log(f'账号 {i} 等待超时（10分钟），跳过')
    log('全部完成')
    kill_chrome()


if __name__ == '__main__':
    main()
