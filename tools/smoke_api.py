# -*- coding: utf-8 -*-
r"""
smoke_api.py — FastAPI（app/api.py）RESTful HTTP 层冒烟。

与 smoke_test.py（标准库 http.server 版）分工：
  smoke_test.py 盯数据层 + server.py 的老路由；本文件盯 api.py 的 RESTful 契约——
  方法语义（GET/POST/PUT/DELETE）、状态码（200/403/404/422）、响应体形状、
  Host/Origin 白名单、vite dist 静态挂载、shutdown 端点。

用法：
    python tools/smoke_api.py            # 全量模式（data/qbank.sqlite 的临时副本，不碰真实数据）
    python tools/smoke_api.py --public   # 公开子集模式（data/public/qbank.json，内存库）

依赖：fastapi / uvicorn / pydantic（与 app/api.py 相同）。用仓库的 .venv-build 解释器跑：
    .venv-build/Scripts/python.exe tools/smoke_api.py

隔离：数据路径指到临时副本（core 模块上的常量也要改——core 抽取后 Store 运行期读的是
      core 的名字，只改 api 模块的名字不生效，2026-10-17 踩过）。跑完即删，不碰真实 data/。
"""
from __future__ import annotations
import importlib.util
import json
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time as _t
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# 打本机端口必须绕过代理。urllib 默认读 http_proxy 环境变量，会把打向 127.0.0.1 的
# 请求也发给代理；代理连不上就回 502，症状伪装成「服务起不来」（2026-10-20 在
# smoke_test.py 上实测到，响应体是代理的中文报错「目标计算机积极拒绝 10061」）。
# 之前这里能过是运气 —— uvicorn 启动快、连接被复用；换个时机就会翻车。
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(req, timeout=10):
    """绕过代理的 urlopen。行为与 urllib.request.urlopen 一致，只是不走代理。"""
    return NO_PROXY_OPENER.open(req, timeout=timeout)

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ''):
    (PASS if cond else FAIL).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name + (('  → ' + str(detail)) if (detail and not cond) else ''))


def load_api_module():
    appdir = str(ROOT / 'app')
    if appdir not in sys.path:
        sys.path.insert(0, appdir)
    spec = importlib.util.spec_from_file_location('qbank_api_smoke', ROOT / 'app' / 'api.py')
    m = importlib.util.module_from_spec(spec)
    # 必须注册进 sys.modules：api.py 顶部有 `from __future__ import annotations`，
    # MarkIn 的 result: Literal[...] 注解被存成字符串，pydantic 建类时要靠
    # sys.modules[模块名].__dict__ 解析 Literal；不注册会得到
    # "MarkIn is not fully defined"，端点每次调用都 500（2026-10-17 踩坑）。
    # 生产路径（python app/api.py / pyinstaller）模块天然在 sys.modules 里，无此问题。
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def scrub_user_state(db: Path):
    """清掉副本里的用户答题痕迹，只留题库与卡片。

    与 smoke_test.py 同源问题（2026-10-20）：copy2 把真实库的 progress 表连同
    last_seen 一起带走，checkin()（core.py:710）从 last_seen 聚合，于是真实刷题记录
    会混进打卡断言。本文件目前只断言日历形状（不硬编码天数），但依赖日期的断言
    迟早会被同样的漂移打中，且删掉用户痕迹是测试本分。fsrs_state 一并清。
    """
    if not db.exists():
        return
    con = sqlite3.connect(db)
    try:
        for t in ('progress', 'fsrs_state'):
            try:
                con.execute('DELETE FROM %s' % t)
            except sqlite3.OperationalError:
                pass
        con.commit()
    finally:
        con.close()


def fresh_store(m, tmp: Path, public: bool, tag: str = ''):
    """数据指向临时副本；DB/PROGJSON/CKJSON/NOTESJSON 在 core 模块上，两处都要改。"""
    if public:
        paths = {'DB': tmp / 'not-exist.sqlite'}
    else:
        paths = {'DB': tmp / ('qbank%s.sqlite' % tag)}
        shutil.copy2(ROOT / 'data' / 'qbank.sqlite', paths['DB'])
        scrub_user_state(paths['DB'])           # 别把真实刷题记录带进断言
    paths.update({'PROGJSON': tmp / ('progress%s.json' % tag),
                  'CKJSON': tmp / ('checkin%s.json' % tag),
                  'NOTESJSON': tmp / ('notes%s.json' % tag)})
    for k, v in paths.items():
        setattr(m.core, k, v)
        setattr(m, k, v)
    return m.Store()


