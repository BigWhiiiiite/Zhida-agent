import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
registerHooks({ resolve(specifier, context, nextResolve) {
  return nextResolve(specifier === './manualQuestionPolicy' ? './manualQuestionPolicy.ts' : specifier, context)
} })
const { checkboxQuestionGroups, checkboxQuestionKey, checkboxGroupAnswers, checkboxGroupChecked,
  checkboxGroupAnswered, checkboxOptionMemoryIdentity, verifiedCheckboxSelection } = await import('./checkboxGroups.ts')

const options = Array.from({ length: 14 }, (_, index) => `匿名来源${index + 1}`)
const fields = options.map((option, index) => ({
  selector: `#source-${index}`, field_type: 'checkbox', question_text: '招聘信息来源', group_label: '招聘信息来源',
  label: option, option_label: option, option_value: `source-${index}`, options, multiple: false, required: true,
  current_value: index === 2 ? 'true' : 'false', control_group_key: 'anonymous-source-group', container_key: '',
  label_source: 'container-owned', recognition_confidence: 1, semantic_key: 'application.custom', entity_scope: 'application',
  section: '其他信息', section_path: [], name: 'shared-name', field_signature: `rank-${index}`, signature_rank: index,
  observation: { question_status: 'verified', options_status: 'group_complete', record_status: 'not_applicable', issues: [] },
}))
let groups = checkboxQuestionGroups(fields)
assert.equal(groups.length, 1)
const group = groups[0]
assert.equal(group.fields.length, 14)
assert.equal(group.blockReason, '')
assert.equal(group.question, '招聘信息来源')
assert.equal(checkboxGroupAnswered(group, {}), false)
assert.equal(checkboxGroupChecked(fields[2], {}), true)
let answers = checkboxGroupAnswers(group, { '#other': '保留其他题草稿' }, fields[0].selector, true)
assert.equal(Object.keys(answers).length, 15)
assert.equal(answers['#other'], '保留其他题草稿')
assert.equal(answers[fields[0].selector], 'true')
assert.equal(answers[fields[2].selector], 'true', 'Keep an existing checked option when the checklist is edited')
assert.equal(answers[fields[1].selector], 'false', 'Every unselected member receives an explicit uncheck')
assert.equal(checkboxGroupAnswered(group, answers), true)
answers = checkboxGroupAnswers(group, answers, fields[2].selector, false)
assert.equal(answers[fields[2].selector], 'false', 'An old selection can be explicitly removed')
assert.equal(checkboxGroupChecked(fields[2], answers), false)
const identities = fields.map(checkboxOptionMemoryIdentity)
assert.equal(new Set(identities).size, 14)
const reordered = [...fields].reverse().map((field, index) => ({ ...field, selector: `#new-${index}`, field_signature: `rank-${index}`, signature_rank: index }))
for (const field of reordered) assert.equal(checkboxOptionMemoryIdentity(field), identities[fields.findIndex(old => old.option_label === field.option_label)])
assert.notEqual(checkboxOptionMemoryIdentity(fields[0]), checkboxOptionMemoryIdentity(fields[1]))
assert.equal(checkboxQuestionKey({ ...fields[0], control_group_key: '' }), null)
assert.equal(checkboxQuestionGroups([fields[0]]).length, 0, 'A standalone checkbox is not a multi-option question')

const receipt = { results: group.fields.map(field => ({ selector: field.selector, status: 'filled', verified: true, actual_value: answers[field.selector] })) }
assert.deepEqual(verifiedCheckboxSelection(group, answers, receipt), ['匿名来源1'])
assert.equal(verifiedCheckboxSelection(group, answers, { results: receipt.results.slice(1) }), null)
for (const patch of [{ verified: false }, { status: 'skipped' }, { actual_value: 'true' }]) {
  const failed = receipt.results.map(result => result.selector === fields[1].selector ? { ...result, ...patch } : result)
  assert.equal(verifiedCheckboxSelection(group, answers, { results: failed }), null)
}
answers = checkboxGroupAnswers(group, answers, fields[0].selector, false)
assert.equal(checkboxGroupAnswered(group, answers), false, 'A required checklist needs at least one selected option')
assert.equal(verifiedCheckboxSelection(group, answers, receipt), null)

for (const patch of [
  { options: options.slice(1) },
  { observation: { ...fields[0].observation, options_status: 'observed_subset' } },
  { question_text: '另一个问题' },
  { option_label: fields[1].option_label },
  { current_value: 'unread' },
]) {
  const blocked = checkboxQuestionGroups(fields.map((field, index) => index === 0 ? { ...field, ...patch } : field))[0]
  assert.ok(blocked.blockReason)
  assert.throws(() => checkboxGroupAnswers(blocked, {}, fields[0].selector, true))
}
const otherQuestion = fields.map(field => ({ ...field, selector: `${field.selector}-other`, control_group_key: 'other-group', question_text: '技术领域偏好', group_label: '技术领域偏好' }))
assert.equal(checkboxQuestionGroups([...fields, ...otherQuestion]).length, 2, 'Same names and options do not merge different DOM groups')
const otherRecord = fields.map(field => ({ ...field, selector: `${field.selector}-record`, container_key: 'another-record' }))
assert.equal(checkboxQuestionGroups([...fields, ...otherRecord]).length, 2, 'Same group key across record boundaries is still two questions')
console.log('checkbox groups: one 14-option checklist, explicit per-selector checks, stable option identities, complete read-back learning gate passed')
