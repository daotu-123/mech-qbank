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
  · 安全：只监听 127.0.0.1，且 Host/Origin 必须在 ALLOWED_HOSTS 白名单内（挡 DNS rebinding）
  · 打包：PyInstaller --windowed（无终端窗口），日志写 exe 同级 qbank.log；
    关闭服务走页面「退出程序」按钮 → GET /api/shutdown（_guard() 保证只有本机能调）

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
  GET  /api/notes      题目备注列表（按更新时间倒序；每条带题号/题干/题型，指明属于哪道题）
  GET  /api/question   按题号取单题 ?id=（不带答案，备注列表「跳到该题」用）
  GET  /api/shutdown  退出服务（无终端窗口时的关闭方式；页面右下角按钮调用）
  POST /api/grade      判分 {id, given}
  POST /api/mark       记录结果 {id, result: correct|wrong|skip}
  POST /api/progress   导入进度 {progress: {...}}（合并，不覆盖未出现的题）
  POST /api/checkin    手动打卡 {action: "checkin"}（答过题的日子自动算，这是补签）
  POST /api/notes      保存题目备注 {id, note}（note 为空 = 删除该题备注）
"""
from __future__ import annotations
import atexit, calendar, json, os, re, sqlite3, sys, mimetypes, threading, traceback, webbrowser
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
NOTESJSON = DATA / 'notes.json'         # 题目备注 {id: {note, updated_at}}（重建题库不丢）
PORT = int(os.environ.get('QBANK_PORT', '8640'))
PORT_SCAN = max(1, int(os.environ.get('QBANK_PORT_SCAN', '10')))  # 端口被占用时向后尝试的个数
INTERVALS = [int(x) for x in os.environ.get('QBANK_INTERVALS', '1,3,7,15,30,60').replace('，', ',').split(',')
             if x.strip().isdigit()] or [1, 3, 7, 15, 30, 60]
WRONG_BACK = 1   # 答错次日必回
SKIP_BACK = 3    # 跳过 3 天后回
# 本机服务只认这些 Host / Origin —— 挡 DNS rebinding（恶意页面把域名重绑到 127.0.0.1 后与目标同源，
# 浏览器同源策略会放行，届时它能读 /api/progress、/api/notes 并跨站 POST 改写进度/备注）。
# 将来若要局域网/手机访问，需显式把 IP 加进来，并同时把绑定地址从 127.0.0.1 放开。
ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}
TRUE_SET = {'对', '正确', '是', '√', '✓', 'true', 't', 'yes', 'y', '1'}
FALSE_SET = {'错', '错误', '否', '×', '✗', 'x', 'false', 'f', 'no', '0', '2'}
SRV = None          # main() 里赋值，供 /api/shutdown 用


def _log_path() -> str:
    """frozen 时日志放 exe 同级 qbank.log（windowed 打包没有控制台，排障全靠它）。
    开发/测试模式返回 ''（不写文件，免得污染仓库目录）。"""
    if not FROZEN:
        return ''
    try:
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'qbank.log')
    except Exception:
        return ''


def _log(msg: str) -> None:
    """统一输出：PyInstaller --windowed 打包后 sys.stdout/sys.stderr 是 None，
    裸 print() / print(file=sys.stderr) 会直接 AttributeError 把程序打崩，所以都走这里。"""
    if not FROZEN:
        print(msg)
        return
    if sys.stdout is not None:
        try:
            print(msg)
        except Exception:
            pass
    try:
        with open(_log_path(), 'a', encoding='utf-8') as f:
            f.write(msg.rstrip() + '\n')
    except Exception:
        pass


def _log_exc() -> None:
    """traceback 安全版：windowed 下 sys.stderr 为 None，裸 traceback.print_exc() 会崩。"""
    if not FROZEN:
        traceback.print_exc()
        return
    try:
        if sys.stderr is not None:
            traceback.print_exc()
    except Exception:
        pass
    try:
        with open(_log_path(), 'a', encoding='utf-8') as f:
            traceback.print_exc(file=f)
    except Exception:
        pass


def _alert(msg: str) -> None:
    """windowed 模式启动失败时弹个原生消息框 —— 否则用户双击后「什么都没发生」，完全没法排障。"""
    if not FROZEN or sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, 'mech-qbank', 0x30)   # MB_ICONWARNING
    except Exception:
        pass


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
                _log('⚠ 进度文件不可写（%s），本次运行进度仅存内存' % PROGJSON)
    # ---- 进度落盘：1 秒防抖，且不持锁 ----
    # 全量重写 2000 条进度约 3 ms / 280 KB；持锁做等于让所有 /api 请求排队等一次磁盘 I/O。
    def _flush_progress(self) -> None:
        """立即落盘（atexit 兜底用）。"""
        if self.mode != 'public':
            return
        with self.lock:
            self.save_progress_json()

    def _schedule_progress_save(self) -> None:
        """把落盘挪出调用方的锁，并做 1s 防抖 —— 连答多题只写一次盘。"""
        if self.mode != 'public':
            return
        with self.lock:
            t = getattr(self, '_save_timer', None)
            if t is not None and t.is_alive():
                return                      # 已有待写，本次跳过
            self._save_timer = threading.Timer(1.0, self._flush_progress)
            self._save_timer.daemon = True
            self._save_timer.start()

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
                # section_path 形如 'A. 来源名… › A1 …'，按第一段（章节来源）过滤
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
        # section_path 形如 'A. 来源名… › A1 …'，按第一段（章节来源）聚合。
        # 原先全表拉进 Python 聚合（4.65 ms），改成 SQL GROUP BY（3.42 ms）；
        # 收益有限但顺手，而且有 _sections 缓存，冷启动只跑一次。
        if self._sections is None:
            agg: dict = {}
            for r in self.q("SELECT section_path FROM questions WHERE section_path LIKE '% › %'"):
                seg = (r['section_path'] or '').split(' › ')[0].strip()
                if seg:
                    agg[seg] = agg.get(seg, 0) + 1
            # 无 ' › ' 的行（单段路径）整条算一段
            for r in self.q("SELECT section_path FROM questions "
                            "WHERE section_path IS NOT NULL AND section_path != '' "
                            "AND section_path NOT LIKE '% › %'"):
                seg = (r['section_path'] or '').strip()
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
        # 落盘移到锁外：磁盘 I/O 不再阻塞并发的答题/查询请求
        self._schedule_progress_save()
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
        # 导入不频繁，锁外同步写一次即可
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

    # ---- 题目备注（data/notes.json；与题库分离，重建题库不丢）----
    NOTE_MAX = 2000

    def _notes(self) -> dict:
        if getattr(self, '_note_cache', None) is None:
            self._note_cache = {}
            try:
                raw = json.loads(NOTESJSON.read_text(encoding='utf-8')) if NOTESJSON.exists() else {}
                src = raw.get('notes') if isinstance(raw, dict) else None
                if isinstance(src, dict):
                    for qid, v in src.items():
                        if isinstance(v, dict) and isinstance(v.get('note'), str) and v['note'].strip():
                            self._note_cache[str(qid)] = {'note': v['note'],
                                                          'updated_at': str(v.get('updated_at') or '')}
            except Exception:
                self._note_cache = {}   # 文件损坏降级为空，不阻塞启动
        return self._note_cache

    def _save_notes(self) -> bool:
        try:
            NOTESJSON.write_text(json.dumps({'notes': self._notes()}, ensure_ascii=False, indent=1),
                                 encoding='utf-8')
            return True
        except OSError:
            return False   # 写不了就当次运行有效，不打断

    def notes_list(self, limit=500) -> list:
        """每条备注都带题号/题干/题型 —— 前端据此明确「这条备注属于哪道题」。

        原先是 N+1：每条备注一次 `WHERE id=?`。300 条备注 = 300 次查询 + 300 次加锁，
        在 ThreadingHTTPServer 下会跟答题的 mark() 抢同一把锁。改成分片 IN 查询，
        全程只加一次锁（实测 6.74 ms → 2.09 ms）。
        """
        limit = clamp_int(limit, 500, 1, 2000)
        notes = self._notes()
        if not notes:
            return []
        ids = list(notes.keys())
        meta: dict = {}
        CHUNK = 900                     # SQLite 变量上限 999，留余量
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            sql = ('SELECT id, stem, qtype, level, bank FROM questions WHERE id IN (%s)'
                   % ','.join('?' * len(chunk)))
            with self.lock:             # 一次加锁拿完，而不是每条一次
                for r in self.con.execute(sql, chunk).fetchall():
                    meta[r['id']] = r
        out = []
        for qid, v in notes.items():
            r = meta.get(qid)
            out.append({'id': qid, 'note': v['note'], 'updated_at': v['updated_at'],
                        'stem': r['stem'] if r else '', 'qtype': r['qtype'] if r else '',
                        'level': r['level'] if r else None, 'bank': r['bank'] if r else '',
                        'missing': r is None})   # 题被删/换库仍列出，但标明不存在
        out.sort(key=lambda x: (x['updated_at'] or '', x['id']), reverse=True)
        return out[:limit]

    def set_note(self, qid: str, note: str) -> dict:
        qid = (qid or '').strip()
        if not qid:
            return {'error': '缺少题号 id'}
        if not self.full(qid):
            return {'error': 'not found'}
        note = (note or '').replace('\r\n', '\n').strip()[:self.NOTE_MAX]
        with self.lock:
            notes = self._notes()
            now = now_iso()
            if not note:
                notes.pop(qid, None)
                self._save_notes()
                return {'ok': True, 'id': qid, 'note': '', 'updated_at': '', 'removed': True}
            notes[qid] = {'note': note, 'updated_at': now}
            self._save_notes()
            return {'ok': True, 'id': qid, 'note': note, 'updated_at': now, 'removed': False}


STORE: Store | None = None

# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = 'mech-qbank/1.0'
    protocol_version = 'HTTP/1.1'   # keep-alive：前端一连多个 API 调用少握手
    timeout = 60                    # 空闲连接 60s 自动收，避免线程堆积

    def log_message(self, *a):        # 静音
        pass

    def _guard(self) -> bool:
        """只放行 127.0.0.1 / localhost 的请求，挡 DNS rebinding 与跨站调用。

        场景：用户开着 App 时访问恶意页面，该页面把域名重绑到 127.0.0.1 后即与本服务同源，
        fetch('/api/progress') 会被浏览器放行 —— 全部进度/备注/打卡记录随之泄露。
        """
        host = (self.headers.get('Host') or '').split(':')[0].strip()
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get('Origin')
        if origin:
            if (urlparse(origin).hostname or '') not in ALLOWED_HOSTS:
                return False
        return True

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
        if not self._guard():
            return self.j({'error': 'forbidden'}, 403)
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
            if p == '/api/notes':
                return self.j({'items': STORE.notes_list(q.get('limit', 500))})
            if p == '/api/question':
                row = STORE.full(q.get('id', ''))
                return self.j({'item': STORE._pub_row(row) if row else None})
            if p == '/api/shutdown':
                # windowed 打包后没有控制台可按 Ctrl+C，只能从页面退出；_guard() 已保证只有本机能调
                def _stop():
                    try:
                        SRV.shutdown()
                    except Exception:
                        pass
                threading.Thread(target=_stop, daemon=True).start()
                return self.j({'ok': True})
            return self.j({'error': 'not found'}, 404)
        except Exception:
            _log_exc()                       # 只进控制台/日志，别把异常原文（含库路径/SQL）回给客户端
            return self.j({'error': 'internal error'}, 500)

    def do_POST(self):
        if not self._guard():
            return self.j({'error': 'forbidden'}, 403)
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
            if p == '/api/notes':
                return self.j(STORE.set_note(str(body.get('id', '')), str(body.get('note', ''))))
            return self.j({'error': 'not found'}, 404)
        except Exception:
            _log_exc()
            return self.j({'error': 'internal error'}, 500)


def _flush_on_exit() -> None:
    """进程退出前把防抖窗口内未落盘的进度补写一次（最多补 1 秒的量）。

    测试里 store.con 已被 close、或模式非 public 时静默跳过 —— 不刷 traceback。
    """
    st = STORE
    if st is None or st.mode != 'public':
        return
    try:
        with st.lock:
            st.save_progress_json()
    except Exception:
        pass


atexit.register(_flush_on_exit)


def main() -> int:
    global STORE, SRV
    try:
        STORE = Store()
    except Exception:
        _log_exc()
        _alert('启动失败：数据初始化出错，详见 exe 同目录的 qbank.log')
        return 1
    s = STORE.stats()
    _log('mech-qbank 数据模式：%s ｜ 题 %d ｜ 卡片 %d ｜ 已答 %d ｜ 待复习 %d'
         % (s['mode'], s['total'], s['cards'], s['answered'], s['due']))
    _log('  来源分级：%s' % s['by_class'])
    srv = None
    ports = [PORT + i for i in range(PORT_SCAN)]      # 上次没关干净时自动换端口，双击就能起
    for port in ports:
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), H)
            break
        except OSError:
            continue
    if srv is None:
        _log('⚠ 端口 %d–%d 都被占用了。关掉旧实例，或设 QBANK_PORT 换一个端口。'
             % (ports[0], ports[-1]))
        _alert('端口 %d–%d 都被占用了。\n关掉旧实例（或从任务栏/任务管理器结束 mech-qbank），或设 QBANK_PORT 换端口。'
               % (ports[0], ports[-1]))
        return 1
    actual = srv.server_address[1]
    if actual != PORT:
        _log('⚠ %d 被占用，已自动改用 %d' % (PORT, actual))
    _log('mech-qbank 已启动  http://127.0.0.1:%d' % actual)
    _log('  关闭方式：点页面右下角「退出程序」按钮（本程序无终端窗口）')
    SRV = srv
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
