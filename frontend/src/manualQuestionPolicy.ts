import type {PageField} from './types'

const extractionIssues = new Set([
  'question_missing', 'question_unowned', 'question_conflict',
  'group_incomplete', 'group_conflict', 'record_conflict', 'record_unresolved',
  'control_unknown', 'required_conflict', 'date_precision_missing',
  'options_missing', 'options_partial', 'options_deferred', 'dependent_options',
])
const incompleteOptions = new Set(['observed_subset', 'unavailable', 'deferred', 'dependent'])
const choiceTypes = new Set(['select-one', 'select-multiple', 'combobox', 'radio'])
const choiceKinds = new Set(['select', 'custom_select', 'native_select', 'dropdown', 'cascade', 'region', 'region_picker'])
const answerTypes = new Set(['text', 'textarea', 'email', 'tel', 'url', 'number', 'date', 'month', 'checkbox', ...choiceTypes])
const declaration = /承诺|声明|真实可信|真实性|我保证|法律责任|协议|条款|隐私|\b(?:consent|privacy|terms|legal|declaration|attest(?:ation)?|certify|acknowledge(?:ment)?|undertaking)\b/i
const unownedLabelSources = new Set(['generated', 'name', 'placeholder', 'context', 'nearby', 'unknown'])

const clean = (value?:string) => (value || '').replace(/[\s*＊✱:：?？。.]+/g, '').toLowerCase()
function meaningfulTitle(value?:string):boolean {
  const title = clean(value)
  return Boolean(title && !/^(?:是|否|男|女|其他|请选择|选择|未选择|暂未选择|yes|no|male|female|other|select|choose|pleaseselect|pleasechoose)$/.test(title)
    && !/^(?:未识别(?:字段|题目)?|字段|题目|field|question)[-_\d]*$/.test(title))
}

// This policy separates missing applicant facts from missing page evidence.
// It does not authorize a fill, an agreement, a memory write or a submission.
// Reasons are for the developer's inspection view, not questions for the user.
export function userQuestionBlockReason(field:PageField):string {
  if (['section-button', 'file', 'hidden', 'password', 'button', 'submit'].includes(field.field_type)) {
    return '该控件不是可补充的事实题，需由对应页面操作处理。'
  }
  const title = field.question_text?.trim() || field.group_label?.trim() || field.label?.trim() || ''
  const ownWording = [title, field.group_label, field.option_label, field.name].filter(Boolean).join(' ')
  if (declaration.test(ownWording) || /^(?:agreechk|agreement|consent)$/i.test(field.name || '')) {
    return '协议或声明须由用户在招聘网站阅读并亲自决定，不能作为自动填写的事实答案。'
  }
  if (!meaningfulTitle(title)) return '尚未读取完整题干，不能让用户根据选项或占位文字猜题。'

  const observation = field.observation
  if (observation && observation.question_status !== 'verified') {
    return '题干及其控件归属尚未核实，需补读网页证据。'
  }
  if (observation?.issues?.some(issue => extractionIssues.has(issue))) {
    return '题干、控件、选项或经历归属存在识别缺口，需由系统补读。'
  }
  if (observation && ['unresolved', 'ambiguous'].includes(observation.record_status)) {
    return '尚未核实对应哪条经历，不能让用户补入可能串填的答案。'
  }
  if (/^(?:education|experience|project|language)\./.test(field.semantic_key || '') && !field.container_key) {
    return '缺少独立经历记录的边界，需先核实记录归属。'
  }
  if (!observation) {
    const candidates = field.question_candidates || []
    const owned = candidates.filter(candidate => candidate.owned && meaningfulTitle(candidate.text))
    if (candidates.length && (!owned.length || new Set(owned.map(candidate => clean(candidate.text))).size > 1
        || !owned.some(candidate => clean(candidate.text) === clean(title)))) {
      return '网页题干缺少唯一归属证据，需重新读取。'
    }
    if (unownedLabelSources.has(field.label_source) ||
        Number.isFinite(field.recognition_confidence) && field.recognition_confidence < .7) {
      return '当前标题仅来自低可信或非归属文字，需先核实网页原题。'
    }
  }
  if (field.control_kind === 'unknown' || !answerTypes.has(field.field_type)) {
    return '控件的真实填写方式尚未核实。'
  }
  if (field.control_kind === 'calendar' && !field.date_precision) {
    return '日期控件精度尚未核实，需先读取日期格式。'
  }
  if (field.region_picker || ['cascade', 'region', 'region_picker'].includes(field.control_kind || '')) {
    return '级联选项及完整选择路径需由系统核实。'
  }
  const date = Boolean(field.date_precision || ['date', 'month'].includes(field.field_type))
  const selection = !date && (choiceTypes.has(field.field_type) || choiceKinds.has(field.control_kind || ''))
  if (!date && (selection || field.field_type === 'checkbox')) {
    if (incompleteOptions.has(observation?.options_status || '') || incompleteOptions.has(field.options_capture || '')) {
      return '网页真实选项尚未完整读取，需由系统补读。'
    }
    if ((selection || field.multiple) && !(field.options || []).some(option => {
      const value = clean(option)
      return Boolean(value && !/^(?:请选择|选择|未选择|暂未选择|select|choose|pleaseselect|pleasechoose)$/.test(value))
    })) {
      return '尚未读取可用的网页真实选项，不能以自由文本代替选择。'
    }
  }
  return ''
}

export function isUserAnswerQuestion(field:PageField):boolean {
  return userQuestionBlockReason(field) === ''
}
