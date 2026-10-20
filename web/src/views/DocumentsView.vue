/* DocumentsView.vue — PDF 文档：上传 → 解析状态 → 按页看抽取文本 → 送去摘录
 *
 * ## 铁律：永不渲染 PDF
 * 不引 pdf.js、不引任何渲染库、不动态 import worker。
 * 前端只显示**服务端已抽好的纯文本** + 页码锚点。
 * 理由不是"没必要"，是三条硬约束叠加：
 *   · pdf.js 要动态 import worker → 撞碎 vite 的 IIFE 单包（file:// 双闸之一）；
 *   · 它还要额外二进制资源 → onefile 打包体积与冷启动雪上加霜（实测已 7.28s）；
 *   · 本项目 file:// 必须能双击打开，渲染库这条路是死路。
 * 服务端 PyMuPDF 已经在做版式还原（栏序、表格），再在前端重渲一遍只会引入
 * 第二套不一致的版式。
 *
 * ## 状态显示：报真实阶段与真实秒数
 * 实测 frozen exe 首次解析 7.28s（onefile 解包 4.34s + import pymupdf 1.78s
 * + 抽取 0.46s）。所以这里显示「已等待 N 秒」并**点名最慢的那一段**，
 * 而不是画一根编出来的进度条——用户据此知道等 7 秒正常、等 30 秒不正常。
 *
 * ## 离线（file://）
 * 列表区显示"需要连接服务器"，但**保留一条可用出路**：
 * 引导去「PDF 摘录」粘贴文本（那条流程不依赖服务器，一直可用）。
 */
