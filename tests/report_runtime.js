// Execute the actual offline report script against a minimal DOM, without network access.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const payload=html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1];
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.options=[];this.style={};this._text='';this._value='';this.classList={toggle(){}};}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>typeof c==='string'?c:c.textContent).join('');}
  set value(v){this._value=String(v);}
  get value(){return this._value;}
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=[];this.options=[];this._text='';this._value='';this.append(...values);}
  add(o){this.options.push(o);if(this.options.length===1)this.value=o.value;}
  closest(){return this;}
}
const elements=new Map(),selects=['date','run','country','family','category','chart','release-months'];
function get(id){if(!elements.has(id))elements.set(id,new Element(selects.includes(id)?'select':'div'));return elements.get(id);}
const option=(text,value)=>({text,value});
get('payload').textContent=payload;
for(const id of ['country','family','category'])get(id).add(option('全部',''));
for(const [v,t] of [['','全部'],['free','免费榜'],['grossing','畅销榜']])get('chart').add(option(t,v));
for(let i=6;i>=1;i--)get('release-months').add(option(String(i),String(i)));
const data=JSON.parse(payload),context={
  document:{getElementById:get,createElement:t=>new Element(t),createTextNode:t=>String(t)},
  Option:function(t,v){return option(t,v);},Intl,Date,URLSearchParams,
  location:{hash:'#run='+data.runs.at(-1).run.runId,assign(){throw Error('Unexpected navigation');}}
};
vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
assert.equal(get('rows').children.length,80);assert.equal(get('new-count').textContent,'160');
assert.equal(get('page').textContent,'1 / 2');get('next').onclick();assert.equal(get('page').textContent,'2 / 2');
get('chart').value='free';get('chart').onchange();assert.equal(get('new-count').textContent,'80');
assert.equal(get('page').textContent,'1 / 1');assert(get('rows').textContent.includes('实际跨度 9小时'));
assert(!get('rows').textContent.includes('实际跨度 6小时'));
get('search').value='Synthetic 83';get('search').oninput();assert.equal(get('rows').children.length,1);
get('country').value='de';get('country').onchange();assert.equal(get('rows').children.length,0);
get('reset').onclick();assert.equal(get('new-count').textContent,'160');
get('full').onclick();assert.equal(get('rows').children.length,80);assert(get('release-months').disabled);
assert(get('rows').textContent.includes('完整榜单视图不作新进判断'));
process.stdout.write('pagination, chart/country/search filters, intervals and full view passed\n');
