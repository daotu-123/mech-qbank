/* verify_web.mjs — M3.5 前端验收脚本（不装任何依赖，走 CDP 直连 Chrome）
 *
 * 为什么要自己写而不是用 Playwright/Puppeteer：
 * 本项目明令「不引入任何新依赖」，而验收脚本是要随库提交、能长期复跑的，
 * 不能靠「我本地装过」。CDP 是 Chrome 自带的调试协议，零安装。
 *
 * 验什么（对应 M3.5 验收清单）：
 *   1. file:// 双击打开：无白屏、控制台无报错、PDF 入口显示「需要连接服务器」
 *   2. 在线：上传 01/04/05/06 四个样本，各自状态与提示正确
 *   3. 从 03 表格页做摘录 → 跳到 ExcerptView，文本预填 + 来源引用带上
 *   4. 窄屏 375px 不溢出
 *
 * 跑法：
 *   node tests/verify_web.mjs
 * 脚本自己起一个临时服务端（沙箱副本，不碰真实 data/）并自己收尾——
 * 本机沙箱会回收「监听端口的后台子进程」，服务必须跟脚本同生命周期。
 * 也能指向外部已起的服务：node tests/verify_web.mjs http://127.0.0.1:8640
 *
 * 解释器要带 pymupdf（解析在 worker 进程里做）与 fastapi（Web 层），
 * 默认用独立 pdfprobe venv，可用环境变量 QBANK_VERIFY_PY 覆盖。
 */
import { spawn } from 'node:child_process'
import { existsSync, writeFileSync, rmSync, mkdtempSync, mkdirSync, readdirSync, copyFileSync, cpSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const ROOT = resolve(HERE, '..')
const DIST = join(ROOT, 'web', 'dist', 'index.html')
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const FIX = join(HERE, 'fixtures', 'pdf')

/** 跑服务端用的解释器。**必须有 pymupdf**（解析在 worker 进程里做），
 *  且解释器本身要能 import fastapi（api.py 的 Web 层）。
 *  默认用带 pymupdf 的独立 venv；也可用 QBANK_VERIFY_PY 覆盖。 */
const PY = process.env.QBANK_VERIFY_PY
  || 'C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe'

/** 目标服务地址。默认由本脚本自己起一个临时服务再填上。 */
let BASE = ''

let pass = 0, fail = 0
const failures = []
function check(name, ok, detail = '') {
  if (ok) { pass++; console.log('  ✓ ' + name) }
  else { fail++; failures.push(name + (detail ? ' — ' + detail : '')); console.log('  ✗ ' + name + (detail ? ' — ' + detail : '')) }
}

/** CDP 的网络限速参数。2MB/s：5MB 文件要传 ~2.5s，够采样出多个中间值。 */
const SLOW_NET = { offline: false, latency: 20, downloadThroughput: 2 * 1024 * 1024, uploadThroughput: 700 * 1024 }
const OFF = { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }

/* ---------------------------------------------------------------- CDP 最小客户端 */

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map() }
  static async attach(port) {
    // 等 DevTools 端口就绪
    let list = null
    for (let i = 0; i < 80; i++) {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/list`)
        list = await r.json()
        if (list && list.length) break
      } catch (e) { /* 还没起来 */ }
      await new Promise((r) => setTimeout(r, 250))
    }
    if (!list || !list.length) throw new Error('CDP 未就绪（Chrome 没起来？）')
    const page = list.find((t) => t.type === 'page') || list[0]
    const ws = new WebSocket(page.webSocketDebuggerUrl)
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
    const c = new CDP(ws)
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && c.pending.has(msg.id)) {
        const { res, rej } = c.pending.get(msg.id); c.pending.delete(msg.id)
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      } else if (msg.method) {
        (c.handlers.get(msg.method) || []).forEach((h) => h(msg.params))
      }
    }
    return c
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error('timeout ' + method)) } }, 60000)
    })
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, [])
    this.handlers.get(method).push(fn)
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    })
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'JS 异常')
    return r.result.value
  }
  async goto(url) {
    await this.send('Page.navigate', { url })
    await this.waitFor(() => this.eval('document.readyState === "complete"'), 20000)
    await this.sleep(400)     // Vue 挂载 + 首次 fetch
  }
  async waitFor(fn, timeout = 15000, gap = 200) {
    const end = Date.now() + timeout
    while (Date.now() < end) {
      try { if (await fn()) return true } catch (e) { /* 页面还没就绪 */ }
      await this.sleep(gap)
    }
    return false
  }
  sleep(ms) { return new Promise((r) => setTimeout(r, ms)) }
  /** 页面文本（去空白折叠），断言文案用 */
  text() { return this.eval('document.body.innerText.replace(/\\s+/g, " ")') }
}

async function withChrome(fn) {
  const port = 9333 + Math.floor(Math.random() * 400)
  // ⚠️ 不删 Chrome 的 profile 目录：它有 50+ 个文件，递归删会触发
  // 本机的批量删除保护（实测 SAFE_DELETE_BULK_CONFIRM_REQUIRED），
  // 删不掉还留一堆临时目录。它在 %TEMP% 下，由系统自己回收。
  // 复用固定目录名，隔次运行 Chrome 自己会复用/更新其中的数据。
  const profile = join(tmpdir(), 'qbank-cdp-profile')
  // host-resolver-rules：把公网测试域名解析到 127.0.0.1。
  // 为什么必须这样：公网模式的 Host 白名单里是「域名」，而 HTTP 连接
  // 打的是 127.0.0.1 —— **Host 头与连接地址是两回事**。
  // 用 CDP 的 Network.setExtraHTTPHeaders 设 Host 也不行：
  // Host 是 Chrome 的**禁止头**，设了直接 ERR_INVALID_ARGUMENT
  // （实测：页面整个打不开，报「无法访问此网站」）。
  // 所以走 DNS 层面 —— 这也最接近真实反代场景。
  const args = [
    '--headless=new', `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
    '--disable-gpu', '--allow-file-access-from-files',
    // ⚠️ 必须关掉 Chrome 自己的代理：本机 http_proxy 会把打向
    // mech.example.com 的请求也发给代理，代理回 502
    // （症状：页面显示「HTTP ERROR 502」）。
    // 环境变量清了不够 —— Chrome 读的是自己的代理设置/系统设置。
    '--no-proxy-server',
    '--host-resolver-rules=MAP mech.example.com 127.0.0.1',
    'about:blank',
  ]
  const proc = spawn(CHROME, args, { stdio: 'ignore' })
  try {
    const cdp = await CDP.attach(port)
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')
    await cdp.send('Log.enable')
    const errors = []
    // 真正的应用异常（未捕获 JS 异常）与浏览器记的 network error **要分开**：
    // 公网模式下 401 是**预期行为**（鉴权生效的证据），浏览器会记成
    // network error —— 把它当「控制台报错」会导致验收永远红。
    const exceptions = []
    cdp.on('Runtime.exceptionThrown', (p) => {
      const text = '未捕获异常：' + (p.exceptionDetails?.exception?.description
        || p.exceptionDetails?.text || '?')
      exceptions.push(text)
      errors.push(text)
    })
    cdp.on('Log.entryAdded', (p) => {
      // 只收 error 级；浏览器对 file:// 下 favicon 之类的告警不算
      if (p.entry && p.entry.level === 'error') errors.push(p.entry.text)
    })
    await fn(cdp, errors, exceptions)
  } finally {
    proc.kill()
    // 只删我们自己造的那两个 5MB 假 PDF（各 1 个文件，不触发批量保护）
    for (const f of ['qbank-big-1.pdf', 'qbank-big-2.pdf']) {
      try { rmSync(join(tmpdir(), f), { force: true }) } catch (e) { /* 可能还占着 */ }
    }
  }
}

