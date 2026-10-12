// Read-only preview contract. No candidate answers, model, or browser action API.
export type RecognitionPreviewScope = {sessionId:string;resumeId:string;url:string}
export type RecognitionPreviewField = {
  id:string;question:string;question_status:string;control_kind:string;required:boolean;
  required_status:string;options_status:string;section_path:string[];matchable:boolean;reasons:string[];
  options:{label:string;disabled:boolean}[];
  entity_binding:{kind:string;status:string};
}
export type RecognitionPreview = {
  schema_version:'live-recognition-preview-v1';session_id:string;capture_id:string;document_id:string;
  captured_at:string;url:string;title:string;browser_actions_executed:0;model_calls_executed:0;
  graph_executed:false;persisted:false;preview_only:true;
  recognition:{schema_version:'form-recognition-v1';fields:RecognitionPreviewField[];
    issues:{code:string;field_id:string;detail:string}[];
    coverage:{scope:'current_visible_document';status:string;observed_controls:number;captured_controls:number;
      unmapped_controls:number;pending_sections:string[];embedded_regions:number;unread_shadow_regions:number;
      limitations:string[];application_complete:false};
    needs_observation:boolean;read_only:true;ready_for_review:false;executable_actions:[];
  }
}

const object=(value:unknown):value is Record<string,unknown>=>Boolean(value)&&typeof value==='object'&&!Array.isArray(value)
const strings=(value:unknown):value is string[]=>Array.isArray(value)&&value.every(item=>typeof item==='string')
const count=(value:unknown)=>Number.isInteger(value)&&Number(value)>=0
const nonempty=(value:unknown):value is string=>typeof value==='string'&&Boolean(value.trim())
export const previewScopeKey=(scope:RecognitionPreviewScope)=>JSON.stringify([scope.sessionId,scope.resumeId,scope.url])

// Exact path/query/hash are significant: two jobs on one host are not one document.
export function samePreviewUrl(left:string,right:string){
  try{
    const a=new URL(left),b=new URL(right)
    return ['http:','https:'].includes(a.protocol)&&['http:','https:'].includes(b.protocol)&&
      !a.username&&!a.password&&!b.username&&!b.password&&a.href===b.href
  }catch{return false}
}

export function validateRecognitionPreview(value:unknown,scope:RecognitionPreviewScope):RecognitionPreview{
  const fail=()=>{throw new Error('只读识别结果的会话、页面或数据格式不匹配，未采纳本轮结果。')}
  if(!object(value)||!nonempty(scope.sessionId)||!nonempty(scope.resumeId)||
    value.schema_version!=='live-recognition-preview-v1'||value.session_id!==scope.sessionId||
    !nonempty(value.url)||!samePreviewUrl(value.url,scope.url)||!nonempty(value.capture_id)||!nonempty(value.document_id)||
    !nonempty(value.captured_at)||!Number.isFinite(Date.parse(value.captured_at))||typeof value.title!=='string'||
    value.browser_actions_executed!==0||value.model_calls_executed!==0||value.graph_executed!==false||value.persisted!==false||value.preview_only!==true)return fail()
  const recognition=value.recognition
  if(!object(recognition)||recognition.schema_version!=='form-recognition-v1'||recognition.read_only!==true||
    recognition.ready_for_review!==false||typeof recognition.needs_observation!=='boolean'||
    !Array.isArray(recognition.executable_actions)||recognition.executable_actions.length!==0||
    !Array.isArray(recognition.fields)||recognition.fields.length>500||!Array.isArray(recognition.issues))return fail()
  const coverage=recognition.coverage
  if(!object(coverage)||coverage.scope!=='current_visible_document'||coverage.application_complete!==false||
    !['observed','partial','unknown'].includes(String(coverage.status))||!strings(coverage.pending_sections)||!strings(coverage.limitations)||
    !['observed_controls','captured_controls','unmapped_controls','embedded_regions','unread_shadow_regions'].every(key=>count(coverage[key])))return fail()
  for(const item of recognition.fields){
    if(!object(item)||!nonempty(item.id)||typeof item.question!=='string'||typeof item.question_status!=='string'||
      typeof item.control_kind!=='string'||typeof item.required!=='boolean'||typeof item.required_status!=='string'||
      typeof item.options_status!=='string'||typeof item.matchable!=='boolean'||!strings(item.section_path)||!strings(item.reasons)||
      !Array.isArray(item.options)||!object(item.entity_binding)||
      typeof item.entity_binding.kind!=='string'||typeof item.entity_binding.status!=='string')return fail()
    for(const option of item.options){
      if(!object(option)||typeof option.label!=='string'||typeof option.disabled!=='boolean')return fail()
    }
  }
  if(new Set(recognition.fields.map(field=>(field as RecognitionPreviewField).id)).size!==recognition.fields.length)return fail()
  if(!recognition.issues.every(issue=>object(issue)&&typeof issue.code==='string'&&typeof issue.field_id==='string'&&typeof issue.detail==='string'))return fail()
  return value as RecognitionPreview
}

