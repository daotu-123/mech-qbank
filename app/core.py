# -*- coding: utf-8 -*-
r"""
mech-qbank · 数据层（无 HTTP 依赖，服务端与 API 共用）

纯标准库（sqlite3 + json），零第三方依赖。本模块只负责：
  · 题库读取（sqlite 全量库 / public/qbank.json 公开子集，自动降级为内存库）
  · 判分 / 间隔重复排期 / 进度 / 打卡 / 备注
  · 公开模式下的进度防抖落盘（1 秒防抖 + atexit 兜底）

数据格式（迁移到 FastAPI 后完全不变，老数据零迁移）：
  data/qbank.sqlite   题库（只读）+ progress 表
  data/progress.json  公开模式进度 {id: {state, streak, wrong, due, last_seen}}
  data/checkin.json   手动补签 {'manual': ['YYYY-MM-DD', ...]}
  data/notes.json     题目备注 {'notes': {id: {note, updated_at}}}

设计约定
  · 不做 LLM 批改：选择题/判断题本地自动判分；问答/计算类返回 correct=None，由用户自评
  · 复习间隔：答对按 1/3/7/15/30/60 天（QBANK_INTERVALS 可覆盖）；答错次日必回；跳过 3 天后回
  · 只读题库，进度写进 progress 表（公开模式写 progress.json）
  · 安全：只监听 127.0.0.1，且 Host/Origin 必须在 ALLOWED_HOSTS 白名单内（挡 DNS rebinding）
    —— 白名单校验在传输层做（server.py 的 _guard() / api.py 的中间件），此处只给常量
  · windowed 打包后 sys.stdout/stderr 为 None：所有输出走 _log()/_log_exc()，
    并追加写 exe 同级 qbank.log；开发/测试模式仍是裸 print
"""
from __future__ import annotations
import atexit, calendar, io, json, os, re, sqlite3, sys, threading, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage
#分层（2026-10-20 后端重构，纯结构调整）：文件读写的唯一出口是 storage 模块。
# 本文件原有的 6 处 JSON 读写（PROGJSON / CKJSON / NOTESJSON / PUBJSON / DB 探测 / qbank.log）
# 已全部改走它。注意 storage 是**普通公开名字**，所以 `from core import *` 会把它一并带进
# api.py 与 server.py —— 那两个文件不需要单独 import。_log_path/_log/_log_exc 是下划线名字，
# import * 会跳过，api.py/server.py 必须显式导入（server.py:55已补）。

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
WRONG_BACK = 1   # 答错次日必回（仅在 FSRS 不可用时作为回退）
SKIP_BACK = 3    # 跳过 3 天后回

# ---------------------------------------------------------------- FSRS 排期
# 2026-10-18：把「答对次数计数器」换成 FSRS-6 间隔重复。
#
# 旧实现的问题（core.py 原 mark()）：
#   days = INTERVALS[min(streak,6)-1]，答错 streak 归零 → 下次答对回到 1 天，
#   此前进度全部清零；无 ease、无四档评分、无遗忘曲线。
#
# 新实现：调用 py-fsrs（FSRS-6 官方 Python 实现），前端 static-store.js 用
# ts-fsrs（官方 JS 实现），两端算法同源。二者在「到期后复习」时输出完全一致；
# 对「提前复习」（未到期就答）两库处理不同，因此本层把复习时间钳到 max(now, due)，
# 保证两端输入一致。
#
# 评分映射：答对 → Good；答错 → Again；跳过的语义是「不知道」→ Again；
# 前端四档按钮可显式传 again/hard/good/easy。
FSRS_RATINGS = ('again', 'hard', 'good', 'easy')
FSRS_ENABLED_ENV = 'QBANK_FSRS'
FSRS_ENABLED = os.environ.get(FSRS_ENABLED_ENV, '1').strip() not in ('0', 'false', 'False', 'off')

try:
    from fsrs import Scheduler as _FsrsScheduler, Card as _FsrsCard, Rating as _FsrsRating
    _FSRS_OK = True
except Exception:                                    # pragma: no cover - 依赖缺失时回退旧逻辑
    _FSRS_OK = False
    _FsrsScheduler = _FsrsCard = _FsrsRating = None

