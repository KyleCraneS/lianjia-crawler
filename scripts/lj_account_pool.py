# -*- coding: utf-8 -*-
"""账号池：多账号状态管理 + 指纹分配 + 健康轮换。"""
import glob
import json
import os
import random
import time

ACCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_accounts')
STATUS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'lj_pool_status.json')
QUOTA_PER_ACCOUNT_PER_DAY = 1300   # 单账号每天最多抓多少套（保守可设 900）

# 每个账号一个不同 Chrome 版本指纹（不同 JA3 = 不同设备身份，防关联连坐）
IMPERSONATE_TARGETS = ['chrome99', 'chrome100', 'chrome107', 'chrome110', 'chrome112', 'chrome116',
                       'chrome119', 'chrome120', 'chrome123', 'chrome124']


class AccountPool:
    def __init__(self):
        self.accounts = {}       # id -> {cookies, impersonate, status, ban_until, quota{day:n}, last_used}
        self._load()

    def _load(self):
        status = {}
        if os.path.exists(STATUS_FILE):
            status = json.load(open(STATUS_FILE, encoding='utf-8'))
        files = sorted(glob.glob(f'{ACCS_DIR}/acc_*.json'),
                       key=lambda p: int(''.join(c for c in os.path.basename(p) if c.isdigit()) or 0))
        for idx, f in enumerate(files):
            acc_id = os.path.basename(f).replace('acc_', '').replace('.json', '')
            raw = json.load(open(f, encoding='utf-8'))
            jar = {c['name']: c['value'] for c in raw.get('cookies', [])}
            st = status.get(acc_id, {})
            self.accounts[acc_id] = {
                'jar': jar,
                'impersonate': IMPERSONATE_TARGETS[idx % len(IMPERSONATE_TARGETS)],
                'status': st.get('status', 'ok'),          # ok / hot / banned
                'ban_until': st.get('ban_until', 0),
                'quota': st.get('quota', {}),
                'last_used': st.get('last_used', 0),
                'source': f,
            }

    def _save(self):
        status = {aid: {k: a[k] for k in ('status', 'ban_until', 'quota', 'last_used')}
                  for aid, a in self.accounts.items()}
        json.dump(status, open(STATUS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    def quota_left(self, aid):
        a = self.accounts[aid]
        day = time.strftime('%Y-%m-%d')
        return max(0, QUOTA_PER_ACCOUNT_PER_DAY - a['quota'].get(day, 0))

    def add_quota(self, aid, n):
        a = self.accounts[aid]
        day = time.strftime('%Y-%m-%d')
        a['quota'][day] = a['quota'].get(day, 0) + n
        self._save()

    def healthy(self):
        now = time.time()
        out = []
        for aid, a in self.accounts.items():
            if a['status'] == 'banned':
                if now >= a['ban_until']:
                    a['status'] = 'ok'      # 冷却到期，回池前会在会话里先探测
                    self._save()
                else:
                    continue
            if a['status'] in ('ok', 'hot') and self.quota_left(aid) > 50 and 'login_ucid' in a['jar']:
                out.append(aid)
        return out

    def pick(self):
        """选一个账号：健康池内最久未用优先 + 随机扰动"""
        cands = self.healthy()
        if not cands:
            return None
        cands.sort(key=lambda aid: self.accounts[aid]['last_used'] + random.uniform(0, 300))
        aid = cands[0]
        self.accounts[aid]['last_used'] = time.time()
        self._save()
        return aid

    def mark_hot(self, aid):
        """频繁被拦 → 临时降权 2 小时"""
        a = self.accounts[aid]
        a['status'] = 'hot'
        a['ban_until'] = time.time() + 2 * 3600
        self._save()

    def mark_banned(self, aid, hours=24):
        a = self.accounts[aid]
        a['status'] = 'banned'
        a['ban_until'] = time.time() + hours * 3600
        self._save()

    def info(self):
        lines = []
        for aid, a in self.accounts.items():
            lines.append(f"  账号{aid}: status={a['status']} 指纹={a['impersonate']} "
                         f"今日剩余配额={self.quota_left(aid)}")
        return '\n'.join(lines)
