"""Read-only field metadata repair for the observed Autohome delivery form."""
from __future__ import annotations

from urllib.parse import urlparse

from playwright.async_api import Page

from .field_semantics import education_level_hint


async def refine_autohome_fields(page: Page, data: list[dict]) -> list[dict]:
    parsed = urlparse(page.url)
    if (parsed.scheme != "https" or parsed.netloc.casefold() not in {
            "talent.autohome.com.cn", "talent.autohome.com.cn:443"}
            or parsed.path != "/recruit-delivery.html"):
        return data
    patches = await page.evaluate(r"""selectors => {
      if(location.origin!=='https://talent.autohome.com.cn'||location.pathname!=='/recruit-delivery.html') return [];
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>el&&el.getClientRects().length>0&&getComputedStyle(el).display!=='none'&&
        getComputedStyle(el).visibility!=='hidden';
      const ownLabel=el=>clean(el.closest('.col')?.querySelector(':scope > .labeltag > .label')?.innerText);
      const sections=[...document.querySelectorAll('.icard')];
      const repeatedConfigs={
        '实习/工作经历':{kind:'experience',template:'workTemplate',anchor:'Company',fields:{
          Company:['企业名称','organization'],Title:['所任职位','role'],
          StartDate:['开始时间','start_date'],EndDate:['结束时间','end_date'],Summary:['工作描述','description']}},
        '项目经历':{kind:'project',template:'projectTemplate',anchor:'ProjectName',fields:{
          ProjectName:['项目名称','name'],Title:['项目角色','role'],
          StartDate:['开始时间','start_date'],EndDate:['结束时间','end_date'],ProjectDescription:['项目描述','description']}}
      };
      const valueBinding=el=>(/(?:^|,)\s*value\s*:\s*([A-Za-z_$][\w$]*)\s*(?:,|$)/
        .exec(el.getAttribute('data-bind')||'')||[])[1]||'';
      const recordFor=(el,section,config)=>{
        if(!section?.closest('form.validform')) return null;
        const sameSections=sections.filter(n=>visible(n)&&
          clean(n.querySelector(':scope > .title')?.innerText)===clean(section.querySelector(':scope > .title')?.innerText));
        if(sameSections.length!==1) return null;
        const roots=[...section.querySelectorAll('div[data-bind]')].filter(n=>
          new RegExp("name\\s*:\\s*['\"]"+config.template+"['\"]").test(n.getAttribute('data-bind')||''));
        if(roots.length!==1||!roots[0].contains(el)) return null;
        const root=roots[0],children=[...root.children],groups=[];
        let start=0;
        for(let index=0;index<children.length;index++){
          if(!children[index].matches('.option')) continue;
          const group=children.slice(start,index);
          const anchors=group.flatMap(n=>[...n.querySelectorAll('input[data-bind]')])
            .filter(n=>valueBinding(n)===config.anchor);
          if(anchors.length!==1) return null;
          groups.push(group);start=index+1;
        }
        if(!groups.length||groups.length>30||children.slice(start).some(n=>
          n.matches('input,select,textarea')||n.querySelector('input,select,textarea'))) return null;
        const index=groups.findIndex(group=>group.some(n=>n.contains(el)));
        return index<0?null:{index,group:groups[index]};
      };
      return selectors.map(selector=>{
        let matches;
        try { matches=document.querySelectorAll(selector); } catch { return null; }
        if(matches.length!==1) return null;
        let el=matches[0];
        const wrapper=el.closest('.select2-container');
        if(wrapper?.previousElementSibling?.matches('select')) el=wrapper.previousElementSibling;
        if(!el.matches('input,select,textarea')) return null;
        const visual=el.matches('select.select2-hidden-accessible')?el.nextElementSibling:el;
        if(!visible(visual)||el.disabled) return null;
        const col=el.closest('.col');
        const card=el.closest('.delivery_card');
        const section=el.closest('.icard');
        let label=ownLabel(el), sectionTitle=clean(section?.querySelector(':scope > .title')?.innerText);
        let containerKey=section?`autohome:section:${sections.indexOf(section)}`:'';
        let sectionPath=sectionTitle?[sectionTitle]:[];
        let help=clean(col?.querySelector(':scope > .labeltag > .tip')?.innerText);
        let degree='', education=false, firstBachelor=false;
        const required=Boolean(el.required||el.getAttribute('aria-required')==='true'||
          (el.hasAttribute('datatype')&&el.getAttribute('ignore')!=='ignore')||
          (col&&clean(col.querySelector(':scope > .labeltag > .requireTag')?.innerText)==='*'));
        const patch={required};
        if(sectionTitle==='教育经历'){
          const repeated=el.closest('div[data-bind*="eduTemplate"]');
          if(!repeated||!section?.contains(repeated)) return null;
          let child=el;
          while(child.parentElement&&child.parentElement!==repeated) child=child.parentElement;
          const children=[...repeated.children], index=children.indexOf(child);
          if(index<0) return null;
          const before=children.slice(0,index), groupIndex=before.filter(n=>n.matches('.option')).length;
          const start=before.reduce((last,n,i)=>n.matches('.option')?i+1:last,0);
          let end=children.findIndex((n,i)=>i>=index&&n.matches('.option'));
          if(end<0) end=children.length;
          const group=children.slice(start,end);
          const degrees=group.flatMap(n=>[...n.querySelectorAll('select')]).filter(n=>ownLabel(n)==='学历');
          if(degrees.length===1&&degrees[0].value){
            degree=clean(degrees[0].selectedOptions[0]?.textContent);
            if(/^请选择|^选择$/.test(degree)) degree='';
          }
          const instruction=clean(section.querySelector('.contentFirstRow .tip')?.innerText);
          firstBachelor=groupIndex===0&&/请从本科学历开始填写/.test(instruction);
          help=[help,firstBachelor?instruction:''].filter(Boolean).join(' ');
          containerKey=`autohome:education:${sections.indexOf(section)}:${groupIndex+1}`;
          sectionPath=['教育经历',`教育经历-${groupIndex+1}`];
          education=true;
        } else if(repeatedConfigs[sectionTitle]){
          const config=repeatedConfigs[sectionTitle],record=recordFor(el,section,config);
          const binding=valueBinding(el),field=config.fields[binding];
          if(!record||!field||label!==field[0]) return null;
          // The same binding must occur once inside this particular record.
          // Never identify a resume item from DOM order or borrow a sibling's value.
          const peers=record.group.flatMap(n=>[...n.querySelectorAll('input,select,textarea')])
            .filter(n=>valueBinding(n)===binding);
          if(peers.length!==1) return null;
          containerKey=`autohome:${config.kind}:${sections.indexOf(section)}:${record.index+1}`;
          sectionPath=[sectionTitle,`${sectionTitle}-${record.index+1}`];
          patch.semantic_key=`${config.kind}.${field[1]}`;
          patch.entity_scope=`${config.kind}:unspecified`;
        } else if(el.closest('.personDevCard')&&card){
          const question=clean(card.querySelector(':scope > .title')?.innerText).replace(/^\*\s*/,'');
          if(question!=='您是如何得知此次校园招聘信息的') return null;
          const binding=el.getAttribute('data-bind')||'';
          if(el.id==='knowType') label=question+'（来源类型）';
          else if(/\bknowSource\b/.test(binding)) label=question+'（具体来源）';
          else return null;
          sectionTitle='招聘信息来源'; sectionPath=[sectionTitle]; containerKey='autohome:source';
          help=clean(el.getAttribute('placeholder'));
        } else if(el.id==='uploadFile'&&el.type==='file'&&card){
          label='简历附件'; sectionTitle=label; sectionPath=[label]; containerKey='autohome:attachment';
          patch.required=clean(card.querySelector(':scope > .title > .requiretag')?.innerText)==='*';
          help=clean(card.querySelector(':scope > .title > .tip')?.innerText);
        } else if(el.id==='agreechk'&&el.type==='checkbox'&&el.closest('.scard.fixbottom')){
          label=clean(el.parentElement.querySelector(':scope > .txt')?.innerText);
          if(!/^我承诺所填简历真实可信/.test(label)) return null;
          sectionTitle='诚信承诺'; sectionPath=[sectionTitle]; containerKey='autohome:declaration';
          patch.required=true; patch.group_label=label; patch.option_label=label;
          help='诚信承诺需由本人阅读并确认';
        }
        if(!label) return null;
        return {...patch,label,question_text:label,label_source:'container-owned',recognition_confidence:.99,
          context:[...sectionPath,label].join(' / '),help_text:help,nearby_labels:[label],
          group_label:patch.group_label||label,section:sectionTitle,section_path:sectionPath,
          container_key:containerKey,_education:education,_degree:degree,_firstBachelor:firstBachelor};
      });
    }""", [str(item.get("selector") or "") for item in data])
    result = []
    for index, item in enumerate(data):
        patch = patches[index] if index < len(patches) else None
        if not patch:
            result.append(dict(item))
            continue
        education = patch.pop("_education", False)
        degree = patch.pop("_degree", "")
        first_bachelor = patch.pop("_firstBachelor", False)
        if education:
            level = education_level_hint(degree) if degree else ("bachelor" if first_bachelor else "")
            patch["entity_scope"] = f"education:{level or 'unspecified'}"
        result.append({**item, **patch})
    return result
