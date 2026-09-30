import type { CandidateProfile, Education, KnowledgeMappingTarget } from './types'

function educationLevel(value:string) {
  const doctorate = /博士|doctor|\bph\.?d\.?\b/i.test(value)
  const levels = [
    [/高中|中专|high school|secondary school/i.test(value), 'education:high_school'],
    [/专科|大专|associate|college diploma/i.test(value), 'education:associate'],
    [/本科|学士|bachelor|undergraduate|\b(?:bsc|bs|beng|b\.sc\.?|b\.s\.?)\b/i.test(value), 'education:bachelor'],
    [/硕士|master|\b(?:msc|ms|meng|m\.sc\.?|m\.s\.?)\b/i.test(value) || !doctorate && /研究生/.test(value), 'education:master'],
    [doctorate, 'education:doctorate'],
  ].filter(([matches])=>matches)
  return levels.length===1 ? levels[0][1] : ''
}

export function mappingPreview(profile:CandidateProfile|null, target:KnowledgeMappingTarget|undefined, entityScope:string) {
  if (!profile || !target) return {value:'', message:'选择对应的主档案字段后，预览当前资料。'}
  let value:unknown
  if (target.path.startsWith('education.')) {
    if (!entityScope) return {value:'', message:'必须指定学历层级，不能把本科与硕士资料混用。'}
    const matches = profile.education.filter(item => educationLevel(item.degree) === entityScope)
    if (matches.length !== 1) return {value:'', message:matches.length ? '主档案中有多条同层级教育经历，无法唯一确定，仍需人工确认。' : '主档案没有可唯一识别的该学历经历。可保存对照，但补全资料前不会自动填入其他学历。'}
    value = matches[0][target.path.slice('education.'.length) as keyof Education]
  } else {
    value = profile[target.path as keyof CandidateProfile]
  }
  const text = Array.isArray(value) ? value.join('、') : typeof value === 'string' || typeof value === 'number' ? String(value) : ''
  return text && text !== '未识别'
    ? {value:text, message:'仅预览当前主档案；执行时会重新读取最新值，并匹配网页真实选项。'}
    : {value:'', message:'主档案暂未填写这一项。对照关系可以保存，但不会生成或猜测答案。'}
}
