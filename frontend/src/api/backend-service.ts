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

export type EffectiveView = {
  ok: boolean
  集市编号: string
  当前生效批次: string
  当前生效契约: string
  当前字段: string
  契约字段: string
  可承接查询: boolean
  核验问题: string[]
  最近回滚?: { 回滚编号: string; 回滚状态: string; 变更回执: string } | null
}

export async function loadEffective(martRef: string): Promise<EffectiveView> {
  const response = await request(`/api/mart-release/${encodeURIComponent(martRef)}/effective`)
  if (!response.ok) throw new Error('生效批与契约读取失败')
  return await response.json()
}

export async function rollbackToPrevious(
  martRef: string,
  options: { batch?: string; acceptConflict?: boolean } = {},
): Promise<{ ok: boolean; message: string; needs_confirmation?: boolean; rollback?: EntryRow }> {
  const query = new URLSearchParams()
  if (options.batch) query.set('batch', options.batch)
  if (options.acceptConflict) query.set('accept_conflict', 'true')
  const response = await request(
    `/api/mart-release/${encodeURIComponent(martRef)}/rollback?${query.toString()}`,
    { method: 'POST' },
  )
  return await response.json()
}

export async function reconcileClaim(claimRef: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/mart-release/claims/${encodeURIComponent(claimRef)}/reconcile`)
  if (!response.ok) throw new Error('领取文件对账失败')
  return await response.json()
}

export async function traceReceipt(receiptNo: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/mart-release/receipts/${encodeURIComponent(receiptNo)}`)
  if (!response.ok) throw new Error('变更回执追溯失败')
  return await response.json()
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
