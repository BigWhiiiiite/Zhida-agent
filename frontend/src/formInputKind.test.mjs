import assert from 'node:assert/strict'
import {isSelectionField, manualFillAction} from './formInputKind.ts'

for (const field_type of ['combobox', 'text']) {
  for (const date_precision of ['', 'date', 'month']) {
    const calendar = {field_type, control_kind: 'calendar', date_precision}
    assert.equal(isSelectionField(calendar), false)
    assert.equal(manualFillAction(calendar), field_type === 'combobox' ? 'select' : 'fill')
  }
}
for (const field_type of ['combobox', 'select-one', 'select-multiple']) {
  assert.equal(isSelectionField({field_type}), true)
  assert.equal(manualFillAction({field_type}), 'select')
}
assert.equal(isSelectionField({field_type:'combobox', control_kind:'cascade'}), true)
assert.equal(manualFillAction({field_type:'checkbox'}), 'check')
assert.equal(manualFillAction({field_type:'radio'}), 'check')
assert.equal(manualFillAction({field_type:'text'}), 'fill')
assert.equal(manualFillAction({field_type:'date', date_precision:'date'}), 'fill')
assert.equal(manualFillAction(undefined), 'fill')
console.log('form input kind: calendars stay date inputs; selection and check controls unchanged')
