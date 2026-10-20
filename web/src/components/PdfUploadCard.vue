/* PdfUploadCard.vue — 上传入口：点选 + 拖拽 + 真实上传进度 + 取消
 *
 * ## 离线（file://）行为
 * 不静默失败、不假装能传：直接显示「需要连接服务器」并说明怎么连。
 * 这是硬约束——file:// 下没有服务器，PDF 域整块不可用。
 *
 * ## 为什么用 XHR 而不是 fetch
 * fetch 拿不到 upload 进度（request.body 的 stream 形态不支持 FormData，
 * 也不能自己设 multipart 边界）。要真实进度条只能 XHR。
 * 详见 pdfdocs.js 的 uploadDocument。
 *
 * ## 为什么取消是「真的取消」
 * xhr.abort() 会真的断开连接。此时服务端**可能**已经收到部分数据——
 * 这一点如实写在取消后的提示里，不假装「取消 = 没上传过」。
 * （服务端按 Content-Length 提前拒绝超大请求，所以半截数据不会变成坏文档。）
 */
<template>
  <div class="card">
    <!-- 离线态：明确提示，不给假入口 -->
    <template v-if="offline">
      <div class="row">
        <b>PDF 文档</b>
        <span class="sub">需要连接服务器</span>
      </div>
      <div class="notice offline-notice">
        <b>当前是离线模式（file:// 直接打开），无法上传 PDF。</b>
        <div class="meta" style="margin-top:6px">
          PDF 解析在服务端完成（独立的解析进程，机器 117MB 固定开销，
          不能塞进浏览器），所以离线状态下整个「PDF 文档」功能不可用。
        </div>
        <div class="meta" style="margin-top:6px">
          要用它：启动 mech-qbank（双击 exe），然后访问
          <code>http://127.0.0.1:8640</code>；或在浏览器里打开那个地址，
          再从页面进入「PDF 文档」。
        </div>
        <div class="meta" style="margin-top:6px">
          离线也有可用的路径：把 PDF 里的文字复制出来，用
          <router-link to="/excerpt">「PDF 摘录」</router-link> 粘贴录入——这条流程不依赖服务器。
        </div>
      </div>
    </template>

    <!-- 在线态：上传入口 -->
    <template v-else>
      <div class="row">
        <b>上传 PDF</b>
        <span class="sub">上传 PDF</span>
      </div>

      <div ref="dropzone"
           class="dropzone"
           :class="{ over: dragOver, busy: !!uploading }"
           @dragover.prevent="onDragOver"
           @dragleave.prevent="dragOver = false"
           @drop.prevent="onDrop">
        <input ref="picker" type="file" accept="application/pdf,.pdf" hidden
               @change="onPick" />
        <template v-if="!uploading">
          <div class="dz-title">把 PDF 拖到这里，或
            <button class="linkish" @click="pick">点选文件</button>
          </div>
          <div class="meta">上传后由服务端独立进程解析（并发上限 1，多传会排队）</div>
        </template>

        <!-- 上传中：真实进度 + 取消 -->
        <template v-else>
          <div class="dz-title">{{ cur.name }}</div>
          <div class="progress-track">
            <div class="progress-fill" :style="{ width: pct() + '%' }"></div>
          </div>
          <div class="row" style="margin-top:6px">
            <span class="meta">{{ phaseText() }}</span>
            <button class="ghost" style="margin-left:auto" @click="cancel">取消</button>
          </div>
        </template>
      </div>

      <!-- 失败：按类型给不同出路，绝不只写「上传失败」 -->
      <div v-if="errMsg" class="notice notice-bad">
        <b>{{ errTitle }}</b>
        <div class="meta" style="margin-top:4px">{{ errMsg }}</div>
        <div v-if="errAction" class="meta" style="margin-top:6px">{{ errAction }}</div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  MAX_UPLOAD_BYTES, uploadDocument, fmtBytes,
} from '@/pdfdocs'

const props = defineProps({
  offline: { type: Boolean, default: false },
})
const emit = defineEmits(['uploaded', 'gone'])

const dropzone = ref(null)
const picker = ref(null)
const dragOver = ref(false)
const uploading = ref(false)
const cur = ref({ name: '' })
const loaded = ref(0)
const total = ref(0)
/** 上传字节已发完但还在等 202 —— 这段最容易被误显示成「上传卡住了」。 */
const awaiting = ref(false)

const errMsg = ref('')
const errTitle = ref('')
const errAction = ref('')

const pct = () => (total.value ? Math.min(100, Math.round((loaded.value / total.value) * 100)) : 0)
const phaseText = () => (awaiting.value
  ? '已传完，正在等服务器接收（下一步开始解析）'
  : '上传中 ' + fmtBytes(loaded.value) + ' / ' + fmtBytes(total.value)
     + '（' + pct() + '%）')

