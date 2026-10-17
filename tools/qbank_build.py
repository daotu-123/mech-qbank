# -*- coding: utf-8 -*-
r"""
mech-qbank 数据构建器 v2：把源题库目录里的四份权威文件淬成 App 可直接读的 SQLite + 公开子集。

源目录（只读，绝不改写源文件）由环境变量 QBANK_SRC 指定，默认 ./源材料
  1. <源>\机械面试题库-总库.md        → 主库（唯一权威）
  2. <源>\学习题库\<科>\<科>-题库.md  → 学习库（明确标注"非公司真题"）
  3. <源>\分类索引.json               → 题号→类别/难度（辅助；ans 字段已过时，以正文为准）
  4. <源>\职业知识点总纲.md           → 通用知识点卡片

真实格式要点（2026-10-10 实测）
  · 题块：`**Q1147. 题干（　）【判断】**〔L2〕（备注）` 后跟选项行与答案行
  · 区标题级别不统一（`# A. …` / `## F …`），子区 `## A1 …` / `### <公司名>（N 题）`
    → 必须用**标题栈**拼出完整路径再分级，只看最近一个标题会错判
  · 答案写法：`- 答案：C` 与 `- 参考答案：…`（主力）；题解 `- 参考要点：`；另有 `- 来源：` `- 收录：`
  · 题型标记 `【判断】/【单选】/【多选】/【问答】` 部分缺失 → 按选项/括号推断

输出
  data\qbank.sqlite      全量库（本地用：含公司真题）
  data\public\qbank.json 公开子集（只含自出题 + 通用知识点）
  BUILD_REPORT.md        构建报告（已 gitignore）
"""
from __future__ import annotations
import json, os, re, sqlite3, sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'; DATA.mkdir(parents=True, exist_ok=True)
PUB = DATA / 'public'; PUB.mkdir(parents=True, exist_ok=True)

def _resolve_src() -> Path:
    """源题库目录：QBANK_SRC 环境变量 > tools/qbank_src.txt（本机私有）> ./源材料。
    不在此处报错（make_webdata.py 会 import 本模块）。"""
    env = os.environ.get('QBANK_SRC', '').strip().strip('"').strip("'")
    if env:
        return Path(env).expanduser()
    loc = Path(__file__).resolve().parent / 'qbank_src.txt'
    try:
        line = loc.read_text(encoding='utf-8').strip().strip('"').strip("'")
        if line and not line.startswith('#'):
            return Path(line).expanduser()
    except OSError:
        pass
    for cand in (ROOT / '源材料', Path.cwd() / '源材料'):
        if cand.is_dir():
            return cand
    return ROOT / '源材料'

F = _resolve_src()

# 公司名等第三方标记属于个人题库的私有信息，不写进开源代码；
# 如需要，放到 data/private/taint-extra.txt（每行一个词，已被 .gitignore 排除）。
def _load_taint_extra() -> list:
    p = ROOT / 'data' / 'private' / 'taint-extra.txt'
    try:
        lines = p.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    return [w.strip() for w in lines if w.strip() and not w.startswith('#')]

TAINT_EXTRA = _load_taint_extra()
_EX = ('|' + '|'.join(re.escape(w) for w in TAINT_EXTRA)) if TAINT_EXTRA else ''

# ---------------- 来源分级（按完整标题路径） ----------------
def classify(path: str) -> str:
    """按完整标题路径分级。注意：文档大标题（# 🎯 机械面试题库 · 总库…）含"面试题"字样，
    必须先剥掉，否则整库会被误判为公司真题（2026-10-10 踩过）。"""
    p = re.sub(r'^[^›]*总库[^›]*›?\s*', '', path or '')
    if re.search(r'学习库|课程|教材|课件|笔记', p): return '自出'
    if re.search(r'公司真题|真题包|资格考' + _EX, p): return '公司真题'
    if re.search(r'网络搜集|网络真题|牛客|CSDN|面经', p): return '网络搜集'
    if re.search(r'种子题库|每日题组|HR 通识|应届通识|通识|回填区', p): return '自出'
    return '待定'

