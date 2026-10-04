import type {ConfirmedResumeFact, ResumeFactTargets, ResumeRecord} from './types'

export function resumeChoiceLabel(resume:Pick<ResumeRecord,'label'|'filename'>):string {
  const extension=resume.filename.match(/\.([a-z0-9]+)$/i)?.[1]?.toUpperCase()||'文件'
  return `${resume.label||resume.filename} · ${extension}`
}

export function factInputError(attribute:string,value:string):string {
  if(!value.trim())return '请填写真实值；此入口不用于清空已有事实。'
  if(!['start_date','end_date'].includes(attribute))return ''
  if(attribute==='end_date'&&value==='至今')return ''
  if(!/^\d{4}(?:-\d{2}(?:-\d{2})?)?$/.test(value))return '日期请用 YYYY、YYYY-MM 或 YYYY-MM-DD；只知道年月时不要补日期。'
  const [year,month,day]=value.split('-').map(Number)
  if(month!==undefined&&(month<1||month>12))return '月份应为 01 到 12。'
  if(day!==undefined&&(day<1||day>new Date(Date.UTC(year,month,0)).getUTCDate()))return '请检查该月的真实日期。'
  return ''
}

export function confirmedFactRequest(data:ResumeFactTargets,resumeId:string,recordKey:string,attribute:string,value:string):ConfirmedResumeFact {
  if(!resumeId||data.resume_id!==resumeId||!data.revision)throw new Error('简历版本已变化，请重新读取可补充资料。')
  const record=data.records.find(item=>item.record_key===recordKey)
  const field=record?.attributes.find(item=>item.key===attribute)
  if(!record||!field)throw new Error('请明确选择这份简历中的经历和属性，不会自动猜测归属。')
  const next=value.trim()
  const error=factInputError(attribute,next)
  if(error)throw new Error(error)
  if(next===field.value)throw new Error('新值与已保存值相同，无需重复保存。')
  return {revision:data.revision,record_key:recordKey,attribute,value:next,confirmed:true}
}
