import type { ExecutionResult, PageField } from './types'
import { userQuestionBlockReason } from './manualQuestionPolicy'

export type CheckboxQuestionGroup = {
  key: string
  question: string
  fields: PageField[]
  required: boolean
  blockReason: string
}

const normalized = (value = '') => value.normalize('NFKC').replace(/\s+/g, '').toLowerCase()
const caption = (field: PageField) => (field.option_label || field.option_value || '').trim()
const question = (field: PageField) => (field.question_text || field.group_label || '').trim()
const booleanValue = (value = ''): string | null => value === 'true' ? 'true' : value === 'false' || value === '' ? 'false' : null

// The current DOM group and record boundary are evidence for UI grouping.
// A repeated caption or equal HTML name alone must never merge two questions.
export function checkboxQuestionKey(field: PageField): string | null {
  return field.field_type === 'checkbox' && field.control_group_key
    ? JSON.stringify(['checkbox', field.container_key || '', field.control_group_key]) : null
}

function groupBlockReason(fields: PageField[]): string {
  const reasons = fields.map(userQuestionBlockReason).filter(Boolean)
  if (reasons.length) return reasons[0]
  if (new Set(fields.map(field => normalized(question(field)))).size !== 1) return '同组控件的共同题干存在冲突，需重新读取。'
  if (fields.some(field => field.observation?.question_status !== 'verified' || field.observation?.options_status !== 'group_complete')) {
    return '复选题的控件归属或完整选项尚未核实，需重新读取。'
  }
  const captions = fields.map(field => normalized(caption(field)))
  if (captions.some(value => !value) || new Set(captions).size !== fields.length) return '复选项缺少唯一的真实选项文字，需重新读取。'
  const observed = new Set(captions)
  if (fields.some(field => {
    const advertised = new Set(field.options.map(normalized).filter(Boolean))
    return advertised.size !== observed.size || [...advertised].some(value => !observed.has(value))
  })) return '复选题的选项清单与已读取控件不一致，需补读缺少的选项。'
  if (fields.some(field => booleanValue(field.current_value) === null)) return '复选项当前勾选状态尚未核实，需重新读取。'
  return ''
}

// Native checkbox nodes commonly report multiple:false even in a multi-option
// question. Use verified same-question DOM membership rather than that flag.
// Incomplete groups remain one blocked group so they cannot become many facts.
export function checkboxQuestionGroups(fields: PageField[]): CheckboxQuestionGroup[] {
  const members = new Map<string, PageField[]>()
  for (const field of fields) {
    const key = checkboxQuestionKey(field)
    if (!key) continue
    members.set(key, [...members.get(key) || [], field])
  }
  return [...members].filter(([, fields]) => fields.length > 1).map(([key, fields]) => ({
    key, question: question(fields[0]), fields, required: fields.some(field => field.required), blockReason: groupBlockReason(fields),
  }))
}

export function checkboxGroupChecked(field: PageField, answers: Record<string, string>): boolean {
  return (answers[field.selector] ?? field.current_value) === 'true'
}

// A checklist edit confirms the displayed whole set. Compile explicit true and
// false for EVERY member so an old checked option can be unchecked as well.
export function checkboxGroupAnswers(group: CheckboxQuestionGroup, answers: Record<string, string>, selector: string, checked: boolean): Record<string, string> {
  if (group.blockReason) throw new Error(group.blockReason)
  if (!group.fields.some(field => field.selector === selector)) throw new Error('此选项不属于当前复选题。')
  const next = { ...answers }
  for (const field of group.fields) next[field.selector] = checkboxGroupChecked(field, answers) ? 'true' : 'false'
  next[selector] = checked ? 'true' : 'false'
  return next
}

export function checkboxGroupAnswered(group: CheckboxQuestionGroup, answers: Record<string, string>): boolean {
  return !group.blockReason && group.fields.every(field => ['true', 'false'].includes(answers[field.selector]))
    && (!group.required || group.fields.some(field => answers[field.selector] === 'true'))
}

// This is a stable identity specification for memory, NOT a replacement for the
// backend's current field_signature. Sending a frontend-only signature to the
// old matcher would still permit its unsafe same-question boolean fallback.
export function checkboxOptionMemoryIdentity(field: PageField): string | null {
  if (field.field_type !== 'checkbox' || !question(field) || !caption(field)) return null
  return JSON.stringify(['checkbox-option-v1', normalized(field.section), normalized(question(field)),
    field.semantic_key || '', field.entity_scope || '', normalized(caption(field)), normalized(field.option_value)])
}

// Return a structured selected-caption set only when the complete group was
// explicitly answered and EVERY true/false action was read back as expected.
// No API call is made here; the old per-checkbox 是/否 persistence is unsuitable
// for a multi-option question and must not be used for this result.
export function verifiedCheckboxSelection(group: CheckboxQuestionGroup, answers: Record<string, string>, execution: Pick<ExecutionResult, 'results'>): string[] | null {
  if (!checkboxGroupAnswered(group, answers)) return null
  const receipt = new Map(execution.results.map(result => [result.selector, result]))
  if (!group.fields.every(field => {
    const result = receipt.get(field.selector)
    return result?.status === 'filled' && result.verified && result.actual_value === answers[field.selector]
  })) return null
  return group.fields.filter(field => answers[field.selector] === 'true').map(caption)
}
