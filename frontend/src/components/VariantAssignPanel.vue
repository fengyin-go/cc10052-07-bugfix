<template>
  <section class="flow-panel" data-testid="variant-assignment-panel">
    <div class="flow-head">
      <div>
        <h3>名单提交与分组办理回执</h3>
        <p>逐条确认、合并和未办状态都保留在回执中；断线后可打开原回执继续领取。</p>
      </div>
      <button class="btn ghost" type="button" data-testid="refresh-receipts" @click="refreshCurrent">
        刷新回执
      </button>
    </div>

    <form class="flow-form" data-testid="batch-form" @submit.prevent="submitManifest">
      <label class="flow-field">
        <span>实验编码</span>
        <input v-model="experimentCode" data-testid="batch-experiment" placeholder="例如 EXP-001" required />
      </label>
      <label class="flow-field">
        <span>名单/确认批次（可作为幂等回执号）</span>
        <input v-model="batchId" data-testid="batch-id" placeholder="例如 A" />
      </label>
      <label class="flow-field wide">
        <span>名单明细（逗号、空格或换行分隔；可直接粘贴 u1,u1,u2）</span>
        <textarea
          v-model="usersText"
          data-testid="batch-users"
          rows="3"
          placeholder="u1,u1,u2"
          required
        />
      </label>
      <label class="flow-field">
        <span>分组策略</span>
        <select v-model="strategy" data-testid="batch-strategy">
          <option>轮转分组</option>
          <option>前半实验组/后半对照组</option>
          <option>稳定哈希</option>
        </select>
      </label>
      <label class="flow-field narrow">
        <span>模拟处理 N 条后中断（可选）</span>
        <input
          v-model="interruptAfter"
          data-testid="interrupt-after"
          type="number"
          min="0"
          placeholder="留空全部办结"
        />
      </label>
      <div class="flow-actions wide">
        <button class="btn primary" type="submit" data-testid="submit-manifest" :disabled="submitting">
          {{ submitting ? '提交中…' : '批量分组并提交名单' }}
        </button>
        <span v-if="message" class="flow-message" data-testid="batch-message">{{ message }}</span>
      </div>
    </form>

    <div class="receipt-opener">
      <input
        v-model="openReceiptId"
        data-testid="open-receipt-input"
        placeholder="连接恢复后输入原回执编号，如 A"
      />
      <button class="btn" type="button" data-testid="open-receipt-button" @click="openReceipt">
        打开原回执
      </button>
    </div>

    <div v-if="receipt" class="receipt-card" data-testid="receipt-detail" :data-receipt-id="receipt.receipt_id">
      <div class="receipt-head">
        <div>
          <h4>分组办理回执 <span data-testid="receipt-id">{{ receipt.receipt_id }}</span></h4>
          <p>
            实验 {{ receipt.实验编码 }} · 当前状态
            <strong data-testid="receipt-status">{{ receipt.status }}</strong>
          </p>
        </div>
        <div class="receipt-actions">
          <button
            class="btn"
            type="button"
            data-testid="claim-pending"
            :disabled="!receipt.counts?.pending"
            @click="claimAllPending"
          >
            重新领取/办理未办行（{{ receipt.counts?.pending ?? 0 }}）
          </button>
          <button
            class="btn primary"
            type="button"
            data-testid="start-check-from-receipt"
            @click="startCheck"
          >
            从回执发起本实验核对
          </button>
        </div>
      </div>

      <div class="mini-stats">
        <span>逐条确认：<b data-testid="count-confirmed">{{ receipt.counts?.confirmed ?? 0 }}</b></span>
        <span>合并：<b data-testid="count-merged">{{ receipt.counts?.merged ?? 0 }}</b></span>
        <span>未办：<b data-testid="count-pending">{{ receipt.counts?.pending ?? 0 }}</b></span>
        <span>冲突：<b data-testid="count-conflict">{{ receipt.counts?.conflict ?? 0 }}</b></span>
      </div>

      <table class="line-table">
        <thead>
          <tr>
            <th>#</th>
            <th>用户</th>
            <th>办理状态</th>
            <th>实际组别</th>
            <th>归属确认结果</th>
            <th>说明</th>
            <th>继续办理</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="line in receipt.items"
            :key="line.line_id"
            :data-testid="`receipt-line-${line.user_id}-${line.line_no}`"
            :data-state="line.state"
          >
            <td>{{ line.line_no }}</td>
            <td>{{ line.用户标识 }}</td>
            <td>
              <span class="badge" :data-state="line.state" data-testid="line-status">
                {{ line.status }}<template v-if="line.conflict">/冲突</template>
              </span>
            </td>
            <td>{{ line.实际组别 || '—' }}</td>
            <td>{{ line.来源确认批次 || '—' }}</td>
            <td class="detail-cell">
              <div>{{ line.说明 }}</div>
              <div v-if="line.later_excluded" class="later-excluded" data-testid="line-later-excluded">
                后来已剔除<template v-if="line.剔除原因">：{{ line.剔除原因 }}</template>
              </div>
              <div v-if="line.差异编号" class="line-difference" data-testid="line-difference-ref">
                差异记录：{{ line.差异编号 }}
              </div>
            </td>
            <td>
              <button
                v-if="line.state === 'pending'"
                class="link"
                type="button"
                :data-testid="`claim-line-${line.line_id}`"
                @click="claimLine(line.line_id)"
              >
                领取本行
              </button>
              <span v-else>已办结</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="checkResult" class="check-result" data-testid="receipt-check-result">
        已发起核对 <strong>{{ checkResult.核对批次 || checkResult.entry?.核对批次 }}</strong>：
        分子 {{ checkResult.分子 ?? checkResult.entry?.分子 }}，
        分母 {{ checkResult.分母 ?? checkResult.entry?.分母 }}，
        重复排除 {{ checkResult.重复排除数 ?? checkResult.entry?.重复排除数 }}。
        <RouterLink :to="`/sample_ratio?experiment_code=${encodeURIComponent(String(receipt.实验编码 || ''))}`">
          前往样本量核对页
        </RouterLink>
      </div>
    </div>

    <div class="receipt-list" data-testid="receipt-list">
      <h4>最近回执</h4>
      <button
        v-for="item in receipts"
        :key="item.receipt_id"
        type="button"
        class="receipt-chip"
        :data-testid="`receipt-list-item-${item.receipt_id}`"
        @click="chooseReceipt(item.receipt_id)"
      >
        {{ item.receipt_id }} · {{ item.实验编码 }} · {{ item.status }}
        （确认 {{ item.counts?.confirmed ?? 0 }}/合并 {{ item.counts?.merged ?? 0 }}/未办 {{ item.counts?.pending ?? 0 }}）
      </button>
      <span v-if="!receipts.length" class="muted">暂无回执</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import {
  claimAssignmentReceipt,
  createCheckFromReceipt,
  getAssignmentReceipt,
  listAssignmentReceipts,
  submitAssignment,
} from '@/api/backend-service'

