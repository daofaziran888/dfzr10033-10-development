<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <section v-if="isRollbackModule" class="mart-panel" data-testid="effective-panel">
      <form class="mart-inline" @submit.prevent="loadEffectiveBatch">
        <label class="filter-item">
          <span>集市编号</span>
          <input v-model="martRef" data-testid="effective-mart" placeholder="如 RESU-0001" />
        </label>
        <button class="btn" type="submit">读取当前生效批与契约</button>
        <button class="btn ghost" type="button" @click="triggerRollback">回滚到上一发布批</button>
      </form>
      <p v-if="effective" class="mart-result" data-testid="effective-result">
        当前生效批次 <strong>{{ effective.当前生效批次 || '—' }}</strong>，
        契约 <strong>{{ effective.当前生效契约 || '—' }}</strong>，
        当前字段 <code>{{ effective.当前字段 || '—' }}</code>，
        契约字段 <code>{{ effective.契约字段 || '—' }}</code>，
        可承接新查询：<strong>{{ effective.可承接查询 ? '是' : '否' }}</strong>
        <span v-if="effective.核验问题.length">（{{ effective.核验问题.join('；') }}）</span>
      </p>
    </section>

    <section v-if="isClaimModule" class="mart-panel" data-testid="claim-panel">
      <form class="mart-inline" @submit.prevent="loadClaimReconcile">
        <label class="filter-item">
          <span>领取编号</span>
          <input v-model="claimRef" data-testid="claim-ref" placeholder="如 CLAIM-0001" />
        </label>
        <button class="btn" type="submit">按确认版本对账</button>
      </form>
      <p v-if="claimView" class="mart-result" data-testid="claim-result">
        确认版本 <code>{{ claimView.确认版本 }}</code>；新查询读到
        <code>{{ claimView.新查询可读定义 || '—' }}</code>；当前生效批
        <strong>{{ claimView.当前生效批次 || '—' }}</strong>，契约
        <strong>{{ claimView.当前生效契约 || '—' }}</strong>。
        {{ claimView.对账口径 }}
      </p>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadEffective,
  moduleMeta,
  reconcileClaim,
  rollbackToPrevious,
  runAction as applyAction,
} from '@/api/backend-service'
import type { ClaimReconcile, EffectiveView, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const isRollbackModule = computed(() => meta.key === 'mart_release_rollback')
const isClaimModule = computed(() => meta.key === 'mart_release_claim')
const martRef = ref('RESU-0001')
const claimRef = ref('CLAIM-0001')
const effective = ref<EffectiveView | null>(null)
const claimView = ref<ClaimReconcile | null>(null)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function loadEffectiveBatch() {
  errorMessage.value = ''
  try {
    effective.value = await loadEffective(martRef.value.trim() || 'RESU-0001')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生效批读取失败'
  }
}

async function triggerRollback() {
  errorMessage.value = ''
  try {
    const result = await rollbackToPrevious(martRef.value.trim() || 'RESU-0001')
    effective.value = await loadEffective(martRef.value.trim() || 'RESU-0001')
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚失败'
  }
}

async function loadClaimReconcile() {
  errorMessage.value = ''
  try {
    claimView.value = (await reconcileClaim(claimRef.value.trim() || 'CLAIM-0001')) as unknown as ClaimReconcile
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