PUBLIC_CLASSES = {'自出', '通用知识点'}

# ---------------- 公开/私有三道闸之二：污染检测（模块级，make_webdata.py 复用） ----------------
# 2026-10-10：初次按"分级=自出"直接公开，实测 233 题里 187 条带第三方痕迹
# （来源 URL、"变体自 C 区"、混在 H 区的课件题）→ 白名单 + 污染检测，宁可少放。
TAINT = re.compile(r'https?://|网络搜集|网络真题|牛客|CSDN|知乎|搜狐|面经|真题包|笔试题|面试题|考试试题|'
                   r'招聘笔试|原卷|投喂|培训机构|变体自|(?<!非)公司真题|(?<!非)公司笔试' + _EX)
TAINT_SECTION = re.compile(r'网络搜集|网络真题|公司真题包|资格考' + _EX)

def clean(q) -> bool:
    """公开前最后一道闸：题干/答案/题解/来源等任一字段命中硬痕迹即不公开。
    对知识点卡片可传 {'stem': 名称+正文, 'section_path': 章节, 'meta': 关联题号} 形状的字典复用。"""
    blob = ' '.join(str(q.get(k, '')) for k in ('stem', 'answer', 'explanation', 'source', 'note', 'meta'))
    return not (TAINT.search(blob) or TAINT_SECTION.search(q.get('section_path') or ''))

# ---------------- 解析 ----------------
HEAD = re.compile(r'^(#{1,6})\s+(?P<name>.+?)\s*$')
QID = re.compile(r'^\*\*(?P<id>[A-Z]?\d{3,5})[.、]\s*(?P<body>.*?)\*\*(?P<meta>.*)$')
TYPE = re.compile(r'【(?P<t>[^】]{1,12})】')
LV = re.compile(r'〔L(?P<lv>[1-5])〕')
RE_ANS = re.compile(r'^-\s*(?:参考)?答案[：:]\s*(?P<a>.*)$')
RE_EXP = re.compile(r'^-\s*(?:参考要点|解析|说明)[：:]\s*(?P<a>.*)$')
RE_SRC = re.compile(r'^-\s*(?:来源|依据|收录)[：:]\s*(?P<a>.*)$')
RE_OPT = re.compile(r'^\s*-\s*(?P<o>[A-H][.、．]\s*.*)$')
RE_OPT2 = re.compile(r'^\s*(?P<o>[A-H][.、．]\s+\S.*)$')
RE_CARD = re.compile(r'^\s*-\s*\[[ xX]\]\s*\*\*(?P<name>[^*]+?)\*\*\s*(?:—|–|-{1,2})?\s*(?P<body>.*)$')

def infer_type(stem: str, options: list[str], meta: str) -> str:
    # 先看疑问词：`…有哪些（　）` 是问答，不是判断（2026-10-10 踩过 Q2085 被误判成判断题）
    if re.search(r'哪些|什么|为什么|如何|怎样|简述|说明|请|计算|区别|比较|列举', stem):
        return '问答' if not options else '单选'
    if re.search(r'（\s*　*\s*）|（\s*）', stem):
        return '判断' if len(options) <= 2 else '单选'
    if len(options) >= 2:
        return '多选' if '多选' in meta else '单选'
    if re.search(r'[？?]$|请|说明|简述|为什么|如何|计算', stem):
        return '问答'
    return '识记'

def split_options(line: str) -> list[str]:
    """选项常挤在一行：`- A. 溢流阀　B. 精密调压阀　C. 节流阀　D. 单向阀`
    也可能每个选项一行 → 统一按 A/B/C… 标记切开（2026-10-10 实测 504 行是挤在一行的）。
    兼容小写字母（c．）、无空格（D.BS）与全角句号，字母统一大写归一（2026-10-16 补）。"""
    s = re.sub(r'^\s*-+\s*', '', line.strip())
    parts = re.split(r'\s*(?=[A-Ha-h][.、．]\s*)', s)
    out = []
    for p in parts:
        p = p.strip(' 　')
        if p and re.match(r'^[A-Ha-h][.、．]', p):
            out.append(re.sub(r'^([A-Ha-h])(?=[.、．])', lambda m: m.group(1).upper(), p))
    return out or ([s] if s else [])


