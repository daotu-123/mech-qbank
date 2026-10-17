/* NotePad.vue — 题卡备注区：三态（无备注 → 编辑中 → 已写） */
<template>
  <div class="note-box">
    <template v-if="editing">
      <div class="note-head">
        <span class="note-tag">备注 · {{ qid }}</span>
        <span class="meta">Esc 取消</span>
      </div>
      <textarea v-model="draft" style="min-height:78px;margin-top:7px"
        placeholder="写给自己看的：易错点、口诀、出处、和哪章串起来…"></textarea>
      <div class="row" style="margin-top:7px">
        <button class="act" @click="save">保存备注</button>
        <button class="ghost" @click="cancel">取消</button>
      </div>
    </template>
    <template v-else-if="my">
      <div class="note-head">
        <span class="note-tag">我的备注 · {{ qid }}</span>
        <button class="ghost" style="padding:2px 10px;font-size:12.5px" @click="edit">编辑</button>
        <button class="ghost" style="padding:2px 10px;font-size:12.5px" @click="remove">删除</button>
      </div>
      <div class="note-body">{{ my.note }}</div>
    </template>
    <template v-else>
      <button class="ghost" @click="edit">＋ 给这道题添加备注</button>
      <span class="meta" style="margin-left:8px">备注只属于当前这道题（{{ qid }}），进度页可统一查看</span>
    </template>
  </div>
</template>

<script setup>
/* editing 用 v-model:editing 与父组件联动：父组件的键盘监听要靠它判断 Esc 归谁 */
import { computed, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus';
import { notes, saveNote } from '@/store/notes';

const props = defineProps({ qid: { type: String, required: true } });
const editing = defineModel('editing', { type: Boolean, default: false });
const draft = ref('');
const my = computed(() => notes.value[props.qid] || null);

watch(editing, (v) => { if (v) draft.value = (my.value && my.value.note) || ''; });

function edit() { editing.value = true; }
function cancel() { editing.value = false; }
async function save() {
  if (await saveNote(props.qid, draft.value)) editing.value = false;
}
async function remove() {
  try {
    await ElMessageBox.confirm('删除这道题（' + props.qid + '）的备注？', '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' });
  } catch (e) { return; }
  await saveNote(props.qid, '');
}
</script>
