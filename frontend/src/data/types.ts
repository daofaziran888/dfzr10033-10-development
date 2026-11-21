/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
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

export type ClaimReconcile = {
  ok: boolean
  领取编号: string
  文件名称: string
  领取批次: string
  确认版本: string
  领取快照一致: boolean
  历史领取可读定义: string
  新查询可读定义: string
  当前生效批次: string
  当前生效契约: string
  字段差异: { 领取字段: string; 新查询字段: string }[]
  对账口径: string
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
