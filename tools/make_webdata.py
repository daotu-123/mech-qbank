# -*- coding: utf-8 -*-
r"""
make_webdata.py — 把公开子集压成 APK 可直接加载的 JS 数据文件（file:// 静态模式专用）

为什么需要它
  WebView 里 file:///android_asset/www/index.html 用 fetch 读本地 JSON 会被 CORS 拦
  （file 协议不支持跨源 XHR）→ 把公开子集内联成 <script src="qbank-data.js"> 加载的
  JS 变量 window.QBANK_DATA，配 web/qbank-static.js 即可完全离线刷题。

输入   data/public/qbank.json（tools/qbank_build.py 生成的公开子集）
输出   web/qbank-data.js（window.QBANK_DATA = {...}，约 200KB，随仓库提交）

安全（写盘前重跑三道闸的后两道，与构建器同源，不另造规则）
  闸一  来源分级白名单：source_class 必须属于 PUBLIC_CLASSES（自出/通用知识点）
  闸二  污染检测 clean()：题干/答案/题解等命中硬痕迹正则即拒绝生成
  附加  题号唯一性校验（APK 进度按题号存 localStorage，重复会互相覆盖）
  任一不过 → 退出码 1 并列出问题条目，绝不静默放行。
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from qbank_build import PUBLIC_CLASSES, clean as pub_clean  # noqa: E402  复用三道闸，防规则漂移

SRC = ROOT / 'data' / 'public' / 'qbank.json'
OUT = ROOT / 'web' / 'qbank-data.js'
PROG_KEY = 'mech-qbank-progress'   # 与 web/qbank-static.js 的 localStorage 键一致


def main() -> int:
    if not SRC.exists():
        print('找不到 %s：先跑 tools/qbank_build.py 生成公开子集' % SRC, file=sys.stderr)
        return 1
    try:
        pub = json.loads(SRC.read_text(encoding='utf-8'))
    except Exception as e:
        print('data/public/qbank.json 解析失败：%s' % e, file=sys.stderr)
        return 1
    questions, cards = pub.get('questions', []), pub.get('cards', [])

    # ---- 闸一：来源分级白名单 ----
    bad_cls = [q.get('id', '?') for q in questions if q.get('source_class') not in PUBLIC_CLASSES]
    # ---- 闸二：污染检测（题目五字段 + 卡片 章节/名称/正文/关联题号）----
    bad_q = [q.get('id', '?') for q in questions if not pub_clean(q)]
    bad_c = [c.get('name', '?') for c in cards
             if not pub_clean({'stem': '%s %s %s' % (c.get('name', ''), c.get('body', ''), c.get('topic', '')),
                               'section_path': c.get('chapter', ''),
                               'meta': ' '.join(str(x) for x in (c.get('qids') or []))})]
    if bad_cls or bad_q or bad_c:
        print('✗ 拒绝生成：公开子集未过三道闸', file=sys.stderr)
        if bad_cls:
            print('  分级不在白名单：%s' % ', '.join(map(str, bad_cls[:20])), file=sys.stderr)
        if bad_q:
            print('  题目带污染痕迹：%s' % ', '.join(map(str, bad_q[:20])), file=sys.stderr)
        if bad_c:
            print('  卡片带污染痕迹：%s' % ', '.join(map(str, bad_c[:20])), file=sys.stderr)
        return 1
    # ---- 附加：题号唯一（进度按 id 存 localStorage，重复会互相覆盖）----
    ids = [q.get('id') for q in questions]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        print('✗ 拒绝生成：题号重复 %s' % ', '.join(map(str, dup[:20])), file=sys.stderr)
        return 1

    payload = {
        'schema': 2,
        'license': pub.get('license', ''),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'generator': 'tools/make_webdata.py',
        'questions': [{k: q.get(k) for k in ('id', 'bank', 'section_path', 'cat_name', 'stem', 'qtype',
                                             'level', 'options', 'answer', 'explanation', 'source_class')}
                      for q in questions],
        'cards': [{k: c.get(k) for k in ('chapter', 'topic', 'name', 'body', 'qids', 'source_class')}
                  for c in cards],
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))   # 紧凑排版，APK 体积优先
    js = ('/* 由 tools/make_webdata.py 自动生成，请勿手改。\n'
          '   数据来源 data/public/qbank.json（公开子集：自出题 + 通用知识点，已过三道闸）；\n'
          '   APK 静态模式经 <script src="qbank-data.js"> 加载，绕过 file:// 的 fetch CORS 限制。 */\n'
          'window.QBANK_DATA = %s;\n' % body)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(js, encoding='utf-8')
    print('✓ 已生成 %s' % OUT)
    print('  题目 %d ｜ 卡片 %d ｜ %.1f KB ｜ 生成于 %s'
          % (len(payload['questions']), len(payload['cards']),
             OUT.stat().st_size / 1024, payload['generated_at']))
    print('  进度存储键：%s（localStorage，见 web/qbank-static.js）' % PROG_KEY)
    return 0


if __name__ == '__main__':
    sys.exit(main())
