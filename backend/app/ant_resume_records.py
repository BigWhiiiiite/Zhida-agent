"""Read-only record boundaries for an observed Ant resume template.

The DOM shape was obtained through Zhida's control diagnostics. This adapter
does not guess a resume record from its page order, create records, or click
the template's unverified add/delete controls. Formal employment is excluded.
"""
from uuid import uuid4

from .form_observation import retain_adapter_evidence


RECORD_EVIDENCE_JS = r"""
const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
const visible=n=>n&&n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"],[inert]')&&
  getComputedStyle(n).display!=='none'&&!['hidden','collapse'].includes(getComputedStyle(n).visibility);
const controls='input:not([type="hidden"]),select,textarea,[role="combobox"],[role="radio"],[role="checkbox"]';
const helpers='small,.help,.hint,.tip,.description,[class*="help" i],[class*="error" i],[class*="tip" i]';
const renderedControl=n=>visible(n)||(n?.matches('input[type="file"]')&&
  !n.closest('[hidden],[aria-hidden="true"],[inert]')&&visible(n.parentElement));
const canonical=n=>n.matches('[role="radio"],[role="checkbox"]')?
  n.querySelector('input[type="'+n.getAttribute('role')+'"]')||n:n;
const rowControls=row=>[...new Set([...row.querySelectorAll(controls)]
  .filter(renderedControl).map(canonical))];
// Auxiliary questions are not education attributes or identity anchors. A
// familiar caption is insufficient: attachment rows must actually own a file.
const educationAux={
  '是否最高学历':'choice','是否为最高学历':'choice',
  '上传成绩单':'file','成绩单':'file','成绩单附件':'file',
  '学历附件':'file','学历证明附件':'file','学位证明附件':'file','学历学位证明附件':'file'
};
const ownValue=(mapping,key)=>Object.prototype.hasOwnProperty.call(mapping,key)?mapping[key]:'';
const attrs={
  education:{'学校名称':'school','院校名称':'school','毕业院校':'school','就读院校':'school','学校':'school',
    '学院名称':'college','院系名称':'college','所属学院':'college','学院':'college','院系':'college',
    '专业名称':'major','所学专业':'major','专业':'major','学历':'degree','学历层次':'degree','学位':'degree',
    '入学时间':'start_date','入学日期':'start_date','开始日期':'start_date','开始时间':'start_date',
    '毕业时间':'end_date','毕业日期':'end_date','结束日期':'end_date','结束时间':'end_date','预计毕业时间':'end_date',
    '学校所在地':'location','院校所在地':'location','就读地':'location',
    '培养方式':'study_mode','学习形式':'study_mode','是否统招':'study_mode','是否为统招':'study_mode',
    '学制':'academic_system','学号':'student_id','导师':'advisor','导师姓名':'advisor',
    '实验室':'laboratory','实验室名称':'laboratory','研究方向':'research_direction',
    '专业排名':'ranking','成绩排名':'ranking','GPA':'gpa','绩点':'gpa'},
  experience:{'实习单位':'organization','实习公司':'organization','实习岗位':'role','实习职位':'role',
    '实习内容':'description','开始日期':'start_date','结束日期':'end_date'},
  project:{'项目名称':'name','项目角色':'role','项目描述':'description','项目内容':'description',
    '项目职责':'responsibilities','开始日期':'start_date','结束日期':'end_date'}
};
const titles={'教育经历':'education','教育背景':'education','实习经历':'experience','实习经验':'experience','学生实践经验':'experience',
  '项目经历':'project','项目经验':'project'};
const caption=n=>{
  const copy=n.cloneNode(true);copy.querySelectorAll(controls+',button,a,svg,script,style,'+helpers).forEach(c=>c.remove());
  return clean(copy.textContent);
};
const label=row=>{
  const nodes=[...row.querySelectorAll('.ant-form-item-label,:scope > .resume-form-title')].filter(n=>visible(n)&&
    n.closest('.resume-form-item')===row&&!n.querySelector(controls));
  const text=[...new Set(nodes.map(caption).map(s=>s.replace(/^[*＊]\s*|\s*[*＊]$/g,'')))];
  return text.length===1?text[0]:'';
};
const oneQuestion=(row,aux)=>{
  const items=rowControls(row),roots=items.filter(n=>!items.some(p=>p!==n&&p.contains(n)));
  if(!roots.length)return false;
  if(aux==='file')return roots.length===1&&roots[0].matches('input[type="file"]');
  if(!aux&&roots.some(n=>n.matches('input[type="file"]')))return false;
  const choiceType=n=>n.matches('input[type="radio"],[role="radio"]')?'radio':
    n.matches('input[type="checkbox"],[role="checkbox"]')?'checkbox':'';
  if(roots.length===1)return aux!=='choice'||choiceType(roots[0])==='checkbox';
  const type=choiceType(roots[0]);
  if(!type||!roots.every(n=>choiceType(n)===type))return false;
  // Framework-controlled radios may omit every native name, but a mixture
  // of distinct or partly missing names is not one exclusive choice group.
  if(type==='radio'){
    const names=roots.map(n=>n.matches('input[type="radio"]')?clean(n.getAttribute('name')):'');
    if(names.some(Boolean)&&new Set(names).size!==1)return false;
  }
  const selector=type==='radio'?'[role="radiogroup"],.ant-radio-group':'[role="group"],.ant-checkbox-group';
  const groups=[...row.querySelectorAll(selector)].filter(group=>
    group.closest('.resume-form-item')===row&&roots.every(n=>group.contains(n)));
  return groups.length===1;
};
const surface=(record,report={})=>{
  const reject=reason=>{report.reason=reason;return null;};
  let section=record.parentElement;
  // Observed CEB template: one classless DIV holds sibling record roots
  // between .resume-tpl-wrap and .resume-form-wrap. Accept exactly this
  // transparent layer, never an arbitrary closest ancestor or mixed form.
  if(section?.tagName==='DIV'&&!section.classList.length&&!section.getAttribute('role')&&
    section.parentElement?.matches('.resume-tpl-wrap')&&section.children.length&&
    [...section.children].every(n=>n.matches('.resume-form-wrap')||
      n.matches('div.delete-item')&&!n.querySelector(controls+',.resume-form-wrap')&&caption(n)==='删除'))
    section=section.parentElement;
  if(!visible(record)||!section?.matches('.resume-tpl-wrap')||!visible(section)||
    record.querySelector('.resume-form-wrap')||record.closest('header,nav,[role="dialog"]'))
    return reject('record_boundary_unverified');
  const headings=[...new Set([...section.children].filter(n=>n!==record&&!n.querySelector(controls)&&visible(n))
    .map(caption).map(s=>s.replace(/\s*[+＋]?\s*添加\s*$/,'').trim()).filter(s=>ownValue(titles,s)))];
  if(headings.length!==1)return reject('section_heading_unverified');
  const title=headings[0],kind=titles[title];
  report.section=title;report.kind=kind;
  const rows=[...record.querySelectorAll('.resume-form-item')].filter(n=>visible(n)&&
    n.closest('.resume-form-wrap')===record);
  const all=[...record.querySelectorAll(controls)].filter(renderedControl);
  // Education has more attributes than a five-field internship. A bound on
  // size is not ownership: every caption/control and the unique school anchor
  // below must still prove this exact record, independently of page order.
  if(rows.length<3||rows.length>40||!all.length||all.some(n=>!rows.some(row=>row.contains(n))))
    return reject('row_control_boundary_unverified');
  const labels=rows.map(label),mapping=attrs[kind];
  // Unknown/mixed question sets and duplicated anchors are not one record.
  const auxiliary=labels.map(s=>kind==='education'?ownValue(educationAux,s):'');
  if(labels.some((s,i)=>!ownValue(mapping,s)&&!auxiliary[i]))return reject('unknown_question');
  if(new Set(labels).size!==labels.length)return reject('duplicate_question');
  const anchor=kind==='education'?'school':kind==='experience'?'organization':'name';
  const anchors=labels.filter(s=>ownValue(mapping,s)===anchor).length;
  if(anchors>1)return reject('identity_anchor_unverified');
  if(rows.some((row,i)=>row.querySelector('.resume-form-item')||!oneQuestion(row,auxiliary[i])))
    return reject('multiple_controls_in_row');
  // A verified local surface is not a source-record binding. The education
  // shell must not receive the trusted ant-resume-* prefix or allocation key.
  if(kind==='education'&&!anchors){
    if(!record.dataset.zhidaAntShell?.startsWith('ant-shell-observed:'))
      record.dataset.zhidaAntShell='ant-shell-observed:'+prefix+':'+sequence++;
    report.reason='identity_anchor_unverified';report.surface_observed=true;
    return {record,section,title,kind,rows,labels,auxiliary,key:record.dataset.zhidaAntShell,complete:false};
  }
  if(anchors!==1)return reject('identity_anchor_unverified');
  const anchorItems=rowControls(rows[labels.findIndex(s=>mapping[s]===anchor)]);
  const anchorRoots=anchorItems.filter(n=>!anchorItems.some(p=>p!==n&&p.contains(n)));
  if(anchorRoots.length!==1||!(anchorRoots[0].matches('select,textarea,[role="combobox"]')||
      anchorRoots[0].matches('input')&&['text','search'].includes(anchorRoots[0].type)))
    return reject('identity_control_unverified');
  if(kind==='education'?!labels.some(s=>['major','degree'].includes(mapping[s])):
    !labels.some(s=>mapping[s]==='description')||!labels.some(s=>mapping[s]==='role'))
    return reject('record_attributes_missing');
  record.dataset.zhidaAntRecord ||= 'ant-resume-'+prefix+'-'+sequence++;
  report.reason='verified';report.surface_observed=true;
  return {record,section,title,kind,rows,labels,auxiliary,key:record.dataset.zhidaAntRecord,complete:true};
};
const evidence=(record,report={})=>{
  const proof=surface(record,report);
  return proof?.complete?proof:null;
};
"""


