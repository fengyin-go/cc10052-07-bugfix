<template>
  <section class="sample-panel" data-testid="sample-ratio-panel">
    <div class="panel-head">
      <div>
        <h3>从分组确认结果重算样本比例</h3>
        <p>分母按实验内有效用户去重；核对办结时冻结分子、分母、重复排除数和用户来源。</p>
      </div>
      <button class="btn ghost" type="button" data-testid="refresh-sample-panel" @click="loadAll">
        刷新
      </button>
    </div>

    <form class="calc-bar" @submit.prevent="calculate">
      <label>
        <span>实验编码</span>
        <input v-model="experimentCode" data-testid="sample-experiment" placeholder="例如 EXP-E2E" required />
      </label>
      <button class="btn primary" type="submit" data-testid="calculate-sample-check" :disabled="calculating">
        {{ calculating ? '重算中…' : '领用已确认用户并计算/核对' }}
      </button>
      <span v-if="message" class="message" data-testid="calculation-message">{{ message }}</span>
    </form>

    <div class="check-list" data-testid="sample-check-list">
      <article
        v-for="check in checks"
        :key="check.id"
        class="check-card"
        :data-testid="`check-card-${check.id}`"
        :data-status="check.status"
      >
        <header>
          <div>
            <strong data-testid="check-batch">{{ check.核对批次 }}</strong>
            <span class="status-pill">{{ check.status }}</span>
          </div>
          <span class="exp">{{ check.实验编码 }}</span>
        </header>
        <div class="metric-grid">
          <div><span>分子</span><b :data-testid="`check-numerator-${check.id}`">{{ check.分子 ?? '—' }}</b></div>
          <div><span>分母</span><b :data-testid="`check-denominator-${check.id}`">{{ check.分母 ?? '—' }}</b></div>
          <div><span>重复排除数</span><b :data-testid="`check-duplicates-${check.id}`">{{ check.重复排除数 ?? '—' }}</b></div>
          <div><span>实际比例</span><b>{{ check.实际比例 ?? '—' }}</b></div>
          <div><span>计划比例</span><b>{{ check.计划比例 ?? '—' }}</b></div>
        </div>
        <p class="source-line">
          确认结果：{{ check.确认批次 || (check.来源确认批次 || []).join('、') || '—' }}
        </p>
        <div class="member-line" data-testid="check-members">
          <span
            v-for="member in check.样本明细 || check.members || []"
            :key="`${check.id}-${member.用户标识}`"
            class="member-chip"
            :data-testid="`check-member-${check.id}-${member.用户标识}`"
          >
            {{ member.用户标识 }} · {{ member.实际组别 || member.分组名称 }} · 领取自
            {{ member.来源确认批次 || member.确认批次 }}
            <button
              class="mini-danger"
              type="button"
              :data-testid="`exclude-member-${check.id}-${member.用户标识}`"
              @click="excludeMember(member)"
            >
              剔除
            </button>
          </span>
        </div>
      </article>
      <p v-if="!checks.length" class="muted">暂无核对批次，先从分组回执或上方计算入口发起。</p>
    </div>

    <section class="diff-section" data-testid="difference-section">
      <div class="diff-head">
        <h4>核对后剔除差异</h4>
        <button class="btn" type="button" data-testid="refresh-differences" @click="loadDifferences">
          刷新差异
        </button>
      </div>
      <div class="difference-list" data-testid="difference-list">
        <article
          v-for="diff in differences"
          :key="diff.id"
          class="difference-card"
          :data-testid="`difference-row-${diff.id}`"
        >
          <div class="diff-main">
            <strong>{{ diff.差异编号 }}</strong>
            <span>实验 {{ diff.实验编码 }}</span>
            <span>用户 {{ diff.用户标识 }}</span>
            <span>实际组别 <b data-testid="diff-group">{{ diff.实际组别 }}</b></span>
            <span>提出确认批次 <b data-testid="diff-confirmation-batch">{{ diff.确认批次 }}</b></span>
          </div>
          <p class="preserve-line">
            原核对 {{ diff.原核对批次 || '—' }}；保留原比例
            <b data-testid="diff-original-ratio">{{ diff.原比例 || '—' }}</b>
            （{{ diff.原分子 ?? '—' }}/{{ diff.原分母 ?? '—' }}）
          </p>
          <div class="diff-actions">
            <span class="status-pill">{{ diff.status }}</span>
            <button
              class="btn primary"
              type="button"
              :data-testid="`new-check-from-difference-${diff.id}`"
              @click="newCheck(diff.id)"
            >
              据差异发起新核对
            </button>
          </div>
          <p v-if="diff.新核对批次" class="new-check-line">
            新核对：<strong>{{ diff.新核对批次 }}</strong>
          </p>
        </article>
        <p v-if="!differences.length" class="muted">暂无剔除差异。</p>
      </div>
      <p v-if="newCheckMessage" class="message success" data-testid="new-check-result">{{ newCheckMessage }}</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  calculateSampleCheck,
  createCheckFromDifference,
  excludeAssignment,
  listEntries,
  listSampleDifferences,
} from '@/api/backend-service'

