const el=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,body){
 const r=await fetch('/desk/api/'+url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});
 let d={};try{d=await r.json()}catch(e){}if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}
 if(!r.ok)throw Error(d.error||'讀取失敗，請稍後再試');return d;
}
function say(t,bad){const f=el('feedback');f.textContent=t||'';f.classList.toggle('bad',!!bad);if(t&&!bad)setTimeout(()=>{if(f.textContent===t)f.textContent=''},4000)}
// 一次只看一題（ChatGPT／Gemini 11/18：10 題全展開太長）：點一個說法 →「確定，下一題」；都不對 →「我自己寫」；不急 →「之後再說」
let tab='open',page=0,ITEMS=[],picked={},writing={};
function src(o){const b=[];if(o.latest)b.push('最近 '+o.latest);if(o.replies)b.push(`過往 ${o.replies} 則回覆`);if(o.named.length)b.push(o.named.join('、'));return b.join('・')}
function card(x){
 if(tab==='decided'){
  const said=x.choice>=0&&x.options[x.choice]?x.options[x.choice].say:'';
  return `<div class="q"><span class="grp">${esc(x.group)}</span><h3>${esc(x.topic)}</h3>${said?`<div class="chosen">${esc(said)}</div>`:''}${x.answer?`<div class="chosen">${x.choice>=0?'補充：':''}${esc(x.answer)}</div>`:''}<p class="meta">${esc(x.decided)} 決定</p><div class="row"><button data-reopen="${x.id}">重新決定</button></div></div>`}
 const p=picked[x.id],w=writing[x.id];
 return `<div class="q"><span class="grp">${esc(x.group)}</span><h3>${esc(x.topic)}</h3>${x.options.map((o,i)=>`<button class="opt" data-pick="${x.id}" data-i="${i}" aria-pressed="${p===i}">${esc(o.say)}<small>${esc(src(o))}</small></button>`).join('')}${x.suggest?`<p class="ai">AI 的看法：${esc(x.suggest)}</p>`:''}${w?`<div class="own"><textarea id="own-${x.id}" maxlength="500" placeholder="寫現在正確的說法，例如：刷卡與現金同價，分期另計">${esc(w.text||'')}</textarea></div>`:''}<div class="row">${w?`<button class="primary" data-own-save="${x.id}">儲存我寫的</button><button data-own-cancel="${x.id}">取消</button>`:`<button class="primary" data-ok="${x.id}">確定，下一題</button><button data-own="${x.id}">都不對，我自己寫</button>${tab==='open'?`<button data-later="${x.id}">之後再說</button>`:''}`}</div></div>`}
function draw(d){
 const total=d.open+d.later+d.decided;
 el('n-open').textContent=d.open;el('n-later').textContent=d.later;el('n-decided').textContent=d.decided;
 el('bar').style.width=total?Math.round(d.decided*100/total)+'%':'0';
 el('prog').textContent=`已決定 ${d.decided}／共 ${total} 題`;
 const shown=tab==='decided'?ITEMS:ITEMS.slice(0,1);
 el('list').innerHTML=shown.map(card).join('')||`<div class="done">${tab==='open'?(d.later?'待決定的都做完了 👍 還有「之後再說」的題目。':'全部決定完了 👍'):tab==='later'?'沒有「之後再說」的題目。':'還沒有決定的題目。'}</div>`;
 el('prev').hidden=page===0;el('next').hidden=!d.has_more;el('prev').parentElement.hidden=tab!=='decided'||(page===0&&!d.has_more);
}
let LAST=null;
async function load(){const d=await api(`conflicts?tab=${tab}&page=${page}`);ITEMS=d.items;LAST=d;draw(d)}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b||b.closest('header'))return;
 try{
  if(b.dataset.tab){tab=b.dataset.tab;page=0;picked={};writing={};document.querySelectorAll('[data-tab]').forEach(t=>t.setAttribute('aria-pressed',t===b));await load();return}
  if(b.dataset.pick){picked[b.dataset.pick]=Number(b.dataset.i);draw(LAST);return}
  if(b.dataset.own){writing[b.dataset.own]={};draw(LAST);el('own-'+b.dataset.own).focus();return}
  if(b.dataset.ownCancel){delete writing[b.dataset.ownCancel];draw(LAST);return}
  const id=Number(b.dataset.ok||b.dataset.ownSave||b.dataset.later||b.dataset.reopen);if(!id)return;
  b.disabled=true;
  if(b.dataset.ok){if(picked[id]===undefined){b.disabled=false;say('請先點選上面其中一個說法。',true);return}await api('conflicts/decide',{id,choice:picked[id]});say('已記下，AI 之後照這個說法寫。')}
  else if(b.dataset.ownSave){const t=el('own-'+id).value.trim();if(t.length<2){b.disabled=false;say('請寫正確的說法。',true);return}await api('conflicts/decide',{id,choice:-1,answer:t});say('已記下你寫的說法。')}
  else if(b.dataset.later){await api('conflicts/decide',{id,choice:null});say('先放到「之後再說」。')}
  else{await api('conflicts/reopen',{id});say('已放回「待決定」。')}
  delete picked[id];delete writing[id];await load();if(tab!=='decided')scrollTo(0,0);
 }catch(x){say(x.message,true);b.disabled=false}
});
document.addEventListener('input',e=>{const m=e.target.id.match(/^own-(\d+)$/);if(m&&writing[m[1]])writing[m[1]].text=e.target.value});
el('prev').onclick=async()=>{page=Math.max(0,page-1);await load().catch(x=>say(x.message,true));scrollTo(0,0)};
el('next').onclick=async()=>{page++;await load().catch(x=>say(x.message,true));scrollTo(0,0)};
load().catch(x=>say(x.message,true));