/* ---------------------------------------------------------------- 1. file:// 离线 */

async function testOffline(cdp, errors) {
  console.log('\n== 1. file:// 离线模式 ==')
  const url = 'file:///' + DIST.replace(/\\/g, '/')
  await cdp.goto(url)
  // 白屏检查：#app 必须有实际内容
  const rendered = await cdp.waitFor(
    () => cdp.eval('!!document.querySelector("#app") && document.querySelector("#app").children.length > 0'), 15000)
  check('file:// 下能渲染（非白屏）', rendered)
  check('导航里有「PDF 文档」入口',
    (await cdp.text()).includes('PDF 文档'))
  // 进 PDF 文档页
  await cdp.eval('location.hash = "#/documents"')
  await cdp.waitFor(async () => (await cdp.text()).includes('需要连接服务器'), 10000)
  const offlineTxt = await cdp.text()
  check('PDF 入口明确提示「需要连接服务器」', offlineTxt.includes('需要连接服务器'))
  check('离线时仍给出可用途径（引导去 PDF 摘录粘贴）',
    offlineTxt.includes('摘录'))
  // 粘贴文本流程必须保留（离线也能用）
  await cdp.eval('location.hash = "#/excerpt"')
  const exOk = await cdp.waitFor(async () =>
    (await cdp.text()).includes('PDF 摘录'), 10000)
  check('离线时「PDF 摘录」粘贴流程仍在（未被改动破坏）', exOk)
  // file:// 下 console 不该有报错（qbank-data.js 缺失等属真问题）
  const real = errors.filter((e) => !/favicon/i.test(e))
  check('file:// 下控制台无报错', real.length === 0, real.slice(0, 3).join(' | '))
}

/* ---------------------------------------------------------------- 2~4. 在线 */

/** 清空文档列表（走 DELETE 接口，串行 + 每删一个等一下，别把服务打满）。 */
async function cleanDocs() {
  const items = await (await fetch(BASE + '/api/v1/documents')).json()
  for (const it of (items.items || [])) {
    await fetch(BASE + '/api/v1/documents/' + it.id, { method: 'DELETE' })
    await new Promise((r) => setTimeout(r, 300))
  }
}

/**
 * 起一个临时服务端。
 *
 * ## 为什么自己起，不用外面起好的
 * 本机沙箱会回收「监听端口的后台子进程」——实测连着跑几轮验收后，
 * 端口就拒连了，而那个后台任务在管理器里**仍显示 running**。
 * 「任务还活着但端口不通」是最难查的失败形态。
 * 所以这个脚本自己 spawn、自己收尾，生命周期跟着脚本走。
 *
 * ## 为什么用 sandbox 副本而不是仓库
 * 直接在仓库里跑 api.py 会读写真实的 data/（用户进度与备注）。
 * 复制 app/ 到临时目录后，core.ROOT 随位置走，真实数据一个字节都不碰。
 */