<template>
  <section>
    <PdfUploadCard :offline="offline" @uploaded="onUploaded" @gone="refresh" />

    <!-- ============ 解析中的实时反馈 ============ -->
    <div v-if="activeJobs.length" class="card">
      <div class="row">
        <b>解析中</b>
        <span class="sub">{{ activeJobs.length }} 个任务</span>
      </div>
      <div v-for="a in activeJobs" :key="a.jobId" class="job-row">
        <span class="pill amber">{{ a.docName }}</span>
        <span class="meta">{{ a.text }}</span>
        <button class="ghost" style="margin-left:auto;flex:none" @click="dropJob(a.jobId)">
          不再跟踪
        </button>
      </div>
      <div v-if="jobOfflineHint" class="notice notice-warn">
        <b>与服务器的连接断了</b>
        <div class="meta" style="margin-top:4px">
          已重试多次仍拿不到状态。解析**可能已在服务端完成**——
          恢复连接后点「刷新」看真实结果，不要重复上传同一个文件。
        </div>
      </div>
    </div>

    <!-- ============ 列表 ============ -->
    <div class="card">
      <div class="row">
        <b>文档列表</b>
        <span class="sub">{{ docs.length }} 个</span>
        <button class="ghost" style="margin-left:auto" @click="refresh">刷新</button>
      </div>

      <div v-if="offline" class="empty">
        需要连接服务器才能列出 PDF 文档（文档存在服务端磁盘上）。
      </div>
      <div v-else-if="!docs.length" class="empty">
        还没有文档——上面传一个 PDF。
      </div>

      <div v-for="d in docs" :key="d.id" class="doc-row"
           :class="{ open: sel && sel.id === d.id }">
        <div class="row" style="flex-wrap:nowrap;align-items:flex-start">
          <span class="pill" :class="tone(d)">{{ statusText(d) }}</span>
          <span class="doc-name">{{ d.original_name || d.id }}</span>
          <button class="ghost" style="flex:none;margin-left:auto"
                  @click="open(d)">{{ sel && sel.id === d.id ? '收起' : '查看' }}</button>
        </div>
        <div class="meta doc-meta">
          {{ fmtBytes(d.bytes) }}
          <template v-if="d.page_count"> · {{ d.page_count }} 页</template>
          <template v-if="d.total_chars"> · {{ d.total_chars }} 字</template>
          <template v-if="d.columns_detected === 2"> · 双栏</template>
          <template v-if="(d.table_pages || []).length"> · 含表格</template>
          · {{ fmtTime(d.created_at) }}
        </div>

        <!-- 失败/中断：给不同的可操作建议 -->
        <div v-if="hint(d)" class="notice" :class="noticeClass(d)">
          <b>{{ resultLabel(d.result_status) || statusText(d) }}</b>
          <div class="meta" style="margin-top:4px">{{ hint(d) }}</div>
          <div v-if="d.result_status === 'interrupted'" class="meta" style="margin-top:6px">
            重新上传同一个文件即可重解析（服务端不会自动重试）。
          </div>
        </div>

        <div class="row" style="margin-top:8px">
          <span class="meta" style="font-size:11.5px">id {{ d.id }}</span>
          <button class="ghost" style="margin-left:auto;flex:none" @click="remove(d)">
            删除
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 详情：页码锚点 + 该页文本 ============ -->
    <div v-if="detail" class="card">
      <div class="row">
        <b>{{ detail.doc.original_name || detail.doc.id }}</b>
        <span class="sub">{{ detail.doc.page_count || 0 }} 页 · 点页码看该页文本</span>
        <button class="ghost" style="margin-left:auto" @click="sel = null">关闭</button>
      </div>

      <div class="pager">
        <button class="ghost" :disabled="detail.page <= 1" @click="gotoPage(detail.page - 1)">
          上一页
        </button>
        <div class="anchors">
          <button v-for="n in pageNums" :key="n" class="anchor"
                  :class="{ on: n === detail.page }" @click="gotoPage(n)">{{ n }}</button>
        </div>
        <button class="ghost" :disabled="detail.page >= pageTotal"
                @click="gotoPage(detail.page + 1)">下一页</button>
      </div>

      <div v-if="pageLoading" class="meta" style="margin-top:8px">正在取第 {{ detail.page }} 页…</div>

      <template v-else-if="detail.item">
        <div class="row" style="margin-top:8px">
          <span class="pill gray">第 {{ detail.item.printed_label || detail.page }} 页</span>
          <span class="meta">{{ detail.item.chars }} 字</span>
          <span v-if="detail.item.columns === 2" class="pill amber">双栏（已按左→右序拼接）</span>
          <span v-if="detail.item.has_table" class="pill amber">含表格</span>
          <span v-if="detail.item.has_images" class="pill gray">含图片</span>
          <button class="act" style="margin-left:auto;flex:none" @click="sendToExcerpt">
            用此页做摘录
          </button>
        </div>

        <!-- 这一页本身没有文字（扫描页/空白页）→ 明确说清为什么空 -->
        <div v-if="!detail.item.chars" class="notice notice-warn" style="margin-top:10px">
          <b>这一页没有可抽取的文字</b>
          <div class="meta" style="margin-top:4px">
            常见原因：整页是扫描图片（<span v-if="detail.item.image_coverage > 0.5">
              图片覆盖率约 {{ Math.round(detail.item.image_coverage * 100) }}%
            </span><span v-else>但图片覆盖率不高</span>），
            或这一页本来就是空白/图表页。
          </div>
          <div class="meta" style="margin-top:6px">
            想要这页的内容：先用 OCR 工具转出文字层再上传，或在阅读器里复制文字
            到「PDF 摘录」粘贴录入。
          </div>
        </div>

        <div v-else class="page-text">{{ detail.item.text }}</div>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { STATIC } from '@/api'
import PdfUploadCard from '@/components/PdfUploadCard.vue'
import {
  listDocuments, getDocument, getPage, deleteDocument, pollJob,
  fmtBytes, fmtTime, statusTone, resultHint, resultLabel, JOB_STATUS_TEXT,
} from '@/pdfdocs'

const router = useRouter()
const offline = STATIC           // file:// 下没有服务器

const docs = ref([])
const sel = ref(null)            // 选中的文档元数据
const detail = ref(null)         // {doc, page, item}
const pageLoading = ref(false)
const jobOfflineHint = ref(false)

