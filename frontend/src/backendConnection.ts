export type BackendHealth={
  status:string
  storage?:string
  backend_generation?:string
  compatibility?:boolean
  langgraph_enabled?:boolean
  schema_revision?:string
}

export function backendConnection(health:BackendHealth):{label:string;detail:string;legacy:boolean}{
  if(health.status!=='ok')throw new Error('后端尚未就绪，请检查服务后重试。')
  if(health.backend_generation==='v2'&&health.storage==='postgresql')return{
    label:'已连接新后端 · PostgreSQL',
    detail:health.langgraph_enabled?'LangGraph 已启用':'资料已使用新后端；保留现有工作台流程，LangGraph 尚未启用。',
    legacy:false,
  }
  return{label:'当前连接旧后端',detail:'当前接口不是 PostgreSQL V2 网关，请核对启动服务和前端连接配置。',legacy:true}
}
