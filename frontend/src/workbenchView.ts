import type {ApplicationWorkflowState,BrowserSnapshot} from './types'
import {formStageReady} from './chatTask'

type Inputs={workflow:ApplicationWorkflowState|null;snapshot:BrowserSnapshot|null;hasPlan:boolean;hasDraftEdits:boolean;unresolvedQuestions:number;ready:boolean;hasExecution:boolean}
export type WorkbenchNext='refresh'|'choose'|'enter'|'fill'|'questions'|'continue'|'review'
export function actionableNavigation(workflow:ApplicationWorkflowState|null) {
  const hasSearchTarget=Boolean(workflow?.target?.job_title?.trim())
  return workflow?.navigation_candidates?.filter(candidate=>candidate.kind==='open_job'||candidate.kind==='browse_jobs'||candidate.kind==='search_jobs'&&hasSearchTarget)??[]
}
export function canContinueWorkflow(workflow:ApplicationWorkflowState|null) {
  return Boolean(workflow&&['profile_form','application_form'].includes(workflow.stage)&&workflow.safe_next_present&&!workflow.requires_consent)
}
export function workbenchView({workflow,snapshot,hasPlan,hasDraftEdits,unresolvedQuestions,ready,hasExecution}:Inputs) {
  const showForms=formStageReady(workflow,snapshot)
  const navigationCandidates=actionableNavigation(workflow)
  const base={showForms,navigationCandidates,showNavigation:Boolean(workflow&&['homepage','job_list'].includes(workflow.stage)&&navigationCandidates.length),showAccountTools:Boolean(workflow&&['auth_required','registration_required','verification_required'].includes(workflow.stage)),action:'refresh' as WorkbenchNext,label:'我已在招聘网页操作，重新识别',title:'先看看招聘浏览器中实际显示了什么',description:'页面可能仍在加载、弹出登录窗口，或尚未进入申请表单。请切到刚打开的招聘浏览器查看；如果仍然空白或无法判断，请提供那一页的截图。重复刷新不能代替登录或打开岗位。'}
  if(!workflow||!snapshot)return base
  if(showForms){
    if(hasDraftEdits)return {...base,action:'questions' as const,label:'处理需要我确认的问题',title:'你的修改已保留，先核对这些答案',description:'请先在下方填写并验证这些修改，或明确清除临时修改；不会跳过尚未执行的答案进入下一页。'}
    if(ready&&workflow.requires_consent)return {...base,title:'请先亲自核对招聘网站的确认项',description:'当前仍有协议或本人确认要求。请在招聘浏览器阅读并自行决定，完成后回来重新识别；系统不会代替你同意。'}
    if(ready&&canContinueWorkflow(workflow))return {...base,action:'continue' as const,label:`进入${workflow.safe_next_label||'下一页'}`,title:'当前页检查通过，可以继续',description:'只进入下一步，不会点击最终提交。'}
    if(ready)return {...base,action:'review' as const,label:'查看人工终审清单',title:'请最后核对这份申请',description:'已识别的表单项检查通过。请到招聘网页核对完整资料、附件和岗位；最终提交由你操作。'}
    if(unresolvedQuestions>0&&hasExecution)return {...base,action:'questions' as const,label:'处理需要我确认的问题',title:`还需要你确认 ${unresolvedQuestions} 道问题`,description:'已确定的资料不用重复填写。补充缺少的信息后，在下方填写并验证。'}
    if(hasExecution)return {...base,action:'review' as const,label:'查看填写结果与未完成项',title:'已完成一轮填写，请核对剩余问题',description:'下面会列出回读结果和仍未完成的项目；不会自动重复执行或宣称已提交。'}
    return {...base,action:'fill' as const,label:'开始智能填写',title:hasPlan?'已找到表单，可以开始填写':'申请表单已打开',description:'先填写有确定依据的资料，再让模型理解歧义项，最后只向你询问确实缺少的信息。'}
  }
  if(workflow.stage==='registration_required')return {...base,title:'先在招聘网站完成注册',description:'请切到招聘浏览器创建账号，并亲自确认隐私协议。完成后回来点击下面的按钮；需要填入账号或验证码时，可展开辅助工具。'}
  if(workflow.stage==='auth_required')return {...base,title:'先在招聘网站登录',description:'请切到招聘浏览器完成手机号、邮箱或扫码登录。这不是职达的登录页；登录后回来重新识别。'}
  if(workflow.stage==='verification_required')return {...base,title:'招聘网站正在等待验证码或人工验证',description:'请在招聘浏览器中完成验证码、人机验证或扫码。验证码辅助输入放在下方展开区，系统不会替你绕过验证。'}
  if(workflow.stage==='job_detail')return {...base,action:'enter' as const,label:'进入该岗位申请',title:'已打开岗位详情',description:'先进入招聘网站的申请入口；如果接下来需要登录，会停下提示你。'}
  if(['homepage','job_list'].includes(workflow.stage)&&base.showNavigation){const hasJob=navigationCandidates.some(candidate=>candidate.kind==='open_job');return {...base,action:'choose' as const,label:hasJob?'选择要申请的岗位':'查看可用招聘入口',title:hasJob?'先确认一个具体岗位':'先找到目标岗位',description:workflow.navigation_blocker||'下面只列出当前网页真实存在、并满足使用条件的入口。请确认目标岗位后再进入申请，不会擅自换岗。'}}
  if(['homepage','job_list'].includes(workflow.stage)&&workflow.navigation_candidates?.some(candidate=>candidate.kind==='search_jobs')&&!workflow.target?.job_title?.trim())return {...base,title:'请先在招聘网页输入想找的岗位',description:'目前只识别到搜索框，还没有具体岗位或明确的搜索目标。请切到招聘浏览器搜索、打开你想申请的岗位，再回来重新识别；这里不会把搜索框当成岗位让你选择。'}
  if(workflow.stage==='job_list')return {...base,title:'还没有读取到可进入的岗位',description:workflow.navigation_blocker||'请切到招聘浏览器，确认职位列表已加载；必要时先搜索或打开目标岗位，再回来重新识别。若列表一直空白，请提供招聘网页截图。'}
  if(workflow.stage==='homepage')return {...base,title:'请先在招聘网站打开岗位列表',description:workflow.navigation_blocker||'当前仍在招聘首页，尚未找到可靠的岗位入口。请在招聘浏览器打开校招或职位列表，再回来重新识别。'}
  if(workflow.stage==='unknown'&&workflow.form_fields>0)return {...base,label:'同步当前页面',title:`已读取到 ${workflow.form_fields} 个控件，页面类型仍待核对`,description:'网站可能已经打开申请表，但职达尚未确认页面类型。请先同步当前页面，不必重复登录或重新找岗位；如果同步后仍未识别，请提供当前招聘网页截图供核对。'}
  if(workflow.job_id)return {...base,title:'岗位链接已识别，但申请页面还没准备好',description:'网址中有岗位编号，不代表岗位内容或申请表单已经加载。请切到招聘浏览器查看是否空白、需要登录或出现弹窗；完成操作后再重新识别，仍异常时请提供该页截图。'}
  return base
}
