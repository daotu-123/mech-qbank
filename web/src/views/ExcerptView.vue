/* ExcerptView.vue — PDF 摘录编辑器：粘贴文本 → 划词挖空 / 设为判断 → 存进题库
 *
 * 为什么不做 PDF 解析：PDF 版式（两栏/公式/表格）还原永远有残渣，且 file:// 下
 * pdf.js 的 worker 加载会冲击 vite iife 单包 + file:// 双闸。用户从阅读器复制文本
 * 本就是既有习惯，这里只把「录入题库」这一步压缩到几秒。
 *
 * 数据真相：段落 p.text 是唯一权威（含【1】【2】空位标记）；划词偏移用 Range 计算，
 * 挖空后立即重编号，保证「题干第 N 个空」与「答案数组第 N 项」严格对齐。
 * 保存走 /import_questions：在线模式写 sqlite（source_class=自定义，重建不丢），
 * 离线模式写 localStorage（static-store 的 merge_questions）。
 */
<template>
  <section>
    <div class="card">
      <div class="row">
        <b>PDF 摘录 → 挖空 / 判断题</b>
        <span class="sub">从 PDF 复制文本粘贴到下面，或把 .txt / .md 拖进来</span>
      </div>
      <div style="margin-top:10px" @dragover.prevent @drop.prevent="onDrop">
        <el-input v-model="raw" type="textarea" :rows="6"
                  placeholder="在这里粘贴从 PDF 复制的文本…（支持整段粘贴，按空行或单行拆段）" />
      </div>
      <div class="row" style="margin-top:10px">
        <button class="act" @click="split">拆成段落</button>
        <button class="ghost" @click="raw = ''">清空</button>
        <span class="meta">拆段规则：优先按空行，没有空行时按单行</span>
      </div>
    </div>

    <div v-if="paras.length" class="card">
      <div class="row">
        <b>逐段摘录</b>
        <span class="sub">{{ paras.length }} 段 · 先在段落里划词，再点「挖空」</span>
        <button class="ghost" style="margin-left:auto" @click="paras = []">收起</button>
      </div>
    </div>

    <div v-for="p in paras" :key="p.pid" class="card">
      <div class="row">
        <span class="pill gray">第 {{ p.pid + 1 }} 段</span>
        <span v-if="p.savedId" class="pill green">已存 {{ p.savedId }}</span>
        <span class="pill amber" v-if="p.mode === 'judge'">判断</span>
        <span class="pill amber" v-else-if="p.answers.length">{{ p.answers.length }} 个空</span>
        <span class="meta" style="margin-left:auto">{{ p.answers.length ? '答案：' + p.answers.join(' | ') : '' }}</span>
      </div>

      <div class="para-text" @mouseup="onSelect(p, $event)">{{ p.text }}</div>

      <div v-if="p.sel" class="row" style="margin-top:6px">
        <span class="meta">已选「{{ trunc(p.sel.text, 40) }}」</span>
        <button class="act" @click="makeBlank(p)">挖空</button>
        <button class="ghost" @click="p.sel = null">取消选择</button>
      </div>

      <div class="row" style="margin-top:8px">
        <button class="ghost" @click="toggleJudge(p)">{{ p.mode === 'judge' ? '取消判断模式' : '设为判断题' }}</button>
        <button class="act" :disabled="!canSave(p)" @click="save(p)">保存本题</button>
        <button v-if="p.answers.length || p.savedId" class="ghost" @click="resetPara(p)">重置本段</button>
        <span class="meta">{{ hint(p) }}</span>
      </div>

      <template v-if="p.mode === 'judge'">
        <div class="row" style="margin-top:8px">
          <span class="meta">这句是对还是错：</span>
          <el-radio-group v-model="p.truth" size="small">
            <el-radio-button label="对" />
            <el-radio-button label="错" />
          </el-radio-group>
        </div>
      </template>
      <el-input v-model="p.expl" size="small" style="margin-top:8px"
                placeholder="题解（可选：为什么对 / 错在哪）" />
    </div>

    <div class="card">
      <div class="row">
        <b>已录入的自定义题</b>
        <span class="sub">{{ customItems.length }} 道</span>
        <button class="ghost" style="margin-left:auto" @click="loadCustom">刷新</button>
      </div>
      <div v-if="!customItems.length" class="empty">还没有——上面摘录一段文本开始</div>
      <div v-for="it in customItems" :key="it.id" class="row"
           style="border-top:1px solid var(--line);padding:8px 0;flex-wrap:nowrap">
        <span class="pill gray">{{ it.id }}</span>
        <span class="pill amber">{{ it.qtype }}</span>
        <span style="flex:1;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ it.stem }}</span>
        <button class="ghost" style="flex:none" @click="removeOne(it.id)">删除</button>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { bump } from '@/store/bus'

const raw = ref('')
const paras = ref([])
const customItems = ref([])
const BANK = 'PDF 自建'

const today = computed(() => {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
});