let handle = null

function pick() { if (picker.value) picker.value.click(); }

function onPick(e) {
  const f = e.target.files && e.target.files[0];
  e.target.value = '';                 // 允许连续选同一个文件
  if (f) start(f);
}

function onDragOver(e) {
  if (props.offline || uploading.value) return;
  if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0) {
    dragOver.value = true;
  }
}

function onDrop(e) {
  dragOver.value = false;
  if (props.offline || uploading.value) return;
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  if (!/\.pdf$/i.test(f.name)) {
    fail('不是 PDF 文件', '只接受 .pdf。「' + f.name + '」看起来不是 PDF。', '');
    return;
  }
  start(f);
}

/** 前置大小检查：服务端也会拒（413），但本地早拒能省掉一次白跑的传输。 */
function start(file) {
  clearErr();
  if (!/\.pdf$/i.test(file.name)) {
    fail('不是 PDF 文件', '只接受 .pdf。「' + file.name + '」看起来不是 PDF。', '');
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    fail('文件太大',
      '这个文件 ' + fmtBytes(file.size) + '，超过上限 ' + fmtBytes(MAX_UPLOAD_BYTES) + '。',
      '服务端按请求体大小硬拒（HTTP 413），传不上去。可以先用阅读器把相关章节'
      + '另存成小文件，或删掉不相关的页面后再传。');
    return;
  }
  uploading.value = true;
  awaiting.value = false;
  cur.value = { name: file.name, size: file.size };
  loaded.value = 0;
  total.value = file.size;

  handle = uploadDocument(file, (l, t) => { loaded.value = l; total.value = t; });
  handle.promise.then((body) => {
    uploading.value = false;
    handle = null;
    ElMessage.success('已接收「' + file.name + '」，正在后台解析');
    emit('uploaded', (body && body.job) || null);
  }).catch((err) => {
    uploading.value = false;
    handle = null;
    if (err.code === 'aborted') {
      // 说实话：abort 后服务端可能已收到数据，所以给「刷新列表确认」的出路，
      // 而不是宣称「已完全取消」。
      ElMessage.warning('已取消上传。若列表里出现了这个文件，说明服务端已收到，请删除它。');
      emit('gone');
      return;
    }
    if (err.code === 'too_large') {
      fail('文件太大（HTTP 413）',
        '这个文件 ' + fmtBytes(file.size) + '，超过上限 ' + fmtBytes(MAX_UPLOAD_BYTES) + '。',
        '把不相关的页面删掉再传，或用阅读器「另存为」只保留需要的章节。');
      return;
    }
    if (err.code === 'network') {
      fail('连接中断', err.message || '上传过程中与服务器的连接断了。',
        '确认 mech-qbank 还在运行（页面右下角有「退出程序」按钮可证），'
        + '然后重试。已上传成功的文件不受影响。');
      return;
    }
    fail('上传失败', err.message || '服务器没有接受这个文件。',
      '如果是文件本身的问题，试试先用阅读器打开它——打不开就是文件坏了。');
  });
}

function cancel() {
  if (handle) handle.cancel();
}

function clearErr() { errMsg.value = ''; errTitle.value = ''; errAction.value = ''; }
function fail(t, m, a) { errTitle.value = t; errMsg.value = m; errAction.value = a; }
</script>

<style scoped>
.dropzone {
  margin-top: 10px;
  border: 1.5px dashed var(--line);
  border-radius: 10px;
  padding: 20px 14px;
  text-align: center;
  background: #fcfcfd;
  transition: border-color .15s, background .15s;
}
.dropzone.over { border-color: var(--steel); background: var(--steel-weak); }
.dropzone.busy { border-style: solid; }
.dz-title { font-size: 14.5px; font-weight: 600; }
.linkish {
  font: inherit; font-size: inherit; font-weight: 600;
  border: 0; background: none; padding: 0; cursor: pointer;
  color: var(--steel-deep); text-decoration: underline;
}
.progress-track {
  margin: 10px auto 0;
  height: 6px;
  max-width: 420px;
  background: #eceef1;
  border-radius: 20px;
  overflow: hidden;
}
.progress-fill { height: 100%; background: var(--steel); transition: width .2s; }
.notice {
  margin-top: 10px;
  padding: 11px 13px;
  border-radius: 9px;
  font-size: 13.5px;
  line-height: 1.7;
}
.notice-bad { background: var(--bad-weak); color: var(--bad); }
.notice-bad .meta { color: #8a5050; }
.offline-notice { background: var(--amber-weak); color: #92580a; }
.offline-notice .meta { color: #7d5a17; }
.offline-notice code {
  background: rgba(0, 0, 0, .07); padding: 1px 5px; border-radius: 4px;
  font-size: 12.5px; word-break: break-all;
}
</style>
