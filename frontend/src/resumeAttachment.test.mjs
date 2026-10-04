import assert from 'node:assert/strict'
import {resumeAttachmentBlocker,resumeAttachmentRequest} from './resumeAttachment.ts'

const cv={field_type:'file',label:'简历附件',name:'',current_value:''}
const other={field_type:'file',label:'附件',name:'',current_value:''}
assert.equal(resumeAttachmentBlocker({fields:[cv,other]}),'')
assert.notEqual(resumeAttachmentBlocker(null),'')
assert.notEqual(resumeAttachmentBlocker({fields:[other]}),'')
assert.notEqual(resumeAttachmentBlocker({fields:[cv,{...cv,name:'resume2'}]}),'')
assert.notEqual(resumeAttachmentBlocker({fields:[{...cv,current_value:'existing.pdf'}]}),'')
assert.notEqual(resumeAttachmentBlocker({fields:[{...cv,label:'简历/身份证照片'}]}),'')
assert.notEqual(resumeAttachmentBlocker({fields:[{...cv,field_type:'text'}]}),'')
assert.throws(()=>resumeAttachmentRequest('cv','revision'),/确认/)
assert.throws(()=>resumeAttachmentRequest('','revision',true),/失效/)
assert.throws(()=>resumeAttachmentRequest('cv','',true),/失效/)
const request=resumeAttachmentRequest('cv','revision',true)
assert.deepEqual(request,{actions:[],min_confidence:.85,resume_id:'cv',context_token:'revision',upload_resume:true})
assert.equal('allow_site_parse' in request,false)
assert.throws(()=>resumeAttachmentRequest('cv','revision'),/确认/,'Permission is one-shot, never retained')
console.log('resume attachment: unique empty CV input, explicit one-shot consent, upload-only request passed')