_FSRS_ENGINE = _FsrsScheduler(enable_fuzzing=False) if _FSRS_OK else None
_FSRS_RATING_MAP = {
    'again': _FsrsRating.Again, 'hard': _FsrsRating.Hard,
    'good': _FsrsRating.Good, 'easy': _FsrsRating.Easy,
} if _FSRS_OK else {}


def fsrs_available() -> bool:
    return bool(_FSRS_OK and FSRS_ENABLED)


def resolve_rating(result: str) -> str:
    """把旧的 correct/wrong/skip 与新的四档评分统一成 FSRS 评分名。"""
    r = (result or '').strip().lower()
    if r in FSRS_RATINGS:
        return r
    if r == 'correct':
        return 'good'
    if r in ('wrong', 'skip'):
        return 'again'          # 跳过语义上是「不知道」，与答错同样进重学
    return 'good'
# 本机服务只认这些 Host / Origin —— 挡 DNS rebinding（恶意页面把域名重绑到 127.0.0.1 后与目标同源，
# 浏览器同源策略会放行，届时它能读 /api/progress、/api/notes 并跨站 POST 改写进度/备注）。
# 将来若要局域网/手机访问，需显式把 IP 加进来，并同时把绑定地址从 127.0.0.1 放开。
ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}
TRUE_SET = {'对', '正确', '是', '√', '✓', 'true', 't', 'yes', 'y', '1'}
FALSE_SET = {'错', '错误', '否', '×', '✗', 'x', 'false', 'f', 'no', '0', '2'}
SRV = None          # 运行入口里赋值，供 /api/system/shutdown 用

# 组卷的题型排序标签（隐藏字段，只用于排序不显示）：
# 先选择（单选→多选）、再判断，填空/问答/识记/综合居中，计算垫底。
# 与 web/src/store/static-store.js 的 QT_RANK / qtypeRank 同序（parity 差分兜底）。
QT_RANK = {'单选': 1, '多选': 2, '判断': 3, '填空': 4, '问答': 5, '识记': 6, '综合': 7, '计算': 8}
# 匹配顺序 ≠ 档位顺序：「计算」提前到「问答」之前判，让「计算/问答」这类混合题也垫底
QT_MATCH_ORDER = ('单选', '多选', '判断', '填空', '计算', '问答', '识记', '综合')
RANK_UNKNOWN = 6    # 未标注题型落在「识记」一档，不抢计算题的末位


def qtype_rank(qtype: str) -> int:
    """题型 → 组卷排序序号（越小越靠前）。混合题型按 QT_MATCH_ORDER 先命中者定档
    （'填空/问答' → 4，'计算/问答' → 8：带计算的题一律垫底）。"""
    qt = qtype or ''
    for k in QT_MATCH_ORDER:
        if k in qt:
            return QT_RANK[k]
    return RANK_UNKNOWN


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
    storage.append_text(_log_path(), msg.rstrip() + '\n')


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
    # traceback.print_exc(file=...) 要文件句柄，storage.append_text 收字符串 —— 用 StringIO 桥接
    buf = io.StringIO()
    traceback.print_exc(file=buf)
    storage.append_text(_log_path(), buf.getvalue())


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


# 全角字母数字 → 半角（填空判分归一化用；与 static-store.js 的 normFill 同规则）
_F2H = {ord(f): ord(t) for f, t in zip(
    '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ',
    '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')}


def _norm_fill(s: str) -> str:
    """填空判分归一化：全角转半角、去首尾空白与尾部标点、小写。
    与 web/src/store/static-store.js 的 normFill 同规则（parity 差分兜底）。"""
    s = (s or '').strip().translate(_F2H)
    return s.rstrip('。.，,；;：:！!？?').lower()


def norm_judge(s):
    """把 对/正确/√/T/1 之类归一成 '对'，错/×/F/0 归一成 '错'，其余返回 None。"""
    s = (s or '').strip().lower().rstrip('。.')
    if s in TRUE_SET: return '对'
    if s in FALSE_SET: return '错'
    return None


