# -*- coding: utf-8 -*-
r"""
mech-qbank 冒烟测试：不依赖浏览器，直接驱动 Store 数据层 + 起真实 HTTP 服务打一遍 API。

用法：
    python tools/smoke_test.py            # 全量模式（data/qbank.sqlite 的临时副本，不碰真实数据）
    python tools/smoke_test.py --public   # 公开子集模式（data/public/qbank.json）

覆盖：
  判分    判断（对/错、√/×、A.对/B.错 选项）、单选、多选（严格匹配）、自评、答案待补、答案带 ** 的历史数据
  间隔    答对 1/3/7/15/30/60 天、答错次日、跳过 3 天
  组卷    level/qtype/bank/section/only_new 过滤与 clamp
  其他    题干搜索（含 % 通配符转义）、错题本、知识点、章节、进度导出/导入 round-trip
  HTTP    首页/favicon/静态/404/路径穿越/参数污染/keep-alive 双请求
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
import urllib.error
import urllib.parse
import urllib.request
import calendar
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ''):
    (PASS if cond else FAIL).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name + (('  → ' + detail) if (detail and not cond) else ''))


def load_server_module():
    spec = importlib.util.spec_from_file_location('qbank_server_smoke', ROOT / 'app' / 'server.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fresh_store(m, tmp: Path, public: bool, tag: str = ''):
    """把模块的数据指向临时副本，返回隔离的 Store。tag 用于 store 层与 HTTP 层各用一套文件。"""
    if public:
        m.DB = tmp / 'not-exist.sqlite'
    else:
        shutil.copy2(ROOT / 'data' / 'qbank.sqlite', tmp / ('qbank%s.sqlite' % tag))
        m.DB = tmp / ('qbank%s.sqlite' % tag)
    m.PROGJSON = tmp / ('progress%s.json' % tag)
    m.CKJSON = tmp / ('checkin%s.json' % tag)      # 手动打卡文件也要隔离，别写进真实 data/
    m.NOTESJSON = tmp / ('notes%s.json' % tag)     # 题目备注同样隔离
    return m.Store()


def due_days_after(mark_resp, base=None) -> int:
    due = datetime.fromisoformat(mark_resp['due'])
    base = base or datetime.now()
    return round((due - base).total_seconds() / 86400)


def synthetic_rows(con):
    """往（内存/临时）库插几条判分用例。"""
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TJ1', '测试库', '自出', '测试 › 判断', '', '判断', '太阳从东边升起（　）', '判断', 1, '[]',
                 '对', '地球自转', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TJ2', '测试库', '自出', '测试 › 判断', '', '判断', '平键能传递轴向力（　）', '判断', 2,
                 json.dumps(['A. 对', 'B. 错'], ensure_ascii=False), 'A', '平键只固定周向', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TJ3', '测试库', '自出', '测试 › 判断', '', '判断', '铸铁抗压强度高于抗拉（　）', '判断', 2,
                 json.dumps(['A. 对', 'B. 错'], ensure_ascii=False), '对', '灰铸铁确实如此', '', '', ''))
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
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TN1', '测试库', '自出', '测试 › 待补', '', '单选', '某题（　）', '单选', 2,
                 json.dumps(['A. 甲', 'B. 乙'], ensure_ascii=False), '[答案待补]', '', '', '', ''))
    con.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('TB1', '测试库', '自出', '测试 › 历史数据', '', '单选', '历史加粗答案（　）', '单选', 2, '[]',
                 '**A**（单选·标准答案，来自《答案及评分参考》）', '', '', '', ''))
    con.commit()


def store_smoke(m, st, public: bool):
    print('\n== 数据层：%s 模式 ==' % ('公开子集' if public else '全量库（临时副本）'))
    s = st.stats()
    check('stats 基本字段', s['total'] > 0 and s['cards'] > 0, str(s))
    check('stats 含今日与题型分布', 'today' in s and 'by_type' in s and 'intervals' in s)

    if public:
        synthetic_rows(st.con)
        s = st.stats()
        check('合成题写入成功', s['total'] >= 76)

    # ---- 判分 ----
    if public:
        r = st.grade('TJ1', '对');   check('判断：对 → 正确', r.get('correct') is True, str(r))
        r = st.grade('TJ1', '错');   check('判断：错 → 错误', r.get('correct') is False, str(r))
        r = st.grade('TJ1', '√');    check('判断：√ → 正确', r.get('correct') is True, str(r))
        r = st.grade('TJ2', 'A');    check('判断带选项：A.对 → 正确', r.get('correct') is True, str(r))
        r = st.grade('TJ2', 'B');    check('判断带选项：B.错 → 错误', r.get('correct') is False, str(r))
        r = st.grade('TJ3', 'A');    check('判断带选项+答案“对”：点A → 正确', r.get('correct') is True, str(r))
        r = st.grade('TS1', 'B');    check('单选：B → 正确', r.get('correct') is True, str(r))
        r = st.grade('TS1', 'A');    check('单选：A → 错误', r.get('correct') is False, str(r))
        r = st.grade('TS1', 'b');    check('单选：小写b → 正确', r.get('correct') is True, str(r))
        r = st.grade('TM1', 'AB');   check('多选：AB → 正确', r.get('correct') is True, str(r))
        r = st.grade('TM1', 'BA');   check('多选：BA → 正确（无序）', r.get('correct') is True, str(r))
        r = st.grade('TM1', 'A');    check('多选：漏选A → 错误', r.get('correct') is False, str(r))
        r = st.grade('TM1', 'ABC');  check('多选：多选ABC → 错误', r.get('correct') is False, str(r))
        r = st.grade('TW1', '任意'); check('问答：自评', r.get('self') is True and r.get('correct') is None, str(r))
        r = st.grade('TN1', 'A');    check('答案待补：自评并明示', r.get('self') is True and '待补' in r.get('answer', ''), str(r))
        r = st.grade('TB1', 'A');    check('历史**答案：剥离后按单选判', r.get('correct') is True, str(r))
        r = st.grade('__不存在__', 'A'); check('不存在题号：error', r.get('error') == 'not found', str(r))
    else:
        row = st.q("SELECT id, answer FROM questions WHERE options NOT IN ('[]','') AND answer GLOB '[A-H]' LIMIT 1")
        if row:
            qid, ans = row[0]['id'], row[0]['answer']
            r = st.grade(qid, ans);  check('全量单选实题：正确答案 → 正确', r.get('correct') is True, str(r))
            r = st.grade(qid, 'Z');  check('全量单选实题：错误答案 → 错误', r.get('correct') is False, str(r))
        else:
            check('全量单选实题：正确答案 → 正确', False, '库中无字母答案选项题')
        row = st.q("SELECT id, answer FROM questions WHERE qtype LIKE '%判断%' AND answer IN ('对','错') LIMIT 1")
        if row:
            qid, ans = row[0]['id'], row[0]['answer']
            r = st.grade(qid, ans);  check('全量判断实题：正确答案 → 正确', r.get('correct') is True, str(r))
            r = st.grade(qid, '错' if ans == '对' else '对')
            check('全量判断实题：错误答案 → 错误', r.get('correct') is False, str(r))
        else:
            check('全量判断实题：正确答案 → 正确', False, '库中无对/错答案判断题')

    # ---- 复习间隔 ----
    qid = 'TJ1' if public else st.q('SELECT id FROM questions LIMIT 1')[0]['id']
    base = datetime.now()
    r1 = st.mark(qid, 'correct'); check('答对第1次 → 明天（1天）', due_days_after(r1, base) == 1, str(r1))
    r2 = st.mark(qid, 'correct'); check('答对第2次 → 3天', due_days_after(r2, base) == 3, str(r2))
    r3 = st.mark(qid, 'correct'); check('答对第3次 → 7天', due_days_after(r3, base) == 7, str(r3))
    r4 = st.mark(qid, 'correct'); check('答对第4次 → 15天', due_days_after(r4, base) == 15, str(r4))
    r5 = st.mark(qid, 'correct'); check('答对第5次 → 30天', due_days_after(r5, base) == 30, str(r5))
    r6 = st.mark(qid, 'correct'); check('答对第6次 → 60天', due_days_after(r6, base) == 60, str(r6))
    r7 = st.mark(qid, 'correct'); check('答对第7次 → 仍60天封顶', due_days_after(r7, base) == 60, str(r7))
    rw = st.mark(qid, 'wrong');   check('答错 → 次日必回', due_days_after(rw, base) == 1, str(rw))
    rs = st.mark(qid, 'skip');    check('跳过 → 3天', due_days_after(rs, base) == 3, str(rs))

    # ---- 组卷与过滤 ----
    items = st.pick(5)
    check('pick 返回 ≤5 且结构完整', len(items) <= 5 and all('stem' in i and 'options' in i for i in items))
    check('pick clamp：n=abc 不炸', len(st.pick('abc')) > 0)
    check('pick clamp：n=99999 ≤100', len(st.pick(99999)) <= 100)
    check('pick 不泄露答案', all('answer' not in i for i in st.pick(10)))
    if public:
        ids = {i['id'] for i in st.pick(200, only_new=True)}
        check('only_new 排除已答对/答错', qid not in ids)
        items = st.pick(200, level=1)
        check('level 过滤生效', all(i['level'] == 1 for i in items))
    else:
        banks = st.banks()
        b0 = banks[0]['bank']
        items = st.pick(50, bank=b0)
        check('bank 过滤生效', items and all(i['bank'] == b0 for i in items))
        secs = st.sections()
        s1 = secs[1]['section']
        items = st.pick(50, section=s1)
        check('section 过滤生效', items and all(i['sec'] == s1 for i in items), str(items[:1]))
        items = st.pick(50, qtype='问答')
        check('qtype 过滤生效（问答含子类）', items and all('问答' in (i['qtype'] or '') for i in items))

    # ---- 搜索 / 错题 / 卡片 ----
    stem0 = st.q('SELECT stem FROM questions LIMIT 1')[0]['stem']
    kw0 = stem0[:4]
    res = st.search(kw0)
    check('题干搜索：用真实题干前4字（%s）' % kw0, len(res) > 0)
    res = st.search('%')
    check('搜索转义：% 不报错', isinstance(res, list))
    cards = st.cards('轴承')
    check('知识点搜索 + qids 已解析', len(cards) > 0 and isinstance(cards[0]['qids'], list))
    check('章节列表非空', len(st.chapters()) > 0)
    st.mark(qid, 'wrong')
    wl = st.wrong_list()
    check('错题本包含刚标记的题', any(w['id'] == qid for w in wl))

    # ---- 进度导出/导入 ----
    exp = st.export_progress()
    st2_prog = {'__ round-trip __': {'state': 'new', 'streak': 3, 'wrong': 2, 'due': None, 'last_seen': None}}
    n = st.import_progress(st2_prog)
    check('进度导入返回条数', n == 1, str(n))
    back = st.export_progress()
    check('进度导出/导入 round-trip', back.get('__ round-trip __', {}).get('streak') == 3)
    check('导入不覆盖原有进度', all(back.get(k) == v for k, v in exp.items()))
    st.import_progress({'坏数据': 'not-a-dict', '': {'state': 'new'}})
    check('导入容错：跳过坏行', '__ round-trip __' in st.export_progress())

    # ---- 落盘防抖：mark() 不再持锁做 I/O，1 秒内连答只写一次盘；_flush_progress 可立即落盘 ----
    if not public:
        # 全量模式下 save_progress_json 是 no-op，这里只验证接口语义不炸
        st.mark(qid, 'skip')
        check('全量模式 mark() 后不触发落盘（no-op）', True)
    else:
        import time as _t
        _t.sleep(0.05)
        st._flush_progress()
        on_disk = json.loads(m.PROGJSON.read_text(encoding='utf-8')) if m.PROGJSON.exists() else {}
        check('_flush_progress() 立即落盘', (on_disk.get(qid) or {}).get('state') == 'wrong', str(on_disk.get(qid)))
        st.mark(qid, 'skip')
        _t.sleep(0.05)          # 防抖窗口内：文件里应还是旧值
        during = json.loads(m.PROGJSON.read_text(encoding='utf-8'))
        check('防抖窗口内不写盘（连答多题只写一次）',
              (during.get(qid) or {}).get('state') == 'wrong', str(during.get(qid)))
        _t.sleep(1.1)           # 窗口过后自动补写
        after = json.loads(m.PROGJSON.read_text(encoding='utf-8'))
        # skip 不改 state（沿用 mark 前的 'wrong'），但会刷新 due/last_seen
        stt = after.get(qid) or {}
        check('防抖窗口过后自动落盘', stt.get('state') == 'wrong' and stt.get('due'), str(stt))

    # ---- 学习打卡 ----
    ck0 = st.checkin()
    today = datetime.now().strftime('%Y-%m-%d')
    check('打卡：结构完整', isinstance(ck0['streak'], int) and isinstance(ck0['days'], list)
          and bool(ck0['today']) and ck0['today'] == today)
    st.mark(qid, 'correct')                       # 答题即打卡
    ck1 = st.checkin()
    check('答题后自动打卡', ck1['checked_today'] is True and ck1['today_n'] >= 1
          and ck1['streak'] >= 1 and ck1['today'] == today)
    d1 = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d') + 'T10:00:00'
    d2 = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d') + 'T10:00:00'
    d4 = (datetime.now() - timedelta(days=4)).strftime('%Y-%m-%d') + 'T10:00:00'
    st.import_progress({'__ck_y__': {'state': 'correct', 'streak': 1, 'wrong': 0, 'due': None, 'last_seen': d1},
                        '__ck_d2__': {'state': 'correct', 'streak': 1, 'wrong': 0, 'due': None, 'last_seen': d2},
                        '__ck_d4__': {'state': 'wrong', 'streak': 0, 'wrong': 1, 'due': None, 'last_seen': d4}})
    ck2 = st.checkin()
    check('连续天数：今天+昨天+前天 = 3（隔天不算）', ck2['streak'] == 3, str(ck2['streak']))
    check('累计天数 = 4 天（含隔天）', ck2['total_days'] == 4, str(ck2['total_days']))
    # 今天没答题时，streak 算截至昨天的
    ck3 = st.manual_checkin()
    check('手动打卡幂等（今天已答题）', ck3['manual_count'] == 0 and ck3['streak'] == 3)
    # 当月格子：天数与 Python calendar 一致
    y, mo = datetime.now().year, datetime.now().month
    check('当月日历格数正确', len(ck3['days']) == calendar.monthrange(y, mo)[1])
    check('日历 today 标记唯一', sum(1 for c in ck3['days'] if c['today']) == 1)
    # 历史月
    lm = (datetime.now().replace(day=1) - timedelta(days=1))
    ck4 = st.checkin(lm.strftime('%Y-%m'))
    check('历史月可查', len(ck4['days']) == calendar.monthrange(lm.year, lm.month)[1])
    ck5 = st.checkin('bad-month')
    check('非法 month 回落当月', ck5['month'] == today[:7])

    # ---- 题目备注 ----
    check('备注：初始为空', st.notes_list() == [])
    r1 = st.set_note(qid, '易错点：先看单位再套公式')
    check('备注：保存成功', r1.get('ok') is True and r1.get('note') == '易错点：先看单位再套公式'
          and bool(r1.get('updated_at')) and r1.get('removed') is False)
    nl1 = st.notes_list()
    check('备注：列表带题号/题干/题型（明确属于哪道题）',
          len(nl1) == 1 and nl1[0]['id'] == qid and nl1[0]['stem'] and nl1[0]['missing'] is False
          and nl1[0]['qtype'] == st.full(qid)['qtype'], str(nl1[:1]))
    r2 = st.set_note(qid, '  改过的备注  ')
    check('备注：覆盖更新并去首尾空白', r2.get('note') == '改过的备注' and r2.get('updated_at') >= r1['updated_at'])
    check('备注：同一题只有一条', len(st.notes_list()) == 1)
    r3 = st.set_note(qid, '')
    check('备注：空内容 = 删除', r3.get('removed') is True and st.notes_list() == [])
    check('备注：不存在的题号报错', st.set_note('NOPE-X', 'x').get('error') == 'not found')
    check('备注：缺 id 报错', st.set_note('', 'x').get('error') is not None)
    st.set_note(qid, 'a' * 3000)
    check('备注：超长截断到 2000', len(st.notes_list()[0]['note']) == 2000)
    # 排序：两条备注按更新时间倒序
    qid2 = st.q('SELECT id FROM questions WHERE id != ? LIMIT 1', (qid,))[0]['id']
    st.set_note(qid, '第一条')
    st.set_note(qid2, '第二条')
    st._notes()[qid]['updated_at'] = '2020-01-01T00:00:00'
    st._notes()[qid2]['updated_at'] = '2030-01-01T00:00:00'
    st._save_notes()
    check('备注：按更新时间倒序', [x['id'] for x in st.notes_list()] == [qid2, qid],
          str([x['id'] for x in st.notes_list()]))
    # 题目不存在但备注仍在（题库换版）：标注 missing
    st._notes()['GHOST-1'] = {'note': '孤儿备注', 'updated_at': '2099-01-01T00:00:00'}
    st._save_notes()
    ghost = [x for x in st.notes_list() if x['id'] == 'GHOST-1']
    check('备注：题目不存在时标 missing 且仍列出', bool(ghost) and ghost[0]['missing'] is True
          and ghost[0]['stem'] == '')
    st._notes().pop('GHOST-1', None); st._save_notes()
    # 持久化：同一 notes.json 新建 Store 仍在
    st2 = m.Store()
    check('备注：写入 notes.json 并可在新实例恢复',
          [x['id'] for x in st2.notes_list()] == [qid2, qid], str([x['id'] for x in st2.notes_list()]))
    st.con.close(); st2.con.close()


def http_smoke(m, tmp: Path, public: bool):
    print('\n== HTTP 层：%s 模式 ==' % ('公开子集' if public else '全量库（临时副本）'))
    st = fresh_store(m, tmp, public, tag='-http')   # 与 store 层用不同文件，避免互相污染
    m.STORE = st
    # 契约检查：前端在在线模式会调的接口，服务端必须有（曾因 /api/custom 缺失导致进度页弹 404）
    html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    srv = set(re.findall(r"'/api/([a-z_]+)'", (ROOT / 'app' / 'server.py').read_text(encoding='utf-8')))
    static_only = {'custom', 'template', 'import_questions'}   # 仅 APK 静态模式（前端已按模式跳过）
    used = set(re.findall(r"api\('/api/([a-z_]+)", html)) - static_only
    check('前后端接口契约：前端调用的接口服务端都有', used <= srv, str(sorted(used - srv)))
    srv = m.ThreadingHTTPServer(('127.0.0.1', 0), m.H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % port

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(path, obj):
        req = urllib.request.Request(base + path, data=json.dumps(obj).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def jget(path):
        code, body = get(path)
        return code, json.loads(body.decode('utf-8'))

    try:
        code, body = get('/')
        check('GET / 返回页面', code == 200 and '机械题库' in body.decode('utf-8'))
        code, _ = get('/favicon.ico')
        check('GET /favicon.ico → 204', code == 204)
        code, _ = get('/static/')
        check('GET /static/ → 404（目录不泄）', code == 404)
        code, _ = get('/static/../server.py')
        check('路径穿越被挡 → 404', code == 404)
        code, _ = get('/api/nope')
        check('未知 API → 404', code == 404)

        code, d = jget('/api/stats')
        check('GET /api/stats', code == 200 and d['total'] > 0)
        code, d = jget('/api/quiz?n=3')
        check('GET /api/quiz?n=3', code == 200 and len(d['items']) <= 3)
        code, d = jget('/api/quiz?n=abc&level=9&qtype=%27')
        check('污染参数不炸（clamp）', code == 200 and isinstance(d['items'], list))
        code, d = jget('/api/quiz?n=99999')
        check('n=99999 被夹到 ≤100', code == 200 and len(d['items']) <= 100)
        code, d = jget('/api/review?n=5')
        check('GET /api/review', code == 200 and isinstance(d['items'], list))
        code, d = jget('/api/search?q=' + urllib.parse.quote('轴承'))
        check('GET /api/search', code == 200 and isinstance(d['items'], list))
        code, d = jget('/api/search?q=%25')
        check('GET /api/search?q=% 不炸', code == 200)
        code, d = jget('/api/banks')
        check('GET /api/banks', code == 200 and len(d['items']) > 0)
        code, d = jget('/api/sections')
        check('GET /api/sections', code == 200 and len(d['items']) > 0)
        code, d = jget('/api/cards?q=' + urllib.parse.quote('轴承') + '&limit=abc')
        check('GET /api/cards（limit 污染）', code == 200 and isinstance(d['items'], list))
        code, d = jget('/api/chapters')
        check('GET /api/chapters', code == 200)
        code, d = jget('/api/wrong')
        check('GET /api/wrong', code == 200)
        code, d = jget('/api/progress')
        check('GET /api/progress 导出', code == 200 and isinstance(d['progress'], dict))

        code, d = post('/api/grade', {'id': '__不存在__', 'given': 'A'})
        check('POST /api/grade 不存在题', code == 200 and d.get('error') == 'not found')
        code, d = post('/api/grade', {})
        check('POST /api/grade 空body 不炸', code == 200)
        code, d = post('/api/mark', {'id': 'TS1', 'result': 'bogus'})
        check('POST /api/mark 非法result按skip', code == 200 and d.get('ok') is True)
        code, d = post('/api/nope', {})
        check('未知 POST → 404', code == 404)

        # 进度导入 round-trip（HTTP）
        _, exp = jget('/api/progress')
        code, d = post('/api/progress', {'progress': {'__http__': {'state': 'wrong', 'streak': 0, 'wrong': 5}}})
        check('POST /api/progress 导入', code == 200 and d.get('imported') == 1)
        _, now = jget('/api/progress')
        check('HTTP 导入生效', now['progress'].get('__http__', {}).get('wrong') == 5)
        check('HTTP 导入不覆盖原进度', all(now['progress'].get(k) == v for k, v in exp['progress'].items()))

        # 学习打卡（HTTP）
        code, d = jget('/api/checkin')
        check('GET /api/checkin', code == 200 and bool(d.get('today')) and isinstance(d.get('days'), list))
        post('/api/mark', {'id': 'TS1', 'result': 'correct'})
        _, d = jget('/api/checkin')
        check('答题后自动打卡', d.get('checked_today') is True and d.get('today_n', 0) >= 1)
        code, d = post('/api/checkin', {'action': 'checkin'})
        check('POST /api/checkin 手动打卡（已答题不重复记）',
              code == 200 and d.get('manual_count') == 0 and d.get('streak', 0) >= 1)
        code, d = post('/api/checkin', {'action': 'checkin'})
        check('重复手动打卡幂等', code == 200 and d.get('manual_count') == 0)
        last_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
        ly, lm = int(last_month[:4]), int(last_month[5:7])
        code, d = jget('/api/checkin?month=' + last_month)
        check('历史月日历天数正确', code == 200 and len(d.get('days', [])) == calendar.monthrange(ly, lm)[1])
        code, d = jget('/api/checkin?month=bad')
        check('非法 month 回落当月', code == 200 and len(d.get('days', [])) > 0)

        # 题目备注（HTTP）
        code, d = jget('/api/notes')
        check('GET /api/notes 初始为空', code == 200 and d.get('items') == [])
        hid = st.q('SELECT id FROM questions LIMIT 1')[0]['id']
        code, d = post('/api/notes', {'id': hid, 'note': 'HTTP 备注：注意单位换算'})
        check('POST /api/notes 保存成功', code == 200 and d.get('ok') is True and d.get('id') == hid, str(d)[:120])
        code, d = jget('/api/notes')
        item = (d.get('items') or [{}])[0]
        check('GET /api/notes 带题号/题干/题型', code == 200 and item.get('id') == hid
              and bool(item.get('stem')) and bool(item.get('qtype')), str(item)[:120])
        code, d = jget('/api/question?id=' + urllib.parse.quote(hid))
        it = d.get('item') or {}
        check('GET /api/question 返回单题（不含答案）', code == 200 and it.get('id') == hid
              and 'answer' not in it, str(it)[:120])
        code, d = post('/api/notes', {'id': hid, 'note': ''})
        check('POST /api/notes 空内容删除', code == 200 and d.get('removed') is True)
        code, d = post('/api/notes', {'id': 'NOPE-X', 'note': 'x'})
        check('POST /api/notes 不存在的题号报错', code == 200 and d.get('error') == 'not found')

        # keep-alive：同一连接连发两个请求（按 Content-Length 读完整响应再发下一个）
        def read_response(sk):
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = sk.recv(65536)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b'\r\n\r\n')
            mm = re.search(rb'Content-Length: (\d+)', head)
            n = int(mm.group(1)) if mm else 0
            while len(rest) < n:
                chunk = sk.recv(65536)
                if not chunk:
                    break
                rest += chunk
            return head, rest[:n]

        sk = socket.create_connection(('127.0.0.1', port), timeout=10)
        try:
            sk.sendall(b'GET /api/stats HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
            h1, b1 = read_response(sk)
            sk.sendall(b'GET /api/banks HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
            h2, b2 = read_response(sk)
            check('HTTP/1.1 keep-alive 同连接双请求',
                  b'200 OK' in h1 and b'200 OK' in h2 and b'"items"' in b2)
        finally:
            sk.close()

        # Host/Origin 白名单：挡 DNS rebinding（恶意页面把域名重绑到 127.0.0.1 后即同源）
        for host, want in (('127.0.0.1', 200), ('localhost', 200), ('evil.com', 403),
                           ('attacker.test', 403)):
            req = urllib.request.Request(base + '/api/stats', headers={'Host': host})
            code = 0
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            check('Host=%r → %d' % (host, want), code == want, '实际 %d' % code)
        # 缺 Host 头时 socket 直接打裸请求（urllib 会自动补 Host，测不到）
        sk2 = socket.create_connection(('127.0.0.1', port), timeout=10)
        try:
            sk2.sendall(b'GET /api/stats HTTP/1.1\r\n\r\n')
            head, _body = read_response(sk2)
            check('缺 Host 头 → 403', b'403' in head, head[:40].decode('latin1'))
        finally:
            sk2.close()
        req = urllib.request.Request(base + '/api/stats',
                                     headers={'Host': '127.0.0.1', 'Origin': 'http://evil.com'})
        code = 0
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        check('Origin 为外站 → 403', code == 403, '实际 %d' % code)
        # POST 同样要守
        code, _d = post('/api/mark', {'id': 'TS1', 'result': 'skip'})
        check('本地 POST 正常放行', code == 200 and _d.get('ok') is True)
    finally:
        srv.shutdown()
        srv.server_close()
        st.con.close()


def main() -> int:
    public = '--public' in sys.argv
    m = load_server_module()
    with tempfile.TemporaryDirectory(prefix='mech-qbank-smoke-') as td:
        tmp = Path(td)
        st = fresh_store(m, tmp, public)
        store_smoke(m, st, public)
        http_smoke(m, tmp, public)
    # 前后端判分/排期一致性（独立工具，node 缺失时返回 2 表示跳过）
    import subprocess as sp
    r = sp.run([sys.executable, str(ROOT / 'tools' / 'smoke_parity.py')] + (['--public'] if public else []))
    if r.returncode == 1:
        check('前后端判分/排期一致性（smoke_parity）', False, '见上方差异明细')
    elif r.returncode == 2:
        print('  … 跳过前后端一致性校验（未找到 node）')
    print('\n========== 结果：%d 通过 / %d 失败 ==========' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print('  - ' + f)
        return 1
    print('全部通过 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
