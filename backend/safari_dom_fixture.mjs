// A deterministic DOM shim in Node, not a browser or a real ATS session.
import vm from 'node:vm';
import readline from 'node:readline';

class Event {
  constructor(type, options={}) { this.type=type; Object.assign(this,options); }
}
class Element {
  constructor(tag, attributes={}) {
    this.tagName=tag.toUpperCase(); this.attributes=attributes; this.listeners={};
    this.innerText=this.textContent=attributes.text||''; this.disabled=false; this.readOnly=false;
    this.hidden=false; this.parentElement={disabled:false}; this.children=[];
  }
  getClientRects() { return this.hidden ? [] : [{}]; }
  closest() { return this.hidden ? this : null; }
  querySelectorAll(selector) { return this.children.filter(el=>matches(el,selector)); }
  matches(selector) {
    if(selector.includes('input:not(')) return ['INPUT','TEXTAREA'].includes(this.tagName)&&!['file','checkbox','radio'].includes(this.type);
    return matches(this,selector);
  }
  getAttribute(name) { return this.attributes[name]??null; }
  addEventListener(type, callback) { (this.listeners[type]||=[]).push(callback); }
  dispatchEvent(event) { for(const listener of this.listeners[event.type]||[]) listener(event); return true; }
  focus() { document.activeElement=this; }
  blur() { document.activeElement=document.body; }
  scrollIntoView() {}
  click() {
    if (['checkbox','radio'].includes(this.type)) this.checked=this.type==='radio'?true:!this.checked;
    this.dispatchEvent(new Event('click'));
  }
}
class HTMLInputElement extends Element {
  constructor(attributes={}) { super('input',attributes); this.type=attributes.type||'text'; this._value=''; this.checked=false; }
  get value() { return this._value; }
  set value(value) { this._value=String(value); }
}
class HTMLTextAreaElement extends Element {
  constructor(attributes={}) { super('textarea',attributes); this._value=''; }
  get value() { return this._value; }
  set value(value) { this._value=String(value); }
}
function matches(el, selector) {
  if(selector.includes(',')) return selector.split(',').some(item=>matches(el,item.trim()));
  if(selector.startsWith('#')) return el.attributes.id===selector.slice(1);
  if(selector.startsWith('.')) return (el.attributes.class||'').split(' ').includes(selector.slice(1));
  const match=selector.match(/^(\w+)(?:\[(name|type)="([^"]+)"\])?$/);
  return Boolean(match&&el.tagName.toLowerCase()===match[1]&&(!match[2]||(el.attributes[match[2]]||el[match[2]])===match[3]));
}
const name=new HTMLInputElement({name:'candidate'});
const checkbox=new HTMLInputElement({name:'confirmed',type:'checkbox'});
const gender=new HTMLInputElement({name:'gender',type:'radio'});
const notes=new HTMLTextAreaElement({name:'notes'});
const city=new Element('select',{name:'city'});
city.multiple=false;
city.options=['请选择','北京','上海'].map((text,index)=>Object.assign(new Element('option',{text}),{value:String(index),selected:index===0}));
Object.defineProperty(city,'value',{get(){return city.options.find(el=>el.selected)?.value||'';}});
city.children=city.options;
const card=new Element('section',{id:'record'}); card.children=[name];
const elements=[name,checkbox,gender,notes,city,card];
const document={
  title:'匿名招聘测试页',body:new Element('body'),activeElement:null,readyState:'complete',
  querySelectorAll(selector){return elements.filter(el=>matches(el,selector));}
};
document.activeElement=document.body;
const window={};
const sandbox=vm.createContext({document,window,location:{href:'https://fixture.example.test/form'},
  Element,HTMLInputElement,HTMLTextAreaElement,Event,KeyboardEvent:Event,MouseEvent:Event,
  getComputedStyle:()=>({visibility:'visible'})});

for await (const line of readline.createInterface({input:process.stdin})) {
  try {
    const value=vm.runInContext(JSON.parse(line).script,sandbox,{timeout:1500});
    process.stdout.write(JSON.stringify({result:value})+'\n');
  } catch {
    process.stdout.write(JSON.stringify({error:'fixture-script-failed'})+'\n');
  }
}
