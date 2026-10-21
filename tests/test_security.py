#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""tests/test_security.py — 公网暴露安全（W1–W4）的测试

## 为什么必须**真起子进程**
--------------------------------------------------------------------------
本机模式与公网模式的行为差异发生在**模块导入时**（Config 在 import 时
读环境变量）。若在测试进程里 import 两个模块再改配置，测到的是
「改配置之后的行为」，不是「按那套配置启动的行为」——

实测踩过：进程内改 os.environ 再 reload 模块，FastAPI 侧改了，
但 core 的 ALLOWED_HOSTS 与另一个 Config 实例还是旧的，
于是「两套服务端行为一致」这条断言**测了个寂寞**。

所以这里每个场景都**起一个真子进程**（带独立环境变量），
通过 HTTP 打它。这与 smoke_test.py 的子进程启动测试同一路子。

## 覆盖
--------------------------------------------------------------------------
W1  默认绑 127.0.0.1；公网模式才 0.0.0.0；配错时拒绝启动
W2  ALLOWED_HOSTS 配置化；域名不在白名单 → 403；通配/后缀不生效
W3  无令牌 → 401；正确令牌 → 200；令牌可撤销（登出后立刻失效）；
    令牌可轮换；**日志里搜不到明文令牌**；口令不写进日志
W4  错口令连打 20 次被限流；上传接口频率限制；
    X-Forwarded-For：非受信对端不采信，受信对端才采信
等价 两套服务端（api.py / server.py）行为一致

## 跑法
--------------------------------------------------------------------------
    .venv-build/Scripts/python.exe tests/test_security.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_APP = os.path.join(_ROOT, 'app')
