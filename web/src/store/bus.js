/* bus.js — 极简事件总线：数据变更版本号。
 * 导入题目/打卡/写备注后 bump()，StatsView 等监听它刷新统计，
 * 免去旧版「if 当前页可见就重载」的手动判断。 */
import { ref } from 'vue'

export const dataVersion = ref(0);
export function bump() { dataVersion.value++; }