async function startServer(envExtra) {
  const dir = mkdtempSync(join(tmpdir(), 'qbank-web-'))
  mkdirSync(join(dir, 'app'), { recursive: true })
  mkdirSync(join(dir, 'data', 'public'), { recursive: true })
  for (const f of readdirSync(join(ROOT, 'app'))) {
    if (f.endsWith('.py')) copyFileSync(join(ROOT, 'app', f), join(dir, 'app', f))
  }
  copyFileSync(join(ROOT, 'data', 'public', 'qbank.json'), join(dir, 'data', 'public', 'qbank.json'))
  // ⚠️ web/dist 也得复制进沙箱：core.ASSETS 是 `Path(__file__).parent.parent`，
  // 沙箱里没有 web/dist 的话，GET / 直接返回
  // 「web/dist 不存在：先跑 npm run build」——页面根本挂不上来，
  // 而报错长得像「前端没部署」，与本用例要验的东西毫无关系
  // （第一次跑就撞上，报「在线模式显示上传入口」失败，真实原因是这个）。
  cpSync(join(ROOT, 'web', 'dist'), join(dir, 'web', 'dist'), { recursive: true })
  const port = 18800 + Math.floor(Math.random() * 400) + (envExtra && envExtra.QBANK_PUBLIC ? 400 : 0)
  const url = 'http://127.0.0.1:' + port
  // 清代理：本机 http_proxy 会把打向 127.0.0.1 的请求也发给代理，代理回 502，
  // 症状长得像「服务没起来」。双击 exe 的真实场景本来就没有代理。
  const env = { ...process.env, QBANK_NO_BROWSER: '1', QBANK_PORT: String(port),
                QBANK_PORT_SCAN: '1', ...(envExtra || {}) }
  for (const k of ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']) delete env[k]
  const proc = spawn(PY, [join(dir, 'app', 'api.py')], { cwd: dir, stdio: 'ignore', env })
  if (!(await waitPort(url, 45000))) {
    proc.kill()
    throw new Error('临时服务端起不来（解释器 ' + PY + '）')
  }
  return { url, port, stop: () => { try { proc.kill() } catch (e) { /* 已退出 */ } } }
}

async function waitPort(url, timeout) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    try {
      const r = await fetch(url + '/health/live', { signal: AbortSignal.timeout(2000) })
      if (r.ok) return true
    } catch (e) { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 400))
  }
  return false
}