const emit = defineEmits<{ changed: [] }>()

const experimentCode = ref('EXP-E2E')
const batchId = ref('')
const usersText = ref('u1,u1,u2')
const strategy = ref('轮转分组')
const interruptAfter = ref('')
const openReceiptId = ref('')
const submitting = ref(false)
const message = ref('')
const receipt = ref<any>(null)
const receipts = ref<any[]>([])
const checkResult = ref<any>(null)

async function loadReceipts() {
  try {
    const payload = await listAssignmentReceipts()
    receipts.value = payload.items ?? []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回执列表读取失败'
  }
}

async function chooseReceipt(id: string) {
  message.value = ''
  checkResult.value = null
  receipt.value = await getAssignmentReceipt(id)
  openReceiptId.value = id
}

async function openReceipt() {
  if (!openReceiptId.value.trim()) return
  try {
    await chooseReceipt(openReceiptId.value.trim())
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回执读取失败'
  }
}

async function submitManifest() {
  submitting.value = true
  message.value = ''
  checkResult.value = null
  try {
    const users = usersText.value.split(/[\s,，;；]+/).map((u) => u.trim()).filter(Boolean)
    const payload: Record<string, unknown> = {
      experiment_code: experimentCode.value.trim(),
      user_ids: users,
      strategy: strategy.value,
    }
    if (batchId.value.trim()) payload.batch_id = batchId.value.trim()
    if (interruptAfter.value !== '') payload.interrupt_after = Number(interruptAfter.value)
    const result = await submitAssignment(payload)
    if (!result.ok) throw new Error(result.message || '名单提交失败')
    receipt.value = result.receipt ?? result
    openReceiptId.value = receipt.value.receipt_id
    message.value = result.message
    await loadReceipts()
    emit('changed')
  } catch (error) {
    message.value = error instanceof Error ? error.message : '名单提交失败'
  } finally {
    submitting.value = false
  }
}

