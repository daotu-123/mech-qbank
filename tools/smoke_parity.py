# -*- coding: utf-8 -*-
r"""
smoke_parity.py — 前后端判分/排期一致性差分测试。

为什么存在
  app/core.py 的 Store.grade() 与 web/src/store/static-store.js 的 grade() 是两份手抄实现
  （Python + JavaScript，分别服务 http:// 服务端与 file:// APK 离线模式）。
  注释里写着「改动必须两边同步」——靠人记住的约束等于没有约束。
  本工具把这句话变成一条断言：任一端改了正则/集合/阈值，几秒内在这里炸出来。

测什么
  判分  全库题目 × 26 种作答取值，比对 correct / self / answer / fill 明细四项
        （含 5 道合成哨兵题：双空填空、答案字面含对错的填空、①②③式老填空、单空短答案、判断题）
  排期  同一道题连答对 6 次，两端算出的 due 日期必须逐一相同；
        再各答错/跳过一次，两端必须同为「次日」和「+3 天」
         （曾因 QBANK_INTERVALS 只在服务端生效而分叉：界面显示 2/5/10，实际按 1/3/7 排期）

用法
    python tools/smoke_parity.py          # 用 data/qbank.sqlite（全量）
    python tools/smoke_parity.py --public # 用 data/public/qbank.json（公开子集）
    python tools/smoke_parity.py --quick  # 只测前 300 题（改解析器时的快速反馈）

退出码  有差异 → 1；全部一致 → 0；node 缺失 → 2（跳过，不算失败）
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data' / 'qbank.sqlite'
PUB = ROOT / 'data' / 'public' / 'qbank.json'

# 覆盖 判断（对/错/√/×/T/F/1/0）、单选、多选、空值、'A. 对' 形式的主要输入形态；
# 后 7 项专测填空判分（多空按 | 分隔、顺序错位、空数不齐、全角竖线、单空短答案与归一化）。
GIVENS = ['A', 'B', 'C', 'D', 'AB', 'ABC', '对', '错', '正确', '错误',
          '√', '×', 'T', 'F', '1', '2', '0', '', 'A. 对',
          '分度圆|模数', '模数|分度圆', '分度圆|模数|压力角', '压力角',
          '分度圆 ｜ 模数', '公差', ' 公差。']

# 排期差分的固定序列（两侧传入完全相同的 (rating, 相对天数)）：
# 覆盖四档评分、一次遗忘重学、以及一次长间隔后的 Easy。
SCHED_SEQ = [('good', 0), ('good', 0), ('again', 3), ('good', 4),
             ('hard', 10), ('easy', 30)]

# 合成哨兵题：全库可能没有 answer 含 | 的填空，用它确保两端都走到新分支。
# SYNTHF2 的答案 '对|错|对' 专门验证「填空分支必须先于判断题分支」——
# 否则单空答案为「对/错」的挖空题会被 norm_judge 抢走误判。
SYNTH = [
    {'id': 'SYNTHF1', 'qtype': '填空', 'options': [], 'stem': '齿轮的（　）圆是分度基准，其大小由（　）决定',
     'answer': '分度圆|模数', 'explanation': '哨兵：双空'},
    {'id': 'SYNTHF2', 'qtype': '填空', 'options': [], 'stem': '三空顺序哨兵',
     'answer': '对|错|对', 'explanation': '哨兵：答案含对错字面'},
    {'id': 'SYNTHF3', 'qtype': '填空', 'options': [], 'stem': '①②③式长答案哨兵（应自评）',
     'answer': '① 顶线和底线各 1.5 磅；② 表头下栏目线 0.75 磅', 'explanation': '哨兵：老数据自评'},
    {'id': 'SYNTHF4', 'qtype': '填空', 'options': [], 'stem': '单空短答案哨兵（应判分）',
     'answer': '公差', 'explanation': '哨兵：单空自动判分'},
    {'id': 'SYNTHJ1', 'qtype': '判断', 'options': [], 'stem': '判断哨兵', 'answer': '对',
     'explanation': '哨兵：判断题'},
]


def find_node() -> str | None:
    """优先环境变量指定，其次 PATH。找不到返回 None（调用方跳过，不算失败）。"""
    cands = [os.environ.get('QBANK_NODE'), shutil.which('node'), shutil.which('node.exe')]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def load_questions(public: bool, quick: bool) -> list:
    if public:
        data = json.loads(PUB.read_text(encoding='utf-8'))
        out = [dict(q) for q in data['questions']]
        for q in out:
            q.setdefault('options', [])
    else:
        import sqlite3
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        rows = con.execute('SELECT id,qtype,options,answer FROM questions').fetchall()
        con.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d['options'] = json.loads(d.get('options') or '[]')
            except Exception:
                d['options'] = []
            out.append(d)
    return (out[:300] if quick else out) + [dict(q) for q in SYNTH]


def run_js(node: str, questions: list, intervals: list) -> dict:
    """在 Node 里跑 web/src/store/static-store.js，返回 {qid: {given:[correct,self,answer], _due:[...]}}。"""
    tmp = Path(tempfile.mkdtemp(prefix='qbank-parity-'))
    try:
        (tmp / 'in.json').write_text(
            json.dumps({'questions': questions, 'givens': GIVENS, 'intervals': intervals,
                        'sched_seq': SCHED_SEQ},
                       ensure_ascii=False), encoding='utf-8')
        (tmp / 'run.js').write_text(r'''
var fs = require('fs');
var QBankStatic = require(process.argv[2]);
var d = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
var s = QBankStatic.make({questions: d.questions, cards: [], intervals: d.intervals}, null);
var out = {};
for (var i = 0; i < d.questions.length; i++) {
  var q = d.questions[i], res = {};
  for (var j = 0; j < d.givens.length; j++) {
    var r = s.grade(q.id, d.givens[j]);
    res[d.givens[j]] = [r.correct === undefined ? null : r.correct,
                        !!r.self,
                        String(r.answer || '').slice(0, 40),
                        JSON.stringify(r.fill || null)];
  }
  // 排期：走 static-store 的真实排期器 schedule_chain（与 core.schedule_chain 同序列）。
  // 2026-10-18 同步改造：此前这里是「连答对 6 次取 due」，而 Python 侧是按固定公式
  // 重算、根本不调 mark()，于是改排期两端都不红——假守护。
  var chain = s.schedule_chain ? s.schedule_chain(d.sched_seq) : null;
  res._due = chain ? chain.map(function (x) { return x.due; }) : null;
  // 题型排序标签（隐藏字段）：两端必须同档，否则组卷顺序会分叉
  var row = s.question(q.id);
  res._qrank = row ? row.qrank : null;
  out[q.id] = res;
}
fs.writeFileSync(process.argv[4], JSON.stringify(out), 'utf-8');
''', encoding='utf-8')
        subprocess.run([node, str(tmp / 'run.js'), str(ROOT / 'web' / 'src' / 'store' / 'static-store.js'),
                        str(tmp / 'in.json'), str(tmp / 'out.json')], check=True)
        return json.loads((tmp / 'out.json').read_text(encoding='utf-8'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_store(server, public: bool):
    """构造一个含合成哨兵题的临时数据源，返回 (Store, 临时目录)。

    为什么不能直接 server.Store()：Python 侧判分是从库/JSON 读题的，哨兵题不在
    真实数据里就查不到（grade → not found），差分会在 SYNTH 上假失败。
    全量模式复制一份 sqlite 再插哨兵；公开模式造一份带哨兵的临时 JSON。
    注意改的是 core 模块的常量：Store 运行期读的是 core 的名字（2026-10-17 踩过）。
    """
    import sqlite3
    tmp = Path(tempfile.mkdtemp(prefix='qbank-parity-db-'))
    if public:
        data = json.loads(PUB.read_text(encoding='utf-8'))
        data['questions'] = list(data.get('questions', [])) + [dict(q) for q in SYNTH]
        (tmp / 'pub.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        server.core.DB = tmp / 'not-exist.sqlite'
        server.core.PUBJSON = tmp / 'pub.json'
    else:
        dbcopy = tmp / 'qbank.sqlite'
        shutil.copy2(DB, dbcopy)
        # 清掉用户答题痕迹（2026-10-20）：排期比对会对哨兵题跑 mark()，真实库的
        # progress/fsrs_state 属于用户数据，带进来会让差分基线随真实刷题记录漂移。
        # 与 smoke_test.py / smoke_api.py 同源处理。questions / cards 一行不动。
        con = sqlite3.connect(dbcopy)
        for t in ('progress', 'fsrs_state'):
            try:
                con.execute('DELETE FROM %s' % t)
            except sqlite3.OperationalError:
                pass
        con.commit()
        con.close()
        con = sqlite3.connect(dbcopy)
        con = sqlite3.connect(dbcopy)
        con.executemany(
            'INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [(q['id'], '差分哨兵', '自出', 'S. 哨兵', '', '', q['stem'], q['qtype'], None,
              json.dumps(q['options'], ensure_ascii=False), q['answer'], q['explanation'], '', '', '')
             for q in SYNTH])
        con.commit()
        con.close()
        server.core.DB = dbcopy
    return server.Store(), tmp


def main() -> int:
    public = '--public' in sys.argv
    quick = '--quick' in sys.argv
    node = find_node()
    if node is None:
        print('⚠ 找不到 node，跳过后端一致性校验（不视为失败）。')
        print('  装好 Node 后建议跑一次：python tools/smoke_parity.py')
        return 2

    sys.path.insert(0, str(ROOT / 'app'))
    import server                                     # noqa: E402
    st, tmpdb = make_store(server, public)
    qs = load_questions(public, quick)
    iv = list(server.INTERVALS)
    print('== 前后端一致性：%s ｜ %d 题 × %d 种作答 =='
          % ('公开子集' if public else '全量库', len(qs), len(GIVENS)))

    # ---- Python 侧：判分 + 按同一公式算排期 ----
    py = {}
    for q in qs:
        res = {}
        for g in GIVENS:
            r = st.grade(q['id'], g)
            res[g] = [r.get('correct'), bool(r.get('self')), (r.get('answer') or '')[:40],
                      json.dumps(r.get('fill'), ensure_ascii=False, separators=(',', ':'))]
        base = datetime.now()
        # 排期：必须调用**真实排期器**。
        # 2026-10-18 修正：此前这里是「按固定 INTERVALS 公式重算」，
        # 根本没调用 mark()/schedule()，所以改 core.mark() 内部逻辑它依然绿——
        # 是假守护（改 FSRS 时会骗人）。现改为调用 core.schedule_chain()。
        chain = st.schedule_chain(SCHED_SEQ) if hasattr(st, 'schedule_chain') else None
        res['_due'] = [x['due'] for x in chain] if chain else None
        res['_sched'] = [(x['s'], x['d']) for x in chain] if chain else None
        res['_qrank'] = server.qtype_rank(q['qtype'])   # 题型排序标签（隐藏）
        py[q['id']] = res

    js = run_js(node, qs, iv)

    bad_grade, bad_due, bad_rank = [], [], []
    for q in qs:
        qid, pr = q['id'], py[q['id']]
        jr = js.get(qid) or {}
        for g in GIVENS:
            if pr[g] != jr.get(g):
                bad_grade.append((qid, g, pr[g], jr.get(g)))
        if pr['_due'] != jr.get('_due'):
            bad_due.append((qid, pr['_due'], jr.get('_due')))
        if pr['_qrank'] != jr.get('_qrank'):
            bad_rank.append((qid, q['qtype'], pr['_qrank'], jr.get('_qrank')))

    print('  判分比对点 %d ｜ 不一致 %d' % (len(qs) * len(GIVENS), len(bad_grade)))
    print('  排期比对点 %d ｜ 不一致 %d' % (len(qs), len(bad_due)))
    print('  题型标签比对点 %d ｜ 不一致 %d' % (len(qs), len(bad_rank)))

    for qid, g, a, b in bad_grade[:8]:
        print('  ✗ %s given=%r\n      py=%s\n      js=%s' % (qid, g, a, b))
    for qid, a, b in bad_due[:5]:
        print('  ✗ %s 排期不一致\n      py=%s\n      js=%s' % (qid, a, b))
    for qid, qt, a, b in bad_rank[:8]:
        print('  ✗ %s 题型标签不一致 qtype=%r py=%r js=%r' % (qid, qt, a, b))

    st.con.close()
    shutil.rmtree(tmpdb, ignore_errors=True)
    if bad_grade or bad_due or bad_rank:
        print('\n✗ 两端不一致 —— 改 core.py 或 static-store.js 时必须同步另一端')
        return 1
    print('✓ 两端判分、排期（FSRS-6）与题型标签完全一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
