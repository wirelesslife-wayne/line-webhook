const el=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,body){
 const r=await fetch('/desk/api/'+url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});
 let d={};try{d=await r.json()}catch(e){}if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}
 if(!r.ok){const e=Error(d.error||'讀取失敗，請稍後再試');e.status=r.status;throw e}return d;
}
function say(t,bad){const f=el('feedback');f.textContent=t||'';f.classList.toggle('bad',!!bad);if(t&&!bad)setTimeout(()=>{if(f.textContent===t)f.textContent=''},4000)}
// 一次只看一題（ChatGPT／Gemini 11/18：10 題全展開太長）：點一個說法 →「確定，下一題」；都不對 →「我自己寫」；不急 →「之後再說」
// 門市、電話、價格、工時的題目（Wayne 11/18 選 A）：上面並排正式資料；選了跟正式資料不同的說法，先問要不要蓋過正式資料
let tab='open',page=0,product='',ITEMS=[],picked={},writing={},confirming={};
function src(o){const b=[];if(o.latest)b.push('最近 '+o.latest);if(o.replies)b.push(`過往 ${o.replies} 則回覆`);if(o.named.length)b.push(o.named.join('、'));return b.join('・')}
function storeNames(x){return x.kind==='dealer'&&x.official.length?x.official[0].text.split('\n').map(l=>l.split('｜')[0]).filter(Boolean):[]}
function choices(x){const n=storeNames(x).join('、');
 return x.kind==='dealer'&&n?[['add','加進正式清單',`${n} 也照列，這家一起介紹；會通知 Claude 更新清單`,'warnbtn'],['replace','取代正式清單',`只介紹這句的門市，${n} 不再列；會通知 Claude 更新清單`,'warnbtn'],['0','不採用這句的門市和電話',`客服照正式清單介紹 ${n}`,'']]
  :[['replace','改用這個，蓋過正式資料','擬稿照這句的金額寫；會通知 Claude 更新正式資料','warnbtn'],['0','不採用這句的金額',x.kind==='hours'?'工時照正式工時表寫':'價格照正式資料寫，這句只當補充說法','']]}
function result(x,scope){const n=storeNames(x).join('、');
 if(scope==='add')return `已決定：這家加進門市清單，${n} 也照列；已通知 Claude 更新正式清單。`;
 if(scope==='replace')return x.kind==='dealer'?`已決定：只介紹這句的門市，${n} 不再列；已通知 Claude 更新正式清單。`:'已決定：擬稿改用這句的金額；已通知 Claude 更新正式資料。';
 if(scope==='official')return x.kind==='dealer'?`已決定：客服仍照正式清單介紹 ${n}，這句的門市和電話不採用。`:'已決定：價格、工時仍照正式資料，這句只當補充說法。';
 return ''}
function official(x){return x.official&&x.official.length?`<div class="off"><b>📋 正式資料目前寫的</b>${x.official.map(p=>`<small>${esc(p.title)}</small><pre>${esc(p.text)}</pre>`).join('')}</div>`:''}
function card(x){
 if(tab==='decided'){
  const said=x.choice>=0&&x.options[x.choice]?x.options[x.choice].say:'';
  return `<div class="q"><span class="grp">${esc(x.group)}</span><h3>${esc(x.topic)}</h3>${said?`<div class="chosen">${esc(said)}</div>`:''}${x.answer?`<div class="chosen">${x.choice>=0?'補充：':''}${esc(x.answer)}</div>`:''}<p class="meta">${esc(x.decided)} 決定</p>${x.scope?`<p class="meta eff">✅ ${esc(result(x,x.scope).replace('已決定：',''))}</p>`:''}<div class="row"><button data-reopen="${x.id}">重新決定</button></div></div>`}
 const p=picked[x.id],w=writing[x.id],c=confirming[x.id];
 const actions=c?`<div class="confirm" role="alert"><p>⚠ ${esc(c.msg)}</p>${choices(x).map(([v,t,sub,cls])=>`<button class="${cls}" data-ov="${x.id}" data-v="${v}">${esc(t)}<small>${esc(sub)}</small></button>`).join('')}<button data-ov-cancel="${x.id}">取消，重新選</button></div>`
  :`<div class="row">${w?`<button class="primary" data-own-save="${x.id}">儲存我寫的</button><button data-own-cancel="${x.id}">取消</button>`:`<button class="primary" data-ok="${x.id}">確定，下一題</button><button data-own="${x.id}">都不對，我自己寫</button>${tab==='open'?`<button data-later="${x.id}">之後再說</button>`:''}`}</div>`;
 return `<div class="q"><span class="grp">${esc(x.group)}</span><h3>${esc(x.topic)}</h3>${official(x)}${x.options.map((o,i)=>`<button class="opt" data-pick="${x.id}" data-i="${i}" aria-pressed="${p===i}">${esc(o.say)}<small>${esc(src(o))}</small>${o.warn&&o.warn.length?`<small class="warn">⚠ 跟正式資料不同：${esc(o.warn.join('；'))}</small>`:''}</button>`).join('')}${x.suggest&&!(x.official&&x.official.length)?`<p class="ai">AI 的看法：${esc(x.suggest)}</p>`:''}${w&&!c?`<div class="own"><textarea id="own-${x.id}" maxlength="500" placeholder="寫現在正確的說法，例如：刷卡與現金同價，分期另計">${esc(w.text||'')}</textarea></div>`:''}${actions}</div>`}
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
// 11/19 第二批（Gemini）：可以只看某個產品的題目，一次處理同一類
function products(d){const s=el('product');const all=(d.products||[]).reduce((a,p)=>a+p.n,0);
 s.innerHTML=`<option value="">全部產品（${all}）</option>`+(d.products||[]).map(p=>`<option value="${esc(p.name)}"${p.name===product?' selected':''}>${esc(p.name)}（${p.n}）</option>`).join('');
 if(product&&!(d.products||[]).some(p=>p.name===product)){product='';s.value=''}}