/** 正在跟踪的任务：{jobId, docId, docName, startedAt, elapsed, text} */
const activeJobs = ref([])
/** pollJob 的 stop 句柄，组件卸载时全部停掉——不然后台定时器会一直跑。
 *  名字带下划线前缀：minify 后短名容易和别的局部变量撞车
 *  （撞过一次：原本的 `pollers` 与 window.__qbankDocs 暴露的 track 内部同名，
 *  压缩后 track 里引用到了钩子对象，导致 `H.promise.then is not a function`）。 */
const _pollers = []

const pageTotal = computed(() => (sel.value ? (sel.value.page_count || 0) : 0))
/** 页码锚点：最多给 200 个，再多就把滚动条撑爆了。 */
const pageNums = computed(() => {
  const n = Math.min(pageTotal.value, 200);
  return Array.from({ length: n }, (_, i) => i + 1);
})

function statusText(d) {
  const st = d.status || '';
  const base = JOB_STATUS_TEXT[st] || st || '未知';
  if (st === 'succeeded') {
    const r = resultLabel(d.result_status);
    return r ? base + ' · ' + r : base;
  }
  return base;
}
function tone(d) { return statusTone(d.status, d.result_status); }
function hint(d) {
  if (d.status === 'failed' || d.status === 'interrupted') return resultHint(d.result_status);
  if (d.status === 'succeeded' && d.result_status && d.result_status !== 'ok') {
    return resultHint(d.result_status);
  }
  return '';
}
function noticeClass(d) {
  return tone(d) === 'red' ? 'notice-bad' : 'notice-warn';
}

/* ---------------------------------------------------------------- 列表 */

async function refresh() {
  if (offline) return;
  try { docs.value = await listDocuments(); }
  catch (e) { ElMessage.error('读文档列表失败：' + e.message + '（服务还在吗？）'); }
}

/** 上传完成（拿到 202 + job id）→ 立刻开始跟踪，并刷新列表显示 queued。 */
function onUploaded(job) {
  refresh();
  if (job && job.id) trackJob(job, '');
}

/**
 * 跟踪一个解析任务。
 *
 * 显示的是**真实等待秒数**，并点名最慢的一段（frozen 首次要解包 + 导 pymupdf）。
 * 不做假进度条：那条进度是编的，用户等 7 秒时看到 90% 会以为快好了，
 * 结果还要再等 7 秒——那是骗人。
 */
function trackJob(job, docName) {
  const jobId = job.id;
  const docId = job.document_id;
  if (activeJobs.value.some((x) => x.jobId === jobId)) return;

  const entry = newEntry(jobId, docId, docName);
  activeJobs.value.push(entry);

  const p = pollJob(jobId, (elapsed, j) => {
    entry.elapsed = elapsed;
    entry.text = j ? phaseText(j, elapsed)
      // 轮询还没回来时（首帧、网络抖动）也要报真实秒数。
      // 曾经这里写死「已排队（并发上限 1，一次一个）」不带秒数，
      // 结果队列一满，界面上就是一排没有时间信息的死文案——
      // 用户不知道是在等第几秒，也看不出程序还活着。
      : '排队中（已等待 ' + Math.round(elapsed) + ' 秒）· 解析并发上限 1，一次一个';
    entry.status = j ? j.status : 'queued';
  });
  _pollers.push(p);
  p.promise.then((r) => {
    activeJobs.value = activeJobs.value.filter((x) => x.jobId !== jobId);
    const idx = _pollers.indexOf(p);
    if (idx >= 0) _pollers.splice(idx, 1);
    reportOutcome(r, docName || (entry.docName || ''));
    refresh();
  });
}

/** 新建一条跟踪记录。初始文案也带秒数（见 trackJob 里的注释）。 */
function newEntry(jobId, docId, docName) {
  return { jobId, docId, docName, elapsed: 0, status: 'queued',
           text: '排队中（已等待 0 秒）· 解析并发上限 1，一次一个' };
}

function phaseText(j, elapsed) {
  const s = Math.round(elapsed);
  if (j.status === 'queued') {
    return '排队中（已等待 ' + s + ' 秒）· 解析并发上限 1，一次一个';
  }
  if (j.status === 'running') {
    // 首次解析的真实构成：onefile 解包 ~4.3s + import pymupdf ~1.8s + 抽取。
    return '正在解析（已等待 ' + s + ' 秒）· 首次约需 7 秒，慢在程序解包与加载 PDF 引擎';
  }
  return JOB_STATUS_TEXT[j.status] || j.status;
}

