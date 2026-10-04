import type {ApplicationJourneyResult, WorkflowStage} from './types'

export function journeyBackendReady(current:{journey_version?:number}):boolean {
  return typeof current.journey_version==='number'&&current.journey_version>=1
}

export function journeyButtonLabel(stage:WorkflowStage|undefined):string {
  if(stage==='auth_required')return '我已完成登录，让职达继续'
  if(stage==='registration_required')return '我已完成注册，让职达继续'
  if(stage==='verification_required')return '我已完成验证，让职达继续'
  return '让职达继续'
}

export function journeyStatus(status:ApplicationJourneyResult['status']):string {
  return {
    waiting_login:'等待你在招聘网站登录',
    waiting_registration:'等待你在招聘网站注册',
    waiting_verification:'等待你完成验证码或人机验证',
    needs_user:'需要你确认缺失信息',
    ready_for_review:'已到人工终审，尚未提交',
    blocked:'已暂停，请处理阻塞原因',
    partial:'已完成部分步骤，需要继续核对',
  }[status]
}

export function journeyGuard(state:{busy:boolean;available:boolean;resumeId:string;hasDraftEdits:boolean}):string {
  if(state.busy)return '正在处理上一项操作，请稍候。'
  if(!state.available)return '后端尚未加载连续推进能力，请重新检查连接状态；仍可只读查看当前页面。'
  if(!state.resumeId)return '请先选择本次使用的简历。'
  if(state.hasDraftEdits)return '请先填写并验证或明确清除本页临时答案，再让职达继续。'
  return ''
}