def norm_answer(a: str, choice: bool = False) -> str:
    """把 `**A**（单选·标准答案…）`、`答案：（√） 正确：大` 这类原文归一成可判分的 `对`/`错`/`A`/`AB`；
    问答类原样返回。choice=True（选择题/判断题）时额外抽取开头的连续字母串，
    原文答案由调用方挪进题解保留，信息不丢。"""
    if not a: return ''
    s = a.strip().replace('**', '').replace('__', '').strip()
    if re.search(r'[（(]\s*[√✓对]\s*[）)]', s) or re.match(r'^[√✓对]', s): return '对'
    if re.search(r'[（(]\s*[×✗xX错]\s*[）)]', s) or re.match(r'^[×✗错]', s): return '错'
    m = re.search(r'(?:答案|参考答案)[：:]\s*([A-H](?:\s*[、,，]?\s*[A-H])*)', s)
    if m: return m.group(1).replace(' ', '').replace('，', ',')
    if re.fullmatch(r'[A-H](?:\s*[、,，]?\s*[A-H])*', s): return s.replace(' ', '')
    if choice:
        # `A（单选·标准答案…）` / `B：直流24伏…` / `A（S136）最贵…` → 取开头连续字母串
        m = re.match(r'^([A-H](?:\s*[、,，/]?\s*[A-H])*)\s*(?:[（(：:．.]|$)', s)
        if m: return re.sub(r'[、,，/\s]', '', m.group(1))
    return s


