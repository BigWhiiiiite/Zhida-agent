import {defineConfig,loadEnv} from 'vite'

export function backendTarget(value='http://127.0.0.1:18000'){
  const target=new URL(value)
  if(!['http:','https:'].includes(target.protocol)||target.username||target.password||target.pathname!=='/'||target.search||target.hash){
    throw new Error('ZHIDA_BACKEND_URL must be an HTTP(S) origin without credentials or a path')
  }
  return target.origin
}

export default defineConfig(({mode})=>{
  const env=loadEnv(mode,process.cwd(),'ZHIDA_')
  const target=backendTarget(process.env.ZHIDA_BACKEND_URL||env.ZHIDA_BACKEND_URL)
  const proxy={'/api':{target,changeOrigin:false}}
  return{
    server:{host:'127.0.0.1',port:5173,strictPort:true,proxy},
    preview:{host:'127.0.0.1',port:4173,strictPort:true,proxy},
  }
})
