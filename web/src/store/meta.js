/* meta.js — 筛选项元数据（题库/来源下拉 + 题型/难度真实分布），懒加载一次；
 * 导入新题后由调用方 force=true 重建（题库分布变了） */
import { ref } from 'vue'
import { api } from '@/api'

export const banks = ref([]);      // [{bank, n}]
export const sections = ref([]);   // [{section, n}]
export const byType = ref({});     // {题型: 题数}
export const byLevel = ref({});    // {难度: 题数}
const loaded = ref(false);

export async function loadMeta(force) {
  if (loaded.value && !force) return;
  loaded.value = true;
  const [bs, ss, st] = await Promise.all([api('/banks'), api('/sections'), api('/stats')]);
  banks.value = (bs && bs.items) || [];
  sections.value = (ss && ss.items) || [];
  if (st) { byType.value = st.by_type || {}; byLevel.value = st.by_level || {}; }
}
