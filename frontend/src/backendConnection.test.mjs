import assert from 'node:assert/strict'
import {backendConnection} from './backendConnection.ts'
import {backendTarget} from '../vite.config.mjs'

assert.equal(backendTarget(),'http://127.0.0.1:18000')
assert.equal(backendTarget('http://127.0.0.1:8000'),'http://127.0.0.1:8000')
for(const invalid of ['file:///tmp/backend','http://user:secret@localhost:18000','http://localhost:18000/api'])assert.throws(()=>backendTarget(invalid))
const current=backendConnection({status:'ok',backend_generation:'v2',storage:'postgresql',langgraph_enabled:false})
assert.equal(current.legacy,false)
assert.match(current.label,/PostgreSQL/)
assert.match(current.detail,/尚未启用/)
assert.equal(backendConnection({status:'ok'}).legacy,true)
assert.equal(backendConnection({status:'ok',backend_generation:'v2',storage:'sqlite'}).legacy,true)
assert.throws(()=>backendConnection({status:'error'}))
console.log('Backend cutover: explicit target and truthful status passed')