async def refine_ant_resume_fields(page, data):
    patches = await page.evaluate("({selectors,prefix})=>{let sequence=0;" + RECORD_EVIDENCE_JS + r"""
      const cache=new Map();
      return selectors.map(selector=>{
        const matches=document.querySelectorAll(selector);if(matches.length!==1)return null;
        const el=matches[0],record=el.closest('.resume-form-wrap');if(!record)return null;
        if(!cache.has(record))cache.set(record,surface(record));
        const proof=cache.get(record);if(!proof)return null;
        const row=el.closest('.resume-form-item'),index=proof.rows.indexOf(row);if(index<0)return null;
        const question=proof.labels[index];
        return {label:question,question_text:question,label_source:'container-owned',recognition_confidence:.99,
          section:proof.title,section_path:[proof.title],container_key:proof.key,
          record_evidence:proof.complete?'ant-resume-owned':'ant-resume-shell-observed',
          semantic_key:proof.auxiliary[index]?'application.custom':proof.kind+'.'+attrs[proof.kind][question],
          entity_scope:proof.kind+':unspecified'};
      });
    }""", {'selectors':[item['selector'] for item in data], 'prefix':uuid4().hex})
    result = []
    for item, patch in zip(data, patches):
        if patch:
            patch['required'] = item.get('required', False)
            source = patch['record_evidence']
            item = {**item, **retain_adapter_evidence(item, patch, source)}
        result.append(item)
    return result


