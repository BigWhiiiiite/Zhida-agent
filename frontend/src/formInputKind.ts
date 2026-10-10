import type {FillAction, PageField} from './types'

type ControlShape = Pick<PageField, 'field_type' | 'control_kind' | 'date_precision'>

// A calendar can expose role=combobox even before its precision is read.
// That role must not hide the user's date editor. The execution protocol still
// routes a combobox calendar through the structured date picker, not raw fill.
export function isSelectionField(field: ControlShape): boolean {
  return field.control_kind !== 'calendar' && !field.date_precision &&
    ['select-one', 'select-multiple', 'combobox'].includes(field.field_type)
}

export function manualFillAction(field?: ControlShape): FillAction['action'] {
  if (!field) return 'fill'
  if (field.control_kind === 'calendar') return field.field_type === 'combobox' ? 'select' : 'fill'
  if (field.date_precision) return field.field_type === 'combobox' ? 'select' : 'fill'
  if (isSelectionField(field)) return 'select'
  return ['checkbox', 'radio'].includes(field.field_type) ? 'check' : 'fill'
}
