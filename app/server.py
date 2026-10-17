# -*- coding: utf-8 -*-
r"""
mech-qbank · 机械题库 App 服务端
纯标准库（http.server + sqlite3），零第三方依赖 —— 双击 start.bat 就能跑，也便于打包成 exe。

数据优先级
  1. data/qbank.sqlite       本地全量库（含公司真题，仅本地用）
  2. data/public/qbank.json  公开子集（开源仓/APK 内置，无 sqlite 时自动转成内存库）

设计约定
  · 不做 LLM 批改：选择题/判断题本地自动判分；问答/计算类返回 correct=null，由用户自评
  · 复习间隔：答对按 1/3/7/15/30/60 天（QBANK_INTERVALS 可覆盖）；答错次日必回；跳过 3 天后回
  · 只读源库，进度写进 qbank.sqlite 的 progress 表（公开模式下写 progress.json）
  · 进度可导出/导入（GET/POST /api/progress），便于备份与换机

API 一览
  GET  /api/stats      总览（总量/已答/掌握/错题/待复习/今日/难度/题型/来源分布）
  GET  /api/quiz       组卷（n/level/qtype/class/bank/section/only_new）
  GET  /api/review     今日到期复习（n/level/qtype）
  GET  /api/search     题干关键词搜索（q/limit）
  GET  /api/banks      题库（bank）分布
  GET  /api/sections   章节来源（section_path 第一段）分布
  GET  /api/cards      知识点卡搜索（q/chapter/limit）
  GET  /api/chapters   知识点章节列表
  GET  /api/wrong      错题本（按累计错次排序）
  GET  /api/progress   导出进度 JSON
  GET  /api/checkin    学习打卡（连续天数/今日状态/当月日历；答题自动打卡，?month=YYYY-MM 看历史月）
  POST /api/grade      判分 {id, given}
  POST /api/mark       记录结果 {id, result: correct|wrong|skip}
  POST /api/progress   导入进度 {progress: {...}}（合并，不覆盖未出现的题）
  POST /api/checkin    手动打卡 {action: "checkin"}（答过题的日子自动算，这是补签）
"""
from __future__ import annotations
import calendar, json, os, re, sqlite3, sys, mimetypes, threading, webbrowser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 路径：源码运行用仓库目录；PyInstaller 打包后资源在 _MEIPASS（只读、临时解包），
# 可写数据（进度/自建库）放 exe 同级目录 —— 2026-10-10 打包时补
FROZEN = bool(getattr(sys, 'frozen', False))
ASSETS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
DATA.mkdir(parents=True, exist_ok=True)
WEB = ASSETS / 'web'
DB = DATA / 'qbank.sqlite'
PUBJSON = ASSETS / 'data' / 'public' / 'qbank.json'
PROGJSON = DATA / 'progress.json'
CKJSON = DATA / 'checkin.json'          # 手动打卡日期（答题打卡从 progress.last_seen 聚合）
PORT = int(os.environ.get('QBANK_PORT', '8640'))
PORT_SCAN = max(1, int(os.environ.get('QBANK_PORT_SCAN', '10')))  # 端口被占用时向后尝试的个数
INTERVALS = [int(x) for x in os.environ.get('QBANK_INTERVALS', '1,3,7,15,30,60').replace('，', ',').split(',')
             if x.strip().isdigit()] or [1, 3, 7, 15, 30, 60]
WRONG_BACK = 1   # 答错次日必回
SKIP_BACK = 3    # 跳过 3 天后回
TRUE_SET = {'对', '正确', '是', '√', '✓', 'true', 't', 'yes', 'y', '1'}
FALSE_SET = {'错', '错误', '否', '×', '✗', 'x', 'false', 'f', 'no', '0', '2'}