async function load(){const d=await api(`conflicts?tab=${tab}&page=${page}&product=${encodeURIComponent(product)}`);ITEMS=d.items;LAST=d;products(d);draw(d)}
async function decide(id,body,done){
 try{const out=await api('conflicts/decide',Object.assign({id},body));const x=ITEMS.find(i=>i.id===id);say(out.scope&&x?result(x,out.scope):done)}
 catch(x){if(x.status!==409)throw x;confirming[id]=Object.assign({msg:x.message},body);draw(LAST);return false}
 return true;
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b||b.closest('header'))return;
 try{
  if(b.dataset.tab){tab=b.dataset.tab;page=0;picked={};writing={};confirming={};document.querySelectorAll('[data-tab]').forEach(t=>t.setAttribute('aria-pressed',t===b));await load();return}
  if(b.dataset.pick){if(confirming[b.dataset.pick])return;picked[b.dataset.pick]=Number(b.dataset.i);draw(LAST);return}
  if(b.dataset.own){writing[b.dataset.own]={};draw(LAST);el('own-'+b.dataset.own).focus();return}
  if(b.dataset.ownCancel){delete writing[b.dataset.ownCancel];draw(LAST);return}
  if(b.dataset.ovCancel){delete confirming[b.dataset.ovCancel];draw(LAST);return}
  const id=Number(b.dataset.ok||b.dataset.ownSave||b.dataset.later||b.dataset.reopen||b.dataset.ov);if(!id)return;
  b.disabled=true;
  if(b.dataset.ov){const c=confirming[id];const {msg,...body}=c;if(!await decide(id,Object.assign(body,{override:b.dataset.v==='0'?false:b.dataset.v}),'已記下。'))return}
  else if(b.dataset.ok){if(picked[id]===undefined){b.disabled=false;say('請先點選上面其中一個說法。',true);return}if(!await decide(id,{choice:picked[id]},'已記下，AI 之後照這個說法寫。'))return}
  else if(b.dataset.ownSave){const t=el('own-'+id).value.trim();if(t.length<2){b.disabled=false;say('請寫正確的說法。',true);return}if(!await decide(id,{choice:-1,answer:t},'已記下你寫的說法。'))return}
  else if(b.dataset.later){await api('conflicts/decide',{id,choice:null});say('先放到「之後再說」。')}
  else{await api('conflicts/reopen',{id});say('已放回「待決定」。')}
  delete picked[id];delete writing[id];delete confirming[id];await load();if(tab!=='decided')scrollTo(0,0);
 }catch(x){say(x.message,true);b.disabled=false}
});
document.addEventListener('input',e=>{const m=e.target.id.match(/^own-(\d+)$/);if(m&&writing[m[1]])writing[m[1]].text=e.target.value});
el('prev').onclick=async()=>{page=Math.max(0,page-1);await load().catch(x=>say(x.message,true));scrollTo(0,0)};
el('next').onclick=async()=>{page++;await load().catch(x=>say(x.message,true));scrollTo(0,0)};
el('product').onchange=async e=>{product=e.target.value;page=0;picked={};writing={};confirming={};await load().catch(x=>say(x.message,true))};
load().catch(x=>say(x.message,true));