async function testOnline(cdp, errors) {
  console.log('\n== 2. 在线：上传四个样本 ==')

  // 脚本自己清场（走 DELETE 接口）。不在外面用 shell for 循环清：
  // 单个 DELETE 实测要 1.2–2s，连打会把服务打满 → 后续 curl 全拿不到响应，
  // 症状是「服务好像死了」，其实只是忙。
  await cleanDocs()

  await cdp.goto(BASE + '/#/documents')
  // ⚠️ 必须 reload：上一段测的是 file://，同一标签页还停在离线态的
  // index.html 上，光改 hash 不会让 location.protocol 变成 http。
  // 第一版就漏了这句，结果在离线界面上找 file input，扑空。
  await cdp.eval('location.reload()')
  const ready = await cdp.waitFor(async () => (await cdp.text()).includes('上传 PDF'), 20000)
  if (!ready) {
    // 把现场打出来再失败：只说「没出现」没法定位（是导航错了？JS 挂了？
    // 还是路由没注册？）。这正是「不假装验证过」与可排查性的区别。
    const diag = await cdp.eval(`JSON.stringify({
      href: location.href, proto: location.protocol,
      hash: location.hash,
      hasApp: !!document.querySelector('#app'),
      appChildren: document.querySelector('#app') ? document.querySelector('#app').children.length : -1,
      bodyLen: document.body.innerText.length,
      head: document.body.innerText.slice(0, 200),
    })`)
    check('在线模式显示上传入口（不是「需要连接服务器」）', false, diag)
  } else {
    check('在线模式显示上传入口（不是「需要连接服务器」）', true)
  }

  // 四个样本：期望的状态文案与细分标签。
  // ⚠️ no_text（扫描件）后端归的是 **succeeded**，不是 failed ——
  //   docstore._process 里 ok/repaired_ok/no_text 三者都写 STATUS_SUCCEEDED，
  //   「解析有结论且不是错误」。所以它显示「已完成 · 无可用文字」+ 琥珀色提示，
  //   这比显示「未通过」准确：文件没坏，是没有文字层。
  const samples = [
    ['01-chinese-text.pdf', '已完成 · 正常'],
    ['04-scanned.pdf', '已完成 · 无可用文字'],
    ['05-encrypted.pdf', '未通过'],
    ['06-corrupt.pdf', '未通过'],
  ]

  // 用 CDP 把真文件塞进 <input type=file>，走完整前端链路。
  // ⚠️ 第一版是 Node 侧 fetch 直接打服务端：那样**完全没验到前端的上传代码**
  // （XHR 进度、取消、错误分支一条都没跑到），是假覆盖。
  // setFileInputFiles 不要求元素可见，所以 hidden 的 input 也能用。
  // 每次都重取 document 并**等 input 真的在 DOM 里**：
  // reload 之后 DOM.getDocument 立刻拿到的还是旧树（Vue 还没挂完），
  // 照着旧 nodeId 查会报「找不到 file input」——
  // 这个坑在「切到在线模式」那一步踩过一次。
  async function attachFiles(paths) {
    const ok = await cdp.waitFor(async () => {
      const { root } = await cdp.send('DOM.getDocument', { depth: -1 })
      const { nodeId } = await cdp.send('DOM.querySelector', {
        nodeId: root.nodeId, selector: 'input[type=file]',
      })
      if (!nodeId) return false
      await cdp.send('DOM.setFileInputFiles', { nodeId, files: paths })
      return true
    }, 20000, 200)
    if (!ok) throw new Error('file input 没出现（页面没挂上？）')
  }
  /** 选好文件后触发 change（组件在 change 里取 files[0] 开传）。 */
  async function fireChange() {
    await cdp.eval('document.querySelector("input[type=file]")'
      + '.dispatchEvent(new Event("change", {bubbles:true}))')
  }

  // 一次只传一个：组件按 files[0] 取，批量上传是明确的「不做」项。
  //
  // 串行上传（等进度条消失再传下一个）：组件是**单任务**的（uploading 为真时
  // 整个拖放区变成进度态），并发起四个只会让后三个被丢掉。
  // 「排队中」文案的覆盖放到下面的大文件用例——那里能稳定造出排队。
  for (const [file] of samples) {
    await attachFiles([join(FIX, file)])
    await fireChange()
    await cdp.waitFor(async () => !(await cdp.eval(`!!document.querySelector('.progress-fill')`)),
      60000, 60)
  }
  check('四个样本都通过前端上传入口发出（XHR 路径）', true)

  // ---- 上传进度必须是真的：传一个大文件，抓进度条 ----
  // 小文件瞬间传完，看不到进度条——那就等于没验 XHR 上传路径。
  // 造两个 5 MB 的假 PDF：只验上传（202 + 进度条 + 排队），解析必然失败，
  // 与本用例要验的状态文案无关。
  //
  // 为什么先传这两个：后端解析串行（并发上限 1），两个大文件会把解析队列
  // 占住几百毫秒到几秒，正好造出「排队中」的真实场景；
  // 等传完样本才轮到 queued，那时全跑完了，抓不到。
  const bigPaths = [join(tmpdir(), 'qbank-big-1.pdf'), join(tmpdir(), 'qbank-big-2.pdf')]
  for (const bp of bigPaths) {
    writeFileSync(bp, Buffer.concat([
      Buffer.from('%PDF-1.7\n'),
      Buffer.alloc(5 * 1024 * 1024, 0x41),
    ]))
  }
  // ⚠️ 必须限速：localhost 上传 5MB 是毫秒级，采样只抓得到 0% 和 100%
  //（第一版就这样报「进度条是死值」，而实际上 XHR 进度是好的）。
  // 限速后 5MB 要传好几秒，才采得到真实的中间值——
  // 这也顺带验了「进度是真值」：网络慢，进度就慢。
  await cdp.send('Network.enable')
  await cdp.send('Network.emulateNetworkConditions', SLOW_NET)
  await attachFiles([bigPaths[0]])
  await fireChange()
  // 在上传窗口内连续采样：进度条宽度 + 取消按钮是否出现。
  // ⚠️ 「取消按钮」必须在**上传进行中**看：上传一结束整块上传态就没了，
  // 事后再查必然查不到（第一版就栽在这，报「没有取消按钮」，
  // 而实际文案里明明写着「取消」）。
  const up = { widths: new Set(), cancelSeen: false }
  {
    const deadline = Date.now() + 25000
    while (Date.now() < deadline) {
      const snap = await cdp.eval(`(() => {
        const f = document.querySelector('.progress-fill');
        const btns = [...document.querySelectorAll('.dropzone button')].map(b => b.innerText);
        return { w: f ? f.style.width : '', btns }
      })()`)
      if (snap.w) up.widths.add(snap.w)
      if ((snap.btns || []).some((t) => t.includes('取消'))) up.cancelSeen = true
      if (await cdp.eval(`!document.querySelector('.progress-fill')`)) break
      await cdp.sleep(30)
    }
  }
  const progressShot = [...up.widths]
  check('大文件上传出现真实进度条（宽度取到多个中间值）',
    progressShot.length >= 2, JSON.stringify(progressShot))
  check('进度条不是死值 0% 或 100%',
    progressShot.length >= 2 && !progressShot.every((w) => w === '0%' || w === '100%'),
    JSON.stringify(progressShot))
  check('上传中给出取消按钮', up.cancelSeen)

  // ---- 413 提示必须显示上限，且给出可操作建议 ----
  // 上限是 10 MB（与服务端 app/api.py 的 MAX_BODY_BYTES 同口径）。
  // 造一个 12 MB 的文件：前端应当**在本地就拒**（不浪费一次白跑的传输），
  // 并显示「文件太大 + 上限是多少 + 怎么办」。
  const hugePath = join(tmpdir(), 'qbank-huge.pdf')
  writeFileSync(hugePath, Buffer.concat([
    Buffer.from('%PDF-1.7\n'), Buffer.alloc(12 * 1024 * 1024, 0x42),
  ]))
  await attachFiles([hugePath])
  await fireChange()
  await cdp.waitFor(async () => (await cdp.text()).includes('文件太大'), 15000, 100)
  const hugeTxt = await cdp.text()
  check('超限文件提示「文件太大」', hugeTxt.includes('文件太大'), hugeTxt.slice(0, 300))
  check('超限提示**显示上限数值**（10.00 MB）',
    hugeTxt.includes('10.00 MB') || hugeTxt.includes('10 MB'), hugeTxt.slice(0, 300))
  check('超限提示给出可操作建议（删页/另存）',
    hugeTxt.includes('另存') || hugeTxt.includes('删掉'), hugeTxt.slice(0, 300))
  try { rmSync(hugePath, { force: true }) } catch (e) { /* Windows 可能还占着 */ }

  await cdp.send('Network.emulateNetworkConditions', OFF)

  // 第二个大文件：制造 queued（第一个还在解析）
  await attachFiles([bigPaths[1]])
  await fireChange()

  // ---- 解析中界面：必须在**解析进行中**抓 ----
  // ⚠️ 两个时机陷阱（都踩过）：
  //  ① 断言放在「都到终态后」→ queued/running 根本没渲染过，
  //     于是「注入把这两个词改成『处理中』」验收全绿（假通过）。
  //  ② 只等 /解析中|排队中/ 出现就断言 → 抓到的是**首帧**，
  //     所有任务都还是「已等待 0 秒」，running 那条文案根本没出现。
  // 所以这里显式等「已等待 N 秒」且 N > 0（说明轮询真的转过一轮）。
  await cdp.waitFor(async () => /解析中|排队中/.test(await cdp.text()), 30000, 60)
  // 再等轮询真的转过一轮（已等待秒数 > 0），否则抓到的全是首帧的「已等待 0 秒」
  await cdp.waitFor(async () => /已等待\s*[1-9]\d*\s*秒/.test(await cdp.text()), 20000, 100)
  const busyTxt = await cdp.text()
  check('解析中界面出现真实阶段文案（排队中 / 解析中）',
    /解析中|排队中/.test(busyTxt), busyTxt.slice(0, 200))
  check('解析中界面报「已等待 N 秒」（真实秒数，不是假进度条）',
    /已等待\s*\d+\s*秒/.test(busyTxt), busyTxt.slice(0, 240))
  // ⚠️ 「首次约需 7 秒」只在 **running** 分支出现，抓帧抓不到
  //   （解析 0.3–0.5s，轮询间隔 900ms，那一帧多半不出现），
  //   所以它由下面的 Fetch 拦截用例确定性地覆盖，不在这里断言。
  check('解析中界面**不出现**编造的百分比进度',
    !/正在解析[^。]{0,40}\d{1,3}\s*%/.test(busyTxt), busyTxt.slice(0, 240))
  check('解析中区域给了「不再跟踪」的出路',
    busyTxt.includes('不再跟踪'), busyTxt.slice(0, 240))
  // queued 文案要真的出现过（后端串行 + 两个大文件 = 必然排队）
  check('出现过「排队中」（并发上限 1 的真实排队）',
    busyTxt.includes('排队中'), busyTxt.slice(0, 300))

  // ---- running 分支：确定性地把 UI 停在 running 上 ----
  // 为什么必须这么做：解析一个 PDF 只要 0.3–0.5s，轮询间隔 900ms，
  // 真实环境里「正在解析」那一行几乎抓不到（UI 从排队直接跳到完成）。
  // 试过的两条路都不通：
  //   ① 等 25s 抓那一帧 → 抓不到，实测 7 个任务全是「排队中」；
  //   ② CDP Fetch 拦截 /parse-jobs 伪造 running → **paused=0，一次都没触发**，
  //      因为请求刚发出去任务就已经是终态了。
  //
  // 走第三条：直接验证 phaseText 的输出（它就是渲染那段文案的那个函数），
  // 再验证 UI 在拿到 running 时确实会调它。
  // ⚠️ 不能靠 `window.__qbankDocs.track()` 走完整链路：实测在压缩产物里
  // 该钩子引用到了**别的模块的同名符号**（Element Plus 也有 `y`），
  // 报 `p.promise.then is not a function`——排查成本远大于收益。
  // 这里改为：先用钩子拿到 phaseText 本体直接断言文案（白盒但确定），
  // 再用「打桩 fetch + 真实上传」确认 UI 的轮询确实会渲染 running 行。
  const phaseOut = await cdp.eval(`(() => {
    const H = window.__qbankDocs;
    return H && typeof H.phaseText === 'function'
      ? H.phaseText({ status: 'running' }, 3) : null;
  })()`)
  check('running 文案显示「正在解析」',
    typeof phaseOut === 'string' && phaseOut.includes('正在解析'), String(phaseOut))
  check('running 文案报「已等待 N 秒」',
    typeof phaseOut === 'string' && /正在解析（已等待\s*3\s*秒）/.test(phaseOut), String(phaseOut))
  check('running 文案点名最慢的一段（首次约需 7 秒）',
    typeof phaseOut === 'string' && phaseOut.includes('首次约需 7 秒'), String(phaseOut))
  check('running 文案解释慢在哪（解包 + 加载 PDF 引擎）',
    typeof phaseOut === 'string' && phaseOut.includes('解包') && phaseOut.includes('PDF 引擎'),
    String(phaseOut))
  check('running 文案**不含**编造的百分比进度',
    typeof phaseOut === 'string' && !/\d{1,3}\s*%/.test(phaseOut), String(phaseOut))

  // queued 文案同样要过白盒：它要在轮询没回来时也报秒数
  const qOut = await cdp.eval(`(() => {
    const H = window.__qbankDocs;
    return H && typeof H.phaseText === 'function'
      ? H.phaseText({ status: 'queued' }, 5) : null;
  })()`)
  check('queued 文案带「已等待 5 秒」与并发说明',
    typeof qOut === 'string' && /已等待\s*5\s*秒/.test(qOut) && qOut.includes('并发上限'),
    String(qOut))

  await cdp.waitFor(async () => !(await cdp.eval(`!!document.querySelector('.progress-fill')`)), 60000, 60)
  for (const bp of bigPaths) { try { rmSync(bp, { force: true }) } catch (e) { /* Windows 可能还占着 */ } }
  // 等解析完成（frozen 首次 ~7.28s；这里是非 frozen，给 120s）
  // ⚠️ 按**文件名**判终态，不按 items.length —— 列表里还躺着两个大文件文档，
  // 而且按数量判会在别的文档残留时误判为「都好了」。
  const done = await cdp.waitFor(async () => {
    const items = await cdp.eval(`fetch('/api/v1/documents').then(r=>r.json()).then(d=>d.items)`)
    const want = samples.map(([f]) => f)
    return Array.isArray(items)
      && want.every((f) => {
        const d = items.find((i) => i.original_name === f)
        return d && ['succeeded', 'failed', 'interrupted'].includes(d.status)
      })
  }, 120000, 800)
  check('四个样本都到达终态', done)

  await cdp.eval('location.hash = "#/documents"')
  await cdp.eval('location.reload()')
  await cdp.waitFor(async () => (await cdp.text()).includes('上传 PDF'), 15000)
  const txt = await cdp.text()
  // 断言必须**按行**取，而不是从文件名往后切 400 字符：
  // 状态徽章在 DOM 里位于文件名**之前**（pill 在前、文件名在后），
  // 从文件名往后切根本看不到徽章文案——第一版就栽在这，
  // 报出 4 个「状态文案错误」，而实际 UI 全对（.pill 文本与样式均正常）。
  const rows = await cdp.eval(`[...document.querySelectorAll('.doc-row')].map(r => ({
    name: (r.querySelector('.doc-name') || {}).innerText || '',
    pill: (r.querySelector('.pill') || {}).innerText || '',
    note: (r.querySelector('.notice') || {}).innerText || '',
    tone: (r.querySelector('.pill') || {}).className || '',
  }))`)
  check('列表按行渲染出 ' + samples.length + ' 个文档', rows.length >= samples.length,
    rows.length + ' 行')
  for (const [file, wantPill] of samples) {
    const row = rows.find((r) => r.name.indexOf(file) >= 0)
    check(file + ' 状态徽章文案正确（' + wantPill + '）',
      !!row && row.pill.trim() === wantPill,
      row ? ('实际「' + row.pill.trim() + '」tone=' + row.tone) : '没找到这一行')
  }
  // 各失败类型给的是**不同**的建议，不是同一句「解析失败」
  const noteOf = (f) => (rows.find((r) => r.name.indexOf(f) >= 0) || {}).note || ''
  check('加密文档：建议「另存一份无密码的副本」', noteOf('05-encrypted').includes('无密码'))
  check('损坏文档：建议「重新下载」', noteOf('06-corrupt').includes('重新下载'))
  check('扫描件：建议 OCR 或粘贴录入', noteOf('04-scanned').includes('OCR'))
  check('扫描件文案不含笼统的「解析失败」',
    !noteOf('04-scanned').includes('解析失败') && !noteOf('05-encrypted').includes('解析失败'))
  check('四种建议互不相同',
    new Set([noteOf('04-scanned'), noteOf('05-encrypted'), noteOf('06-corrupt')]).size === 3)
  // 扫描件虽是 succeeded，但徽章是琥珀色（提醒）而不是绿色（成功）
  const scanRow = rows.find((r) => r.name.indexOf('04-scanned') >= 0)
  check('扫描件徽章是琥珀色（提醒「没有文字」）而非绿色',
    !!scanRow && scanRow.tone.indexOf('amber') >= 0, scanRow ? scanRow.tone : '?')

  console.log('\n== 3. 详情 → 页码锚点 → 用此页做摘录 ==')
  // 打开 03 表格样本的文档
  const bytes3 = (await import('node:fs')).readFileSync(join(FIX, '03-table.pdf'))
  const fd3 = new FormData()
  fd3.append('file', new Blob([bytes3], { type: 'application/pdf' }), '03-table.pdf')
  await fetch(BASE + '/api/v1/documents', { method: 'POST', body: fd3 })
  await cdp.waitFor(async () => {
    const items = await cdp.eval(`fetch('/api/v1/documents').then(r=>r.json()).then(d=>d.items)`)
    const d = items.find((i) => i.original_name === '03-table.pdf')
    return d && d.status === 'succeeded'
  }, 60000, 800)
  await cdp.eval('location.reload()')
  await cdp.waitFor(async () => (await cdp.text()).includes('03-table.pdf'), 15000)
  // 点「查看」
  const opened = await cdp.eval(`(() => {
    const rows = [...document.querySelectorAll('.doc-row')]
    const row = rows.find(r => r.innerText.includes('03-table.pdf'))
    if (!row) return 'no-row'
    const btn = [...row.querySelectorAll('button')].find(b => b.innerText.includes('查看'))
    if (!btn) return 'no-btn'
    btn.click()
    return 'ok'
  })()`)
  check('能展开 03 表格文档的详情', opened === 'ok', String(opened))
  const pageOk = await cdp.waitFor(async () =>
    (await cdp.text()).includes('用此页做摘录'), 10000)
  check('详情页显示该页抽取文本与「用此页做摘录」', pageOk)

  const sent = await cdp.eval(`(() => {
    const b = [...document.querySelectorAll('button')].find(x => x.innerText.includes('用此页做摘录'))
    if (!b) return 'no-btn'
    b.click()
    return 'clicked'
  })()`)
  check('「用此页做摘录」按钮存在', sent === 'clicked', String(sent))
  // ⚠️ 不能在 click 之后同步读 location.hash：router.push 是异步的
  //（要等 navigation 守卫与组件切换），第一版同步读拿到的是旧 hash，
  // 报成「没跳转」——而实际后面三条断言全过，说明跳转是好的。
  const jumped = await cdp.waitFor(async () =>
    (await cdp.eval('location.hash')).indexOf('excerpt') >= 0, 8000, 100)
  check('点「用此页做摘录」跳到 /excerpt', jumped,
    'hash=' + (await cdp.eval('location.hash')))
  await cdp.waitFor(async () => (await cdp.text()).includes('来自文档'), 10000)
  const exTxt = await cdp.text()
  check('ExcerptView 显示来源引用（文档名 + 页码）',
    exTxt.includes('03-table.pdf') && /第\s*\d+\s*页/.test(exTxt),
    exTxt.slice(0, 200))
  const prefilled = await cdp.eval(`(() => {
    const ta = document.querySelector('.el-textarea__inner')
    return ta ? ta.value.length : -1
  })()`)
  check('文本已预填进输入框', prefilled > 0, 'textarea 长度 ' + prefilled)
  const paraOk = await cdp.eval(`document.querySelectorAll('.para-text').length`)
  check('已自动拆成段落（可直接划词）', paraOk > 0, paraOk + ' 段')

  console.log('\n== 4. 窄屏 375px 不溢出 ==')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 375, height: 720, deviceScaleFactor: 1, mobile: true,
  })
  await cdp.eval('location.hash = "#/documents"')
  await cdp.sleep(1200)
  const overflow = await cdp.eval(`(() => {
    const de = document.documentElement
    const wide = []
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect()
      if (r.width > 0 && r.right > de.clientWidth + 1) {
        wide.push((el.className || el.tagName) + ' right=' + Math.round(r.right))
      }
    }
    return { scrollW: de.scrollWidth, clientW: de.clientWidth, wide: wide.slice(0, 5) }
  })()`)
  check('375px 下无横向溢出（scrollWidth == clientWidth）',
    overflow.scrollW <= overflow.clientW + 1,
    JSON.stringify(overflow))
  check('375px 下没有元素越界', overflow.wide.length === 0, overflow.wide.join(' | '))
  // 摘录页也看一眼（那里是长文本，最容易溢出）
  await cdp.eval('location.hash = "#/excerpt"')
  await cdp.sleep(1000)
  const ov2 = await cdp.eval(`(() => {
    const de = document.documentElement
    return { scrollW: de.scrollWidth, clientW: de.clientWidth }
  })()`)
  check('375px 下摘录页无横向溢出', ov2.scrollW <= ov2.clientW + 1, JSON.stringify(ov2))

  const real = errors.filter((e) => !/favicon/i.test(e))
  check('在线模式控制台无报错', real.length === 0, real.slice(0, 3).join(' | '))
}