const emit = defineEmits<{ changed: [] }>()

const route = useRoute()
const initialExperiment = typeof route.query.experiment_code === 'string'
  ? route.query.experiment_code
  : 'EXP-E2E'
const experimentCode = ref(initialExperiment)
const calculating = ref(false)
const message = ref('')
const newCheckMessage = ref('')
const checks = ref<any[]>([])
const differences = ref<any[]>([])

async function loadChecks() {
  const payload = await listEntries('sample_ratio', {
    实验编码: experimentCode.value.trim(),
    size: '100',
  })
  checks.value = payload.items ?? []
}

async function loadDifferences() {
  const payload = await listSampleDifferences(experimentCode.value.trim())
  differences.value = payload.items ?? []
}

async function loadAll() {
  message.value = ''
  try {
    await Promise.all([loadChecks(), loadDifferences()])
  } catch (error) {
    message.value = error instanceof Error ? error.message : '核对数据读取失败'
  }
}

async function calculate() {
  calculating.value = true
  message.value = ''
  try {
    const result = await calculateSampleCheck(experimentCode.value.trim())
    if (!result.ok) throw new Error(result.message || '计算失败')
    message.value = result.message
    await loadAll()
    emit('changed')
  } catch (error) {
    message.value = error instanceof Error ? error.message : '计算失败'
  } finally {
    calculating.value = false
  }
}

async function excludeMember(member: any) {
  if (!member.assignment_id) {
    message.value = '该成员缺少分组记录 ID，无法剔除'
    return
  }
  message.value = ''
  try {
    const result = await excludeAssignment(member.assignment_id, '样本核对后剔除')
    if (!result.ok) throw new Error(result.message || '剔除失败')
    message.value = result.message
    await loadAll()
    emit('changed')
  } catch (error) {
    message.value = error instanceof Error ? error.message : '剔除失败'
  }
}

async function newCheck(id: number) {
  newCheckMessage.value = ''
  try {
    const result = await createCheckFromDifference(id)
    if (!result.ok) throw new Error(result.message || '发起新核对失败')
    newCheckMessage.value = result.message
    await loadAll()
    emit('changed')
  } catch (error) {
    newCheckMessage.value = error instanceof Error ? error.message : '发起新核对失败'
  }
}

onMounted(loadAll)
</script>

<style scoped>
.sample-panel { margin: 18px 0; padding: 18px; border: 1px solid #e5e7eb; border-radius: 14px; background: #fff; }
.panel-head, .diff-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
h3, h4 { margin: 0 0 4px; }
.panel-head p { margin: 0; color: #667085; font-size: 13px; }
.calc-bar { display: flex; align-items: end; gap: 12px; margin: 16px 0; flex-wrap: wrap; }
.calc-bar label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.calc-bar input { min-width: 260px; border: 1px solid #d0d5dd; border-radius: 8px; padding: 9px 10px; }
.message { color: #475467; font-size: 13px; }
.message.success { color: #027a48; }
.check-list { display: grid; gap: 12px; }
.check-card { border: 1px solid #d0d5dd; border-radius: 12px; padding: 14px; background: #f9fafb; }
.check-card header { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.status-pill { display: inline-block; margin-left: 8px; border-radius: 999px; padding: 2px 9px; background: #e0e7ff; color: #3538cd; font-size: 12px; }
.exp { color: #667085; font-size: 13px; }
.metric-grid { display: grid; grid-template-columns: repeat(5, minmax(90px, 1fr)); gap: 8px; margin: 12px 0; }
.metric-grid div { background: #fff; border: 1px solid #eaecf0; border-radius: 8px; padding: 8px; display: flex; flex-direction: column; gap: 3px; }
.metric-grid span { color: #667085; font-size: 12px; }
.metric-grid b { font-size: 18px; }
.source-line, .preserve-line { font-size: 13px; color: #344054; margin: 6px 0; }
.member-line { display: flex; gap: 6px; flex-wrap: wrap; }
.member-chip { background: #fff; border: 1px solid #d0d5dd; border-radius: 999px; padding: 3px 9px; font-size: 12px; }
.mini-danger { margin-left: 6px; border: 0; background: #fee4e2; color: #b42318; border-radius: 999px; padding: 1px 7px; cursor: pointer; font-size: 12px; }
.diff-section { margin-top: 18px; border-top: 1px solid #eaecf0; padding-top: 14px; }
.difference-list { display: grid; gap: 10px; margin-top: 10px; }
.difference-card { border: 1px solid #f0a96b; background: #fffaeb; border-radius: 10px; padding: 12px; }
.diff-main { display: flex; gap: 14px; flex-wrap: wrap; font-size: 14px; }
.diff-actions { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 8px; }
.new-check-line { color: #027a48; font-size: 13px; margin: 8px 0 0; }
.muted { color: #667085; font-size: 13px; }
@media (max-width: 900px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
