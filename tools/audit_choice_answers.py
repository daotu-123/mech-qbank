#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选择题答案体检 / 修复助手（只读，不改库）。

背景：源库里有一批选择题的「参考答案」写的是**题解文本**而不是选项字母
（如 Q1726 答案「带的弹性滑动…」→ 应判 D「存在弹性滑动」），判分只能降级为自评。
本脚本把 answer 文本与 options 文本做匹配推字母，输出三档：

  A 强匹配   答案文本与某选项文本互相包含（去掉标点/空格后）→ 几乎可确定
  B 词面匹配  选项核心词被答案覆盖 / bigram 重叠最高且明显领先 → 需抽检
  C 待人工    答案是「[答案待补]」或匹配分过低 → 属于「答案缺失」，另一种活

用法：
  python tools/audit_choice_answers.py            # 控制台摘要
  python tools/audit_choice_answers.py --md out.md # 另写一份对照表
  python tools/audit_choice_answers.py --id Q1726  # 看单题推导细节
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))

PUNCT = '，。、；：？！“”‘’（）()【】[]《》〈〉…—-·.,;:?!\'"<>/\\|　 \t'


def norm(s: str) -> str:
    s = s or ''
    for ch in PUNCT:
        s = s.replace(ch, '')
    return s.strip().lower()


def bigrams(s: str) -> set:
    s = norm(s)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def overlap(a: str, b: str) -> float:
    """a 被 b 覆盖的比例（按 bigram，除数取 a 的长度）"""
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba)


def split_option(o: str) -> tuple[str, str]:
    """'A、轮太大' → ('A', '轮太大')"""
    m = re.match(r'^\s*([A-Ha-h])\s*[、.．:：)）]\s*(.*)$', o or '')
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return '', (o or '').strip()


def analyse(ans: str, options: list, qtype: str = '') -> dict:
    a_norm = norm(ans)
    opts = [split_option(o) for o in options]
    if not a_norm or '待补' in ans:
        return {'grade': 'C', 'letters': '', 'why': '答案待补/为空', 'scores': []}

    strong, weak = [], []
    for letter, text in opts:
        t_norm = norm(text)
        if not t_norm:
            continue
        # 互相包含 → 最强证据（但要注意答案里可能同时提到多个选项）
        if t_norm in a_norm or (len(a_norm) <= 12 and a_norm in t_norm):
            strong.append((1.0, letter, text))
        else:
            weak.append((overlap(text, ans), letter, text))

    multi = '多选' in (qtype or '')
    if len(strong) == 1:
        return {'grade': 'A', 'letters': strong[0][1], 'why': '选项文本与答案互相包含',
                'scores': [(round(s, 2), l, t) for s, l, t in strong + sorted(weak, reverse=True)]}
    if len(strong) > 1:
        letters = ''.join(sorted(l for _, l, _ in strong))
        if multi:   # 多选题命中多个选项是合理的
            return {'grade': 'A', 'letters': letters,
                    'why': '答案同时包含 %d 个选项文本（多选题）' % len(strong),
                    'scores': [(round(s, 2), l, t) for s, l, t in strong]}
        return {'grade': 'B', 'letters': letters,
                'why': '答案同时包含 %d 个选项文本，单选需人工确认' % len(strong),
                'scores': [(round(s, 2), l, t) for s, l, t in strong]}

    weak.sort(reverse=True)
    if not weak:
        return {'grade': 'C', 'letters': '', 'why': '没有可匹配的选项', 'scores': []}
    top = weak[0]
    second = weak[1][0] if len(weak) > 1 else 0.0
    if top[0] >= 0.6 and top[0] - second >= 0.25:
        grade, why = 'B', '选项核心词覆盖 %.0f%%（次高 %.0f%%）' % (top[0] * 100, second * 100)
    else:
        grade, why = 'C', '最高覆盖仅 %.0f%%（次高 %.0f%%），区分度不足' % (top[0] * 100, second * 100)
    return {'grade': grade, 'letters': top[1], 'why': why,
            'scores': [(round(s, 2), l, t) for s, l, t in weak]}


def main() -> int:
    import core
    store = core.Store()

    if '--id' in sys.argv:
        qid = sys.argv[sys.argv.index('--id') + 1]
        rows = store.q('SELECT * FROM questions WHERE id=?', (qid,))
        if not rows:
            print('没有这道题')
            return 1
        r = rows[0]
        options = json.loads(r['options'] or '[]')
        print('题号', r['id'], '｜题型', r['qtype'])
        print('题干', r['stem'])
        for o in options:
            print('   ', o)
        print('现答案', r['answer'])
        print('题解  ', (r['explanation'] or '')[:120])
        print('推导  ', analyse(r['answer'] or '', options, r['qtype'] or ''))
        return 0

    rows = store.q("SELECT * FROM questions WHERE qtype LIKE '%单选%' "
                   "OR qtype LIKE '%多选%' ORDER BY id")
    buckets = {'A': [], 'B': [], 'C': []}
    for r in rows:
        options = json.loads(r['options'] or '[]')
        a = (r['answer'] or '').strip()
        # 已经是纯字母（含 A、B、AC 这类）→ 跳过，不算问题
        if re.fullmatch(r'[A-Ha-h][、,，\s]*([A-Ha-h][、,，\s]*)*', a):
            continue
        res = analyse(a, options, r["qtype"] or "")
        buckets[res['grade']].append((r, res))

    total = sum(len(v) for v in buckets.values())
    print('== 选择题答案体检（需要修的一共 %d 条）==' % total)
    for g, name in (('A', '强匹配（可直接改）'), ('B', '词面匹配（需抽检）'),
                    ('C', '待人工（多半是答案缺失）')):
        print('  %s %-18s %3d' % (g, name, len(buckets[g])))

    if '--md' in sys.argv:
        out = Path(sys.argv[sys.argv.index('--md') + 1])
        lines = ['# 选择题答案对照表', '',
                 '由 `tools/audit_choice_answers.py` 生成。A 档为答案文本与某选项互相包含，'
                 'B 档为词面重叠推断（建议抽检），C 档多半是「答案待补」，推不出字母。', '',
                 '| 档 | 题号 | 题型 | 题干 | 现答案 | 拟修正 | 依据 |',
                 '|---|---|---|---|---|---|---|']
        for g in ('A', 'B', 'C'):
            for r, res in sorted(buckets[g], key=lambda x: x[0]['id']):
                stem = (r['stem'] or '').replace('|', '／')[:38]
                cur = (r['answer'] or '').replace('|', '／')[:30]
                lines.append('| %s | %s | %s | %s | %s | %s | %s |'
                             % (g, r['id'], r['qtype'], stem, cur,
                                res['letters'] or '—', res['why']))
        out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print('  对照表已写入', out)

    for g in ('A', 'B'):
        for r, res in sorted(buckets[g], key=lambda x: x[0]['id'])[:8]:
            print('  %s %s %s → %s ｜ %s' % (g, r['id'], r['qtype'], res['letters'], res['why']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