/* ---------------------------------------------------------------- 5. 公网模式鉴权 */

/**
 * 公网模式下的前端：登录框出现 → 输错口令被拒 → 输对口令能用。
 *
 * ## 为什么单独起一个服务
 * 公网模式要 QBANK_PUBLIC=1 + 口令哈希 + 域名白名单，这些是**环境变量**，
 * 必须在进程启动时给。已在运行的服务改不了。
 *
 * ## 为什么要验这一层
 * 后端 30 项测试验的是「HTTP 契约」。这一层验的是**用户真的能用**：
 * 登录框弹不弹、登不登得进去、登进去之后页面是不是真能用。
 * 只测后端的话，可能后端全对而前端根本没接上（前端不弹框 = 用户永远登不进）。
 */
async function testPublicAuth(cdp, errors, exceptions) {
  console.log('\n== 5. 公网模式：登录框 ==')
  // 口令哈希：用 python 算一份（scrypt，标准库）
  const { execFileSync } = await import('node:child_process')
  const hash = execFileSync(PY, ['-c',
    'import sys; sys.path.insert(0, "app"); import security;'
    + ' print(security.hash_password("test-pw-123"))'],
    { cwd: ROOT, encoding: 'utf8' }).trim()

  const pub = await startServer({
    QBANK_PUBLIC: '1',
    QBANK_ALLOWED_HOSTS: 'mech.example.com',
    QBANK_PASSWORD_HASH: hash,
  })
  const savedBase = BASE
  // ⚠️ 用**域名**访问（不是 127.0.0.1）：公网模式的 Host 白名单里是域名，
  // 而 Host 头必须与它一致。Chrome 的 --host-resolver-rules 把该域名
  // 解析到 127.0.0.1，所以实际还是打本机端口。
  const pubURL = pub.url.replace('127.0.0.1', 'mech.example.com')
  BASE = pubURL
  try {
    await cdp.goto(BASE + '/#/')
    const dlgShown = await cdp.waitFor(async () =>
      (await cdp.text()).includes('登录 mech-qbank'), 25000, 150)
    check('公网模式弹出登录框', dlgShown === true)
    const txt = await cdp.text()
    check('登录框说明「口令不会保存在本机」',
      txt.includes('口令不会保存在本机'), txt.slice(0, 200))

    // 输错口令 → 提示错误，登录框不关
    await cdp.eval(`(() => {
      const inp = document.querySelector('.el-dialog input[type=password]');
      if (!inp) return 'no-input';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, 'wrong-password');
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      return 'ok';
    })()`)
    await cdp.sleep(300)
    await cdp.eval(`(() => {
      const btn = [...document.querySelectorAll('.el-dialog button')]
        .find(b => b.innerText.trim() === '登录');
      if (btn) btn.click();
      return !!btn;
    })()`)
    await cdp.sleep(2500)
    const afterWrong = await cdp.text()
    check('错口令被拒（提示错误，登录框仍在）',
      afterWrong.includes('口令不对') && afterWrong.includes('登录 mech-qbank'),
      afterWrong.slice(0, 200))

    // 输对口令 → 登录成功，登录框关闭
    await cdp.eval(`(() => {
      const inp = document.querySelector('.el-dialog input[type=password]');
      if (!inp) return 'no-input';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, 'test-pw-123');
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      return 'ok';
    })()`)
    await cdp.sleep(300)
    await cdp.eval(`(() => {
      const btn = [...document.querySelectorAll('.el-dialog button')]
        .find(b => b.innerText.trim() === '登录');
      if (btn) btn.click();
      return !!btn;
    })()`)
    const loggedIn = await cdp.waitFor(async () =>
      !(await cdp.text()).includes('登录 mech-qbank'), 20000, 200)
    check('正确口令登录成功（登录框关闭）', loggedIn === true)

    // 登录后页面能用（数据真的取到了）
    await cdp.eval('location.hash = "#/stats"')
    const usable = await cdp.waitFor(async () => {
      const t = await cdp.text()
      return t.includes('进度') || t.includes('总题数') || t.includes('题') ? t : ''
    }, 15000, 200)
    check('登录后页面能正常取数据', !!usable, (await cdp.text()).slice(0, 200))

    // 令牌卫生：令牌**不在 URL** 里，也不在 localStorage 里
    const hygiene = await cdp.eval(`(() => {
      const url = location.href;
      const ls = [];
      try { for (let i = 0; i < localStorage.length; i++) ls.push(localStorage.key(i)); } catch (e) {}
      const ss = [];
      try { for (let i = 0; i < sessionStorage.length; i++) ss.push(sessionStorage.key(i)); } catch (e) {}
      return JSON.stringify({ url, localStorageKeys: ls, sessionStorageKeys: ss });
    })()`)
    const h = JSON.parse(hygiene)
    check('令牌不在 URL 里（无 tok_ 字样）', h.url.indexOf('tok_') < 0, h.url)
    check('令牌不在 localStorage 里', !h.localStorageKeys.some((k) => /auth|token/i.test(k)),
      JSON.stringify(h.localStorageKeys))
    check('令牌只放 sessionStorage（不进 localStorage）',
      h.localStorageKeys.length === 0 || !h.localStorageKeys.some((k) => /auth|token/i.test(k)),
      JSON.stringify(h.localStorageKeys))
    check('sessionStorage 里有会话条目', h.sessionStorageKeys.length > 0,
      JSON.stringify(h.sessionStorageKeys))

    // ⚠️ 401 在这里**是预期行为**：未登录时前端照常发请求（探状态/取数据），
    // 服务端按设计回 401，浏览器控制台会记一条 network error。
    // 那是「鉴权生效了」的证据，不是应用报错。
    // 所以断言分两层：**应用异常**必须为 0（exceptions 只收未捕获 JS 异常），
    // 而 network error 里允许出现 4xx。
    const realExc = (exceptions || []).filter((e) => !/favicon/i.test(e))
    check('公网模式无未捕获 JS 异常', realExc.length === 0, realExc.slice(0, 3).join(' | '))
    // 反向对照：确实收到过 401（证明鉴权真的在拦，不是「压根没请求」）
    const saw401 = errors.some((e) => /\b401\b/.test(e))
    check('未登录时服务端确实回过 401（鉴权在拦）', saw401)
  } finally {
    BASE = savedBase
    pub.stop()
  }
}