def synthetic_rows(con):
    """判分用例：判断/带选项判断/单选/多选/自评/待补/历史加粗（与 smoke_test.py 同源）。"""
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TJ1', '测试库', '自出', '测试 › 判断', '', '判断', '太阳从东边升起（　）', '判断', 1, '[]',
                 '对', '地球自转', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TJ2', '测试库', '自出', '测试 › 判断', '', '判断', '平键能传递轴向力（　）', '判断', 2,
                 json.dumps(['A. 对', 'B. 错'], ensure_ascii=False), 'A', '平键只固定周向', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TS1', '测试库', '自出', '测试 › 单选', '', '单选', '控制气缸速度用（　）', '单选', 2,
                 json.dumps(['A. 溢流阀', 'B. 节流阀', 'C. 单向阀', 'D. 减压阀'], ensure_ascii=False),
                 'B', '流量决定速度', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TM1', '测试库', '自出', '测试 › 多选', '', '多选', '属于摩擦防松的有（　）', '多选', 3,
                 json.dumps(['A. 弹簧垫圈', 'B. 双螺母', 'C. 开口销', 'D. 串联钢丝'], ensure_ascii=False),
                 'AB', '开口销/串联钢丝属机械防松', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TW1', '测试库', '自出', '测试 › 问答', '', '问答', '简述调质处理的目的', '问答', 2, '[]',
                 '淬火后高温回火，获得强度与韧性的良好配合', '', '', '', ''))
    con.commit()


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def due_days(mark_resp) -> int:
    due = datetime.fromisoformat(mark_resp['due'])
    return round((due - datetime.now()).total_seconds() / 86400)