def parse_bank(path: Path, bank: str) -> list[dict]:
    lines = path.read_text(encoding='utf-8', errors='replace').split('\n')
    items, stack, cur = [], {}, None
    for raw in lines:
        line = raw.rstrip('\r')
        h = HEAD.match(line)
        if h:
            lv = len(h.group(1)); stack[lv] = h.group('name')
            for k in list(stack):
                if k > lv: del stack[k]
            continue
        q = QID.match(line)
        if q:
            if cur: items.append(cur)
            body, meta = q.group('body'), q.group('meta')
            tm = TYPE.search(body) or TYPE.search(meta)
            lm = LV.search(meta) or LV.search(body)
            stem = TYPE.sub('', body).strip()
            cur = {'id': q.group('id'), 'bank': bank,
                   'section_path': ' › '.join(stack[k] for k in sorted(stack)),
                   'section': stack.get(max(stack) if stack else 0, ''),
                   'stem': stem, 'qtype_mark': tm.group('t') if tm else '',
                   'level': int(lm.group('lv')) if lm else None,
                   'meta': (meta or '').strip(), 'options': [],
                   'answer': '', 'explanation': '', 'source': '', 'note': ''}
            continue
        if cur is None: continue
        s = line.strip()
        if not s: continue
        m = RE_ANS.match(s)
        if m: cur['answer'] = (cur['answer'] + ' ' + m.group('a')).strip(); continue
        m = RE_EXP.match(s)
        if m: cur['explanation'] = (cur['explanation'] + ' ' + m.group('a')).strip(); continue
        m = RE_SRC.match(s)
        if m: cur['source'] = (cur['source'] + ' ' + m.group('a')).strip(); continue
        m = RE_OPT.match(s) or RE_OPT2.match(s)
        if m:
            cur['options'] += split_options(s); continue
        m = re.match(r'^-\s*选项[：:]\s*(.+)$', s)     # `- 选项：A．… B．…`（《答案及评分参考》区主力格式）
        if m:
            cur['options'] += split_options(m.group(1)); continue
        m = re.match(r'^-\s*分类[：:]\s*(.+)$', s)
        if m: cur['cat_line'] = m.group(1).strip(); continue
        if s.startswith('>') or s.startswith('<!--'):
            cur['note'] = (cur['note'] + ' ' + s.lstrip('> ').strip()).strip(); continue
        if cur['answer']:
            cur['explanation'] = (cur['explanation'] + ' ' + s).strip()
        else:
            cur['note'] = (cur['note'] + ' ' + s).strip()
    if cur: items.append(cur)
    KNOWN = ('判断', '单选', '多选', '问答', '填空', '计算', '识记', '简答', '名词解释', '画图', '编程', '选择', '综合')
    for q in items:
        mark = q['qtype_mark']
        # 【公司真题】【培训机构套卷】这类是**卷别标记**不是题型 → 不能当 qtype（2026-10-10 踩过）
        if mark and any(k in mark for k in KNOWN):
            q['qtype'] = mark
        else:
            q['qtype'] = infer_type(q['stem'], q['options'], q['meta'])
            q['paper_tag'] = mark
        # 有选项就一定是选择题，别停在"识记"（先定题型，答案归一依赖它）
        if q['options'] and q['qtype'] in ('识记', '问答', ''):
            letters = set(re.findall(r'[A-H]', (q['answer'] or '').upper()))
            q['qtype'] = '多选' if len(letters) > 1 else '单选'
        # 答案归一：`**A**（单选·标准答案…）`→A、`**√。减压阀…`→对；原文挪进题解保留
        raw = q['answer']
        is_choice = bool(q['options']) or bool(re.search(r'单选|多选|选择|判断|选型', q['qtype'] or ''))
        q['answer'] = norm_answer(raw, is_choice)
        if raw and q['answer'] != raw:
            q['explanation'] = (('原文答案：' + raw + '　') + q['explanation']).strip()
        # 判断+选项+字母答案 = 被误标的单选/多选（《答案及评分参考》区整段如此），纠正题型
        if q['options'] and q['qtype'] == '判断' and re.fullmatch(r'[A-H][A-H、,，\s]{0,7}', q['answer'] or ''):
            q['qtype'] = '多选' if len(set(re.findall(r'[A-H]', q['answer']))) > 1 else '单选'
        if q.get('cat_line'):
            q['cat_name'] = q['cat_line']
        # 答案兜底：正文/备注/题解里内嵌的「答案：X」
        if not q['answer'] or '待补' in q['answer']:
            for blob in (q['stem'], q['meta'], q['note'], q['explanation']):
                m = re.search(r'答案[：:]\s*([A-H](?:\s*[、,，]?\s*[A-H])*|对|错|√|×|正确|错误)', blob or '')
                if m:
                    q['answer'] = m.group(1).strip()
                    break
    return items

def parse_cards(path: Path) -> list[dict]:
    cards, chapter, topic = [], '', ''
    for raw in path.read_text(encoding='utf-8', errors='replace').split('\n'):
        line = raw.rstrip('\r')
        h = re.match(r'^(#{2,3})\s+(.+?)\s*$', line)
        if h:
            if len(h.group(1)) == 2: chapter, topic = h.group(2), ''
            else: topic = h.group(2)
            continue
        c = RE_CARD.match(line)
        if c:
            body = c.group('body').strip()
            cards.append({'chapter': chapter, 'topic': topic, 'name': c.group('name').strip(),
                          'body': re.sub(r'`Q\d{3,5}`', '', body).strip(),
                          'qids': re.findall(r'`(Q\d{3,5})`', body)})
    return cards