export function acceptRecognitionPreview(value:unknown,requested:RecognitionPreviewScope,current:RecognitionPreviewScope){
  return previewScopeKey(requested)===previewScopeKey(current)?validateRecognitionPreview(value,requested):null
}

export function previewQuestionClear(field:RecognitionPreviewField){
  const optionsClear=!['dropdown','radio','checkbox','cascader'].includes(field.control_kind)||
    (['native_complete','group_complete'].includes(field.options_status)&&field.options.length>0)
  return Boolean(field.question.trim())&&field.question_status==='verified'&&field.matchable&&field.reasons.length===0&&
    !['unknown','ambiguous','unresolved'].includes(field.entity_binding.status)&&field.control_kind!=='unknown'&&optionsClear
}

export function recognitionPreviewSummary(preview:RecognitionPreview){
  const fields=preview.recognition.fields,clear=fields.filter(previewQuestionClear).length
  return {total:fields.length,clear,attention:fields.length-clear,
    coverage:preview.recognition.coverage.status==='observed'?'已观察当前文档（不代表整表完整）':
      preview.recognition.coverage.status==='partial'?'读取不完整，仍有缺口':'读取范围尚未核实'}
}

export const previewControlLabel=(kind:string)=>({text:'直接输入',dropdown:'下拉选择',radio:'单选',checkbox:'多选／勾选',
  date:'日期选择',file:'附件上传',cascader:'级联选择',unknown:'控件类型待核实'} as Record<string,string>)[kind]||'控件类型待核实'
export const previewStatusLabel=(status:string)=>({verified:'题干有证据',unverified:'题干证据不足',ambiguous:'证据冲突',missing:'缺失',
  native_complete:'已读取原生选项',group_complete:'已读取同题选项',observed_subset:'只读到部分选项',unavailable:'未读取到选项',
  deferred:'选项未展开',dependent:'依赖上级选择',calendar:'日期控件',not_applicable:'不适用',
  required:'有必填依据',not_marked:'未观察到必填标记（不能认定选填）',record_observed:'已观察到记录边界，尚未匹配简历',
  unresolved:'记录归属待核实',unknown:'待核实'} as Record<string,string>)[status]||status||'待核实'
export const previewReasonLabel=(reason:string)=>({question_missing:'题干缺失',question_unowned:'无法证明题干属于这个控件',question_conflict:'题干证据冲突',
  control_conflict:'控件类型证据冲突',control_unknown:'控件类型未识别',control_disabled:'控件不可操作',readonly_text:'文本控件只读',
  date_precision_missing:'尚未读到日期精度',dependent_options:'级联选项依赖上级选择',group_conflict:'同题分组冲突',group_incomplete:'同题选项未读全',
  group_unproven:'无法证明这些选项属于同一题',options_conflict:'选项证据冲突',options_deferred:'选项尚未展开',options_missing:'尚未读取真实选项',
  options_partial:'真实选项只读到一部分',record_conflict:'经历归属证据冲突',record_unresolved:'经历归属尚未核实',required_conflict:'必填标记冲突',
  target_conflict:'控件定位冲突',target_missing:'缺少控件定位'} as Record<string,string>)[reason]||reason
