# -*- coding: utf-8 -*-
"""极验 v4 点选验证码自动求解（CDP 驱动已登录 Chrome + LeapYa GeetestSolver）。

流程（对齐 record_lianjia.py 的实证路径）：
  1. 轮询找开始按钮并模拟点击（悬停-按下-抬起）
  2. 等背景图+ques 小图稳定（连续3次不变）
  3. 元素区域截图 → 缩放回 CSS 尺寸 → GeetestSolver 求解
  4. 按解出的坐标模拟点击 → 点确定
  5. 判结果；失败刷新重试（最多 max_trials 次）
"""
import base64
import io
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np
import websocket

from lj_config import ERSHOU

REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models_repo')
sys.path.insert(0, REPO)

PROBE_JS = r"""() => {
    const r = {ready:false, x:0, y:0, w:0, h:0, bg:'', ques:[], text:'', submit:null, dpr: window.devicePixelRatio || 1};
    const bgEl = document.querySelector('[class*=geetest_bg]');
    if (bgEl) {
        const b = bgEl.getBoundingClientRect();
        const cs = getComputedStyle(bgEl);
        r.x=b.x; r.y=b.y; r.w=b.width; r.h=b.height;
        r.bg = cs.backgroundImage || '';
        r.ready = r.bg !== '' && r.bg !== 'none';
    }
    for (const el of document.querySelectorAll('img,div,span')) {
        const s = getComputedStyle(el).backgroundImage || el.src || '';
        if (!s || s==='none') continue;
        if (s.indexOf('static.geetest.com') < 0 && s.indexOf('geetest') < 0) continue;
        const b = el.getBoundingClientRect();
        if (b.width < 20 || b.height < 20 || b.width > 200 || b.height > 200) continue;
        const m = s.match(/url\("?([^")]+)"?\)/) || (el.src ? [null, el.src] : null);
        if (m) r.ques.push({x:b.x, y:b.y, w:b.width, h:b.height, src:m[1]});
    }
    const sub = document.querySelector('[class*=geetest_submit]');
    if (sub) { const b = sub.getBoundingClientRect(); r.submit = {x:b.x, y:b.y, w:b.width, h:b.height}; }
    r.text = (document.body.innerText || '').slice(0, 200);
    return r;
}"""

START_JS = r"""() => {
    for (const e of document.querySelectorAll('div,span,a,button')) {
        const t = (e.textContent || '').trim();
        if (t === '点击按钮开始验证' || t === '请依次点击' ||
            t === '点击进行验证' || t === '点击开始验证') {
            const r = e.getBoundingClientRect();
            if (r.width > 100) return {x:r.x, y:r.y, w:r.width, h:r.height, t:t};
        }
    }
    return null;
}"""

SUBMIT_JS = r"""() => {
    const s = document.querySelector('[class*=geetest_submit]');
    if (s) { const r = s.getBoundingClientRect();
        if (r.width > 50) return {x:r.x, y:r.y, w:r.width, h:r.height}; }
    for (const e of document.querySelectorAll('div,span,a,button')) {
        const t = (e.textContent || '').trim();
        if (t === '确定' || t === '确认') {
            const r = e.getBoundingClientRect();
            if (r.width > 80) return {x:r.x, y:r.y, w:r.width, h:r.height};
        }
    }
    return null;
}"""

REFRESH_JS = r"""() => {
    for (const e of document.querySelectorAll('[class*=refresh],[class*=geetest_refresh]')) {
        const r = e.getBoundingClientRect();
        if (r.width >= 10 && r.height >= 10) return {x:r.x+r.width/2, y:r.y+r.height/2};
    }
    return null;
}"""