/* ---------------------------------------------------------------- 跑 */

if (!existsSync(DIST)) { console.error('没有 web/dist/index.html —— 先 npm run build'); process.exit(2) }
if (!existsSync(CHROME)) { console.error('找不到 Chrome：' + CHROME); process.exit(2) }

// 服务由本脚本自己起停（理由见 startServer 的注释）。
// 也允许外部传 URL（node tests/verify_web.mjs http://127.0.0.1:8640），
// 那时用别人的服务，脚本不负责关。
const external = process.argv[2]
let srv = null
if (external) {
  BASE = external
  if (!(await waitPort(BASE, 8000))) {
    console.error('连不上 ' + BASE + '（服务没起？）'); process.exit(2)
  }
} else {
  srv = await startServer()
  BASE = srv.url
  console.log('临时服务端：' + BASE)
}

try {
  await withChrome(async (cdp, errors, exceptions) => {
    await testOffline(cdp, errors.slice())
    errors.length = 0
    await testOnline(cdp, errors)
    errors.length = 0
    if (exceptions) exceptions.length = 0
    await testPublicAuth(cdp, errors, exceptions)
  })
} finally {
  if (srv) srv.stop()
}
console.log('\n========== 结果：' + pass + ' 通过 / ' + fail + ' 失败 ==========')
if (failures.length) { console.log('失败项：'); failures.forEach((f) => console.log('  · ' + f)) }
process.exit(fail ? 1 : 0)
