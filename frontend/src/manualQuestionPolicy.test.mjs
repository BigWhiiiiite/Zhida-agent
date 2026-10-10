import assert from 'node:assert/strict'
import {isUserAnswerQuestion, userQuestionBlockReason} from './manualQuestionPolicy.ts'

// Synthetic fields only: the policy needs page evidence, never applicant data.
const verified = {
  version:1, question_status:'verified', options_status:'not_applicable',
  required_status:'required', required_evidence:['当前题目的必填标记'],
  record_status:'not_applicable', issues:[],
}
const field = (extra={}) => ({
  selector:'[data-test-field="synthetic"]', label:'联系电话', question_text:'联系电话',
  label_source:'explicit', recognition_confidence:.98, field_type:'text', options:[],
  name:'contact', group_label:'', option_label:'', control_kind:'text',
  semantic_key:'candidate.phone', container_key:'', observation:{...verified},
  ...extra,
})
const blocked = (extra, pattern) => {
  const input = field(extra)
  assert.equal(isUserAnswerQuestion(input), false)
  assert.match(userQuestionBlockReason(input), pattern)
}

assert.equal(isUserAnswerQuestion(field()), true)
assert.equal(userQuestionBlockReason(field()), '')
assert.equal(isUserAnswerQuestion(field({observation:undefined, label_source:undefined, recognition_confidence:undefined})), true,
  'An explicit normal title remains compatible with legacy fields without observation metadata')
assert.equal(isUserAnswerQuestion(field({observation:null})), true)
assert.equal(isUserAnswerQuestion(field({question_text:'', group_label:'联系电话', label:''})), true)

for (const title of ['是', '否', '男', '女', '请选择', ' Yes ', 'No:', '* 请选择：', '未识别字段2', 'field_12', '']) {
  blocked({question_text:title, label:title}, /完整题干/)
}
for (const question_status of ['missing', 'ambiguous', 'unverified']) {
  blocked({observation:{...verified, question_status}}, /归属尚未核实/)
}
for (const issue of ['question_missing', 'question_unowned', 'question_conflict', 'group_incomplete', 'group_conflict',
  'record_conflict', 'record_unresolved', 'control_unknown', 'required_conflict', 'options_partial', 'options_missing', 'options_deferred', 'date_precision_missing']) {
  blocked({observation:{...verified, issues:[issue]}}, /识别缺口/)
}
for (const record_status of ['unresolved', 'ambiguous']) {
  blocked({observation:{...verified, record_status}}, /哪条经历/)
}
blocked({semantic_key:'education.major', question_text:'专业'}, /记录的边界/)
assert.equal(isUserAnswerQuestion(field({semantic_key:'education.major', question_text:'专业', container_key:'education:synthetic-record',
  observation:{...verified, record_status:'container_observed'}})), true)

for (const field_type of ['section-button', 'file', 'hidden', 'password', 'button', 'submit']) {
  blocked({field_type}, /不是可补充的事实题/)
}
blocked({control_kind:'unknown'}, /填写方式/)
blocked({field_type:'unrecognized-widget'}, /填写方式/)
blocked({control_kind:'calendar', field_type:'combobox'}, /日期控件精度/)
for (const date_precision of ['date', 'month']) {
  assert.equal(isUserAnswerQuestion(field({question_text:'出生日期', control_kind:'calendar', field_type:'combobox', date_precision,
    observation:{...verified, options_status:'calendar'}})), true)
}
assert.equal(isUserAnswerQuestion(field({field_type:'date', question_text:'出生日期', observation:undefined})), true,
  'Native date fields provide their own precision in legacy metadata')

for (const field_type of ['select-one', 'select-multiple', 'combobox', 'radio']) {
  blocked({field_type, control_kind:field_type, question_text:'是否有正式工作经历', options:[]}, /真实选项/)
  assert.equal(isUserAnswerQuestion(field({field_type, question_text:'是否有正式工作经历', options:['是', '否'],
    observation:{...verified, options_status:field_type==='radio'?'group_complete':'native_complete'}})), true)
}
for (const options_status of ['observed_subset', 'unavailable', 'deferred', 'dependent']) {
  blocked({field_type:'select-one', options:['选项甲'], observation:{...verified, options_status}}, /真实选项尚未完整/)
}
blocked({field_type:'select-one', options:['请选择', ''], observation:undefined}, /真实选项/)
blocked({field_type:'combobox', options:['选项甲'], options_capture:'observed_subset', observation:undefined}, /真实选项尚未完整/)
blocked({field_type:'radio', options:[], option_label:'是', observation:{...verified, options_status:'group_complete'}}, /真实选项/)
assert.equal(isUserAnswerQuestion(field({question_text:'是否仍在读', field_type:'checkbox', options:[],
  observation:{...verified, options_status:'group_complete'}})), true,
  'A single checkbox is a binary fact, not an unread dropdown')
blocked({question_text:'掌握哪些技能', field_type:'checkbox', multiple:true, options:[]}, /真实选项/)
blocked({control_kind:'cascade', field_type:'combobox', options:['地区甲']}, /级联选项/)

for (const question_text of ['我已阅读隐私协议', '我承诺信息真实可信', 'I certify this information is accurate', 'Privacy consent']) {
  blocked({question_text, field_type:'checkbox'}, /亲自决定/)
}
blocked({question_text:'是否确认', field_type:'checkbox', name:'agreechk'}, /亲自决定/)
blocked({field_type:'radio', question_text:'是否确认', options:['是', '否'], option_label:'我承担法律责任'}, /亲自决定/)
assert.equal(isUserAnswerQuestion(field({question_text:'身份证号码', semantic_key:'candidate.government_id'})), true,
  'A clear missing fact is not excluded solely by its subject; this policy grants no fill or storage permission')
assert.equal(isUserAnswerQuestion(field({question_text:'是否接受城市调剂', field_type:'radio', options:['是', '否'],
  observation:{...verified, options_status:'group_complete'}})), true)

blocked({observation:undefined, question_candidates:[{text:'附近题目', source:'nearby', owned:false}]}, /唯一归属/)
blocked({observation:undefined, question_candidates:[{text:'联系邮箱', source:'explicit', owned:true}]}, /唯一归属/)
blocked({observation:undefined, question_candidates:[
  {text:'联系电话', source:'explicit', owned:true}, {text:'联系邮箱', source:'explicit', owned:true},
]}, /唯一归属/)
blocked({observation:undefined, label_source:'placeholder'}, /低可信/)
blocked({observation:undefined, recognition_confidence:.4}, /低可信/)
assert.equal(isUserAnswerQuestion(field({recognition_confidence:.4})), true,
  'Verified ownership is stronger than an older scanner confidence value')
console.log('manual question policy: synthetic facts, extraction gaps, controls and declaration boundaries passed')