def main() -> int:
    if not F.is_dir():
        print('未找到源题库目录：%s\n'
              '请设置环境变量 QBANK_SRC 指向题库根目录（需含 机械面试题库-总库.md / 学习题库 / 分类索引.json），'
              '或把源文件放进 源材料/ 下。' % F, file=sys.stderr)
        return 1
    problems = []
    main_bank = F / '机械面试题库-总库.md'
    qs = parse_bank(main_bank, '主库') if main_bank.exists() else []
    learn = []
    ldir = F / '学习题库'
    if ldir.exists():
        for p in sorted(ldir.rglob('*-题库.md')):
            learn += parse_bank(p, '学习库/' + p.parent.name)
    for q in qs:
        q['source_class'] = classify(q['section_path'])
    for q in learn:
        q['source_class'] = '自出'
    cards = parse_cards(F / '职业知识点总纲.md') if (F / '职业知识点总纲.md').exists() else []
    for c in cards: c['source_class'] = '通用知识点'
    cat = {}
    ip = F / '分类索引.json'
    if ip.exists():
        try: cat = json.loads(ip.read_text(encoding='utf-8'))
        except Exception as e: problems.append(f'分类索引.json 解析失败：{e}')
    for q in qs + learn:
        c = cat.get(q['id']) or {}
        q['cat_name'] = c.get('name', '')
        if q['level'] is None and isinstance(c.get('lv'), int): q['level'] = c['lv']
        q['index_ans_flag'] = c.get('ans')
    # 校验
    dup = [k for k, v in Counter(q['id'] for q in qs).items() if v > 1]
    if dup: problems.append('主库重复题号：%s' % dup[:10])
    no_ans = [q['id'] for q in qs if not q['answer'] or '待补' in q['answer']]
    no_lv = [q['id'] for q in qs if not q['level']]
    stale = sum(1 for q in qs if q['index_ans_flag'] is False and q['answer'] and '待补' not in q['answer'])
    if stale: problems.append(f'分类索引 ans=false 但正文有答案：{stale} 条（索引过时）')
    # SQLite
    db = DATA / 'qbank.sqlite'
    tmp = db.with_suffix('.sqlite.new')          # 先写临时文件：App 开着（句柄被占）也能重建
    if tmp.exists(): tmp.unlink()
    # 迁移旧库学习进度：重建不丢刷题记录（旧库被 App 占用读不到时跳过并记录待办）
    old_prog = []
    if db.exists():
        try:
            oc = sqlite3.connect(db)
            old_prog = oc.execute('SELECT id,state,streak,wrong,due,last_seen FROM progress').fetchall()
            oc.close()
        except Exception as e:
            problems.append(f'旧库进度读取失败（未迁移）：{e}')
    con = sqlite3.connect(tmp)
    con.executescript('''
      CREATE TABLE questions(
        id TEXT PRIMARY KEY, bank TEXT, source_class TEXT, section_path TEXT, section TEXT,
        cat_name TEXT, stem TEXT, qtype TEXT, level INTEGER, options TEXT,
        answer TEXT, explanation TEXT, source TEXT, note TEXT, meta TEXT);
      CREATE INDEX ix_q_class ON questions(source_class);
      CREATE INDEX ix_q_level ON questions(level);
      CREATE TABLE cards(
        cid INTEGER PRIMARY KEY AUTOINCREMENT, chapter TEXT, topic TEXT, name TEXT,
        body TEXT, qids TEXT, source_class TEXT);
      CREATE TABLE progress(
        id TEXT PRIMARY KEY, state TEXT DEFAULT 'new', streak INTEGER DEFAULT 0,
        wrong INTEGER DEFAULT 0, due TEXT, last_seen TEXT);
    ''')
    allq = qs + learn
    if old_prog:
        con.executemany('INSERT OR REPLACE INTO progress(id,state,streak,wrong,due,last_seen) VALUES(?,?,?,?,?,?)', old_prog)
        print('已迁移旧库学习进度 %d 条' % len(old_prog))
    con.executemany('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(q['id'], q['bank'], q['source_class'], q['section_path'], q['section'], q.get('cat_name',''),
          q['stem'], q['qtype'], q['level'], json.dumps(q['options'], ensure_ascii=False),
          q['answer'], q['explanation'], q['source'], q['note'], q['meta']) for q in allq])
    con.executemany('INSERT INTO cards(chapter,topic,name,body,qids,source_class) VALUES(?,?,?,?,?,?)',
        [(c['chapter'], c['topic'], c['name'], c['body'], json.dumps(c['qids']), c['source_class']) for c in cards])
    con.commit()
    # ---- 公开子集：只放"干净的自出题 + 通用知识点" ----
    # 三道闸：分级白名单 PUBLIC_CLASSES + 污染检测 clean()（均在模块级，make_webdata.py 同源复用）
    pub_q = [q for q in allq if q['source_class'] in PUBLIC_CLASSES and clean(q)]
    excluded = [q['id'] for q in allq if q['source_class'] in PUBLIC_CLASSES and not clean(q)]
    # 公开题一律不带来源 URL / 备注；section_path 已过 clean() 的章节污染检查，可带（供 App 按来源筛选）
    pub_export = [{k: q[k] for k in ('id', 'bank', 'section_path', 'cat_name', 'stem', 'qtype', 'level',
                                     'options', 'answer', 'explanation', 'source_class')} for q in pub_q]
    (PUB / 'qbank.json').write_text(json.dumps({
        'schema': 1, 'license': 'CC-BY-SA-4.0 (content) / MIT (code)',
        'questions': pub_export,
        'cards': cards}, ensure_ascii=False, indent=1), encoding='utf-8')
    con.close()
    try:
        tmp.replace(db)          # 原子替换
        print('库已更新：%s（%.2f MB）' % (db, db.stat().st_size/1024/1024))
    except PermissionError:
        print('⚠ %s 正被占用（App 还开着？）→ 新库已留在 %s，关掉 App 后改名即可' % (db.name, tmp.name))
    byclass = Counter(q['source_class'] for q in allq)
    lines = ['# 构建报告（mech-qbank）', '',
             f'- 源：`{F}`（**只读**，未改写任何源文件）',
             f'- 主库 **{len(qs)}** 题 ｜ 学习库 **{len(learn)}** 题 ｜ 合计 **{len(allq)}**',
             f'- 知识点卡片 **{len(cards)}**',
             f'- 迁移旧库学习进度 **{len(old_prog)}** 条', '', '## 来源分级（决定能否公开）', '',
             '| 来源分级 | 题数 | 公开 |', '|---|---:|---|']
    for k, v in byclass.most_common():
        lines.append(f'| {k} | {v} | {"✅" if k in PUBLIC_CLASSES else "⛔ 仅本地"} |')
    lines += ['', f'**公开子集：{len(pub_q)} 题 + {len(cards)} 卡** → `data/public/qbank.json`',
              f'- 因带第三方痕迹被拦下（不公开）：**{len(excluded)}** 题'
              + (f'（{", ".join(excluded[:12])}…）' if excluded else ' ✓'), '',
              '## 质量校验', '',
              f'- 有答案：**{len(allq) - len(no_ans)} / {len(allq)}**（{100*(len(allq)-len(no_ans))/max(1,len(allq)):.1f}%）',
              f'- 答案缺失/待补：**{len(no_ans)}** 条' + (f'（{", ".join(no_ans[:12])}…）' if no_ans else ' ✓'),
              f'- 缺难度：**{len(no_lv)}** 条' + (f'（{", ".join(no_lv[:10])}…）' if no_lv else ' ✓'),
              f'- 重复题号：**{len(dup)}** ✓' if not dup else f'- 重复题号：{dup}',
              f'- 难度分布：{dict(sorted(Counter(q["level"] for q in allq if q["level"]).items()))}',
              f'- 题型分布：{dict(Counter(q["qtype"] for q in allq).most_common(8))}',
              f'- 带题解：{sum(1 for q in allq if q["explanation"])} 条 ｜ 带来源：{sum(1 for q in allq if q["source"])} 条']
    if problems: lines += ['', '## 需人工复核', ''] + [f'- {p}' for p in problems]
    (ROOT / 'BUILD_REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))
    return 0

if __name__ == '__main__':
    sys.exit(main())
