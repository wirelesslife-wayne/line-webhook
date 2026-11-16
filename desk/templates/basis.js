const el=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,body){
 const r=await fetch('/desk/api/'+url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});
 let d={};try{d=await r.json()}catch(e){}if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}
 if(!r.ok)throw Error(d.error||'讀取失敗，請稍後再試');return d;
}
function say(t,bad){const f=el('feedback');f.textContent=t||'';f.classList.toggle('bad',!!bad);if(t&&!bad)setTimeout(()=>{if(f.textContent===t)f.textContent=''},5000)}
// 我的回覆規則: newest active first; stopped ones only when asked; 停用 asks once and says what happens
let RULES=[],rulesView='top',asking=null;
function ruleCard(r){
 const ask=asking&&asking.kind==='rule'&&asking.id===r.id;
 return `<div class="item${r.active?'':' off'}"><div class="rowx"><div><p>${esc(r.text)}${r.active?'':'<span class="tag">已停用</span>'}</p></div>${r.active?`<button data-rule-ask="${r.id}">停用</button>`:`<button data-rule="${r.id}" data-on="1">重新啟用</button>`}</div>${ask?`<div class="confirm">停用後，AI 不再照這條規則寫。規則不會刪掉，之後可以重新啟用。<div class="actions"><button data-cancel>取消</button><button class="warn" data-rule="${r.id}" data-on="0">確認停用</button></div></div>`:''}</div>`}
function drawRules(){
 const on=RULES.filter(r=>r.active),off=RULES.filter(r=>!r.active);
 el('n-rules').textContent=on.length;el('rules-sub').textContent=`${on.length} 條生效中，每一則草稿都會照做`;
 const shown=rulesView==='off'?off:rulesView==='all'?on:on.slice(0,5);
 el('rules-list').innerHTML=shown.map(ruleCard).join('')||`<p class="muted">${rulesView==='off'?'沒有停用的規則。':'還沒有規則。'}</p>`;
 el('rules-all').textContent=rulesView==='all'?'收合':`顯示全部 ${on.length} 條 ›`;el('rules-all').hidden=rulesView!=='off'&&on.length<=5;
 el('rules-off').textContent=rulesView==='off'?'回到生效中的規則':`已停用 ${off.length} 條 ›`;el('rules-off').hidden=!off.length&&rulesView!=='off';
}
async function loadRules(){RULES=(await api('basis/rules')).items;drawRules()}
// 過往核准的回覆
let lq='',lpage=0,loff=false,LEARNED=[],lseq=0;
function exCard(x){
 const ask=asking&&asking.kind==='ex'&&asking.id===x.id;
 return `<div class="item${loff?' off':''}"><div class="rowx"><div><p class="q">客人問：${esc(x.question)}<span class="tag">${esc(x.source)}</span></p><p>${esc(x.answer)}</p></div>${loff?`<button data-ex="${x.id}" data-on="1">重新啟用</button>`:`<button data-ex-ask="${x.id}">停用</button>`}</div>${ask?`<div class="confirm">停用後，AI 不再參考這筆回覆。資料不會刪掉，之後可以重新啟用。<div class="actions"><button data-cancel>取消</button><button class="warn" data-ex="${x.id}" data-on="0">確認停用</button></div></div>`:''}</div>`}
