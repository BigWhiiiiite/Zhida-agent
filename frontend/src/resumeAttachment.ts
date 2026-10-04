import type {BrowserSnapshot} from './types'

// This must match the existing executor's resume-file selection, not a generic
// upload/photo/certificate input. More than one candidate is not safe to pick.
export function resumeAttachmentBlocker(snapshot:Pick<BrowserSnapshot,'fields'>|null):string {
  if(!snapshot)return '请先同步当前申请页。'
  const fields=snapshot.fields.filter(field=>field.field_type==='file'&&
    /resume|cv|curriculum|简历/i.test(`${field.label} ${field.name}`))
  if(fields.length!==1)return fields.length?'识别到多个简历上传框，请先定位正确的附件入口。':'当前页未识别到明确的简历附件上传框。'
  if(/证件|证书|照片|头像|身份证|护照|passport|photo|certificate/i.test(`${fields[0].label} ${fields[0].name}`))return '上传框含义不明确，不会把简历传入证件或照片栏。'
  if(fields[0].current_value.trim())return '官网已有简历附件，本入口不会自动替换；请先核对官网附件。'
  return ''
}

export function resumeAttachmentRequest(resumeId:string,contextToken:string,confirmed=false){
  if(!confirmed)throw new Error('请先确认所选简历和招聘网站，再上传附件。')
  if(!resumeId||!contextToken)throw new Error('简历或填写计划已失效，请重新核对。')
  // No fill actions, native parser trigger, navigation or persistent consent.
  return {actions:[],min_confidence:.85,resume_id:resumeId,context_token:contextToken,upload_resume:true}
}