async function refreshCurrent() {
  if (receipt.value?.receipt_id) {
    receipt.value = await getAssignmentReceipt(receipt.value.receipt_id)
  }
  await loadReceipts()
  emit('changed')
}

async function claimAllPending() {
  message.value = ''
  try {
    const result = await claimAssignmentReceipt(receipt.value.receipt_id)
    message.value = result.message
    await refreshCurrent()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '领取失败'
  }
}

async function claimLine(lineId: string) {
  try {
    await claimAssignmentReceipt(receipt.value.receipt_id, { line_ids: [lineId] })
    await refreshCurrent()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '本行领取失败'
  }
}

async function startCheck() {
  checkResult.value = await createCheckFromReceipt(receipt.value.receipt_id)
  emit('changed')
}

function handleOpenEvent(event: Event) {
  const detail = (event as CustomEvent<string>).detail
  if (detail) void chooseReceipt(String(detail))
}

onMounted(async () => {
  window.addEventListener('open-assignment-receipt', handleOpenEvent)
  await loadReceipts()
  if (!receipt.value && receipts.value[0]?.receipt_id) {
    await chooseReceipt(receipts.value[0].receipt_id)
  }
})

onUnmounted(() => {
  window.removeEventListener('open-assignment-receipt', handleOpenEvent)
})
</script>

<style scoped>
.flow-panel { border: 1px solid var(--border, #e5e7eb); border-radius: 14px; padding: 18px; margin: 18px 0; background: #fff; }
.flow-head, .receipt-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.flow-head h3, .receipt-card h4 { margin: 0 0 4px; }
.flow-head p, .receipt-head p { margin: 0; color: #667085; font-size: 13px; }
.flow-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 16px 0; }
.flow-field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: #344054; }
.flow-field.wide { grid-column: 1 / -1; }
.flow-field.narrow { max-width: 260px; }
.flow-field input, .flow-field textarea, .flow-field select { border: 1px solid #d0d5dd; border-radius: 8px; padding: 9px 10px; font: inherit; }
.flow-actions { display: flex; align-items: center; gap: 12px; }
.flow-message, .muted { color: #475467; font-size: 13px; }
.receipt-opener { display: flex; gap: 8px; margin: 12px 0; }
.receipt-opener input { flex: 1; border: 1px solid #d0d5dd; border-radius: 8px; padding: 9px 10px; }
.receipt-card { border: 1px solid #d0d5dd; border-radius: 12px; padding: 14px; background: #f9fafb; }
.receipt-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.mini-stats { display: flex; gap: 18px; flex-wrap: wrap; margin: 12px 0; font-size: 14px; }
.line-table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; }
.line-table th, .line-table td { border-bottom: 1px solid #eaecf0; padding: 9px 10px; text-align: left; font-size: 13px; vertical-align: top; }
.line-table th { background: #f2f4f7; }
.detail-cell { color: #667085; max-width: 280px; }
.later-excluded { color: #b42318; margin-top: 3px; }
.line-difference { color: #b54708; margin-top: 3px; }
.badge { border-radius: 999px; padding: 2px 9px; background: #eaecf0; white-space: nowrap; }
.badge[data-state='confirmed'] { background: #dcfae6; color: #067647; }
.badge[data-state='merged'] { background: #e0e7ff; color: #3538cd; }
.badge[data-state='pending'] { background: #fef0c7; color: #b54708; }
.check-result { margin-top: 12px; padding: 10px 12px; border-radius: 8px; background: #ecfdf3; color: #027a48; font-size: 14px; }
.receipt-list { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
.receipt-chip { border: 1px solid #d0d5dd; background: #fff; border-radius: 999px; padding: 6px 11px; cursor: pointer; font-size: 12px; }
</style>
