let tab='todo',page=0,selected=null,current=null,sending=false;
let learnTarget=null,learning=false;
let selectionVersion=0,listVersion=0,confirmation=null,editorBase='',retrySnapshot=null;
const el=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusText={waiting:'等待客人停止輸入',drafting:'擬稿中',ready:'草稿就緒',blocked:'可由同事填寫',error:'請重新擬稿',skip:'選單／貼圖',done:'已處理'};
async function api(url,body){
 const r=await fetch('/desk/api/'+url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});
 const data=await r.json();if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}
 if(!r.ok){const e=Error(data.error||'連線失敗，請稍後再試');e.status=r.status;throw e}return data;
}
function feedback(s=''){el('feedback').textContent=s}
function actionFeedback(s='',error=false){const n=el('action-feedback');if(n){n.textContent=s;n.className=error?'error':'note';n.hidden=!s}}
function matches(uid,v){return selected===uid&&selectionVersion===v}
function imageMarkup(i){return `<a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer"><img src="${esc(i.url)}" alt="${esc(i.name)}" loading="lazy" referrerpolicy="no-referrer"><span>${esc(i.name)}</span></a>`}
async function loadList(){
 const v=++listVersion,t=tab,p=page;
 try{const d=await api(`list?tab=${t}&page=${p}`);if(v!==listVersion||t!==tab||p!==page)return null;
 for(const [id,value] of [['price',d.price_warning],['worker',d.worker_warning],['ai-budget',d.ai_budget?.warning]]){el(id).hidden=!value;el(id).textContent=value||''}
 el('ai-budget').className=d.ai_budget?.blocked?'error':'note';
 el('prev').disabled=p===0;el('next').disabled=!d.has_more;
 el('list').innerHTML=d.items.length?d.items.map(i=>`<button class="${i.urgent?'urgent ':''}${i.user_id===selected?'selected':''}" data-uid="${esc(i.user_id)}"><b>${esc(i.name)}</b><span class="${i.minutes>30&&t==='todo'?'late':'muted'}">${t==='todo'?`等待 ${i.minutes} 分鐘`:'最後訊息 '+esc(i.last.slice(5,16))}</span><span><span class="chip">${statusText[i.status]||esc(i.status)}</span> ${i.urgent?'<span class="late">客訴優先</span>':''}</span></button>`).join(''):'<p class="muted">目前沒有這類對話。</p>';
 return d;
 }catch(e){if(v===listVersion)feedback(e.message);return null}
}
function chosenImages(){return current?.pending?.images||[...document.querySelectorAll('[name=image]:checked')].map(x=>x.value)}
function render(d,preserve=null){
 current=d;const r=d.result||{},pending=d.pending;
 const text=preserve?.text??pending?.text??(d.stale_draft?'':r.draft??'');
 editorBase=text;
 el('detail').innerHTML=`<button class="back" id="back">← 對話清單</button><h2>${esc(d.name)}</h2><p class="muted">${statusText[d.status]||esc(d.status)} · 時間為台灣時間</p><button id="refresh-detail" class="note" hidden>有新訊息，按此重新載入</button><div class="messages">${d.messages.map(m=>`<div class="message ${m.role==='assistant'?'ours':''}"><small>${m.role==='assistant'?'我方':'客人'} · ${esc(m.time)}</small>${esc(m.text)}${(m.images||[]).map(u=>`<div><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">查看已送圖片</a></div>`).join('')}</div>`).join('')||'<p class="muted">尚無對話內容</p>'}</div>${d.stale_draft?'<p class="note">草稿是舊的，客人有新訊息；請看完訊息並重新擬稿。</p>':''}${pending?`<p class="note">${esc(pending.staff)} 的傳送結果待確認。${pending.can_retry?'可用原確認框重試。':'請由原傳送同事重試，或到 LINE 後台查核後標記已回覆。'}</p>`:''}${r.staff_note?`<p class="note">${esc(r.staff_note)}</p>`:''}${(r.flags||[]).map(f=>`<p class="error">${esc(f)}</p>`).join('')}<label for="draft">回覆草稿（可修改）</label><textarea id="draft" maxlength="5000" placeholder="在這裡填寫給客人的回覆">${esc(text)}</textarea><details><summary>可附的商品圖片（最多 4 張）</summary><div class="images">${d.images.map(i=>`<label><input type="checkbox" name="image" value="${esc(i.url)}">${imageMarkup(i)}</label>`).join('')||'<p class="muted">沒有可附圖片</p>'}</div></details><div class="actions"><button class="primary" id="open-confirm">檢查並送出</button><button id="redraft">重新擬稿</button><button id="learn-open">存成知識</button><button id="done">已在 LINE 後台回覆（不送）</button></div><div id="action-feedback" role="status" hidden></div>`;
 if(pending)el('draft').readOnly=true;
 document.querySelectorAll('[name=image]').forEach(x=>{x.checked=(preserve?.images??pending?.images??[]).includes(x.value);x.disabled=!!pending});
 el('open-confirm').disabled=!!pending&&!pending.can_retry;
 el('redraft').disabled=!!pending||['done','skip','drafting'].includes(d.status);
 if(d.latest_source>d.source_upto)el('refresh-detail').hidden=false;
}
async function select(uid,force=false,preserve=null){
 if(sending)return;
 if(!force&&el('draft')&&el('draft').value!==editorBase&&!window.confirm('切換對話會放棄尚未送出的修改，確定切換？'))return;
 const v=++selectionVersion;selected=uid;current=null;retrySnapshot=null;
 // Hide previous controls immediately; old replies cannot act on a new recipient.
 el('detail').innerHTML='<button class="back" id="back">← 對話清單</button><p>讀取中…</p>';
 el('layout').classList.add('detail-open');
 try{const d=await api('conversation?user_id='+encodeURIComponent(uid));if(!matches(uid,v)||d.user_id!==uid)return;
 render(d,preserve);void loadList();
 }catch(e){if(matches(uid,v))feedback(e.message)}
}
async function refreshDetail(uid,v,preserve){
 const d=await api('conversation?user_id='+encodeURIComponent(uid));
 if(matches(uid,v)&&d.user_id===uid)render(d,preserve);
}
async function poll(){
 await loadList();const uid=selected,v=selectionVersion;if(!uid||!current||sending)return;
 try{const d=await api('conversation?user_id='+encodeURIComponent(uid));if(!matches(uid,v))return;
 if(d.latest_source!==current.latest_source||d.source_upto!==current.source_upto||d.status!==current.status||JSON.stringify(d.result)!==JSON.stringify(current.result)||JSON.stringify(d.pending)!==JSON.stringify(current.pending)){
  el('refresh-detail').hidden=false;
  el('refresh-detail').textContent=d.latest_source!==current.latest_source?'有新訊息，按此重新載入':'對話或草稿已更新，按此重新載入';
 }
 }catch(e){if(matches(uid,v))actionFeedback(e.message,true)}
}
function back(){++selectionVersion;selected=null;current=null;retrySnapshot=null;el('layout').classList.remove('detail-open')}
async function advance(uid,v,message){
 if(!matches(uid,v))return;
 feedback(message);
 tab='todo';page=0;document.querySelectorAll('[data-tab]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.tab==='todo')));
 if(matchMedia('(max-width:700px)').matches){back();void loadList();return}
 const d=await loadList();if(!matches(uid,v))return;
 const next=d?.items.find(i=>i.user_id!==uid);
 if(next){await select(next.user_id,true)}else{back();el('detail').innerHTML='<p class="muted">本頁已處理完畢，請查看下一頁或其他分類。</p>'}
}
function snapshot(){
 const pending=current.pending;
 if(retrySnapshot)return retrySnapshot;
 return Object.freeze({uid:selected,name:current.name,text:el('draft').value,images:Object.freeze([...chosenImages()]),source_upto:pending?.source_upto??current.source_upto,retry_key:pending?.retry_key??crypto.randomUUID(),version:selectionVersion});
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 try{
 if(b.id==='learn-cancel'&&!learning){el('learn-dialog').close();learnTarget=null;return}
 if(b.id==='learn-open'&&current){
  const uid=selected,v=selectionVersion,draft=el('draft').value;b.disabled=true;
  try{const d=await api('knowledge/prefill?user_id='+encodeURIComponent(uid));if(!matches(uid,v))return;
   learnTarget=uid;el('learn-form').reset();el('learn-question').value=d.question;el('learn-answer').value=d.answer||draft;
   el('learn-feedback').hidden=true;el('learn-dialog').showModal();
  }finally{b.disabled=false}return;
 }
 if(b.dataset.tab){tab=b.dataset.tab;page=0;document.querySelectorAll('[data-tab]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));back();await loadList();return}
 if(b.dataset.uid){await select(b.dataset.uid);return}
 if(b.id==='prev'||b.id==='next'){page=Math.max(0,page+(b.id==='prev'?-1:1));await loadList();return}
 if(b.id==='back'){back();return}
 if(b.id==='cancel'){el('confirm').close();confirmation=null;return}
 if(b.id==='refresh-detail'&&current){const uid=selected,v=selectionVersion;await refreshDetail(uid,v,{text:el('draft').value,images:chosenImages()});actionFeedback('訊息已重新載入，編輯內容已保留。');return}
 if(b.id==='open-confirm'&&current){
  confirmation=snapshot();if(!confirmation.text.trim())throw Error('請先填寫回覆內容');if(confirmation.images.length>4)throw Error('最多只能選 4 張圖片');
  el('recipient').textContent='送給 '+confirmation.name;el('preview').textContent=confirmation.text;
  el('preview-images').innerHTML=confirmation.images.map(u=>imageMarkup({url:u,name:current.images.find(i=>i.url===u)?.name||'原確認附件'})).join('');
  el('image-count').textContent=confirmation.images.length?'另附 '+confirmation.images.length+' 張圖片':'只有文字，不附圖片';el('confirm').showModal();return;
 }
 if(b.id==='send'&&confirmation&&!sending){
  const s=confirmation;sending=true;b.disabled=true;el('cancel').disabled=true;
  try{await api('send',{user_id:s.uid,text:s.text,images:s.images,source_upto:s.source_upto,retry_key:s.retry_key,confirmed:true});el('confirm').close();confirmation=null;retrySnapshot=null;sending=false;await advance(s.uid,s.version,'已送出並記錄。')}
  catch(err){el('confirm').close();confirmation=null;sending=false;
   if(matches(s.uid,s.version)){
    // Never replace textarea after an error. Keep the exact retry snapshot for
    // unknown/network failures; definitive failures get a fresh confirmation.
    try{const d=await api('conversation?user_id='+encodeURIComponent(s.uid));if(matches(s.uid,s.version)){
     if(d.latest_source!==current.latest_source||d.source_upto!==current.source_upto)el('refresh-detail').hidden=false;
     current={...current,pending:d.pending};retrySnapshot=d.pending?s:null;
     el('draft').readOnly=!!d.pending;el('open-confirm').disabled=!!d.pending&&!d.pending.can_retry;
     if(err.status===409){await refreshDetail(s.uid,s.version,{text:s.text,images:s.images})}
    }}catch(_){retrySnapshot=s;el('draft').readOnly=true}
    if(matches(s.uid,s.version))actionFeedback(err.message,true);
   }
  }finally{sending=false;b.disabled=false;el('cancel').disabled=false}return;
 }
 if((b.id==='redraft'||b.id==='done')&&current){
  const uid=selected,v=selectionVersion,upto=current.source_upto,saved={text:el('draft').value,images:chosenImages()};b.disabled=true;
  try{await api('action',{action:b.id==='done'?'done':'redraft',user_id:uid,source_upto:upto});
   if(!matches(uid,v))return;
   if(b.id==='done')await advance(uid,v,'已標記已在 LINE 後台回覆。');
   else{await refreshDetail(uid,v,null);if(matches(uid,v))actionFeedback('已安排重新擬稿。')}
  }catch(err){if(matches(uid,v)){if(err.status===409){try{await refreshDetail(uid,v,saved)}catch(_){}}
   if(matches(uid,v))actionFeedback(err.status===409&&b.id==='done'?'客人有新訊息或狀態已更新，請看完再標記。'+err.message:err.message,true)}
  }finally{b.disabled=false}return;
 }
 }catch(e){if(el('action-feedback'))actionFeedback(e.message,true);else feedback(e.message)}
});
document.addEventListener('input',e=>{if(e.target.id==='draft'&&!current?.pending)retrySnapshot=null});
document.addEventListener('change',e=>{if(e.target.name==='image'&&!current?.pending)retrySnapshot=null});
el('confirm').addEventListener('cancel',e=>{if(sending)e.preventDefault();else confirmation=null});
loadList();setInterval(poll,15000);

el('learn-dialog').addEventListener('cancel',e=>{if(learning)e.preventDefault();else learnTarget=null});
el('learn-form').addEventListener('submit',async e=>{
 e.preventDefault();if(learning||!learnTarget)return;learning=true;el('learn-submit').disabled=true;el('learn-cancel').disabled=true;
 try{await api('knowledge',{user_id:learnTarget,question:el('learn-question').value,answer:el('learn-answer').value,summary:el('learn-summary').value,scope:el('learn-scope').value});
  el('learn-dialog').close();learnTarget=null;feedback('已送交 Wayne 審核，核准後 AI 才會使用。');
 }catch(err){el('learn-feedback').hidden=false;el('learn-feedback').textContent=err.message}
 finally{learning=false;el('learn-submit').disabled=false;el('learn-cancel').disabled=false}
});