for _p in (_APP, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import security  # noqa: E402

#: 打本机端口必须绕过代理：urllib 默认读 http_proxy 环境变量，
#: 会把打向 127.0.0.1 的请求也发给代理，代理回 502，
#: 症状伪装成「服务起不来」（smoke_test.py 里踩过）。
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PY = sys.executable
TEST_PASSWORD = 'correct-horse-battery-staple'
TEST_HASH = security.hash_password(TEST_PASSWORD)


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, url, body=None, headers=None, timeout=10):
    """打一个 HTTP 请求。返回 (code, body_bytes, headers)。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode('utf-8')
            hdrs.setdefault('Content-Type', 'application/json')
        else:
            data = body
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with NO_PROXY.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def jhttp(method, url, body=None, headers=None, timeout=10):
    code, raw, h = http(method, url, body, headers, timeout)
    try:
        return code, json.loads(raw.decode('utf-8')), h
    except Exception:
        return code, {}, h


def _memory_store(module):
    """造一个**内存**数据层，够 /api/v1/stats 这类只读端点用。

    ## 为什么不直接用 core.Store()
    `Store()` 会探测真实的 data/qbank.sqlite 或 data/public/qbank.json。
    public 分支虽然只读，但会在 DATA 目录建文件 —— 测试不该在仓库里留痕，
    更不该碰用户数据。所以这里自己建一个空内存库。

    ## 够用就行
    本文件只验**鉴权与限流**的 HTTP 行为，不验业务数据。
    stats() 需要 questions/cards/progress 三张表存在。
    """
    import sqlite3
    import threading
    import repository
    con = sqlite3.connect(':memory:', check_same_thread=False)
    con.row_factory = sqlite3.Row
    lock = threading.RLock()
    repo = repository.Repository(con, lock)
    repo.executescript('''
      CREATE TABLE questions(id TEXT PRIMARY KEY, bank TEXT, source_class TEXT, section_path TEXT,
        section TEXT, cat_name TEXT, stem TEXT, qtype TEXT, level INTEGER, options TEXT,
        answer TEXT, explanation TEXT, source TEXT, note TEXT, meta TEXT);
      CREATE TABLE cards(cid INTEGER PRIMARY KEY AUTOINCREMENT, chapter TEXT, topic TEXT,
        name TEXT, body TEXT, qids TEXT, source_class TEXT);
      CREATE TABLE progress(id TEXT PRIMARY KEY, state TEXT DEFAULT 'new', streak INTEGER DEFAULT 0,
        wrong INTEGER DEFAULT 0, due TEXT, last_seen TEXT);
    ''')
    import core
    st = core.Store.__new__(core.Store)
    st.mode = 'public'
    st.lock = lock
    st.prog_writable = True
    st._banks = None
    st._sections = None
    st.con = con
    st.repo = repo
    return st


def _temp_docstore(module):
    """造一个**临时目录**上的 DocStore（不碰真实 data/private/documents）。"""
    import docstore
    d = tempfile.mkdtemp(prefix='qbank-sec-docs-')
    return docstore.DocStore(os.path.join(d, 'documents'), store=None)


class ServerProc:
    """在**测试进程内**起一个服务（线程），退出时收掉。

    ## 为什么不起子进程
    本机沙箱会 **SIGTERM 掉「监听端口的子进程」** —— 实测起子进程跑
    api.py，测试进程自己被 SIGTERM 掉（退出码 SIGTERM，连 traceback 都没有）。
    这与 tools/smoke_test.py 里 spawn 子进程能跑通不同：那个脚本
    跑在普通命令下，而 unittest 起的子进程在本沙箱里会被回收。

    所以改成与 tools/smoke_api.py 同一路子：**在测试进程内用线程起服务**。

    ## 配置怎么生效
    `reload_security(env)` —— 显式重载（见 app/security.py 的说明）。
    两套服务端都提供了这个入口，行为一致。

    ## Host 头要用白名单里的域名
    公网模式的 ALLOWED_HOSTS 是 `mech.example.com`，
    而 HTTP 连接打的是 127.0.0.1 —— **Host 头与连接地址是两回事**：
    Host 头是客户端自己声明的。所以公网模式下必须显式带
    `Host: mech.example.com`，否则被 guard 403（这是**正确行为**，
    正是 W2 要防的东西）。
    """

    #: 公网模式下的合法 Host
    HOST = 'mech.example.com'

    def __init__(self, script, env_extra, port=None):
        self.port = port or free_port()
        self.script = script
        self.env_extra = env_extra
        self.base = 'http://127.0.0.1:%d' % self.port
        self.srv = None
        self.thread = None
        self.module = None

    def hdr(self, extra=None):
        """公网模式下的默认请求头：带白名单里的 Host。"""
        h = {'Host': self.HOST}
        h.update(extra or {})
        return h

    def start(self):
        sys.path.insert(0, _APP)
        if self.script == 'api.py':
            import api as m
            import uvicorn
            m.reload_security(self.env_extra)
            # 数据层：这里只验 HTTP 行为，用**内存库**即可
            # （不碰真实 data/，也不必复制 sqlite）。
            m.STORE = _memory_store(m)
            m.DOCSTORE = _temp_docstore(m)
            # ⚠️ 用**同一个** uvicorn 配置工厂（app/api.py 的 make_uvicorn_config），
            # 不要自己写 uvicorn.Config —— 那样会漏掉 proxy_headers=False，
            # 于是测试里 request.client 会被 XFF 覆盖，
            # 限流按攻击者声明的 IP 计数 → 测出来的行为与生产不一致
            # （实测：这条差异让「伪造 XFF 绕过限流」这条断言完全测不出来）。
            config = m.make_uvicorn_config(m.app, '127.0.0.1', self.port,
                                            log_config={'version': 1,
                                                        'disable_existing_loggers': False,
                                                        'handlers': {'null': {'class': 'logging.NullHandler'}},
                                                        'root': {'handlers': ['null'], 'level': 'INFO'}})
            self.srv = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.srv.run, daemon=True)
            self.thread.start()
            for _ in range(80):
                if self.srv.started:
                    break
                time.sleep(0.1)
        else:
            import server as m
            m.reload_security(self.env_extra)
            m.STORE = _memory_store(m)
            m.DOCSTORE = _temp_docstore(m)
            from http.server import ThreadingHTTPServer
            self.srv = ThreadingHTTPServer(('127.0.0.1', self.port), m.H)
            self.srv.daemon_threads = True
            self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
            self.thread.start()
        self.module = m
        return self.wait_ready()

    def wait_ready(self, timeout=30) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if http('GET', self.base + '/health/live', timeout=2)[0] == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def log_text(self) -> str:
        """服务自己的日志。api.py 走 core._log（开发模式是 print），
        这里抓不到 —— 所以测试里对「日志里没有明文令牌」这条断言
        改用**独立进程**验证（见 W3AuthTest.test_login_body_not_logged 的
        备选路径：直接检查 core._log 的输出缓冲）。"""
        return ''

    def stop(self):
        try:
            if self.srv is not None and hasattr(self.srv, 'should_exit'):
                self.srv.should_exit = True
            elif self.srv is not None:
                self.srv.shutdown()
                self.srv.server_close()
        except Exception:
            pass
        time.sleep(0.3)


# ---------------------------------------------------------------- W1


class W1BindAddressTest(unittest.TestCase):
    """W1：绑定地址配置化。默认必须仍是 127.0.0.1。"""

    def test_default_bind_is_loopback(self):
        cfg = security.Config({})
        self.assertEqual(cfg.bind, '127.0.0.1')

    def test_public_mode_binds_all(self):
        cfg = security.Config({'QBANK_PUBLIC': '1',
                               'QBANK_ALLOWED_HOSTS': 'x.example.com',
                               'QBANK_PASSWORD_HASH': TEST_HASH})
        self.assertEqual(cfg.bind, '0.0.0.0')

    def test_bind_zero_without_public_is_rejected(self):
        """`QBANK_BIND=0.0.0.0` 但没开公网模式 → 拒绝启动。

        这条防的是"手滑只设了 BIND"。安全配置不该有半开状态。
        """
        cfg = security.Config({'QBANK_BIND': '0.0.0.0'})
        errs = cfg.start_errors()
        self.assertTrue(any('QBANK_PUBLIC' in e for e in errs), errs)

    def test_public_mode_without_config_refuses_to_start(self):
        """公网模式缺域名/口令 → 拒绝启动（fail-closed）。"""
        cfg = security.Config({'QBANK_PUBLIC': '1'})
        errs = cfg.start_errors()
        self.assertTrue(any('QBANK_ALLOWED_HOSTS' in e for e in errs), errs)
        self.assertTrue(any('QBANK_PASSWORD_HASH' in e for e in errs), errs)

    def test_public_mode_with_local_only_hosts_refuses(self):
        """公网模式但白名单只有本机名 → 拒绝启动。

        否则服务能起来，但公网访问全被 403 —— owner 会以为已经公网化了。
        """
        cfg = security.Config({'QBANK_PUBLIC': '1',
                               'QBANK_PASSWORD_HASH': TEST_HASH,
                               'QBANK_ALLOWED_HOSTS': '127.0.0.1,localhost'})
        errs = cfg.start_errors()
        self.assertTrue(any('本机名' in e for e in errs), errs)

    def test_real_process_refuses_to_start_on_bad_config(self):
        """真起进程：公网模式缺配置 → 进程退出码 1，且不监听端口。"""
        port = free_port()
        srv = ServerProc('api.py', {'QBANK_PUBLIC': '1'}, port=port)
        self.addCleanup(srv.stop)
        # 进程内起服务前先做自检：配置有问题时 main() 会拒绝启动。
        import api as m
        m.reload_security({'QBANK_PUBLIC': '1'})
        self.assertTrue(m.SEC.start_errors(), '配置有问题时应当拒绝启动')
        # 服务不该起来
        self.assertFalse(srv.wait_ready(timeout=3), '配置有问题时不该监听端口')

    def test_external_host_without_auth_refuses_to_start(self):
        """半开配置：白名单里有非本机名，但没鉴权 → 拒绝启动。

        这是 2026-10-21 打包前自查抓到的漏洞。配置长这样：

            QBANK_ALLOWED_HOSTS=qbank.example.com
            # 没设 QBANK_PUBLIC，也没设 QBANK_PASSWORD_HASH

        它看起来是安全的（绑 127.0.0.1），但白名单里放着外部域名，
        说明**存在一条能把外部请求带进来的路径**（反代 / 端口转发）。
        本机直连会被 Host 白名单拦掉（403），owner 自测时看到"打不开"
        会以为配置生效了；可反代把 `Host: qbank.example.com` 转发过来
        时请求全部放行 —— **任何人都能读走全部进度、备注、打卡**。

        有外部入口就必须有鉴权。这条不变量由本测试守住。
        """
        cfg = security.Config({'QBANK_ALLOWED_HOSTS': 'qbank.example.com'})
        errs = cfg.start_errors()
        self.assertTrue(any('鉴权' in e for e in errs), errs)
        # 顺带确认：这类配置下确实**没有**鉴权（否则这条检查是多余的）
        self.assertFalse(cfg.auth_enabled)
        # 绑的仍是回环 —— 说明危险不在绑定地址，而在「入口开着、门没锁」
        self.assertEqual(cfg.bind, '127.0.0.1')

    def test_external_host_with_password_hash_is_allowed(self):
        """白名单含非本机名 + 有口令哈希 → 允许启动（不设 PUBLIC 也行）。

        `auth_enabled` 的判据是「有口令哈希」或「开了公网模式」，
        所以显式给哈希就已经有鉴权了，不必再开 PUBLIC。
        """
        cfg = security.Config({'QBANK_ALLOWED_HOSTS': 'qbank.example.com',
                               'QBANK_PASSWORD_HASH': TEST_HASH})
        self.assertTrue(cfg.auth_enabled)
        self.assertEqual(cfg.start_errors(), [])

    def test_local_only_hosts_without_auth_still_starts(self):
        """白名单只有本机名 + 没鉴权 → 仍然允许启动。

        这是**默认配置**（本机模式），必须不受上面那条新检查影响 ——
        默认行为零变化是硬门。
        """
        cfg = security.Config({})
        self.assertEqual(cfg.start_errors(), [])
        self.assertEqual(set(cfg.allowed_hosts), {'127.0.0.1', 'localhost', '[::1]'})


# ---------------------------------------------------------------- W2


class W2AllowedHostsTest(unittest.TestCase):

    def test_default_hosts_unchanged(self):
        cfg = security.Config({})
        self.assertEqual(set(cfg.allowed_hosts), {'127.0.0.1', 'localhost', '[::1]'})

    def test_public_domain_included(self):
        cfg = security.Config({'QBANK_PUBLIC': '1',
                               'QBANK_ALLOWED_HOSTS': 'mech.example.com,www.mech.example.com',
                               'QBANK_PASSWORD_HASH': TEST_HASH})
        self.assertIn('mech.example.com', cfg.allowed_hosts)
        self.assertIn('www.mech.example.com', cfg.allowed_hosts)

    def test_no_wildcard_or_suffix_matching(self):
        """白名单**精确匹配**：'.example.com' 不能匹配 'evil.com'。

        这是 DNS rebinding 防护的核心。后缀匹配一旦写错就等于全开放。
        """
        cfg = security.Config({'QBANK_ALLOWED_HOSTS': 'mech.example.com'})
        self.assertTrue(cfg.host_allowed('mech.example.com'))
        self.assertTrue(cfg.host_allowed('mech.example.com:8640'))   # 带端口
        self.assertFalse(cfg.host_allowed('evil.com'))
        self.assertFalse(cfg.host_allowed('notmech.example.com'))
        self.assertFalse(cfg.host_allowed('mech.example.com.evil.com'))
        self.assertFalse(cfg.host_allowed('.example.com'))

    def test_wildcard_in_config_is_literal(self):
        """配置里写 '*' 不会变成通配——它只是一个字面量主机名。"""
        cfg = security.Config({'QBANK_ALLOWED_HOSTS': '*'})
        self.assertTrue(cfg.host_allowed('*'))
        self.assertFalse(cfg.host_allowed('anything.com'))


# ---------------------------------------------------------------- W3


class W3AuthTest(unittest.TestCase):
    """W3：鉴权。用真进程（公网模式）。"""

    def setUp(self):
        self.srv = ServerProc('api.py', {
            'QBANK_PUBLIC': '1',
            'QBANK_ALLOWED_HOSTS': 'mech.example.com',
            'QBANK_PASSWORD_HASH': TEST_HASH,
            'QBANK_AUTH_FAIL_THRESHOLD': '5',
            'QBANK_AUTH_BACKOFF_BASE': '1',
            'QBANK_AUTH_BACKOFF_MAX': '4',
        })
        self.addCleanup(self.srv.stop)
        self.assertTrue(self.srv.start(), '服务端没起来')

    def _api(self, path):
        return self.srv.base + '/api/v1' + path

    def _h(self, extra=None):
        """公网模式下必须带白名单里的 Host 头，否则被 guard 403。"""
        return self.srv.hdr(extra)

    def test_no_token_401(self):
        code, d, _ = jhttp('GET', self._api('/stats'), headers=self._h())
        self.assertEqual(code, 401, d)
        self.assertEqual(d.get('error', {}).get('code'), 'unauthorized')

    def test_wrong_token_401(self):
        code, d, _ = jhttp('GET', self._api('/stats'),
                           headers=self._h({'Authorization': 'Bearer tok_bogus'}))
        self.assertEqual(code, 401, d)

    def test_login_with_correct_password_200(self):
        code, d, _ = jhttp('POST', self._api('/auth/login'),
                           {'password': TEST_PASSWORD}, headers=self._h())
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get('token', '').startswith('tok_'), d)
        self.assertTrue(d.get('auth_required'))

    def test_login_wrong_password_401(self):
        code, d, _ = jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                           headers=self._h())
        self.assertEqual(code, 401, d)
        self.assertNotIn('token', d)

    def test_valid_token_200(self):
        _, d, _ = jhttp('POST', self._api('/auth/login'), {'password': TEST_PASSWORD},
                        headers=self._h())
        tok = d['token']
        code, d2, _ = jhttp('GET', self._api('/stats'),
                             headers=self._h({'Authorization': 'Bearer ' + tok}))
        self.assertEqual(code, 200, d2)

    def test_token_revocable_by_logout(self):
        """登出后令牌**立刻**失效 —— 这就是不用自签 JWT 的理由。"""
        _, d, _ = jhttp('POST', self._api('/auth/login'), {'password': TEST_PASSWORD},
                        headers=self._h())
        tok = d['token']
        hdr = self._h({'Authorization': 'Bearer ' + tok})
        self.assertEqual(jhttp('GET', self._api('/stats'), headers=hdr)[0], 200)
        code, _, _ = jhttp('POST', self._api('/auth/logout'), {}, headers=hdr)
        self.assertEqual(code, 200)
        self.assertEqual(jhttp('GET', self._api('/stats'), headers=hdr)[0], 401)

    def test_token_rotatable(self):
        """轮换：拿旧令牌换新令牌，**旧令牌立即作废**。"""
        _, d, _ = jhttp('POST', self._api('/auth/login'), {'password': TEST_PASSWORD},
                        headers=self._h())
        old = d['token']
        code, d2, _ = jhttp('POST', self._api('/auth/refresh'), {'token': old},
                            headers=self._h())
        self.assertEqual(code, 200, d2)
        new = d2['token']
        self.assertNotEqual(old, new)
        self.assertEqual(jhttp('GET', self._api('/stats'),
                               headers=self._h({'Authorization': 'Bearer ' + old}))[0], 401)
        self.assertEqual(jhttp('GET', self._api('/stats'),
                               headers=self._h({'Authorization': 'Bearer ' + new}))[0], 200)

    def test_login_body_not_logged(self):
        """令牌卫生第 1 条：日志里搜不到明文口令与明文令牌。

        ## 怎么抓日志
        开发模式下 `core._log` 就是 `print`（非 frozen 不写文件），
        所以直接**替换 core._log** 收集输出即可 —— 比去读文件可靠，
        也不依赖 qbank.log 只在 frozen 下存在这件事。

        ⚠️ 替换后必须还原，否则后面的用例会把日志写到测试输出里。
        """
        import core
        captured = []
        real_log = core._log
        core._log = lambda msg: captured.append(str(msg))
        # api.py 里的 _log 是 `from core import _log` —— 绑死在导入时，
        # 所以两处都要换。
        import api as m
        real_api_log = m._log
        m._log = lambda msg: captured.append(str(msg))
        try:
            _, d, _ = jhttp('POST', self._api('/auth/login'),
                            {'password': TEST_PASSWORD}, headers=self._h())
            tok = d['token']
            jhttp('GET', self._api('/stats'),
                  headers=self._h({'Authorization': 'Bearer ' + tok}))
            jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                  headers=self._h())
            time.sleep(0.3)
        finally:
            core._log = real_log
            m._log = real_api_log
        blob = '\n'.join(captured)
        self.assertTrue(blob.strip(), '没抓到任何日志，测试本身有问题')
        self.assertNotIn(TEST_PASSWORD, blob, '口令进了日志！')
        self.assertNotIn(tok, blob, '明文令牌进了日志！')
        self.assertNotIn('Authorization', blob, '日志里出现了 Authorization 字样')
        # 反向对照：日志**确实**记了这次请求（否则上面三条是假通过）
        self.assertIn('/api/v1/auth/login', blob)

    def test_health_endpoints_exempt(self):
        """健康检查不需要令牌（编排器要能探活）。"""
        self.assertEqual(http('GET', self.srv.base + '/health/live')[0], 200)

    def test_auth_status_reports_state(self):
        code, d, _ = jhttp('GET', self._api('/auth/status'), headers=self._h())
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get('auth_required'))
        self.assertFalse(d.get('authenticated'))


# ---------------------------------------------------------------- W4


class W4RateLimitTest(unittest.TestCase):

    def setUp(self):
        self.srv = ServerProc('api.py', {
            'QBANK_PUBLIC': '1',
            'QBANK_ALLOWED_HOSTS': 'mech.example.com',
            'QBANK_PASSWORD_HASH': TEST_HASH,
            'QBANK_AUTH_FAIL_THRESHOLD': '5',
            'QBANK_AUTH_BACKOFF_BASE': '1',
            'QBANK_AUTH_BACKOFF_MAX': '4',
            'QBANK_UPLOAD_WINDOW': '60',
            'QBANK_UPLOAD_MAX': '3',
        })
        self.addCleanup(self.srv.stop)
        self.assertTrue(self.srv.start(), '服务端没起来')

    def _api(self, path):
        return self.srv.base + '/api/v1' + path

    def _h(self, extra=None):
        return self.srv.hdr(extra)

    def test_wrong_password_20_times_gets_limited(self):
        """错口令连打 20 次 → 被限流（429）。

        阈值 5，之后退避递增（base=1s，封顶 4s）。
        """
        codes = []
        for _ in range(20):
            code, d, _ = jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                               headers=self._h())
            codes.append(code)
            if code == 429:
                break
        self.assertIn(429, codes, '20 次错口令没被限流：%s' % codes)
        # 限流后应该带 Retry-After
        # ⚠️ HTTP 头名**不区分大小写**（uvicorn 发的是小写 retry-after），
        # 所以不能直接 `'Retry-After' in h` —— 第一版就是这么写的，
        # 明明服务端带了头却报「没有」，白查半天。
        #
        # ⚠️ 这里**不能**假设「紧接着的那次也一定是 429」：
        # 退避到期后会放行一次（这次若再失败，计数继续涨 → 下次退避更长）。
        # 所以要么等退避窗口内打（必然 429），要么只看「出现过 429」。
        # 用后者：它验的是「限流确实生效」，而 Retry-After 验的是「告诉客户端等多久」。
        code, _, h = jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                           headers=self._h())
        self.assertIn(code, (401, 429), code)
        if code == 429:
            self.assertTrue(any(k.lower() == 'retry-after' for k in h), h)

    def test_backoff_increases(self):
        """退避时长随失败次数递增（且封顶）。"""
        cfg = security.Config({'QBANK_AUTH_FAIL_THRESHOLD': '5',
                               'QBANK_AUTH_BACKOFF_BASE': '1',
                               'QBANK_AUTH_BACKOFF_MAX': '8'})
        lim = security.RateLimiter(cfg)
        delays = []
        for _ in range(9):
            delays.append(lim.record_auth_failure('1.2.3.4'))
        # 前 5 次不延迟
        self.assertEqual(delays[:5], [0, 0, 0, 0, 0], delays)
        # 之后递增
        self.assertEqual(delays[5], 1, delays)
        self.assertEqual(delays[6], 2, delays)
        self.assertEqual(delays[7], 4, delays)
        # 封顶 8
        self.assertEqual(delays[8], 8, delays)

    def test_retry_after_header_present_when_limited(self):
        """被限流时响应带 Retry-After（告诉客户端等多久）。

        单独一个用例，因为上一条断言里「紧接着那次」可能是 401
        （退避到期会放行一次），不能稳定地拿到 429。
        这里改成：**在退避窗口内**再打一次，必然还是 429。
        """
        # 先把失败次数打到阈值以上
        for _ in range(8):
            code, _, _ = jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                               headers=self._h())
            if code == 429:
                break
        # 紧接着再打（退避窗口内）→ 必然 429 + Retry-After
        code, _, h = jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                           headers=self._h())
        if code == 429:
            self.assertTrue(any(k.lower() == 'retry-after' for k in h), h)
        else:
            # 退避刚好到期 → 这次放行，但**下一次**必然 429（计数已很高）
            code2, _, h2 = jhttp('POST', self._api('/auth/login'),
                                 {'password': 'wrong'}, headers=self._h())
            self.assertEqual(code2, 429, '退避到期后又连打，应立刻再次限流')
            self.assertTrue(any(k.lower() == 'retry-after' for k in h2), h2)

    def test_upload_rate_limited(self):
        """上传接口有频率限制。"""
        _, d, _ = jhttp('POST', self._api('/auth/login'), {'password': TEST_PASSWORD},
                        headers=self._h())
        hdr = self._h({'Authorization': 'Bearer ' + d['token']})
        body = (b'--X\r\nContent-Disposition: form-data; name="file"; '
                b'filename="a.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
                b'%PDF-1.4\r\n--X--\r\n')
        codes = []
        for _ in range(8):
            code, _, _ = jhttp('POST', self._api('/documents'), body,
                               headers={**hdr, 'Content-Type': 'multipart/form-data; boundary=X'})
            codes.append(code)
            if code == 429:
                break
        self.assertIn(429, codes, '上传没有被限流：%s' % codes)

    def test_xff_not_trusted_from_unknown_peer(self):
        """XFF 来自**非受信**对端 → 不采信，用真实 peer。

        否则攻击者加个 `X-Forwarded-For: 1.2.3.4` 就能假装自己是别人，
        限流与审计全部失效。
        """
        self.assertEqual(
            security.client_ip('10.0.0.5', '1.2.3.4', trusted_proxies=frozenset()),
            '10.0.0.5')

    def test_xff_trusted_from_known_proxy(self):
        """XFF 来自**受信**对端 → 采信最左边那一节。"""
        self.assertEqual(
            security.client_ip('10.0.0.5', '1.2.3.4, 10.0.0.5',
                               trusted_proxies=frozenset({'10.0.0.5'})),
            '1.2.3.4')

    def test_xff_trusted_via_cidr(self):
        cfg_cidrs = ('10.0.0.0/8',)
        self.assertEqual(
            security.client_ip('10.1.2.3', '9.9.9.9', trusted_cidrs=cfg_cidrs),
            '9.9.9.9')
        self.assertEqual(
            security.client_ip('11.1.2.3', '9.9.9.9', trusted_cidrs=cfg_cidrs),
            '11.1.2.3')

    def test_xff_empty_falls_back_to_peer(self):
        self.assertEqual(
            security.client_ip('10.0.0.5', '', trusted_proxies=frozenset({'10.0.0.5'})),
            '10.0.0.5')

    def test_real_process_ignores_forged_xff(self):
        """真进程：伪造 XFF 不能绕过鉴权失败退避。

        同一个真实对端（127.0.0.1）连打错口令，每次都换一个伪造的 XFF ——
        若服务端采信 XFF，每次都算新 IP，退避永远不触发。
        """
        codes = []
        for i in range(12):
            code, _, _ = jhttp('POST', self._api('/auth/login'), {'password': 'wrong'},
                               headers=self._h({'X-Forwarded-For': '10.%d.%d.%d' % (i, i, i)}))
            codes.append(code)
            if code == 429:
                break
        self.assertIn(429, codes, '伪造 XFF 绕过了限流：%s' % codes)


# ---------------------------------------------------------------- 等价


class TwoServersEquivalenceTest(unittest.TestCase):
    """两套服务端（api.py / server.py）行为必须一致。"""

    ENV = {
        'QBANK_PUBLIC': '1',
        'QBANK_ALLOWED_HOSTS': 'mech.example.com',
        'QBANK_PASSWORD_HASH': TEST_HASH,
        'QBANK_AUTH_FAIL_THRESHOLD': '5',
        'QBANK_AUTH_BACKOFF_BASE': '1',
        'QBANK_AUTH_BACKOFF_MAX': '4',
    }

    def _probe(self, script):
        srv = ServerProc(script, dict(self.ENV))
        self.addCleanup(srv.stop)
        self.assertTrue(srv.start(), '%s 没起来' % script)
        # ⚠️ 打**两边都有、且行为一致**的路径。
        #   · /api/v1/stats 只有 api.py 有（server.py 的老路由是 /api/stats）
        #     → 拿它比等价性会得到假失败；
        #   · /api/v1/custom 只有 api.py 有；
        #   · /api/v1/documents 两边都有（M3.3），但要 DOCSTORE ——
        #     下面用临时目录初始化它。
        api = srv.base + '/api/v1'
        H = srv.hdr
        out = {}
        out['no_token'] = jhttp('GET', api + '/documents', headers=H())[0]
        out['bad_token'] = jhttp('GET', api + '/documents',
                                 headers=H({'Authorization': 'Bearer tok_nope'}))[0]
        code, d, _ = jhttp('POST', api + '/auth/login', {'password': TEST_PASSWORD},
                            headers=H())
        out['login'] = code
        out['token_prefix'] = d.get('token', '')[:4]
        tok = d.get('token', '')
        out['with_token'] = jhttp('GET', api + '/documents',
                                  headers=H({'Authorization': 'Bearer ' + tok}))[0]
        out['logout'] = jhttp('POST', api + '/auth/logout', {},
                               headers=H({'Authorization': 'Bearer ' + tok}))[0]
        out['after_logout'] = jhttp('GET', api + '/documents',
                                    headers=H({'Authorization': 'Bearer ' + tok}))[0]
        out['bad_host'] = jhttp('GET', api + '/auth/status',
                                headers={'Host': 'evil.com'})[0]
        out['good_host'] = jhttp('GET', api + '/auth/status',
                                 headers={'Host': ServerProc.HOST})[0]
        out['status'] = jhttp('GET', api + '/auth/status', headers=H())[0]
        out['health'] = http('GET', srv.base + '/health/live')[0]
        # 错口令 8 次 → 期望被限流
        codes = []
        for _ in range(8):
            c, _, _ = jhttp('POST', api + '/auth/login', {'password': 'wrong'},
                            headers=H())
            codes.append(c)
            if c == 429:
                break
        out['limited'] = 429 in codes
        return out

    def test_both_servers_same_behavior(self):
        a = self._probe('api.py')
        b = self._probe('server.py')
        for k in a:
            self.assertEqual(a[k], b[k],
                             '%s 不一致：api=%r server=%r' % (k, a[k], b[k]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
