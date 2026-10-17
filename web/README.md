# mech-qbank 前端（Vue 3 + Element Plus）

刷题界面。两种运行形态：

| 形态 | 入口 | 数据通道 |
| --- | --- | --- |
| 在线模式 | `python app/api.py`（FastAPI，默认 <http://127.0.0.1:8640>） | `fetch /api/v1/...` |
| 离线模式 | 直接双击 `dist/index.html`，或 APK WebView 加载 | `public/qbank-data.js` 内联数据 + 纯前端 Store（localStorage） |

UI 零分叉：`src/api.js` 按 `location.protocol === 'file:'` 自动分流，视图层不关心差异。

## 目录

```
web/
├── index.html             vite 入口（file:// 时先注入 qbank-data.js）
├── public/qbank-data.js   公开子集数据（tools/make_webdata.py 生成，勿手改）
├── src/
│   ├── main.js            createApp + ElementPlus（zh-cn）+ hash 路由
│   ├── router.js          五视图：刷题/复习/错题/知识点/进度（hash 模式，file:// 可用）
│   ├── api.js             唯一数据通道：在线打 /api/v1，离线走静态 Store
│   ├── util.js            题型/难度元数据、判分配色助手
│   ├── store/
│   │   ├── session.js     刷题会话（跨视图共享：复习/错题/备注跳转续上）
│   │   ├── notes.js       备注缓存与增删
│   │   ├── meta.js        筛选项（题库/来源/题型/难度分布）
│   │   ├── bus.js         数据变更版本号（统计页据此刷新）
│   │   └── static-store.js  离线模式纯前端 Store（判分/间隔与服务端同源，Node 可直接 require 跑冒烟）
│   ├── components/        QuestionCard / NotePad / FilterBar / CheckinCard / NotesCard / ImportCard / ProgressCard
│   └── views/             Quiz / Review / Wrong / Cards / Stats
└── vite.config.js         base:'./' + 单 IIFE 包（file:// 下 ES module 会被 CORS 拦）
```

## 开发

```bash
cd web
npm install
npm run dev        # http://localhost:5173，/api 代理到 127.0.0.1:8640（先起 app/api.py）
```

## 构建

```bash
npm run build      # 产物 web/dist/（index.html + assets/ + qbank-data.js）
```

`web/dist` 随仓库提交：exe/APK 打包直接吃它，不要求构建机装 node。

## 两个容易踩的坑

1. **必须打单 IIFE 包**。`file://` 下 `<script type="module">` 被浏览器按跨源拦截，
   vite 默认的 ESM 产物双击打不开。`vite.config.js` 里 `format:'iife' + inlineDynamicImports:true`
   就是为了这个，别改成默认。
2. **`public/qbank-data.js` 必须存在**。离线模式靠它提供题目；重新生成：
   `python tools/qbank_build.py && python tools/make_webdata.py`（在仓库根目录跑）。
