import assert from 'node:assert/strict'
import {confirmedFactRequest,factInputError,resumeChoiceLabel} from './resumeFacts.ts'

const data={resume_id:'cv-agent',revision:'revision-v1',records:[
  {record_key:'masters',section:'education',label:'Example University / Masters',attributes:[{key:'major',label:'专业',value:'AI'},{key:'start_date',label:'开始日期',value:'2024'}]},
  {record_key:'bachelors',section:'education',label:'Example College / Bachelors',attributes:[{key:'major',label:'专业',value:'CS'}]},
  {record_key:'project-a',section:'projects',label:'Agent Project',attributes:[{key:'role',label:'角色',value:''}]},
],warnings:[]}
assert.deepEqual(confirmedFactRequest(data,'cv-agent','masters','major','Machine Learning'),{revision:'revision-v1',record_key:'masters',attribute:'major',value:'Machine Learning',confirmed:true})
assert.equal(data.records[1].attributes[0].value,'CS','The undergraduate record is not patched by a masters confirmation')
assert.equal(confirmedFactRequest(data,'cv-agent','masters','start_date','2024-09').value,'2024-09','No invented first day')
for(const input of [['other-cv','masters','major','AI'],['cv-agent','','major','AI'],['cv-agent','unknown','major','AI'],['cv-agent','masters','ranking','前20%'],['cv-agent','masters','education[0].major','AI'],['cv-agent','masters','major','AI']]){
  assert.throws(()=>confirmedFactRequest(data,...input),'Must require explicit valid identity and a changed supported attribute')
}
for(const date of ['2024','2024-09','2024-02-29'])assert.equal(factInputError('start_date',date),'')
for(const date of ['2023-02-29','2024-13','2024-00','2024-04-31','2024.9','2024-9','tomorrow','至今'])assert.notEqual(factInputError('start_date',date),'')
assert.equal(factInputError('end_date','至今'),'')
assert.notEqual(factInputError('role','  '),'')
assert.equal(resumeChoiceLabel({label:'Agent Resume',filename:'resume.pdf'}),'Agent Resume · PDF')
assert.equal(resumeChoiceLabel({label:'Agent Resume',filename:'resume.docx'}),'Agent Resume · DOCX')
assert.notEqual(resumeChoiceLabel({label:'Same',filename:'a.pdf'}),resumeChoiceLabel({label:'Same',filename:'a.docx'}))
console.log('resume facts: explicit scoped identity, stale version guard, no date invention, distinct attachment types passed')
