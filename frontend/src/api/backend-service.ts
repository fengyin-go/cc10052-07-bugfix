import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

async function postJson<T = any>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return await response.json() as T
}

async function getJson<T = any>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json() as T
}

export function submitAssignment(payload: Record<string, unknown>) {
  return postJson<any>('/api/variant_assign/batches', payload)
}

export function listAssignmentReceipts(experimentCode = '') {
  const query = experimentCode ? `?experiment_code=${encodeURIComponent(experimentCode)}` : ''
  return getJson<{ items: any[] }>(`/api/variant_assign/receipts${query}`)
}

export function getAssignmentReceipt(receiptId: string) {
  return getJson<any>(`/api/variant_assign/receipts/${encodeURIComponent(receiptId)}`)
}

export function claimAssignmentReceipt(receiptId: string, payload: Record<string, unknown> = {}) {
  return postJson<any>(`/api/variant_assign/receipts/${encodeURIComponent(receiptId)}/claim`, payload)
}

export function createCheckFromReceipt(receiptId: string) {
  return postJson<any>(`/api/variant_assign/receipts/${encodeURIComponent(receiptId)}/check`, {})
}

export function calculateSampleCheck(experimentCode: string) {
  return postJson<any>('/api/sample_ratio/calculate', { experiment_code: experimentCode })
}

export function listSampleDifferences(experimentCode = '') {
  const query = experimentCode ? `?experiment_code=${encodeURIComponent(experimentCode)}` : ''
  return getJson<{ items: any[] }>(`/api/sample_ratio/differences${query}`)
}

export function createCheckFromDifference(differenceId: number | string) {
  return postJson<any>(`/api/sample_ratio/differences/${differenceId}/new-check`, {})
}

export function excludeAssignment(assignmentId: number | string, reason = '核对后剔除') {
  return postJson<any>(`/api/variant_assign/assignments/${assignmentId}/exclude`, { reason })
}
