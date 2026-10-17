# -*- coding: utf-8 -*-
r"""
smoke_parity.py — 前后端判分/排期一致性差分测试。

为什么存在
  app/core.py 的 Store.grade() 与 web/src/store/static-store.js 的 grade() 是两份手抄实现
  （Python + JavaScript，分别服务 http:// 服务端与 file:// APK 离线模式）。
  注释里写着「改动必须两边同步」——靠人记住的约束等于没有约束。
  本工具把这句话变成一条断言：任一端改了正则/集合/阈值，几秒内在这里炸出来。

测什么
  判分  全库题目 × 19 种作答取值，比对 correct / self / answer 三项
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

# 覆盖 判断（对/错/√/×/T/F/1/0）、单选、多选、空值、'A. 对' 形式的主要输入形态
GIVENS = ['A', 'B', 'C', 'D', 'AB', 'ABC', '对', '错', '正确', '错误',
          '√', '×', 'T', 'F', '1', '2', '0', '', 'A. 对']


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
    return out[:300] if quick else out


def run_js(node: str, questions: list, intervals: list) -> dict:
    """在 Node 里跑 web/src/store/static-store.js，返回 {qid: {given:[correct,self,answer], _due:[...]}}。"""
    tmp = Path(tempfile.mkdtemp(prefix='qbank-parity-'))
    try:
        (tmp / 'in.json').write_text(
            json.dumps({'questions': questions, 'givens': GIVENS, 'intervals': intervals},
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
                        String(r.answer || '').slice(0, 40)];
  }
  // 连答对 6 次（streak 1..6）取每次 due，再各答错/跳过一次
  var due = [];
  for (var k = 0; k < d.intervals.length; k++) due.push(s.mark(q.id, 'correct').due.slice(0, 10));
  due.push(s.mark(q.id, 'wrong').due.slice(0, 10));
  due.push(s.mark(q.id, 'skip').due.slice(0, 10));
  res._due = due;
  out[q.id] = res;
}
fs.writeFileSync(process.argv[4], JSON.stringify(out), 'utf-8');
''', encoding='utf-8')
        subprocess.run([node, str(tmp / 'run.js'), str(ROOT / 'web' / 'src' / 'store' / 'static-store.js'),
                        str(tmp / 'in.json'), str(tmp / 'out.json')], check=True)
        return json.loads((tmp / 'out.json').read_text(encoding='utf-8'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    st = server.Store()
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
            res[g] = [r.get('correct'), bool(r.get('self')), (r.get('answer') or '')[:40]]
        base = datetime.now()
        res['_due'] = [(base + timedelta(days=iv[min(k + 1, len(iv)) - 1])).strftime('%Y-%m-%d')
                       for k in range(len(iv))]
        res['_due'].append((base + timedelta(days=server.WRONG_BACK)).strftime('%Y-%m-%d'))
        res['_due'].append((base + timedelta(days=server.SKIP_BACK)).strftime('%Y-%m-%d'))
        py[q['id']] = res

    js = run_js(node, qs, iv)

    bad_grade, bad_due = [], []
    for q in qs:
        qid, pr = q['id'], py[q['id']]
        jr = js.get(qid) or {}
        for g in GIVENS:
            if pr[g] != jr.get(g):
                bad_grade.append((qid, g, pr[g], jr.get(g)))
        if pr['_due'] != jr.get('_due'):
            bad_due.append((qid, pr['_due'], jr.get('_due')))

    print('  判分比对点 %d ｜ 不一致 %d' % (len(qs) * len(GIVENS), len(bad_grade)))
    print('  排期比对点 %d ｜ 不一致 %d' % (len(qs), len(bad_due)))

    for qid, g, a, b in bad_grade[:8]:
        print('  ✗ %s given=%r\n      py=%s\n      js=%s' % (qid, g, a, b))
    for qid, a, b in bad_due[:5]:
        print('  ✗ %s 排期不一致\n      py=%s\n      js=%s' % (qid, a, b))

    st.con.close()
    if bad_grade or bad_due:
        print('\n✗ 两端不一致 —— 改 core.py 或 static-store.js 时必须同步另一端')
        return 1
    print('✓ 两端判分与排期完全一致（间隔 %s 天）' % '/'.join(str(x) for x in iv))
    return 0


if __name__ == '__main__':
    sys.exit(main())