function reportOutcome(r, name) {
  if (r.stopped) return;
  if (r.timeout) {
    ElMessage.warning('「' + name + '」等待超过 3 分钟仍未返回。'
      + '解析可能还在跑（超大文件），刷新列表可看到最终状态。');
    return;
  }
  if (r.offline) {
    jobOfflineHint.value = true;
    ElMessage.warning('与服务器失去联系，停止跟踪「' + name + '」。'
      + '解析可能已完成，刷新列表确认。');
    return;
  }
  if (r.gone) {
    ElMessage.info('「' + name + '」的任务记录已不在（服务重启过）。'
      + '文件本身还在，刷新列表看它的状态。');
    return;
  }
  const j = r.job;
  if (!j) return;
  if (j.status === 'succeeded') {
    ElMessage.success('「' + name + '」解析完成' + (j.result_status === 'repaired_ok'
      ? '（文件有损坏，已尽力修复）' : ''));
  } else if (j.status === 'interrupted') {
    ElMessage.warning('「' + name + '」解析被中断，重新上传即可重试。');
  } else {
    ElMessage.error('「' + name + '」未通过：' + (resultLabel(j.result_status) || j.result_status));
  }
}

function dropJob(jobId) {
  const p = _pollers.find((x) => x.jobId === jobId);
  if (p) { p.stop(); const i = _pollers.indexOf(p); if (i >= 0) _pollers.splice(i, 1); }
  activeJobs.value = activeJobs.value.filter((x) => x.jobId !== jobId);
  refresh();
}

/* ---------------------------------------------------------------- 详情 */

async function open(d) {
  if (sel.value && sel.value.id === d.id) { sel.value = null; detail.value = null; return; }
  sel.value = d;
  detail.value = null;
  // job 表不跨重启，meta 里没有 job_id 时只能靠 status 猜当前态；
  // 有 job_id 就恢复跟踪（刷新页面后不丢进度反馈）。
  if (d.job_id && (d.status === 'queued' || d.status === 'running')) {
    trackJob({ id: d.job_id, document_id: d.id }, d.original_name);
  }
  await gotoPage(1);
}

async function gotoPage(n) {
  if (!sel.value || n < 1 || (pageTotal.value && n > pageTotal.value)) return;
  pageLoading.value = true;
  try {
    const body = await getPage(sel.value.id, n);
    detail.value = { doc: sel.value, page: body.page, item: body.item };
  } catch (e) {
    ElMessage.error('读第 ' + n + ' 页失败：' + e.message);
    detail.value = { doc: sel.value, page: n, item: null };
  } finally {
    pageLoading.value = false;
  }
}

/**
 * 送去摘录：把这一页的文本预填进 ExcerptView，并带上来源引用。
 *
 * 来源引用走 **sessionStorage** 而不是 URL query：
 * 一页文本可能几千字，塞进 URL 会撑爆地址栏（且 hash 路由下更难看），
 * 还会把公司真题内容留在浏览器历史里——这是用户数据，不该出现在 URL。
 * sessionStorage 刷新页面还在，关掉标签页就没了，生命周期刚好。
 */
function sendToExcerpt() {
  const it = detail.value && detail.value.item;
  if (!it || !it.chars) {
    ElMessage.warning('这一页没有文字，没法预填。先用 OCR 转出文字，或从别的页选。');
    return;
  }
  try {
    sessionStorage.setItem('qbank.excerptSeed', JSON.stringify({
      text: it.text,
      docId: sel.value.id,
      docName: sel.value.original_name || sel.value.id,
      page: detail.value.page,
      printedLabel: it.printed_label || detail.value.page,
      chars: it.chars,
    }));
  } catch (e) {
    ElMessage.error('无法暂存本页文本（浏览器存储被禁）。请改用「PDF 摘录」手工粘贴。');
    return;
  }
  router.push('/excerpt');
}

