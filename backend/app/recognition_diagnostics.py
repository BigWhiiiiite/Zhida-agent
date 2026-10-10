"""Bounded, read-only DOM evidence for debugging missing ATS questions.

No HTML dump, input values, cookies, arbitrary attributes or model request.
The authenticated browser owner receives only control types/classes and nearby
caption structure. This helps repair adapters from observed templates instead
of inventing selectors from an unreadable question.
"""
from __future__ import annotations


async def inspect_recognition_structure(page):
    return await page.evaluate(r"""() => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>el&&el.getClientRects().length>0&&!el.closest('[hidden],[aria-hidden="true"]')&&
        getComputedStyle(el).display!=='none'&&!['hidden','collapse'].includes(getComputedStyle(el).visibility);
      const captions='label,legend,h1,h2,h3,h4,[role="heading"],[class*="label" i],[class*="title" i],[class*="caption" i]';
      const controls='input,select,textarea,[role="combobox"],[role="radio"],[role="checkbox"],[aria-haspopup="listbox"]';
      const caption=node=>{
        const copy=node.cloneNode(true);
        copy.querySelectorAll(controls+',button,script,style,svg,[hidden],[aria-hidden="true"],[contenteditable],[role="option"],[class*="selected" i],[class*="selection-item" i]')
          .forEach(item=>item.remove());
        return clean(copy.textContent).slice(0,180);
      };
      const shape=node=>({tag:node.tagName.toLowerCase(),classes:[...node.classList].slice(0,8),
        role:node.getAttribute('role')||'',controls:node.querySelectorAll(controls).length});
      const fields=[...document.querySelectorAll(controls)].filter(el=>visible(el)&&
        !['hidden','password','submit','button','reset'].includes(el.type)&&el.autocomplete!=='one-time-code');
      const components=[...document.querySelectorAll('[class]')].filter(el=>visible(el)&&[...el.classList].some(c=>
        /^(phoenix-|ant-)/.test(c)));
      const unique=[]; const signatures=new Set();
      for(const el of components){
        const signature=[el.tagName,[...el.classList].join(' '),[...el.children].map(n=>[...n.classList].join(' ')).join('|')].join(':');
        if(!signatures.has(signature)){signatures.add(signature);unique.push(el);}
      }
      const tree=(node,depth)=>({...shape(node),before:getComputedStyle(node,'::before').content,
        after:getComputedStyle(node,'::after').content,
        children:depth?[...node.children].slice(0,10).map(n=>tree(n,depth-1)):[]});
      const questions=[...document.querySelectorAll('.form-item.form-item--phoenix')].filter(visible).slice(0,60);
      return {question_shapes:questions.map(el=>({ ...shape(el),children:[...el.children].slice(0,6).map(n=>({
        ...shape(n),caption:n.querySelector(controls)?'':caption(n),
        before:getComputedStyle(n,'::before').content,after:getComputedStyle(n,'::after').content,
        children:[...n.children].slice(0,8).map(c=>tree(c,1))
      })),ancestors:(()=>{const result=[];for(let n=el.parentElement,i=0;n&&i<18&&n!==document.body;n=n.parentElement,i++){
        result.push({...shape(n),headings:[...n.children].filter(c=>!c.contains(el)&&visible(c)&&
          (c.matches(captions)||c.querySelector(captions))).flatMap(c=>c.matches(captions)?[c]:[...c.querySelectorAll(captions)])
          .map(caption).filter(Boolean).slice(0,4)});}return result;})()})),
        repeat_add_shapes:[...document.querySelectorAll('.phoenix-text-button')].slice(0,30).map(el=>({
          ...shape(el), caption:caption(el), own_caption:caption(el.querySelector('.phoenix-text-button__textWraper')||el),
          visible:Boolean(visible(el)), opacity_chain:(()=>{const a=[];for(let n=el,d=0;n&&d<8;n=n.parentElement,d++)a.push(getComputedStyle(n).opacity);return a;})(),
          inline_click:el.hasAttribute('onclick'),href:el.hasAttribute('href'),
          disabled:el.matches('[disabled],[aria-disabled="true"],.phoenix-text-button--disabled'),
          inert_ancestor:Boolean(el.closest('[hidden],[aria-hidden="true"],[inert],[aria-disabled="true"]')),
          forbidden_ancestor:Boolean(el.closest('.ux-standard-form,header,nav,[role="dialog"]')),
          nested_controls:Boolean(el.querySelector('input,select,textarea,a')),
          direct_text:Boolean(el.querySelector(':scope > .phoenix-text-button__textWraper')),
          direct_icon:Boolean(el.querySelector(':scope > .phoenix-text-button__preIcon'))
        })),
        layout_shapes:[...document.querySelectorAll('.ux-standard-form')].filter(visible).slice(0,30).map(el=>{
          const result=[];
          for(let n=el,i=0;n&&i<7&&n!==document.body;n=n.parentElement,i++){
            result.push({...shape(n),siblings:[...n.parentElement.children].filter(c=>c!==n&&!c.querySelector(controls))
              .slice(0,8).map(c=>({...shape(c),caption:caption(c)}))});
          }return result;
        }),component_shapes:unique.slice(0,240).map(el=>({...shape(el),
        children:[...el.children].slice(0,8).map(shape)})),controls:fields.slice(0,80).map((el,index)=>{
        const ancestors=[];
        for(let node=el.parentElement,depth=0;node&&depth<14&&node!==document.body;node=node.parentElement,depth++){
          const labels=[...node.children].filter(child=>child!==el&&!child.contains(el)&&visible(child)&&
            (child.matches(captions)||child.querySelector(captions)))
            .flatMap(child=>child.matches(captions)?[child]:[...child.querySelectorAll(captions)])
            .filter(child=>!child.contains(el)&&!child.closest('header,nav,[role="navigation"],[role="search"]'))
            .map(caption).filter(Boolean);
          ancestors.push({...shape(node),captions:[...new Set(labels)].slice(0,5)});
        }
        return {ordinal:index+1,...shape(el),type:el.type||'',readonly:Boolean(el.readOnly),ancestors};
      }),truncated:fields.length>80};
    }""")