async function loadLearned(){
 const v=++lseq;const d=await api(`basis/learned?q=${encodeURIComponent(lq)}&page=${lpage}${loff?'&stopped=1':''}`);if(v!==lseq)return;
 LEARNED=d.items;el('n-learned').textContent=d.active;
 el('learned-sub').textContent=loff?`已停用 ${d.stopped} 筆，AI 不會參考這些`:`${d.active} 筆，來自你核准或改過的回覆。AI 拿來參考說法，不會照抄`;
 el('learned-list').innerHTML=LEARNED.map(exCard).join('')||`<p class="muted">${lq?'找不到符合的回覆。':loff?'沒有停用的回覆。':'還沒有。'}</p>`;
 el('learned-prev').disabled=lpage===0;el('learned-next').disabled=!d.has_more;
 el('learned-off').textContent=loff?'回到使用中的':`看已停用的（${d.stopped}）`;
}
function redrawLearned(){el('learned-list').innerHTML=LEARNED.map(exCard).join('')||'<p class="muted">找不到符合的回覆。</p>'}
// 公司正式資料: read only; one open at a time
let openDoc='';
async function loadDocs(){
 const d=await api('basis/docs');el('n-docs').textContent=d.items.length;
 el('docs-list').innerHTML=d.items.map(x=>`<div class="li"><b>${esc(x.title)}<small>${esc(x.size)}${x.updated?'・'+esc(x.updated)+' 更新':''}</small></b><button data-doc="${x.key}" aria-expanded="false">查看</button></div><div class="doc" id="doc-${x.key}" hidden></div>`).join('');
}
function drawDoc(key,d){
 const box=el('doc-'+key);
 if(d.kind==='table')box.innerHTML=`<table><tr>${d.head.map(h=>`<th>${esc(h)}</th>`).join('')}</tr>${d.rows.map(r=>`<tr>${r.map(c=>`<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table>`;
 else if(d.kind==='list')box.innerHTML=d.items.map(i=>`<details><summary>${esc(i.title)}</summary><pre>${esc(i.text)}</pre></details>`).join('');
 else if(d.kind==='text')box.innerHTML=`<pre>${esc(d.text)}</pre>`;
 else box.innerHTML=`<input class="search" id="price-q" type="search" maxlength="40" placeholder="輸入車款，可加年份，如：CX-30 2021" aria-label="查車款價格"><div id="price-out">${d.lines&&d.lines.length?d.lines.map(l=>`<p>${esc(l)}</p>`).join(''):'<p class="muted">輸入車款查官網的完工價。</p>'}</div>`;
}
let pt=null;
document.addEventListener('input',e=>{
 if(e.target.id==='learned-q'){clearTimeout(pt);pt=setTimeout(()=>{lq=e.target.value.trim();lpage=0;loadLearned().catch(x=>say(x.message,true))},350)}
 if(e.target.id==='price-q'){clearTimeout(pt);const q=e.target.value.trim();pt=setTimeout(async()=>{try{const d=await api('basis/doc?key=price&q='+encodeURIComponent(q));el('price-out').innerHTML=d.lines.length?d.lines.map(l=>`<p>${esc(l)}</p>`).join(''):`<p class="muted">${q?'找不到這個車款，試試只打車型，例如「CX-30」。':'輸入車款查官網的完工價。'}</p>`}catch(x){say(x.message,true)}},350)}
});
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 try{
  if(b.id==='rule-add-open'){el('rule-add').hidden=false;el('rule-text').focus();return}
  if(b.id==='rule-add-cancel'){el('rule-add').hidden=true;el('rule-text').value='';return}
  if(b.id==='rule-add-save'){const t=el('rule-text').value.trim();if(!t){say('請先寫一句規則。',true);return}b.disabled=true;try{await api('basis/rules',{text:t})}finally{b.disabled=false}el('rule-text').value='';el('rule-add').hidden=true;rulesView='top';await loadRules();say('已加入，之後的草稿都會照這條寫。');return}
  if(b.id==='rules-all'){rulesView=rulesView==='all'?'top':'all';drawRules();return}
  if(b.id==='rules-off'){rulesView=rulesView==='off'?'top':'off';asking=null;drawRules();return}
  if(b.dataset.ruleAsk){asking={kind:'rule',id:Number(b.dataset.ruleAsk)};drawRules();return}
  if(b.dataset.exAsk){asking={kind:'ex',id:b.dataset.exAsk};redrawLearned();return}
  if(b.hasAttribute('data-cancel')){asking=null;drawRules();redrawLearned();return}
  if(b.dataset.rule){const on=b.dataset.on==='1';b.disabled=true;await api('fb/rules/'+b.dataset.rule,{active:on});asking=null;await loadRules();say(on?'已重新啟用。':'已停用，AI 不再照這條寫；在「已停用」裡可以重新啟用。');return}
  if(b.dataset.ex){const on=b.dataset.on==='1';b.disabled=true;await api('basis/learned',{id:b.dataset.ex,active:on});asking=null;await loadLearned();say(on?'已重新啟用。':'已停用，AI 不再參考這筆；在「看已停用的」裡可以重新啟用。');return}
  if(b.id==='learned-prev'){lpage=Math.max(0,lpage-1);asking=null;await loadLearned();return}
  if(b.id==='learned-next'){lpage++;asking=null;await loadLearned();return}
  if(b.id==='learned-off'){loff=!loff;lpage=0;asking=null;await loadLearned();return}
  if(b.dataset.doc){const k=b.dataset.doc,box=el('doc-'+k),open=box.hidden;
   document.querySelectorAll('.kb .doc').forEach(x=>x.hidden=true);document.querySelectorAll('[data-doc]').forEach(x=>{x.textContent='查看';x.setAttribute('aria-expanded','false')});
   if(open){box.hidden=false;b.textContent='收合 ▴';b.setAttribute('aria-expanded','true');if(!box.dataset.loaded){box.innerHTML='<p class="muted">讀取中…</p>';drawDoc(k,await api('basis/doc?key='+k));box.dataset.loaded='1'}}
   return}
 }catch(x){say(x.message,true);b.disabled=false}
});
Promise.all([loadRules(),loadLearned(),loadDocs()]).catch(x=>say(x.message,true));