async function remove(d) {
  try {
    await ElMessageBox.confirm(
      '删除「' + (d.original_name || d.id) + '」？\n'
      + 'PDF 文件、抽取出的页文本，以及由它生成的题目草稿会一并删除，无法恢复。',
      '删除确认', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' });
  } catch (e) { return; }
  try {
    await deleteDocument(d.id);
    if (sel.value && sel.value.id === d.id) { sel.value = null; detail.value = null; }
    ElMessage.success('已删除');
    refresh();
  } catch (e) {
    ElMessage.error('删除失败：' + e.message);
  }
}

onMounted(refresh);
onBeforeUnmount(() => { _pollers.forEach((p) => p.stop()); _pollers.length = 0; });

/**
 * 测试钩子：暴露给验收脚本用的**只读**接口。
 *
 * ## 为什么需要它
 * 「正在解析」那一行在真实环境里**几乎抓不到**：解析一个 PDF 只要
 * 0.3–0.5s，而轮询间隔 900ms，UI 会从「排队中」直接跳到完成。
 * 用 CDP 拦截 /parse-jobs 的响应也拦不到（实测 paused=0）——
 * 请求刚发出，任务就已经是终态了。
 *
 * 所以让验收脚本能**确定性地**检查这段文案：phaseText 就是渲染它的那个函数。
 *
 * ⚠️ 只暴露 phaseText，**不暴露 trackJob**：
 * 实测压缩产物里 `window.__qbankDocs.track` 引用到了别的模块的同名符号
 * （Element Plus 里也有一个短名 y），调用时报
 * `p.promise.then is not a function`。phaseText 是纯函数（只吃 status+秒数），
 * 不引用组件内部状态，所以没有这个问题。
 *
 * 它不改变任何运行时行为，只挂一个只读函数。
 */
if (typeof window !== 'undefined') {
  window.__qbankDocs = {
    phaseText,
    state: () => ({ jobs: activeJobs.value, docs: docs.value, offline }),
  };
}
</script>

<style scoped>
.job-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line);
}
.job-row .pill { max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.doc-row {
  margin-top: 12px; padding-top: 12px;
  border-top: 1px solid var(--line);
}
.doc-row.open { background: #fbfcfd; border-radius: 8px; padding: 10px 10px 0; margin-top: 12px; }
.doc-name {
  flex: 1; min-width: 0;
  font-size: 14px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.doc-meta { margin-top: 3px; font-size: 12px; }

.notice {
  margin-top: 8px; padding: 10px 12px; border-radius: 9px;
  font-size: 13.5px; line-height: 1.7;
}
.notice-bad { background: var(--bad-weak); color: var(--bad); }
.notice-bad .meta { color: #8a5050; }
.notice-warn { background: var(--amber-weak); color: #92580a; }
.notice-warn .meta { color: #7d5a17; }

.pager {
  display: flex; align-items: center; gap: 8px;
  margin-top: 10px; flex-wrap: wrap;
}
.anchors {
  flex: 1 1 200px; min-width: 0;
  display: flex; flex-wrap: wrap; gap: 4px;
  max-height: 76px; overflow-y: auto;
}
.anchor {
  font: inherit; font-size: 12px; line-height: 1;
  padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 6px; background: #fff; color: var(--ink); cursor: pointer;
}
.anchor:hover { border-color: var(--steel); color: var(--steel-deep); }
.anchor.on { background: var(--steel); border-color: var(--steel); color: #fff; font-weight: 600; }

.page-text {
  margin-top: 10px; padding: 12px 14px;
  border: 1px solid var(--line); border-radius: 9px;
  background: #fcfcfd;
  font-size: 15px; line-height: 1.9;
  white-space: pre-wrap; word-break: break-word;
  max-height: 60vh; overflow-y: auto;
}

@media (max-width: 480px) {
  .doc-row.open { padding: 8px 8px 0; }
  .page-text { font-size: 14.5px; padding: 10px 11px; max-height: 50vh; }
  .anchors { max-height: 60px; }
  .job-row .pill { max-width: 100%; }
}
</style>