async def inspect_component_shapes(page):
    """Remember only component class/child shapes while an option menu is open."""
    return await page.evaluate(r"""() => {
      const caption=n=>String(n.innerText||'').replace(/\s+/g,' ').trim();
      const shape=n=>({tag:n.tagName.toLowerCase(),classes:[...n.classList].slice(0,8),role:n.getAttribute('role')||'',
        // Only format metadata, never the typed/selected personal date.
        ...(n.matches('input.phoenix-calendar-input')?{placeholder:n.getAttribute('placeholder')||'',
          readonly:Boolean(n.readOnly),type:n.type}:{}),
        ...(n.matches('.phoenix-calendar-header,.phoenix-calendar-footer')?{caption:caption(n).slice(0,120)}:{}),
        // Fixed widget-action captions only. Never arbitrary form values.
        ...(/^(确定|确认|完成|取消|清空|返回|上一级)$/.test(caption(n))?{caption:caption(n)}:{})});
      const seen=new Set(), result=[];
      const nodes=new Set([...document.querySelectorAll('[class]')].filter(n=>[...n.classList].some(c=>/^(phoenix-|ant-)/.test(c))));
      // Region pickers embed Phoenix breadcrumbs inside a separate widget.
      // Capture its bounded class-only shape, not its HTML or input values.
      for(const crumb of document.querySelectorAll('.phoenix-breadcrumb')){
        for(let n=crumb,d=0;n&&d<4&&n!==document.body;n=n.parentElement,d++){
          nodes.add(n);for(const child of [...n.querySelectorAll('[class]')].slice(0,160))nodes.add(child);
        }
      }
      for(const n of nodes){
        if(!n.getClientRects().length)continue;
        const item={...shape(n),children:[...n.children].slice(0,8).map(shape),
          focused:n===document.activeElement, owns:[n,...n.querySelectorAll('[aria-controls],[aria-owns]')]
            .flatMap(el=>(String(el.getAttribute('aria-controls')||'')+' '+String(el.getAttribute('aria-owns')||''))
              .trim().split(/\s+/).filter(Boolean)).map(id=>{
                const target=document.getElementById(id);
                return {exists:Boolean(target),target:target?shape(target):null,
                  ancestors:(()=>{const a=[];for(let p=target?.parentElement,d=0;p&&p!==document.body&&d<4;p=p.parentElement,d++)a.push(shape(p));return a;})()};
              }).slice(0,5)};
        const signature=JSON.stringify(item);if(seen.has(signature))continue;
        seen.add(signature);result.push(item);if(result.length>=160)break;
      }
      return result;
    }""")
