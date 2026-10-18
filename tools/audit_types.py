# -*- coding: utf-8 -*-
r"""
audit_types.py — 题库题型审查：按「结构证据」核对每道题的题型是否自洽，输出问题清单与建议题型。

结构证据（不依赖人工标注）：
  · 选项数 ≥2              → 选择题；答案字母数 >1 → 多选，否则 单选
  · 答案为 对/错/√/×/正确/错误 → 判断（且不应带 >2 个选项）
  · 无选项 + 答案长文本      → 主观题（问答/填空/计算/识记），保留原标注
  · 答案「待补」            → 信息缺失，不计为类型错误

用法：
  python tools/audit_types.py                 # 审 data/qbank.sqlite（全量）
  python tools/audit_types.py --public        # 审 data/public/qbank.json（公开子集）
  python tools/audit_types.py --report build/type-audit.md
退出码：有结构性问题 → 1；全部自洽 → 0
"""
from __future__ import annotations
import json, re, sqlite3, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data' / 'qbank.sqlite'
PUB = ROOT / 'data' / 'public' / 'qbank.json'
JUDGE_OK = {'对', '错', '正确', '错误', '√', '×', '✓', '✗', 't', 'f', 'true', 'false'}
CHOICE_KEYS = ('单选', '多选', '选择')
SUBJECTIVE = ('问答', '填空', '计算', '识记', '简答', '名词解释', '画图', '编程', '综合')


def load(public: bool):
    if public:
        data = json.loads(PUB.read_text(encoding='utf-8'))
        for q in data['questions']:
            q.setdefault('options', [])
        return data['questions']
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute('SELECT id,bank,source_class,stem,qtype,options,answer,explanation FROM questions').fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['options'] = json.loads(d.get('options') or '[]')
        except Exception:
            d['options'] = []
        out.append(d)
    return out


def answer_letters(ans: str) -> str:
    a = (ans or '').upper().replace('**', '')
    if re.fullmatch(r'[A-H](?:\s*[、,，/]?\s*[A-H])*', a.strip()):
        return re.sub(r'[^A-H]', '', a)
    return ''


def is_judge_answer(ans: str) -> bool:
    return (ans or '').strip().replace('**', '').lower() in JUDGE_OK


def recommended(q) -> tuple[str, str]:
    """返回 (建议题型, 依据)。只做结构性判断，主观题保留原标注。"""
    qt = (q.get('qtype') or '').strip()
    opts = q.get('options') or []
    ans = (q.get('answer') or '').strip()
    letters = answer_letters(ans)
    if len(opts) >= 2:
        if letters and len(letters) > 1:
            return '多选', '有 %d 个选项、答案 %s 多字母' % (len(opts), letters)
        return '单选', '有 %d 个选项' % len(opts)
    if is_judge_answer(ans):
        return '判断', '答案为「%s」' % ans
    if not ans or '待补' in ans:
        return qt or '未分级', '答案待补（信息缺失，非类型错误）'
    if any(k in qt for k in SUBJECTIVE):
        return qt, '主观题保留原标注'
    # 原标注与结构都不支持时给个保守建议
    if re.search(r'（\s*　*\s*）|（\s*）', q.get('stem') or ''):
        return '填空', '题干含空格且答案非对错'
    return '问答', '答案长文本'


def audit(questions, report_path: Path | None = None) -> int:
    issues, pending = [], []
    types = Counter()
    for q in questions:
        qt = (q.get('qtype') or '').strip()
        types[qt or '(空)'] += 1
        opts = q.get('options') or []
        ans = (q.get('answer') or '').strip()
        letters = answer_letters(ans)
        if not ans or '待补' in ans:
            pending.append(q['id'])
            continue
        rec, why = recommended(q)
        kind = None
        if '判断' in qt and not is_judge_answer(ans):
            kind = '判断但答案不是对/错'
        elif any(k in qt for k in CHOICE_KEYS) and len(opts) < 2:
            kind = '选择题但没有选项'
        elif not any(k in qt for k in CHOICE_KEYS) and not ('判断' in qt) and len(opts) >= 2:
            kind = '非选择题却带选项'
        elif '判断' in qt and len(opts) > 2:
            kind = '判断题带多个选项'
        elif '单选' in qt and letters and len(letters) > 1:
            kind = '单选但答案多字母'
        elif '多选' in qt and letters and len(letters) == 1:
            kind = '多选但答案单字母'
        elif letters and len(opts) < 2 and rec in ('单选', '多选'):
            # 只有「建议题型也是选择类」才算问题；图形题的答案恰好是字母（如「（ B ）」）
            # 但没有文字选项，本就该走自评，不是结构缺陷。
            kind = '字母答案但没有选项'
        elif qt not in (rec,) and any(k in qt for k in SUBJECTIVE) and rec in ('判断',):
            kind = '主观题标注但答案是判断'
        if kind:
            issues.append((q['id'], qt, rec, kind, why, ans[:28], len(opts)))

    by_kind = Counter(i[3] for i in issues)
    print('\n题数：%d ｜ 结构自洽：%d ｜ 有类型问题：%d ｜ 答案待补（跳过）：%d'
          % (len(questions), len(questions) - len(issues) - len(pending), len(issues), len(pending)))
    print('\n当前题型分布：')
    for k, v in types.most_common():
        print('  %-12s %5d' % (k, v))
    if issues:
        print('\n问题分类：')
        for k, v in by_kind.most_common():
            print('  %-22s %5d' % (k, v))
        print('\n样例（最多 12 条）：')
        for i in issues[:12]:
            print('  %s [现:%s → 建议:%s] %s ｜ ans=%r opts=%d' % (i[0], i[1], i[2], i[3], i[5], i[6]))

    if report_path:
        lines = ['# 题型审查报告', '',
                 '- 题数：**%d** ｜ 自洽：%d ｜ 有问题：**%d** ｜ 答案待补：%d'
                 % (len(questions), len(questions) - len(issues) - len(pending), len(issues), len(pending)),
                 '', '## 现有题型分布', '', '| 题型 | 题数 |', '|---|---:|']
        lines += ['| %s | %d |' % (k, v) for k, v in types.most_common()]
        if issues:
            lines += ['', '## 问题分类', '', '| 问题 | 题数 |', '|---|---:|']
            lines += ['| %s | %d |' % (k, v) for k, v in by_kind.most_common()]
            lines += ['', '## 明细（题号 / 现标注 → 建议 / 依据）', '', '| 题号 | 现标注 | 建议 | 问题 | 依据 | 答案 | 选项数 |',
                      '|---|---|---|---|---|---|---:|']
            lines += ['| %s | %s | %s | %s | %s | %s | %d |' % i for i in issues]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print('\n报告已写入 %s' % report_path)
    return 1 if issues else 0


def main() -> int:
    public = '--public' in sys.argv
    rp = None
    if '--report' in sys.argv:
        rp = Path(sys.argv[sys.argv.index('--report') + 1])
    qs = load(public)
    return audit(qs, rp)


if __name__ == '__main__':
    sys.exit(main())