SCROLL_JS = r"""() => {
    const g = document.querySelector('[class*=geetest_captcha],[class*=geetest_box]');
    if (g) g.scrollIntoView({block:'center'});
    return true;
}"""


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.mid = 0

    def call(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({'id': self.mid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == self.mid:
                return msg.get('result', {})

    def js(self, expr):
        r = self.call('Runtime.evaluate', {'expression': expr, 'returnByValue': True})
        return r.get('result', {}).get('value')

    def js_fn(self, fn_src):
        return self.js(f'({fn_src})()')

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def connect_lianjia_tab():
    tabs = json.load(urllib.request.urlopen('http://127.0.0.1:9223/json/list', timeout=5))
    page = next((t for t in tabs if t.get('type') == 'page' and 'lianjia' in t.get('url', '')), None)
    if not page:
        raise RuntimeError('找不到链家标签页')
    return page


class CaptchaSolver:
    def __init__(self, tab=None, verbose=True):
        self.tab = tab or connect_lianjia_tab()
        self.cdp = CDP(self.tab['webSocketDebuggerUrl'])
        self.cdp.call('Page.enable')
        self.cdp.call('Runtime.enable')
        from geetest_solver import GeetestSolver
        self.solver = GeetestSolver()
        self.verbose = verbose

    def log(self, m):
        if self.verbose:
            print(f'[captcha] {m}', flush=True)

    def human_click(self, x, y):
        cdp = self.cdp
        for (mx, my) in [(x - random.randint(30, 80), y - random.randint(5, 20)),
                         (x - random.randint(5, 15), y - random.randint(2, 8)),
                         (x, y)]:
            cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': mx, 'y': my})
            time.sleep(random.uniform(0.05, 0.12))
        cdp.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y,
                                              'button': 'left', 'clickCount': 1})
        time.sleep(random.uniform(0.06, 0.13))
        cdp.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y,
                                              'button': 'left', 'clickCount': 1})

    def click_start(self, wait_s=12):
        t0 = time.time()
        while time.time() - t0 < wait_s:
            box = self.cdp.js_fn(START_JS)
            if box:
                self.log(f'点开始按钮「{box["t"]}」')
                self.human_click(box['x'] + box['w'] / 2, box['y'] + box['h'] / 2)
                return True
            time.sleep(0.4)
        return False

    def wait_ready(self, timeout_s=20):
        t0 = time.time()
        last = None
        stable = 0
        while time.time() - t0 < timeout_s:
            time.sleep(0.32)
            st = self.cdp.js_fn(PROBE_JS)
            if not st or not st.get('ready'):
                stable = 0
                last = None
                continue
            ques = [q for q in (st.get('ques') or [])
                    if 20 <= q['w'] <= 200 and 20 <= q['h'] <= 200]
            st['ques'] = ques
            bg_url = str(st.get('bg') or '')
            is_phrase = '/phrase/' in bg_url or '语序' in (st.get('text') or '')
            # word/icon 型要求 3 张 ques 到齐；phrase 型无 ques
            if not is_phrase and len(ques) < 3:
                stable = 0
                last = None
                continue
            key = (round(st['w']), round(st['h']), bg_url[:150], len(ques))
            if key == last:
                stable += 1
                if stable >= 3:
                    return st
            else:
                stable = 0
                last = key
        return None

    def clip_png(self, x, y, w, h):
        r = self.cdp.call('Page.captureScreenshot', {
            'format': 'png',
            'clip': {'x': max(0, x), 'y': max(0, y), 'width': w, 'height': h, 'scale': 1.0}})
        return base64.b64decode(r['data'])

    @staticmethod
    def to_css_size(png, css_w, css_h):
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        if img.shape[1] != int(css_w) or img.shape[0] != int(css_h):
            img = cv2.resize(img, (int(css_w), int(css_h)))
        return img

    def click_points(self, st, pts):
        for i, (px, py) in enumerate(pts):
            x = st['x'] + px
            y = st['y'] + py
            self.human_click(x, y)
            self.log(f'  点 {i+1}/{len(pts)} ({px:.0f},{py:.0f})')
            time.sleep(random.uniform(0.25, 0.4))

    def click_confirm(self):
        box = self.cdp.js_fn(SUBMIT_JS)
        if not box:
            return False
        self.human_click(box['x'] + box['w'] / 2, box['y'] + box['h'] / 2)
        return True

    def check_result(self):
        time.sleep(2.0)
        body = self.cdp.js("document.body ? document.body.innerText.slice(0,400) : ''") or ''
        has_geetest = bool(self.cdp.js("!!document.querySelector('[class*=geetest_captcha]')"))
        url = self.cdp.js('location.href') or ''
        if '验证通过' in body or '验证成功' in body:
            return 'success'
        if '人机验证' not in body and not has_geetest:
            return 'success'
        if any(k in body for k in ('验证失败', '验证错误', '请重试', '重试')):
            return 'fail'
        if 'captcha' not in url and 'hip.lianjia' not in url:
            return 'success'
        return 'unknown'

    def refresh(self):
        pt = self.cdp.js_fn(REFRESH_JS)
        if pt:
            self.human_click(pt['x'], pt['y'])
            return True
        return False

    def solve(self, max_trials=5):
        """在当前页面上求解验证码（页面应已在 hip.lianjia.com/captcha）"""
        self.cdp.js_fn(SCROLL_JS)
        self.click_start()
        phrase_refreshes = 0
        attempts = 0
        trial = 0
        while attempts < max_trials and trial < max_trials * 4:
            trial += 1
            self.log(f'--- 第 {trial} 轮（实解 {attempts}/{max_trials}）---')
            st = self.wait_ready(20)
            if not st:
                self.log('图片未就绪')
                if not self.click_start():
                    time.sleep(1.5)
                continue
            ques = st.get('ques') or []
            self.log(f'就绪 {st["w"]:.0f}x{st["h"]:.0f} ques={len(ques)}')
            bg_img = self.to_css_size(self.clip_png(st['x'], st['y'], st['w'], st['h']), st['w'], st['h'])
            if bg_img is None:
                self.log('截图失败')
                continue
            ques_imgs = []
            for q in ques[:3]:
                qi = self.to_css_size(self.clip_png(q['x'], q['y'], q['w'], q['h']), q['w'], q['h'])
                if qi is not None:
                    ques_imgs.append(qi)
            try:
                res = self.solver.classification(bg_img, ques_imgs or None)
            except Exception as e:
                self.log(f'solver 异常: {e}')
                res = {}
            pts = res.get('points') or []
            ctype = res.get('type')
            self.log(f'求解 type={ctype} word={res.get("word")} 点数={len(pts)}')
            # 语序题成功率仅~25%，点字/图标题99%：撞 phrase 直接刷新换题（不耗实解次数）
            if ctype == 'phrase' and phrase_refreshes < 5:
                phrase_refreshes += 1
                self.log(f'  语序题，刷新换题({phrase_refreshes}/5)')
                self.refresh()
                time.sleep(0.8)
                continue
            if not pts:
                self.refresh()
                time.sleep(1.0)
                continue
            attempts += 1
            self.click_points(st, pts)
            if not self.click_confirm():
                self.log('找不到确定按钮')
                continue
            r = self.check_result()
            self.log(f'结果: {r}')
            if r == 'success':
                return True
            self.refresh()
            time.sleep(1.2)
        return False

    def goto_captcha(self, target_url):
        """导航到验证码页（模拟 302）"""
        loc = 'https://hip.lianjia.com/captcha?location=' + urllib.parse.quote(target_url, safe='')
        self.log(f'导航到验证码页')
        self.cdp.call('Page.navigate', {'url': loc})
        time.sleep(3)
        return loc

    def close(self):
        self.cdp.close()


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    # 用法1: python lj_captcha.py <目标URL>                      — 测试模式
    # 用法2: python lj_captcha.py <目标URL> <out.html> [cookies.json] — CLI 模式
    #   cookies.json: {name: value} 字典，先注入浏览器再导航（让浏览器身份=爬取账号）
    target = sys.argv[1] if len(sys.argv) > 1 else ERSHOU + 'pg2/'
    out_file = sys.argv[2] if len(sys.argv) > 2 else None
    cookies_file = sys.argv[3] if len(sys.argv) > 3 else None
    s = CaptchaSolver()
    if cookies_file:
        try:
            jar = json.load(open(cookies_file, encoding='utf-8'))
            cdp = s.cdp
            cdp.call('Network.enable')
            cookies = [{'name': k, 'value': v, 'domain': '.lianjia.com', 'path': '/'} for k, v in jar.items()]
            cdp.call('Network.setCookies', {'cookies': cookies})
            print(f'[captcha-cli] 已注入 {len(cookies)} 条爬取账号 Cookie', flush=True)
        except Exception as e:
            print(f'[captcha-cli] Cookie 注入失败（继续）: {e}', flush=True)
    if out_file:
        # CLI 模式：导航到目标页（可能被拦→自动过验证→重试），最终 HTML 写文件
        html = None
        for attempt in range(4):
            s.cdp.call('Page.navigate', {'url': target})
            time.sleep(random.uniform(5, 7))
            title = s.cdp.js('document.title') or ''
            loc = s.cdp.js('location.href') or ''
            if 'CAPTCHA' not in title and 'captcha' not in loc:
                html = s.cdp.js('document.documentElement.outerHTML')
                break
            print('[captcha-cli] 被拦，自动过验证…', flush=True)
            if not s.solve(max_trials=4):
                print('[captcha-cli] 求解失败', flush=True)
                sys.exit(1)
        if html:
            open(out_file, 'w', encoding='utf-8').write(html)
            print('[captcha-cli] OK', flush=True)
            sys.exit(0)
        sys.exit(1)
    s.goto_captcha(target)
    ok = s.solve()
    print('RESULT:', 'PASS' if ok else 'FAIL', flush=True)
    if ok:
        time.sleep(2)
        print('当前页:', s.cdp.js('location.href'), flush=True)
    s.close()