function trunc(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

/* 拆段：优先空行，退化为单行；空白段丢弃 */
function split() {
  const txt = (raw.value || '').replace(/\r\n/g, '\n').trim();
  if (!txt) { ElMessage.warning('先粘贴文本'); return; }
  let blocks = txt.split(/\n\s*\n/).map((x) => x.trim()).filter(Boolean);
  if (blocks.length <= 1) blocks = txt.split('\n').map((x) => x.trim()).filter(Boolean);
  paras.value = blocks.map((text, i) => ({
    pid: i, text, answers: [], sel: null, mode: 'fill', truth: '对', expl: '', savedId: '',
  }));
  ElMessage.success('已拆成 ' + blocks.length + ' 段');
}

function onDrop(e) {
  const f = (e.dataTransfer.files || [])[0];
  if (!f) return;
  if (/\.pdf$/i.test(f.name)) {
    ElMessage.warning('PDF 请先在阅读器里复制文本再粘贴（本页不解析 PDF 版式）');
    return;
  }
  const rd = new FileReader();
  rd.onload = () => { raw.value = String(rd.result || ''); split(); };
  rd.readAsText(f, 'utf-8');
}

/* 划词：Range 前段文本长度即字符偏移。段落 div 内只有纯文本节点，偏移永远准。 */
function onSelect(p, e) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount || sel.isCollapsed) { p.sel = null; return; }
  const range = sel.getRangeAt(0);
  const div = e.currentTarget;
  if (!div.contains(range.commonAncestorContainer)) { p.sel = null; return; }
  const pre = document.createRange();
  pre.selectNodeContents(div);
  pre.setEnd(range.startContainer, range.startOffset);
  const start = pre.toString().length;
  const picked = range.toString();
  if (!picked.trim()) { p.sel = null; return; }
  p.sel = { start, end: start + picked.length, text: picked.trim() };
}

/* 挖空：选中词替换为【N】，随后重编号，保证题干顺序 = 答案数组顺序 */
function makeBlank(p) {
  const sel = p.sel;
  if (!sel) return;
  const s = Math.min(sel.start, sel.end), e = Math.max(sel.start, sel.end);
  if (e <= s) return;
  const word = p.text.slice(s, e);
  if (/【\d+】/.test(word)) { ElMessage.warning('选中范围里有已有空位，换一段'); return; }
  if (!word.trim()) { ElMessage.warning('选中的是空白字符'); return; }
  p.answers.push(word);
  p.text = p.text.slice(0, s) + '【' + p.answers.length + '】' + p.text.slice(e);
  renumber(p);
  p.sel = null;
  p.savedId = '';
}

function renumber(p) {
  const marks = p.text.match(/【\d+】/g) || [];
  if (!marks.length) return;
  p.answers = marks.map((m) => p.answers[+m.slice(1, -1) - 1] || '');
  let k = 0;
  p.text = p.text.replace(/【\d+】/g, () => '【' + (++k) + '】');
}

function toggleJudge(p) {
  p.mode = p.mode === 'judge' ? 'fill' : 'judge';
}

/* 重置本段：占位符按序还原成原词（renumber 保证答案顺序 = 题干占位符顺序） */
function resetPara(p) {
  let k = 0;
  p.text = p.text.replace(/【\d+】/g, () => p.answers[k++] || '');
  p.answers = [];
  p.sel = null;
  p.savedId = '';
  p.expl = '';
}

function canSave(p) {
  if (p.mode === 'judge') return true;
  return p.answers.length > 0;
}

function hint(p) {
  if (p.savedId) return '已入库，可继续在这段上挖空后重复保存（会生成新题号）';
  if (p.mode === 'judge') return '保存为判断题：'+ p.truth + '（自动判分）';
  if (!p.answers.length) return '先划词挖空，再保存为填空题';
  return '保存为填空题：' + p.answers.length + ' 个空（自动判分，可多空）';
}

function nextId() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  const ymd = String(d.getFullYear()).slice(2) + p(d.getMonth() + 1) + p(d.getDate());
  const used = new Set((customItems.value || []).map((x) => x.id));
  let n = 1;
  while (used.has('X' + ymd + '-' + String(n).padStart(3, '0'))) n++;
  return 'X' + ymd + '-' + String(n).padStart(3, '0');
}

function buildQ(p) {
  const base = {
    id: nextId(), level: 1, bank: BANK,
    section_path: 'PDF 摘录 · ' + today.value,
    explanation: (p.expl || '').trim(),
  };
  if (p.mode === 'judge') {
    return { ...base, stem: p.text, qtype: '判断', answer: p.truth };
  }
  if (!p.answers.length) return null;
  return { ...base, stem: p.text, qtype: '填空', answer: p.answers.join('|') };
}

async function save(p) {
  const q = buildQ(p);
  if (!q) { ElMessage.warning('先划词挖空'); return; }
  const r = await api('/import_questions', { questions: [q], mode: 'skip' });
  if (!r || !r.ok) return;
  if (r.added) {
    p.savedId = q.id;
    ElMessage.success('已存入题库：' + q.id + '（在「刷题」里可用题库筛选「' + BANK + '」找到）');
  } else if (r.skipped) {
    ElMessage.warning('题号 ' + q.id + ' 已存在，换个题号再存');
  } else {
    const e = (r.rejected && r.rejected[0]) || {};
    ElMessage.error('保存失败：' + (e.err || '未知原因'));
  }
  await loadCustom();
  bump();
}

async function loadCustom() {
  const r = await api('/custom');
  customItems.value = (r && r.items) || [];
}

async function removeOne(id) {
  try {
    await ElMessageBox.confirm('删除自定义题 ' + id + '？相关进度与备注一并失效。', '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' });
  } catch (e) { return; }
  const r = await api('/custom/' + encodeURIComponent(id), null, 'DELETE');
  if (r && r.ok) { ElMessage.success('已删除 ' + id); await loadCustom(); bump(); }
}

onMounted(loadCustom);
</script>

<style scoped>
.para-text {
  font-size: 15.5px;
  line-height: 1.9;
  white-space: pre-wrap;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-top: 8px;
  background: #fcfcfd;
  cursor: text;
  user-select: text;
}
.para-text::selection { background: #ffe9b8; }
</style>