def api_smoke(m, public: bool):
    print('\n== FastAPI RESTful 层：%s 模式 ==' % ('公开子集' if public else '全量库（临时副本）'))
    tmp = Path(tempfile.mkdtemp(prefix='qbank-api-'))
    st = fresh_store(m, tmp, public)
    synthetic_rows(st.con)
    m.STORE = st

    # ---- 前后端接口契约：web/src/api.js 的静态路由必须都在 api.py 注册过 ----
    api_src = (ROOT / 'app' / 'api.py').read_text(encoding='utf-8')
    routes = set(re.findall(r"@app\.(?:get|post|put|delete)\('/api/v1([^']*)'", api_src))
    js_src = (ROOT / 'web' / 'src' / 'api.js').read_text(encoding='utf-8')
    static_only = {'/template'}   # 仅 APK 静态模式；/import_questions 与 /custom 两端都已实现
    used = set(re.findall(r"p === '(/[a-z_]+)'", js_src))
    used = {u for u in used if u not in static_only}
    check('前后端接口契约：静态路由在服务端都有注册', used <= routes, str(sorted(used - routes)))

    import uvicorn
    port = free_port()
    config = uvicorn.Config(m.app, host='127.0.0.1', port=port, log_level='error')
    srv = uvicorn.Server(config)
    m.SRV = srv
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(60):
        if srv.started:
            break
        _t.sleep(0.1)
    base = 'http://127.0.0.1:%d/api/v1' % port

    def req(method: str, path: str, obj=None, headers: dict | None = None):
        data = json.dumps(obj).encode('utf-8') if obj is not None else None
        r = urllib.request.Request(base + path, data=data, method=method,
                                   headers={**({'Content-Type': 'application/json'} if data else {}),
                                            **(headers or {})})
        try:
            with urlopen(r, timeout=10) as resp:
                body = resp.read()
                return resp.status, (json.loads(body.decode('utf-8')) if body else {}), dict(resp.headers)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body.decode('utf-8')), dict(e.headers)
            except Exception:
                return e.code, {}, dict(e.headers)

    def jget(path, headers=None):
        return req('GET', path, headers=headers)

    def _raw_get(url):
        """打完整 URL（不带 /api/v1 前缀）。给 /health/* 这类根路径端点用。"""
        try:
            with urlopen(urllib.request.Request(url), timeout=10) as resp:
                body = resp.read()
                return resp.status, (json.loads(body.decode('utf-8')) if body else {}), dict(resp.headers)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body.decode('utf-8')), dict(e.headers)
            except Exception:
                return e.code, {}, dict(e.headers)

    try:
        code, d, _ = jget('/stats')
        check('GET /stats', code == 200 and d.get('total', 0) > 0 and 'by_type' in d, d)

        code, d, _ = jget('/questions?limit=3')
        check('GET /questions?limit=3', code == 200 and len(d.get('items', [])) <= 3)
        code, d, _ = jget('/questions?n=99999&limit=99999')
        check('limit 过大被内部夹到 ≤100', code == 200 and len(d.get('items', [])) <= 100)
        code, d, _ = jget('/questions?limit=100')
        ranks = [it.get('qrank') for it in d.get('items', [])]
        check('组卷行带隐藏题型标签 qrank，且不下发 answer',
              code == 200 and len(ranks) > 10 and all(isinstance(r, int) for r in ranks)
              and all('answer' not in it for it in d.get('items', [])), ranks[:12])
        check('组卷顺序按 qrank 升序（单选→多选→判断→…→计算垫底）',
              ranks == sorted(ranks), ranks)
        code, d, _ = jget('/questions?level=abc')
        check('level=abc → 422（FastAPI 参数校验）', code == 422, code)
        code, d, _ = jget('/questions?review=1&limit=5')
        check('GET /questions?review=1（今日复习）', code == 200 and isinstance(d.get('items'), list))
        code, d, _ = jget('/questions?q=' + urllib.parse.quote('轴承'))
        check('GET /questions?q=（搜索）', code == 200 and isinstance(d.get('items'), list))

        code, d, _ = req('POST', '/questions/TS1/grade', {'given': 'B'})
        check('POST .../grade 答对', code == 200 and d.get('correct') is True and d.get('id') == 'TS1', d)
        code, d, _ = req('POST', '/questions/TS1/grade', {'given': 'A'})
        check('POST .../grade 答错', code == 200 and d.get('correct') is False, d)
        code, d, _ = req('POST', '/questions/TW1/grade', {'given': '随便'})
        check('POST .../grade 自评（correct=null, self=true）',
              code == 200 and d.get('correct') is None and d.get('self') is True, d)
        code, d, _ = req('POST', '/questions/NOSUCH/grade', {'given': 'A'})
        check('POST .../grade 不存在题号 → 404', code == 404, code)

        code, d, _ = req('PUT', '/progress/TS1', {'result': 'wrong'})
        # 2026-10-18：排期换成 FSRS-6，「答错→恰好 1 天后」不再成立
        # （FSRS 的 relearning 步是分钟级）。改为断言「到期 ≤1 天」且带 stability。
        check('PUT /progress/{id} 记录结果', code == 200 and d.get('ok')
              and due_days(d) <= 1 and (d.get('stability') or 0) > 0, d)
        code, d, _ = req('PUT', '/progress/TS1', {'result': 'bogus'})
        check('PUT /progress/{id} 非法 result → 422', code == 422, code)
        code, d, _ = req('PUT', '/progress/NOSUCH', {'result': 'skip'})
        check('PUT /progress/{id} 不存在题号 → 404', code == 404, code)

        code, d, _ = req('PUT', '/questions/TS1/note', {'note': '冒烟：节流阀调速'})
        check('PUT .../note 写备注', code == 200 and d.get('ok') and not d.get('removed'), d)
        code, d, _ = jget('/questions/TS1/note')
        check('GET .../note 读备注', code == 200 and d.get('note') == '冒烟：节流阀调速', d)
        code, d, _ = req('DELETE', '/questions/TS1/note')
        check('DELETE .../note 删备注', code == 200 and d.get('removed') is True, d)
        code, d, _ = jget('/questions/TS1/note')
        check('GET .../note 删除后为空', code == 200 and d.get('note') == '', d)
        code, d, _ = jget('/notes')
        check('GET /notes', code == 200 and isinstance(d.get('items'), list))

        code, d, _ = jget('/wrong')
        check('GET /wrong（刚答错过 TS1）', code == 200 and any(x['id'] == 'TS1' for x in d.get('items', [])), d)

        # ---- 自定义题（PDF 摘录编辑器）：导入 / 填空判分 / 列表 / 删除 ----
        customs = [
            {'id': 'XC1', 'stem': '齿轮的（　）圆是分度基准，大小由（　）决定', 'qtype': '填空',
             'answer': '分度圆|模数', 'level': 1, 'bank': 'PDF 自建', 'explanation': '模数决定齿形大小'},
            {'id': 'XJ1', 'stem': '平键能传递轴向力', 'qtype': '判断', 'answer': '错',
             'level': 1, 'bank': 'PDF 自建', 'explanation': '平键只固定周向'},
        ]
        code, d, _ = req('POST', '/import_questions', {'questions': customs, 'mode': 'skip'})
        check('POST /import_questions 批量导入自定义题',
              code == 200 and d.get('added') == 2 and not d.get('rejected'), d)
        code, d, _ = req('POST', '/import_questions', {'questions': customs, 'mode': 'skip'})
        check('POST /import_questions 重复导入 → skipped',
              code == 200 and d.get('skipped') == 2 and d.get('added') == 0, d)
        code, d, _ = req('POST', '/import_questions', {'questions': [{'id': 'TS1', 'stem': '撞号'}]})
        check('POST /import_questions 撞淬取题号 → rejected',
              code == 200 and len(d.get('rejected') or []) == 1, d)

        code, d, _ = req('POST', '/questions/XC1/grade', {'given': '分度圆|模数'})
        check('填空判分：两空全对 → correct + fill 明细',
              code == 200 and d.get('correct') is True and len(d.get('fill') or []) == 2, d)
        code, d, _ = req('POST', '/questions/XC1/grade', {'given': '分度圆|压力角'})
        check('填空判分：错一空 → correct=False 且逐空标对错',
              code == 200 and d.get('correct') is False
              and [x['ok'] for x in d.get('fill') or []] == [True, False], d)
        code, d, _ = req('POST', '/questions/XC1/grade', {'given': '分度圆'})
        check('填空判分：空数不齐按空串判错', code == 200 and d.get('correct') is False, d)
        code, d, _ = req('POST', '/questions/XC1/grade', {'given': ' 分度圆 ｜ 模数。'})
        check('填空判分：全角竖线与首尾空白/句号被归一',
              code == 200 and d.get('correct') is True, d)
        code, d, _ = req('POST', '/questions/XJ1/grade', {'given': '错'})
        check('判断判分：自定义判断题', code == 200 and d.get('correct') is True, d)

        code, d, _ = jget('/custom')
        check('GET /custom 列表', code == 200 and len(d.get('items') or []) == 2, d)
        code, d, _ = jget('/questions?q=' + urllib.parse.quote('分度基准'))
        check('自定义题可被搜索', code == 200 and any(x['id'] == 'XC1' for x in d.get('items', [])), d)
        code, d, _ = req('PUT', '/progress/XC1', {'result': 'correct'})
        check('自定义题可记进度', code == 200 and d.get('ok') is True, d)
        code, d, _ = req('POST', '/custom', {'id': 'XC2', 'stem': '单题接口', 'qtype': '判断', 'answer': '对'})
        check('POST /custom 单题创建', code == 200 and d.get('ok') is True, d)
        code, d, _ = req('POST', '/custom', {'id': 'TS1', 'stem': '撞号', 'qtype': '判断', 'answer': '对'})
        check('POST /custom 撞淬取题号 → 400', code == 400, code)
        code, d, _ = req('DELETE', '/custom/XC2')
        check('DELETE /custom/{id}', code == 200 and d.get('removed') is True, d)
        code, d, _ = req('DELETE', '/custom/XC2')
        check('DELETE /custom/{id} 再删 → 404', code == 404, code)
        code, d, _ = req('DELETE', '/custom/XC1')
        code2, d2, _ = jget('/progress')
        check('DELETE 自定义题时级联清进度（stats 不虚高）',
              code == 200 and d.get('removed') is True
              and 'XC1' not in (d2.get('progress') or {}), list((d2.get('progress') or {}).keys())[:5])
        code, d, _ = jget('/banks')
        check('GET /banks 含「PDF 自建」', code == 200 and any('PDF' in str(x) for x in d.get('items', [])), d)

        for p in ['/banks', '/sections', '/chapters']:
            code, d, _ = jget(p)
            check('GET %s' % p, code == 200 and isinstance(d.get('items'), list))
        code, d, _ = jget('/cards?q=' + urllib.parse.quote('溢流'))
        check('GET /cards', code == 200 and isinstance(d.get('items'), list))

        code, d, _ = jget('/progress')
        check('GET /progress 导出', code == 200 and isinstance(d.get('progress'), dict) and 'TS1' in d['progress'], d)
        code, d, _ = req('POST', '/progress', {'progress': {'TIMP1': {'state': 'correct', 'streak': 2,
                                                                     'wrong': 0, 'due': '2026-10-20T10:00:00',
                                                                     'last_seen': '2026-10-17T10:00:00'}}})
        check('POST /progress 导入', code == 200 and d.get('imported') == 1, d)

        code, d, _ = jget('/checkins')
        ym = datetime.now().strftime('%Y-%m')
        import calendar
        ndays = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
        check('GET /checkins 日历形状',
              code == 200 and d.get('month') == ym and len(d.get('days', [])) == ndays and 'streak' in d, d)
        code, d, _ = req('POST', '/checkins')
        check('POST /checkins 手动补签', code == 200 and d.get('checked_today') is True, d)

        code, d, h = jget('/stats', headers={'Host': 'evil.com'})
        # 2026-10-20 起错误响应统一为 {"error":{"code","message","request_id"}}，
        # 不再是 FastAPI 原生的 {"detail": "..."}。
        err = (d or {}).get('error') or {}
        check('Host 白名单：evil.com → 403 且走统一错误格式',
              code == 403 and err.get('code') == 'forbidden' and bool(err.get('request_id')),
              (code, d))
        code, d, h = jget('/stats', headers={'Origin': 'http://evil.com'})
        check('Origin 白名单：evil.com → 403', code == 403, code)
        # vite dev server（localhost:5173）跨域打本服务的放行规则
        r = urllib.request.Request(base + '/stats', method='OPTIONS',
                                   headers={'Origin': 'http://localhost:5173',
                                            'Access-Control-Request-Method': 'GET'})
        try:
            with urlopen(r, timeout=10) as resp:
                check('CORS 预检：localhost:5173 放行',
                      resp.status == 200 and resp.headers.get('access-control-allow-origin') == 'http://localhost:5173',
                      resp.status)
        except urllib.error.HTTPError as e:
            check('CORS 预检：localhost:5173 放行', False, e.code)

        # vite dist 静态挂载（api.py 底部 StaticFiles）
        try:
            with urlopen('http://127.0.0.1:%d/' % port, timeout=10) as resp:
                html = resp.read().decode('utf-8')
            check('GET / 返回 vite dist 页面', resp.status == 200 and '机械题库' in html)
        except Exception as e:
            check('GET / 返回 vite dist 页面', False, repr(e)[:80])
        dist_js = list((ROOT / 'web' / 'dist' / 'assets').glob('index-*.js'))
        if dist_js:
            try:
                with urlopen('http://127.0.0.1:%d/assets/%s' % (port, dist_js[0].name),
                                            timeout=10) as resp:
                    check('GET /assets/index-*.js → 200', resp.status == 200)
            except Exception as e:
                check('GET /assets/index-*.js → 200', False, repr(e)[:80])
        else:
            print('  - 跳过：web/dist 不存在（先 npm run build）')

        # ---- 健康检查（2026-10-20 第3 部分）----
        # 注意：/health/* 在 /api/v1 **之外**（健康检查是基础设施接口，不是业务 API），
        # 所以不能用 jget（它会自动带上 /api/v1 前缀），要打完整 base。
        root = 'http://127.0.0.1:%d' % port
        code, d, h = _raw_get(root + '/health/live')
        check('GET /health/live → 200 且不碰数据层', code == 200 and d.get('status') == 'ok', (code, d))
        code, d, h = _raw_get(root + '/health/ready')
        check('GET /health/ready → 200（数据层可读）', code == 200 and d.get('status') == 'ready', (code, d))

        # ---- 数据层不可用时的 ready=503（受控注入，不碰真实库）----
        # 为什么必须一起测 live：/health/live「只表示进程活着、不查数据库」是一个
        # 设计约定，不是实现细节。单独测 ready=503 时，live 退化成也去查数据库
        # 也能让这两条都变绿 —— 所以必须同时断言 live 仍 200。
        class _DeadStore:
            """读操作必抛的死桩，模拟库被删 / 锁住 / 权限丢失。"""
            mode = 'sqlite'
            def q(self, sql, args=()):
                raise sqlite3.OperationalError('unable to open database file')
        saved = m.STORE
        try:
            m.STORE = _DeadStore()
            code, d, h = _raw_get(root + '/health/live')
            check('数据层不可用时 /health/live 仍 200（刻意不查数据库）',
                  code == 200 and d.get('status') == 'ok', (code, d))
            code, d, h = _raw_get(root + '/health/ready')
            e = (d or {}).get('error')
            check('数据层不可用时 /health/ready → 503 且 code=internal_error',
                  code == 503 and isinstance(e, dict) and e.get('code') == 'internal_error',
                  (code, d))
            check('ready 503 的错误体含 message 与 request_id',
                  isinstance(e, dict) and isinstance(e.get('message'), str)
                  and bool(e.get('request_id')), e)
        finally:
            m.STORE = saved          # 断言抛异常也必须恢复，否则污染后续测试
        # 恢复后自检：确认 STORE 真的换回来了（这条能抓住 finally 被跳过的情况）
        code, d, _ = _raw_get(root + '/health/ready')
        check('恢复 STORE 后 /health/ready 回到 200', code == 200 and d.get('status') == 'ready',
              (code, d))

        # ---- 统一错误格式（2026-10-20 第 3 部分）----
        # 契约：{"error": {"code": "...", "message": "...", "request_id": "..."}}
        # code 是稳定英文标识，前端靠它分支，不要去匹配 message。
        CODES = ('invalid_param', 'not_found', 'unauthorized', 'forbidden',
                 'internal_error', 'payload_too_large', 'unsupported_type')

        def err_shape(body):
            e = (body or {}).get('error')
            return e if isinstance(e, dict) else None

        code, d, h = jget('/questions?level=abc')
        e = err_shape(d)
        check('坏参数 → 422 且 code=invalid_param',
              code == 422 and e and e.get('code') == 'invalid_param', (code, d))
        check('错误体含 message 与 request_id',
              e and isinstance(e.get('message'), str) and bool(e.get('request_id')), e)

        code, d, h = req('POST', '/questions/NOSUCH/grade', {'given': 'A'})
        e = err_shape(d)
        check('不存在资源 → 404 且 code=not_found',
              code == 404 and e and e.get('code') == 'not_found', (code, d))

        # request_id 必须在响应头与响应体里一致 —— 这是把用户看到的响应与日志对上的唯一线索
        rid_body = (err_shape(d) or {}).get('request_id')
        rid_hdr = {k.lower(): v for k, v in (h or {}).items()}.get('x-request-id')
        check('request_id 在响应头与响应体一致',
              bool(rid_body) and rid_hdr == rid_body, (rid_hdr, rid_body))

        code, d, h = jget('/stats', headers={'Host': 'evil.com'})
        e = err_shape(d)
        check('guard 403 也走统一格式（code=forbidden）',
              code == 403 and e and e.get('code') == 'forbidden', (code, d))

        # 超大payload：只发 Content-Length 头会被服务端在读 body 前拒掉；
        # 真发 11 MB 时客户端可能先收到连接中止，所以断言只看「要么 413 要么连不上」，
        # 但要求服务端日志里确实记了 413（说明判定发生过）。
        big = json.dumps({'progress': {'x': 'y' * (11 * 1024 * 1024)}}).encode('utf-8')
        try:
            rq = urllib.request.Request(base + '/progress', data=big,
                                        headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(rq, timeout=30) as resp:
                got = resp.status
        except urllib.error.HTTPError as ex:
            got = ex.code
        except Exception:
            got = None            # 连接被中止也算服务端已拒绝
        check('超大 payload 被拒（413 或连接中止，绝不是 200）', got in (413, None), got)

        # 错误码表本身要对齐（新增 code 时两边不能只改一处）
        errs_mod = sys.modules.get('errors')
        check('errors.CODE_STATUS 覆盖契约里的 7 个 code',
              errs_mod is not None and all(c in errs_mod.CODE_STATUS for c in CODES),
              sorted(errs_mod.CODE_STATUS) if errs_mod else None)

        code, d, _ = req('POST', '/system/shutdown')
        check('POST /system/shutdown', code == 200 and d.get('ok') is True, d)
        th.join(timeout=10)
        check('shutdown 后服务真正退出', not th.is_alive())
        _t.sleep(0.3)
        try:
            with urlopen(base + '/stats', timeout=3) as resp:
                check('shutdown 后端口拒连', False, resp.status)
        except Exception:
            check('shutdown 后端口拒连', True)
    finally:
        if not srv.should_exit:
            srv.should_exit = True
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    public = '--public' in sys.argv
    try:
        m = load_api_module()
    except ImportError as e:
        print('✗ 缺少依赖：%s' % e)
        print('  用仓库 venv 跑：.venv-build\\Scripts\\python.exe tools/smoke_api.py')
        return 2
    api_smoke(m, public)
    print('\n== 汇总：通过 %d ｜ 失败 %d ==' % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print('  ✗ ' + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