async def inspect_ant_resume_records(page):
    """Known empty slots may be allocated; add buttons remain unsupported."""
    return await page.evaluate("({prefix})=>{let sequence=0;" + RECORD_EVIDENCE_JS + r"""
      const grouped=new Map();
      for(const record of document.querySelectorAll('.resume-form-wrap')){
        const proof=evidence(record);if(!proof)continue;
        if(!grouped.has(proof.section))grouped.set(proof.section,[]);
        grouped.get(proof.section).push(proof);
      }
      return [...grouped.values()].map(rows=>({id:'ant-resume:'+rows[0].key,selector:'',
        semantic_section:rows[0].kind,record_count:rows.length,container_key:rows[0].key,
        record_keys:rows.map(p=>p.key),label:rows[0].title}));
    }""", {'prefix':uuid4().hex})


async def inspect_ant_resume_rejections(page):
    """Fixed rejection codes only; never input values or arbitrary captions.

    A synthetic fixture can pass while a live template is rejected. Exposing
    the exact failed structural gate makes that difference observable without
    weakening record ownership or asking the operator to guess DOM structure.
    """
    return await page.evaluate("({prefix})=>{let sequence=0;" + RECORD_EVIDENCE_JS + r"""
      const records=[...document.querySelectorAll('.resume-form-wrap')].filter(visible);
      const shape=n=>n?{tag:n.tagName.toLowerCase(),classes:[...n.classList].slice(0,8),
        controls:n.querySelectorAll(controls).length}:null;
      return {truncated:records.length>30,records:records.slice(0,30).map(record=>{
        const report={},proof=surface(record,report);
        const rows=[...record.querySelectorAll('.resume-form-item')].filter(n=>visible(n)&&
          n.closest('.resume-form-wrap')===record);
        const known=new Set([...Object.keys(attrs[report.kind]||{}),
          ...(report.kind==='education'?Object.keys(educationAux):[])]);
        return {reason:report.reason,verified:Boolean(proof?.complete),surface_observed:Boolean(report.surface_observed),section:report.section||'',
          row_count:rows.length,control_count:[...record.querySelectorAll(controls)].filter(visible).length,
          known_questions:rows.map(label).filter(s=>known.has(s)),
          unknown_question_count:rows.filter(row=>!known.has(label(row))).length,
          parent_shape:shape(record.parentElement),
          parent_children:[...record.parentElement.children].slice(0,8).map(shape),
          record_visible:Boolean(visible(record)),parent_visible:Boolean(visible(record.parentElement)),
          grandparent_shape:shape(record.parentElement.parentElement),
          nested_record:Boolean(record.querySelector('.resume-form-wrap')),
          forbidden_ancestor:Boolean(record.closest('header,nav,[role="dialog"]')),
          row_shapes:rows.slice(0,8).map(row=>({children:[...row.children].slice(0,6).map(n=>({
            ...shape(n),children:[...n.children].slice(0,6).map(shape)}))}))};
      })};
    }""", {'prefix':uuid4().hex})
