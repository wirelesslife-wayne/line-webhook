const el=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let status='pending',page=0,version=0,busy=false;
async function api(url,body){
 const r=await fetch('/desk/api/'+url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});
 const d=await r.json();if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}
 if(!r.ok)throw Error(d.error||'讀取失敗，請稍後再試');return d;
}
async function load(){
 const v=++version;
 try{const d=await api(`knowledge?status=${status}&page=${page}`);if(v!==version)return;
 el('prev').disabled=page===0;el('next').disabled=!d.has_more;
 el('knowledge-list').innerHTML=d.items.map(r=>`<form class="card knowledge-item" data-id="${r.id}" data-version="${r.version}"><h2>${esc(r.summary)}</h2><p class="muted">${esc(r.submitter)} 提交 · ${esc(r.created_at)}${r.reviewer?`<br>${esc(r.reviewer)} 審核 · ${esc(r.reviewed_at)}`:''}</p>${[['question','客人問的',5000],['answer','我們的回答',5000],['summary','一句話重點',300],['scope','適用車款／產品（選填）',200]].map(([key,label,max])=>`<label for="k-${r.id}-${key}">${label}</label>${['question','answer'].includes(key)?`<textarea id="k-${r.id}-${key}" name="${key}" maxlength="${max}" ${status==='pending'?'':'readonly'} required>${esc(r[key])}</textarea>`:`<input id="k-${r.id}-${key}" name="${key}" type="text" maxlength="${max}" ${key==='summary'?'required':''} ${status==='pending'?'':'readonly'} value="${esc(r[key])}">`}`).join('')}<div class="actions">${status==='pending'?'<button type="submit" class="primary">核准</button><button type="button" data-action="reject">退回</button>':status==='approved'?'<button type="button" data-action="withdraw">撤回核准</button>':''}</div></form>`).join('')||'<p class="card muted">目前沒有這類知識。</p>';
 }catch(e){if(v===version)el('feedback').textContent=e.message}
}
async function review(form,action){
 if(busy)return;busy=true;
 const payload=Object.fromEntries(new FormData(form));payload.action=action;payload.version=Number(form.dataset.version);
 form.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{await api(`knowledge/${form.dataset.id}/review`,payload);await load();el('feedback').textContent={approve:'已核准，之後新擬稿會使用。',reject:'已退回，不會提供給 AI。',withdraw:'已撤回，之後新擬稿不再使用。'}[action]}
 catch(e){el('feedback').textContent=e.message;form.querySelectorAll('button').forEach(b=>b.disabled=false)}finally{busy=false}
}
document.addEventListener('submit',e=>{if(e.target.matches('.knowledge-item')){e.preventDefault();void review(e.target,'approve')}});
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||busy)return;
 if(b.dataset.status){status=b.dataset.status;page=0;document.querySelectorAll('[data-status]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));void load()}
 else if(b.id==='prev'||b.id==='next'){page=Math.max(0,page+(b.id==='prev'?-1:1));void load()}
 else if(b.dataset.action)void review(b.closest('form'),b.dataset.action);
});
load();
