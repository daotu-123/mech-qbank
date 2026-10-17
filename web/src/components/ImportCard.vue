/* ImportCard.vue — 题库导入（仅静态/离线模式：导入的题存在本机 localStorage，随 APK 走）
 * 在线模式题库来自 sqlite，要加题请改源库后重建；这里整体置灰并说明，不发请求。 */
<template>
  <div class="card" id="imp-card">
    <div class="row">
      <b>题库导入</b><span class="sub">{{ modeNote }}</span>
      <span class="pill gray" style="margin-left:auto">{{ countText }}</span>
    </div>
    <div :class="{ 'imp-off': !STATIC }">
      <div class="imp-note">
        支持粘贴或选择 JSON 文件：<code>{"questions":[{"id","stem","qtype","level","options","answer","explanation","bank"}]}</code><br>
        · <b>id</b> 必填且不能与内置题重复；<b>qtype</b> 建议 判断/单选/多选/问答/填空/计算；<b>level</b> 为 1–5 难度（可省）<br>
        · 有 <b>options</b> 按选择题自动判分（answer 写正确字母，如 <code>"A"</code> 或 <code>"AB"</code>）；answer 留空 = 答案待补（自评练习）
      </div>
      <div class="row" style="margin-top:10px">
        <button class="ghost" @click="fillTemplate">填入示例模板</button>
        <button class="ghost" @click="pickFile">选择 .json 文件</button>
        <input ref="fileRef" type="file" accept=".json,application/json" class="hide" @change="onFile">
        <label style="margin-left:auto">重复题号：
          <el-select v-model="mode" style="width:170px">
            <el-option value="skip" label="跳过（保留已导入的）" />
            <el-option value="cover" label="覆盖（用新内容替换）" />
          </el-select>
        </label>
      </div>
      <textarea v-model="text" style="margin-top:10px;min-height:120px;font-family:Consolas,monospace;font-size:12.5px"
        placeholder="把题库 JSON 粘贴到这里，再点「导入」"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="act" @click="doImport">导入</button>
        <button class="ghost" :disabled="!customN" @click="clearAll">清空自定义题库</button>
      </div>
      <div class="imp-result" v-html="resultHtml"></div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus';
import { api, SAPI, STATIC } from '@/api';
import { loadMeta } from '@/store/meta';
import { bump } from '@/store/bus';

const modeNote = computed(() => STATIC ? '（导入的题存在本机，随 APK 一起走）'
  : '（仅 APK 离线模式支持；电脑版请把题加进 sqlite 源库后重建）');
const countText = ref('在线模式：题库来自 sqlite');
const customN = ref(0);
const mode = ref('skip');
const text = ref('');
const resultHtml = ref('');
const fileRef = ref(null);

async function refresh() {
  if (!STATIC) return;
  const r = await api('/custom');
  customN.value = (r && r.items) ? r.items.length : 0;
  countText.value = customN.value ? '自定义题 ' + customN.value + ' 道' : '暂无自定义题';
}

function parseQBankJson(t) {
  let d;
  try { d = JSON.parse(t); } catch (e) { throw new Error('JSON 解析失败：' + e.message); }
  const list = Array.isArray(d) ? d : (d && Array.isArray(d.questions) ? d.questions : null);
  if (!list) throw new Error('格式不符：需要 {"questions":[...]} 或题目数组 [...]');
  return list;
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function doImportQuestions(list, srcName) {
  const r = await api('/import_questions', { questions: list, mode: mode.value });
  if (!r) { resultHtml.value = '<span class="bad">导入失败</span>'; return; }
  const parts = ['<span class="ok">新增 ' + r.added + '</span>', '覆盖 ' + r.updated, '跳过重复 ' + r.skipped];
  let html = '<div>' + srcName + '：' + parts.join(' ｜ ') + '</div>';
  if (r.rejected && r.rejected.length) {
    html += '<div class="bad" style="margin-top:4px">拒绝 ' + r.rejected.length + ' 条：' +
      r.rejected.slice(0, 8).map(x => esc(x.id + '（' + x.err + '）')).join('；') +
      (r.rejected.length > 8 ? ' …等' : '') + '</div>';
  }
  resultHtml.value = html;
  const total = (r.added || 0) + (r.updated || 0);
  if (total) {
    ElMessage.success('导入完成：新增 ' + r.added + '，覆盖 ' + r.updated + '，跳过 ' + r.skipped);
    await loadMeta(true);                 // 筛选项下拉按新分布重建
    await refresh();
    bump();
  } else if (!r.rejected || !r.rejected.length) {
    ElMessage.error('没有可导入的新题目');
  }
}

async function fillTemplate() {
  const t = await api('/template');
  if (!t) return;
  text.value = JSON.stringify(t, null, 1);
  resultHtml.value = '<span class="meta">已填入示例模板（3 题）：可直接导入试跑，再改成你自己的题</span>';
}
async function doImport() {
  const txt = (text.value || '').trim();
  if (!txt) { ElMessage.error('先粘贴题库 JSON，或点「填入示例模板」'); return; }
  try { await doImportQuestions(parseQBankJson(txt), '文本导入'); }
  catch (e) { ElMessage.error(e.message); }
}
function pickFile() { fileRef.value && fileRef.value.click(); }
function onFile(e) {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    try { await doImportQuestions(parseQBankJson(rd.result), '文件 ' + f.name); }
    catch (err) { ElMessage.error(err.message); }
  };
  rd.onerror = () => ElMessage.error('文件读取失败');
  rd.readAsText(f);
}
async function clearAll() {
  if (!STATIC || !SAPI) return;
  const n = SAPI.clear_custom();
  text.value = '';
  resultHtml.value = '';
  await loadMeta(true);
  await refresh();
  bump();
  ElMessage.success(n ? '已清空 ' + n + ' 道自定义题，恢复内置题库' : '没有自定义题');
}

onMounted(refresh);
</script>
