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
import sys
import tempfile
import threading
import time as _t
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

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


def fresh_store(m, tmp: Path, public: bool, tag: str = ''):
    """数据指向临时副本；DB/PROGJSON/CKJSON/NOTESJSON 在 core 模块上，两处都要改。"""
    if public:
        paths = {'DB': tmp / 'not-exist.sqlite'}
    else:
        paths = {'DB': tmp / ('qbank%s.sqlite' % tag)}
        shutil.copy2(ROOT / 'data' / 'qbank.sqlite', paths['DB'])
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
    static_only = {'/template', '/import_questions', '/custom'}   # 仅 APK 静态模式（前端按模式分流）
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
            with urllib.request.urlopen(r, timeout=10) as resp:
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

    try:
        code, d, _ = jget('/stats')
        check('GET /stats', code == 200 and d.get('total', 0) > 0 and 'by_type' in d, d)

        code, d, _ = jget('/questions?limit=3')
        check('GET /questions?limit=3', code == 200 and len(d.get('items', [])) <= 3)
        code, d, _ = jget('/questions?n=99999&limit=99999')
        check('limit 过大被内部夹到 ≤100', code == 200 and len(d.get('items', [])) <= 100)
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
        check('PUT /progress/{id} 记录结果', code == 200 and d.get('ok') and due_days(d) == 1, d)
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
        check('Host 白名单：evil.com → 403', code == 403 and d.get('detail') == 'forbidden', (code, d))
        code, d, h = jget('/stats', headers={'Origin': 'http://evil.com'})
        check('Origin 白名单：evil.com → 403', code == 403, code)
        # vite dev server（localhost:5173）跨域打本服务的放行规则
        r = urllib.request.Request(base + '/stats', method='OPTIONS',
                                   headers={'Origin': 'http://localhost:5173',
                                            'Access-Control-Request-Method': 'GET'})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                check('CORS 预检：localhost:5173 放行',
                      resp.status == 200 and resp.headers.get('access-control-allow-origin') == 'http://localhost:5173',
                      resp.status)
        except urllib.error.HTTPError as e:
            check('CORS 预检：localhost:5173 放行', False, e.code)

        # vite dist 静态挂载（api.py 底部 StaticFiles）
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=10) as resp:
                html = resp.read().decode('utf-8')
            check('GET / 返回 vite dist 页面', resp.status == 200 and '机械题库' in html)
        except Exception as e:
            check('GET / 返回 vite dist 页面', False, repr(e)[:80])
        dist_js = list((ROOT / 'web' / 'dist' / 'assets').glob('index-*.js'))
        if dist_js:
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/assets/%s' % (port, dist_js[0].name),
                                            timeout=10) as resp:
                    check('GET /assets/index-*.js → 200', resp.status == 200)
            except Exception as e:
                check('GET /assets/index-*.js → 200', False, repr(e)[:80])
        else:
            print('  - 跳过：web/dist 不存在（先 npm run build）')

        code, d, _ = req('POST', '/system/shutdown')
        check('POST /system/shutdown', code == 200 and d.get('ok') is True, d)
        th.join(timeout=10)
        check('shutdown 后服务真正退出', not th.is_alive())
        _t.sleep(0.3)
        try:
            with urllib.request.urlopen(base + '/stats', timeout=3) as resp:
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