def fill_slots(qtype: str, answer: str) -> int:
    """可判分填空的空数；0 = 不按填空判分（走自评）。
    与 static-store.js 的 fillSlots / QuestionCard 的 isFill 同规则（parity 差分兜底）：
      · answer 含 | → 多空
      · qtype 恰为「填空」且答案短（≤20 字、无①式圈号与分号）→ 单空
      · 老数据（①②③式长答案、'填空/问答' 混合题型、待补）→ 0
    """
    qt = (qtype or '').strip()
    ans = (answer or '').strip()
    if '填空' not in qt or not ans or '待补' in ans:
        return 0
    if '|' in ans:
        return len(ans.split('|'))
    if qt == '填空' and len(ans) <= 20 and not re.search(r'[①②③④⑤⑥；;]', ans):
        return 1
    return 0


# ---------------------------------------------------------------- 数据层
class Store:
    def __init__(self) -> None:
        self.mode = 'sqlite' if storage.probe(DB) else 'public'
        self.lock = threading.RLock()          # 可重入：mark() 里包着 q()/x()
        self.prog_writable = True
        self._banks = None
        self._sections = None
        if self.mode == 'sqlite':
            self.con = sqlite3.connect(DB, check_same_thread=False)
            self.con.row_factory = sqlite3.Row
        else:
            if not storage.probe(PUBJSON):
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
        # FSRS 卡片状态独立成表：progress 表已有 6 处显式列清单
        # （mark/export/import/save/DDL/迁移），加列要全改；新表只需 2 处。
        self._ensure_fsrs_table()

    def _ensure_fsrs_table(self) -> None:
        self.con.execute('CREATE TABLE IF NOT EXISTS fsrs_state('
                         'id TEXT PRIMARY KEY, card TEXT, updated TEXT)')
        self.con.commit()

    # ---- FSRS 排期 ----
    def schedule_chain(self, sequence, start=None):
        """按给定序列推进一张全新卡片，返回每步 {due, s, d}。

        纯内存、不落库、不读时钟，供前后端差分测试使用（两侧传同一序列必须同结果）。
        sequence: [(rating, day_offset)]，rating 可为 again/hard/good/easy 或 correct/wrong/skip。
        """
        if not fsrs_available():
            return None
        base = start or datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        card = _FsrsCard(due=base)
        out = []
        for rating, off in sequence:
            at = base + timedelta(days=off)
            card, _ = _FSRS_ENGINE.review_card(
                card, _FSRS_RATING_MAP[resolve_rating(rating)], review_datetime=at)
            out.append({
                'due': card.due.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M'),
                's': round(card.stability, 4),
                'd': round(card.difficulty, 4),
            })
        return out

    def _fsrs_load(self, qid: str):
        row = self.con.execute('SELECT card FROM fsrs_state WHERE id=?', (qid,)).fetchone()
        if not row or not row['card']:
            return _FsrsCard()
        try:
            return _FsrsCard.from_dict(json.loads(row['card']))
        except Exception:
            return _FsrsCard()

    def _fsrs_save(self, qid: str, card) -> None:
        self.con.execute('INSERT OR REPLACE INTO fsrs_state(id,card,updated) VALUES(?,?,?)',
                         (qid, json.dumps(card.to_dict(), ensure_ascii=False), now_iso()))

    def schedule(self, qid: str, result: str, now=None, dry: bool = False):
        """用 FSRS-6 计算下一次到期。

        result 接受四档（again/hard/good/easy）或旧语义（correct/wrong/skip）。
        返回 dict；FSRS 不可用时返回 None，调用方回退到旧的固定间隔。
        dry=True 时只算不落库（供差分测试使用）。
        """
        if not fsrs_available():
            return None
        rating = resolve_rating(result)
        base = now or datetime.now(timezone.utc)
        card = self._fsrs_load(qid)
        # 全新卡片：Card() 的默认 due 取的是真实当前时间，会让计算结果不可复现，
        # 也干扰「钳到到期」逻辑，因此显式用 base 作为初始到期。
        is_new = card.stability is None and card.last_review is None
        if is_new:
            card = _FsrsCard(due=base)
        # 提前复习（未到期就答）时，不能把复习时间倒推到上次复习之前，
        # 否则 elapsed 为负、两个官方实现（py-fsrs / ts-fsrs）处理不一致。
        # 这里只保证「时间不倒退」，复习时间仍取 now，语义正确。
        review_at = base
        lr = getattr(card, 'last_review', None)
        if lr is not None and base < lr:
            review_at = lr
        new_card, _log = _FSRS_ENGINE.review_card(
            card, _FSRS_RATING_MAP[rating], review_datetime=review_at)
        if not dry:
            self._fsrs_save(qid, new_card)
        due_local = new_card.due.astimezone().replace(tzinfo=None)
        return {
            'due': due_local.isoformat(timespec='seconds'),
            'rating': rating,
            'fsrs_state': new_card.state.name.lower(),
            'stability': round(new_card.stability, 6) if new_card.stability is not None else None,
            'difficulty': round(new_card.difficulty, 6) if new_card.difficulty is not None else None,
        }

    # ---- 进度文件（仅公开模式）----
    def _load_progress_json(self) -> None:
        # 走 storage.read_json：原实现是 try/except Exception → pass，
        # 即「文件损坏不影响启动」——storage 的 default 语义与此一致。
        raw = storage.read_json(PROGJSON, default=None)
        if raw:
            try:
                for pid, v in raw.items():
                    self.con.execute('INSERT OR REPLACE INTO progress(id,state,streak,wrong,due,last_seen)'
                                     ' VALUES(?,?,?,?,?,?)',
                                     (pid, v.get('state', 'new'), v.get('streak', 0), v.get('wrong', 0),
                                      v.get('due'), v.get('last_seen')))
                self.con.commit()
            except Exception:
                pass  # 内容结构不对不影响启动

    def save_progress_json(self) -> None:
        if self.mode != 'public':
            return
        try:
            rows = self.con.execute('SELECT * FROM progress').fetchall()
            ok = storage.write_json(PROGJSON, {
                r['id']: {k: r[k] for k in ('state', 'streak', 'wrong', 'due', 'last_seen')} for r in rows})
            if not ok:
                raise OSError(PROGJSON)
        except OSError:
            if self.prog_writable:  # 只提醒一次，不打断刷题
                self.prog_writable = False
                _log('⚠ 进度文件不可写（%s），本次运行进度仅存内存' % PROGJSON)

    # ---- 进度落盘：1 秒防抖，且不持锁 ----
    # 全量重写 2000 条进度约 3 ms / 280 KB；持锁做等于让所有请求排队等一次磁盘 I/O。
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
             only_new=False, review=False, limit=None) -> list:
        # limit 是 n 的别名：前端 api.js 与 FastAPI 端点都用 limit 命名，
        # 历史上直接传 pick(limit=...) 会 TypeError（见 tools/smoke_parity.py 的回归断言）。
        if limit is not None:
            n = limit
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
            # 今日复习按到期时间排（复习节奏优先，不套题型顺序）
            rows = self.q('SELECT q.* FROM questions q JOIN progress p ON p.id=q.id'
                          ' WHERE p.due IS NOT NULL AND p.due<=?'
                          + (' AND ' + ' AND '.join(where) if where else '')
                          + ' ORDER BY p.due, q.id LIMIT ?', (now_iso(), *args, n))
        else:
            if only_new:
                where.append("id NOT IN (SELECT id FROM progress WHERE state IN ('correct','wrong'))")
                w = ' WHERE ' + ' AND '.join(where)
            # 先随机抽 n 道（保证题型配比随题库分布），再按题型标签稳定排序：
            # 单选 → 多选 → 判断 → 填空/问答/识记/综合 → 计算垫底；同题型内保持随机序。
            # 不能写成 ORDER BY CASE..., RANDOM() LIMIT n——那样单选会占满名额，一套题全变成单选。
            rows = self.q('SELECT * FROM questions' + w + ' ORDER BY RANDOM() LIMIT ?', (*args, n))
            rows.sort(key=lambda r: qtype_rank(r['qtype']))
        return [self._pub_row(r) for r in rows]

    def _pub_row(self, r) -> dict:
        sp = (r['section_path'] or '') if r.keys() and 'section_path' in r.keys() else ''
        ans = (r['answer'] or '') if r.keys() and 'answer' in r.keys() else ''
        return {'id': r['id'], 'stem': r['stem'], 'qtype': r['qtype'], 'level': r['level'],
                'options': json.loads(r['options'] or '[]'), 'cat': r['cat_name'], 'bank': r['bank'],
                'source_class': r['source_class'], 'sec': sp.split(' › ')[0] if sp else '',
                # 只暴露空数、不暴露答案：前端据此渲染 N 个输入框（防答案泄露前提下的 UI 决策）
                'fill_n': fill_slots(r['qtype'], ans),
                # 题型排序标签（隐藏）：UI 不渲染，供组卷排序与测试断言用，两端同口径
                'qrank': qtype_rank(r['qtype'])}

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
        # 0) 填空题（PDF 摘录挖空题）：空数与判分口径见 fill_slots()，必须最优先——
        #    单空答案恰为「对/错」时会落进下面的判断题分支被误判。
        if fill_slots(qt, ans):
            fill_parts = ([p.strip() for p in ans.split('|')] if '|' in ans else [ans.strip()])
            # 全角竖线 ｜ 是中文输入法下的常见笔误，与半角同义
            gparts = [p.strip() for p in (given or '').replace('｜', '|').split('|')]
            if len(gparts) < len(fill_parts):          # 少填的空按空串判错，不报错
                gparts += [''] * (len(fill_parts) - len(gparts))
            detail, ok = [], True
            for i, a in enumerate(fill_parts):
                na = _norm_fill(a)
                hit = bool(na) and _norm_fill(gparts[i]) == na
                detail.append({'i': i + 1, 'ans': a, 'given': gparts[i], 'ok': hit})
                ok = ok and hit
            return {'answer': ans, 'explanation': row['explanation'], 'correct': ok, 'fill': detail}
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
            rating = resolve_rating(result)

            # FSRS 决定下次到期；streak 降级为纯展示指标。
            # 旧实现 days=INTERVALS[streak-1] 且答错把 streak 清零，导致
            # 一次答错就把此前的排期进度全部作废——这是本次替换的核心目标。
            fs = self.schedule(qid, result)
            if fs:
                due = fs['due']
                if result == 'skip':
                    # 跳过：不进对错统计，但按 Again 排期（语义是「没掌握」）
                    pass
                elif rating == 'again':
                    streak = 0; wrong += 1; state = 'wrong'
                else:
                    streak += 1; state = 'correct'
            else:
                # 回退路径：FSRS 不可用（缺依赖或显式关闭）时保持旧行为
                if result == 'correct':
                    streak += 1
                    state = 'correct'
                    days = INTERVALS[min(streak, len(INTERVALS)) - 1]
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
        out = {'ok': True, 'state': state, 'streak': streak, 'wrong': wrong, 'due': due}
        if fs:
            out.update({'rating': fs['rating'], 'fsrs_state': fs['fsrs_state'],
                        'stability': fs['stability'], 'difficulty': fs['difficulty']})
        return out

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
            raw = storage.read_json(CKJSON, default={})
            try:
                self._ck_manual = set(raw.get('manual') or []) if isinstance(raw, dict) else set()
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
                storage.write_json(CKJSON, {'manual': sorted(days)})   # 写不了就当次运行有效，不打断
        return self.checkin()

    # ---- 题目备注（data/notes.json；与题库分离，重建题库不丢）----
    NOTE_MAX = 2000

    def _notes(self) -> dict:
        if getattr(self, '_note_cache', None) is None:
            self._note_cache = {}
            try:
                raw = storage.read_json(NOTESJSON, default={})
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
        return storage.write_json(NOTESJSON, {'notes': self._notes()})   # 写不了就当次运行有效，不打断

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

    # ---- 自定义题（PDF 摘录编辑器保存的用户题；qbank_build.py 重建时按 source_class 保留）----
    # 与 static-store.js 的 normCustomQ/mergeQuestions 同规则（parity/冒烟兜底）。
    QTYPES = ('判断', '单选', '多选', '填空', '问答', '计算', '识记', '综合')
    CUSTOM_BANK = 'PDF 自建'
    CUSTOM_CLASS = '自定义'

    def _norm_custom_q(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            return {'err': '不是合法题目对象'}
        qid = str(raw.get('id') or '').strip()
        stem = str(raw.get('stem') or '').strip()
        if not qid:
            return {'err': '缺少 id（题号）'}
        if not stem:
            return {'err': '缺少题干（stem）：' + qid}
        # 撞号检查：只挡「淬取题/内置题」撞号；自定义题同 id 视为更新（由 add_custom 的 cover 把关）
        if self.custom_row(qid) is None and self.full(qid):
            return {'err': 'id 与已有题目重复，换一个 id：' + qid}
        qt = str(raw.get('qtype') or '').strip()
        if qt and qt not in self.QTYPES:
            return {'err': '题型不在 8 种之内：' + qt}
        lv = raw.get('level')
        try:
            lv = None if lv in (None, '') else max(1, min(5, int(str(lv).strip())))
        except (TypeError, ValueError):
            lv = None
        opts = raw.get('options') or []
        if not isinstance(opts, list):
            return {'err': 'options 必须是数组：' + qid}
        return {'ok': True, 'q': {
            'id': qid, 'stem': stem,
            'qtype': qt or '问答', 'level': lv,
            'options': [str(o) for o in opts],
            'answer': str(raw.get('answer') or ''),
            'explanation': str(raw.get('explanation') or ''),
            'bank': str(raw.get('bank') or '').strip() or self.CUSTOM_BANK,
            'section_path': str(raw.get('section_path') or '').strip(),
            'cat_name': str(raw.get('cat_name') or '').strip(),
            'source_class': self.CUSTOM_CLASS}}

    def add_custom(self, raw: dict, cover: bool = False) -> dict:
        """写入/更新一道自定义题。返回 {ok, id} 或 {err}。"""
        r = self._norm_custom_q(raw)
        if 'err' in r:
            return {'err': r['err']}
        q = r['q']
        with self.lock:
            if not cover and self.custom_row(q['id']):
                return {'err': '已存在同 id 自定义题（cover=false 不覆盖）：' + q['id']}
            self.x('INSERT OR REPLACE INTO questions(id,bank,source_class,section_path,section,'
                   'cat_name,stem,qtype,level,options,answer,explanation,source,note,meta)'
                   ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (q['id'], q['bank'], self.CUSTOM_CLASS, q['section_path'], '', q['cat_name'],
                    q['stem'], q['qtype'], q['level'], json.dumps(q['options'], ensure_ascii=False),
                    q['answer'], q['explanation'], '', '', ''))
            self._banks = None            # 缓存失效：bank 列表要能看到「PDF 自建」
            self._sections = None
            return {'ok': True, 'id': q['id']}

    def custom_row(self, qid: str):
        with self.lock:
            return self.con.execute(
                'SELECT id,stem,qtype,level,options,answer,explanation,bank,section_path,cat_name'
                ' FROM questions WHERE id=? AND source_class=?', (qid, self.CUSTOM_CLASS)).fetchone()

    def custom_list(self, limit: int = 500) -> list:
        # 不返回 answer：列表只用于管理（题号/题型/题干），与 static-store 的 customList 同口径
        with self.lock:
            rows = self.con.execute(
                'SELECT id,stem,qtype,level,bank FROM questions WHERE source_class=?'
                ' ORDER BY id', (self.CUSTOM_CLASS,)).fetchall()
        return [dict(r) for r in rows[:limit]]

    def delete_custom(self, qid: str) -> dict:
        qid = (qid or '').strip()
        if not self.custom_row(qid):
            return {'error': 'not found'}
        with self.lock:
            self.x('DELETE FROM questions WHERE id=? AND source_class=?', (qid, self.CUSTOM_CLASS))
            # 级联清进度：留着会让 stats 的已答/掌握/错题统计把一道不存在的题算进去
            self.x('DELETE FROM progress WHERE id=?', (qid,))
        return {'ok': True, 'id': qid, 'removed': True}

    def import_customs(self, questions, cover: bool = False) -> dict:
        """批量导入自定义题（编辑器保存 / JSON 粘贴）。返回 {added,skipped,rejected}。"""
        if not isinstance(questions, list):
            return {'added': 0, 'skipped': 0, 'rejected': [{'id': '（无 id）', 'err': 'questions 必须是数组'}]}
        added = skipped = 0
        rejected = []
        for raw in questions:
            r = self.add_custom(raw, cover=cover)
            if r.get('ok'):
                added += 1
            elif '已存在同 id' in (r.get('err') or ''):
                skipped += 1
            else:
                rejected.append({'id': str((raw or {}).get('id') or '（无 id）'), 'err': r['err']})
        return {'added': added, 'skipped': skipped, 'rejected': rejected}


STORE: Store | None = None


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