def now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def clamp_int(v, default: int, lo: int, hi: int) -> int:
    """查询参数取整并夹紧，防 'abc'/负数/超大值把接口打挂。"""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def like_escape(s: str) -> str:
    return (s or '').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def norm_judge(s):
    """把 对/正确/√/T/1 之类归一成 '对'，错/×/F/0 归一成 '错'，其余返回 None。"""
    s = (s or '').strip().lower().rstrip('。.')
    if s in TRUE_SET: return '对'
    if s in FALSE_SET: return '错'
    return None


# ---------------------------------------------------------------- 数据层
class Store:
    def __init__(self) -> None:
        self.mode = 'sqlite' if DB.exists() else 'public'
        self.lock = threading.RLock()          # 可重入：mark() 里包着 q()/x()
        self.prog_writable = True
        self._banks = None
        self._sections = None
        if self.mode == 'sqlite':
            self.con = sqlite3.connect(DB, check_same_thread=False)
            self.con.row_factory = sqlite3.Row
        else:
            if not PUBJSON.exists():
                raise SystemExit('找不到数据：先跑 tools/qbank_build.py 生成 data/qbank.sqlite，'
                                 '或放入 data/public/qbank.json')
            try:
                pub = json.loads(PUBJSON.read_text(encoding='utf-8'))
            except Exception as e:
                raise SystemExit('data/public/qbank.json 解析失败：%s' % e)
            self.con = sqlite3.connect(':memory:', check_same_thread=False)
            self.con.row_factory = sqlite3.Row
            self.con.executescript('''
              CREATE TABLE questions(id TEXT PRIMARY KEY, bank TEXT, source_class TEXT, section_path TEXT,
                section TEXT, cat_name TEXT, stem TEXT, qtype TEXT, level INTEGER, options TEXT,
                answer TEXT, explanation TEXT, source TEXT, note TEXT, meta TEXT);
              CREATE TABLE cards(cid INTEGER PRIMARY KEY AUTOINCREMENT, chapter TEXT, topic TEXT,
                name TEXT, body TEXT, qids TEXT, source_class TEXT);
              CREATE TABLE progress(id TEXT PRIMARY KEY, state TEXT DEFAULT 'new', streak INTEGER DEFAULT 0,
                wrong INTEGER DEFAULT 0, due TEXT, last_seen TEXT);
            ''')
            self.con.executemany('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [(q['id'], q.get('bank', ''), q.get('source_class', ''), q.get('section_path', ''), '',
                  q.get('cat_name', ''), q['stem'], q.get('qtype', ''), q.get('level'),
                  json.dumps(q.get('options', []), ensure_ascii=False),
                  q.get('answer', ''), q.get('explanation', ''), q.get('source', ''), '', '')
                 for q in pub.get('questions', [])])
            self.con.executemany('INSERT INTO cards(chapter,topic,name,body,qids,source_class) VALUES(?,?,?,?,?,?)',
                [(c.get('chapter', ''), c.get('topic', ''), c.get('name', ''), c.get('body', ''),
                  json.dumps(c.get('qids', []), ensure_ascii=False), c.get('source_class', '通用知识点'))
                 for c in pub.get('cards', [])])
            self.con.commit()
            self._load_progress_json()

    # ---- 进度文件（仅公开模式）----
    def _load_progress_json(self) -> None:
        if PROGJSON.exists():
            try:
                for pid, v in json.loads(PROGJSON.read_text(encoding='utf-8')).items():
                    self.con.execute('INSERT OR REPLACE INTO progress(id,state,streak,wrong,due,last_seen)'
                                     ' VALUES(?,?,?,?,?,?)',
                                     (pid, v.get('state', 'new'), v.get('streak', 0), v.get('wrong', 0),
                                      v.get('due'), v.get('last_seen')))
                self.con.commit()
            except Exception:
                pass  # 进度文件损坏不影响启动

    def save_progress_json(self) -> None:
        if self.mode != 'public':
            return
        try:
            rows = self.con.execute('SELECT * FROM progress').fetchall()
            PROGJSON.write_text(json.dumps(
                {r['id']: {k: r[k] for k in ('state', 'streak', 'wrong', 'due', 'last_seen')} for r in rows},
                ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError:
            if self.prog_writable:  # 只提醒一次，不打断刷题
                self.prog_writable = False
                print('⚠ 进度文件不可写（%s），本次运行进度仅存内存' % PROGJSON, file=sys.stderr)

    # ---- 基础查询（RLock 保证 mark 的读-改-写原子）----
    def q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            self.con.execute(sql, args)
            self.con.commit()

    # ---- 查询 ----
    def stats(self) -> dict:
        today = datetime.now().strftime('%Y-%m-%d')
        tot = self.q('SELECT COUNT(*) c FROM questions')[0]['c']
        cards = self.q('SELECT COUNT(*) c FROM cards')[0]['c']
        by_level = {str(r['level'] if r['level'] is not None else '?'): r['c']
                    for r in self.q('SELECT level, COUNT(*) c FROM questions GROUP BY level')}
        by_class = {r['source_class']: r['c']
                    for r in self.q('SELECT source_class, COUNT(*) c FROM questions GROUP BY source_class')}
        # 题型按主类型聚合（'问答/选型' → '问答'），避免分布图几十根柱子
        by_type: dict = {}
        for r in self.q('SELECT qtype, COUNT(*) c FROM questions GROUP BY qtype'):
            k = (r['qtype'] or '未标注').split('/')[0].split('·')[0]
            by_type[k] = by_type.get(k, 0) + r['c']
        p = self.q("SELECT COUNT(*) c FROM progress WHERE state IN ('correct','wrong')")[0]['c']
        ok = self.q("SELECT COUNT(*) c FROM progress WHERE state='correct'")[0]['c']
        wrong = self.q("SELECT COUNT(*) c FROM progress WHERE wrong>0")[0]['c']
        due = self.q("SELECT COUNT(*) c FROM progress WHERE due IS NOT NULL AND due<=?", (now_iso(),))[0]['c']
        today_n = self.q("SELECT COUNT(*) c FROM progress WHERE last_seen>=?", (today,))[0]['c']
        today_ok = self.q("SELECT COUNT(*) c FROM progress WHERE state='correct' AND last_seen>=?",
                          (today,))[0]['c']
        return {'mode': self.mode, 'total': tot, 'cards': cards, 'answered': p, 'correct': ok,
                'wrong': wrong, 'due': due, 'today': today_n, 'today_ok': today_ok,
                'by_level': by_level, 'by_class': by_class, 'by_type': by_type,
                'intervals': INTERVALS}

    def pick(self, n=10, level=None, qtype=None, sclass=None, bank=None, section=None,
             only_new=False, review=False) -> list:
        n = clamp_int(n, 10, 1, 100)
        where, args = [], []
        if level not in (None, ''):
            lv = clamp_int(level, 0, 1, 5)
            if lv:
                where.append('level=?'); args.append(lv)
        if qtype:
            where.append('qtype LIKE ?'); args.append(f'%{qtype}%')
        if sclass:
            where.append('source_class=?'); args.append(sclass)
        if bank:
            where.append('bank=?'); args.append(bank)
        if section:
            seg = str(section).strip()
            if seg:
                # section_path 形如 'A. 赢合科技… › A1 …'，按第一段（章节来源）过滤
                where.append("(section_path = ? OR section_path LIKE ? ESCAPE '\\')")
                args += [seg, like_escape(seg) + ' › %']
        w = (' WHERE ' + ' AND '.join(where)) if where else ''
        if review:
            rows = self.q('SELECT q.* FROM questions q JOIN progress p ON p.id=q.id'
                          ' WHERE p.due IS NOT NULL AND p.due<=?'
                          + (' AND ' + ' AND '.join(where) if where else '')
                          + ' ORDER BY p.due, q.id LIMIT ?', (now_iso(), *args, n))
        else:
            if only_new:
                where.append("id NOT IN (SELECT id FROM progress WHERE state IN ('correct','wrong'))")
                w = ' WHERE ' + ' AND '.join(where)
            rows = self.q('SELECT * FROM questions' + w + ' ORDER BY RANDOM() LIMIT ?', (*args, n))
        return [self._pub_row(r) for r in rows]

    def _pub_row(self, r) -> dict:
        sp = (r['section_path'] or '') if r.keys() and 'section_path' in r.keys() else ''
        return {'id': r['id'], 'stem': r['stem'], 'qtype': r['qtype'], 'level': r['level'],
                'options': json.loads(r['options'] or '[]'), 'cat': r['cat_name'], 'bank': r['bank'],
                'source_class': r['source_class'], 'sec': sp.split(' › ')[0] if sp else ''}

    def full(self, qid: str):
        rows = self.q('SELECT * FROM questions WHERE id=?', (qid,))
        return dict(rows[0]) if rows else None

    def search(self, kw: str, limit=50) -> list:
        limit = clamp_int(limit, 50, 1, 100)
        kw = (kw or '').strip()
        if not kw:
            return []
        k = f'%{like_escape(kw)}%'
        rows = self.q("SELECT * FROM questions WHERE stem LIKE ? ESCAPE '\\' ORDER BY id LIMIT ?", (k, limit))
        return [self._pub_row(r) for r in rows]

    def banks(self) -> list:
        if self._banks is None:
            self._banks = [{'bank': r['bank'], 'n': r['c']} for r in
                           self.q('SELECT bank, COUNT(*) c FROM questions GROUP BY bank ORDER BY c DESC, bank')]
        return self._banks

    def sections(self) -> list:
        if self._sections is None:
            agg: dict = {}
            for r in self.q('SELECT section_path FROM questions'):
                seg = (r['section_path'] or '').split(' › ')[0].strip()
                if seg:
                    agg[seg] = agg.get(seg, 0) + 1
            self._sections = [{'section': k, 'n': v} for k, v in sorted(agg.items(), key=lambda kv: -kv[1])]
        return self._sections

    def cards(self, kw: str = '', chapter: str = '', limit: int = 500) -> list:
        limit = clamp_int(limit, 500, 1, 1000)
        where, args = [], []
        if kw:
            k = f'%{like_escape(kw)}%'
            where.append("(name LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR topic LIKE ? ESCAPE '\\')")
            args += [k, k, k]
        if chapter:
            where.append('chapter=?'); args.append(chapter)
        sql = 'SELECT * FROM cards' + (' WHERE ' + ' AND '.join(where) if where else '') + ' LIMIT ?'
        out = []
        for r in self.q(sql, (*args, limit)):
            d = dict(r)
            try:
                d['qids'] = json.loads(r['qids'] or '[]')   # 服务端解析，前端免判空
            except Exception:
                d['qids'] = []
            out.append(d)
        return out

    def chapters(self) -> list:
        return [{'chapter': r['chapter'], 'n': r['c']} for r in
                self.q('SELECT chapter, COUNT(*) c FROM cards GROUP BY chapter ORDER BY chapter')]

    def wrong_list(self, limit=200) -> list:
        limit = clamp_int(limit, 200, 1, 500)
        rows = self.q('SELECT q.*, p.wrong AS pwrong, p.streak AS pstreak, p.state AS pstate'
                      ' FROM questions q JOIN progress p ON p.id=q.id'
                      ' WHERE p.wrong>0 ORDER BY p.wrong DESC, q.id LIMIT ?', (limit,))
        out = []
        for r in rows:
            d = self._pub_row(r)
            d['wrong'] = r['pwrong']; d['streak'] = r['pstreak']; d['state'] = r['pstate']
            out.append(d)
        return out

    # ---- 判分 ----
    def grade(self, qid: str, given: str) -> dict:
        row = self.full(qid)
        if not row:
            return {'error': 'not found'}
        ans_raw = (row['answer'] or '').strip()
        if not ans_raw or '待补' in ans_raw:
            return {'answer': ans_raw or '（答案待补）', 'explanation': row['explanation'],
                    'correct': None, 'self': True}
        ans = ans_raw.replace('**', '').strip()      # 兜底剥离历史数据的加粗标记
        qt = row['qtype'] or ''
        try:
            opts = json.loads(row['options'] or '[]')
        except Exception:
            opts = []
        # 1) 判断题：答案是对/错，且（无选项 或 题型标注判断）
        j = norm_judge(ans)
        if j and (not opts or '判断' in qt):
            gj = norm_judge(given)
            if gj is None and opts:
                # 'A. 对' / 'B. 错' 形式：按选项文本映射用户点的字母
                m = re.match(r'^\s*([A-H])', (given or '').upper())
                if m:
                    idx = ord(m.group(1)) - 65
                    if 0 <= idx < len(opts):
                        t = opts[idx]
                        if re.search(r'对|正确|√|✓', t): gj = '对'
                        elif re.search(r'错|错误|×|✗', t): gj = '错'
            return {'answer': j, 'explanation': row['explanation'], 'correct': gj == j}
        # 2) 选择题：答案是纯字母串（A / AB / A、B、C），且题型是选择类或带选项
        letters = set(re.findall(r'[A-H]', ans.upper())) if re.fullmatch(r'[A-H][A-H、,，\s/]{0,15}', ans) else set()
        if not letters and re.search(r'单选|多选|选择|选型', qt):
            # 历史数据兜底：`A（单选·标准答案…）`这类剥了 ** 仍带注解的，取开头字母串
            m = re.match(r'^([A-H](?:\s*[、,，/]?\s*[A-H])*)\s*[（(：:．.]', ans)
            if m:
                letters = set(m.group(1))
        if letters and (re.search(r'单选|多选|选择|选型', qt) or opts):
            g = set(re.findall(r'[A-H]', (given or '').upper()))
            return {'answer': ans, 'explanation': row['explanation'], 'correct': bool(g) and g == letters}
        # 3) 其余（问答/计算/长文本答案）自评
        return {'answer': ans, 'explanation': row['explanation'], 'correct': None, 'self': True}

    # ---- 进度 ----
    def mark(self, qid: str, result: str) -> dict:
        with self.lock:  # 读-改-写一次锁内完成，避免并发丢更新
            rows = self.con.execute('SELECT * FROM progress WHERE id=?', (qid,)).fetchall()
            st = dict(rows[0]) if rows else {'state': 'new', 'streak': 0, 'wrong': 0}
            streak, wrong = st.get('streak') or 0, st.get('wrong') or 0
            state = st.get('state') or 'new'
            if result == 'correct':
                streak += 1
                state = 'correct'
                days = INTERVALS[min(streak, len(INTERVALS)) - 1]   # 1→1天 2→3天 … 6+→60天
            elif result == 'wrong':
                streak = 0; wrong += 1; state = 'wrong'; days = WRONG_BACK
            else:
                days = SKIP_BACK
            due = (datetime.now() + timedelta(days=days)).isoformat(timespec='seconds')
            self.con.execute('INSERT OR REPLACE INTO progress(id,state,streak,wrong,due,last_seen)'
                             ' VALUES(?,?,?,?,?,?)', (qid, state, streak, wrong, due, now_iso()))
            self.con.commit()
            self.save_progress_json()
        return {'ok': True, 'state': state, 'streak': streak, 'wrong': wrong, 'due': due}

    def export_progress(self) -> dict:
        rows = self.q('SELECT * FROM progress')
        return {r['id']: {k: r[k] for k in ('state', 'streak', 'wrong', 'due', 'last_seen')} for r in rows}

    def import_progress(self, data: dict) -> int:
        n = 0
        with self.lock:
            for pid, v in (data or {}).items():
                if not pid or not isinstance(v, dict):
                    continue
                self.con.execute('INSERT OR REPLACE INTO progress(id,state,streak,wrong,due,last_seen)'
                                 ' VALUES(?,?,?,?,?,?)',
                                 (str(pid), str(v.get('state', 'new')), clamp_int(v.get('streak'), 0, 0, 9999),
                                  clamp_int(v.get('wrong'), 0, 0, 99999),
                                  str(v.get('due') or '') or None, str(v.get('last_seen') or '') or None))
                n += 1
            self.con.commit()
            self.save_progress_json()
        return n

    # ---- 学习打卡：答题自动打卡（progress.last_seen 按日聚合）+ 手动补签 ----
    def _manual_days(self) -> set:
        if not hasattr(self, '_ck_manual'):
            try:
                self._ck_manual = set(json.loads(CKJSON.read_text(encoding='utf-8')).get('manual') or []) \
                    if CKJSON.exists() else set()
            except Exception:
                self._ck_manual = set()
        return self._ck_manual

    def checkin(self, ym: str = '') -> dict:
        today = datetime.now()
        ym = (ym or today.strftime('%Y-%m'))
        if not re.match(r'^\d{4}-\d{2}$', ym):
            ym = today.strftime('%Y-%m')
        per: dict = {}
        for r in self.q("SELECT last_seen, state FROM progress WHERE last_seen IS NOT NULL AND last_seen != ''"):
            d = (r['last_seen'] or '')[:10]
            if re.match(r'^\d{4}-\d{2}-\d{2}$', d):
                e = per.setdefault(d, {'n': 0, 'ok': 0})
                e['n'] += 1
                if r['state'] == 'correct':
                    e['ok'] += 1
        manual = self._manual_days()
        all_days = set(per.keys()) | manual
        today_s = today.strftime('%Y-%m-%d')
        ti = per.get(today_s, {'n': 0, 'ok': 0})
        # 连续天数：今天打了从今天往前数；今天没打则算截至昨天的（今天补上即续上）
        d = today if today_s in all_days else today - timedelta(days=1)
        streak = 0
        while d.strftime('%Y-%m-%d') in all_days:
            streak += 1
            d -= timedelta(days=1)
        y, m = int(ym[:4]), int(ym[5:7])
        ndays = calendar.monthrange(y, m)[1]
        lead = datetime(y, m, 1).weekday()                 # 周一=0
        cells = [{'day': i, 'n': per.get('%04d-%02d-%02d' % (y, m, i), {}).get('n', 0),
                  'ok': per.get('%04d-%02d-%02d' % (y, m, i), {}).get('ok', 0),
                  'manual': '%04d-%02d-%02d' % (y, m, i) in manual,
                  'today': '%04d-%02d-%02d' % (y, m, i) == today_s} for i in range(1, ndays + 1)]
        return {'today': today_s, 'checked_today': today_s in all_days,
                'today_n': ti['n'], 'today_ok': ti['ok'], 'streak': streak,
                'total_days': len(all_days), 'manual_count': len(manual),
                'month': ym, 'lead': lead, 'days': cells}

    def manual_checkin(self) -> dict:
        with self.lock:
            days = self._manual_days()
            t = datetime.now().strftime('%Y-%m-%d')
            # 今天已答题（自动打卡）则手动不重复记；纯手动补签才落 manual
            already = any((r['last_seen'] or '').startswith(t)
                          for r in self.q("SELECT last_seen FROM progress WHERE last_seen IS NOT NULL"))
            if t not in days and not already:
                days.add(t)
                try:
                    CKJSON.write_text(json.dumps({'manual': sorted(days)}, ensure_ascii=False, indent=1),
                                      encoding='utf-8')
                except OSError:
                    pass  # 写不了就当次运行有效，不打断
        return self.checkin()


STORE: Store | None = None

# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = 'mech-qbank/1.0'
    protocol_version = 'HTTP/1.1'   # keep-alive：前端一连多个 API 调用少握手
    timeout = 60                    # 空闲连接 60s 自动收，避免线程堆积

    def log_message(self, *a):        # 静音
        pass

    def handle(self):
        # 浏览器刷掉页面/连接中断时的 BrokenPipe 很常见，静默处理，不刷 traceback
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass

    def _send(self, code: int, body: bytes, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if body:
            self.wfile.write(body)

    def j(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ('/', '/index.html'):
                return self._send(200, (WEB / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if p == '/favicon.ico':
                return self._send(204, b'')
            if p.startswith('/static/'):
                f = (WEB / p[8:]).resolve()
                if f.is_file() and WEB.resolve() in f.parents:   # is_file：目录/不存在都挡掉
                    return self._send(200, f.read_bytes(),
                                      mimetypes.guess_type(str(f))[0] or 'application/octet-stream')
                return self.j({'error': 'not found'}, 404)
            if p == '/api/stats':
                return self.j(STORE.stats())
            if p == '/api/quiz':
                return self.j({'items': STORE.pick(q.get('n', 10), q.get('level'), q.get('qtype'),
                                                   q.get('class'), q.get('bank'), q.get('section'),
                                                   q.get('only_new') == '1', q.get('review') == '1')})
            if p == '/api/review':
                return self.j({'items': STORE.pick(q.get('n', 20), q.get('level'), q.get('qtype'),
                                                   review=True)})
            if p == '/api/search':
                return self.j({'items': STORE.search(q.get('q', ''), q.get('limit', 50))})
            if p == '/api/banks':
                return self.j({'items': STORE.banks()})
            if p == '/api/sections':
                return self.j({'items': STORE.sections()})
            if p == '/api/cards':
                return self.j({'items': STORE.cards(q.get('q', ''), q.get('chapter', ''),
                                                    q.get('limit', 500))})
            if p == '/api/chapters':
                return self.j({'items': STORE.chapters()})
            if p == '/api/wrong':
                return self.j({'items': STORE.wrong_list()})
            if p == '/api/progress':
                return self.j({'exported_at': now_iso(), 'mode': STORE.mode,
                               'progress': STORE.export_progress()})
            if p == '/api/checkin':
                return self.j(STORE.checkin(q.get('month', '')))
            return self.j({'error': 'not found'}, 404)
        except Exception as e:
            return self.j({'error': str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        n = clamp_int(self.headers.get('Content-Length') or 0, 0, 0, 10 * 1024 * 1024)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        try:
            if p == '/api/grade':
                return self.j(STORE.grade(str(body.get('id', '')), str(body.get('given', ''))))
            if p == '/api/mark':
                return self.j(STORE.mark(str(body.get('id', '')),
                                         body.get('result') if body.get('result') in ('correct', 'wrong', 'skip')
                                         else 'skip'))
            if p == '/api/progress':
                return self.j({'ok': True, 'imported': STORE.import_progress(body.get('progress') or {})})
            if p == '/api/checkin':
                return self.j(STORE.manual_checkin())
            return self.j({'error': 'not found'}, 404)
        except Exception as e:
            return self.j({'error': str(e)}, 500)


def main() -> int:
    global STORE
    STORE = Store()
    s = STORE.stats()
    print('mech-qbank 数据模式：%s ｜ 题 %d ｜ 卡片 %d ｜ 已答 %d ｜ 待复习 %d'
          % (s['mode'], s['total'], s['cards'], s['answered'], s['due']))
    print('  来源分级：%s' % s['by_class'])
    srv = None
    ports = [PORT + i for i in range(PORT_SCAN)]
    for port in ports:                      # 上次没关干净时自动换端口，双击就能起
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), H)
            break
        except OSError:
            continue
    if srv is None:
        print('⚠ 端口 %d–%d 都被占用了。关掉旧实例，或设 QBANK_PORT 换一个端口。'
              % (ports[0], ports[-1]), file=sys.stderr)
        return 1
    actual = srv.server_address[1]
    if actual != PORT:
        print('⚠ %d 被占用，已自动改用 %d' % (PORT, actual))
    print('mech-qbank 已启动  http://127.0.0.1:%d' % actual)
    if os.environ.get('QBANK_NO_BROWSER') != '1':      # 双击 exe 时自动开界面；设 1 可关掉
        threading.Timer(0.8, lambda: webbrowser.open('http://127.0.0.1:%d/' % actual)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
