const f=id=>document.getElementById('fb-'+id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STATUS={waiting:'AI 擬稿中，約 1 分鐘',drafting:'AI 擬稿中，約 1 分鐘',ready:'草稿好了',blocked:'AI 暫停，請直接在框裡寫',error:'擬稿失敗，請按「修改」重試或直接寫',approved:'已核准，等下一輪排程送出',done:'已處理',skip:'不回覆'};
const TODO=['waiting','drafting','ready','blocked','error'];
let tab='todo',channel='',page=0,items=[],edits={},confirmation=null,busy=false,shown='',listVersion=0,polling=false;
async function api(path,body){const r=await fetch('/desk/api/fb/'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json()}catch(e){}if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}if(!r.ok)throw Error(d.error||'暫時無法處理，請重新載入');return d}
function say(text,bad){const e=f('feedback');e.textContent=text||'';e.hidden=!text;e.classList.toggle('bad',!!bad)}
const editKey=d=>d.id+':'+d.version;
function when(t){const d=new Date(t);return /^\d{4}-\d\d-\d\dT/.test(t||'')&&!isNaN(d)?d.toLocaleString('zh-TW',{timeZone:'Asia/Taipei',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}):(t||'')}
function files(list,removable){return (list||[]).map(m=>`<figure>${m.type==='image'?`<img src="/desk/api/fb/media/${esc(m.id)}" alt="" loading="lazy">`:'<div class="vid">🎬</div>'}<span>${esc(m.name)}</span>${removable?`<button data-act="media-remove" data-media="${esc(m.id)}">移除</button>`:''}</figure>`).join('')}
function card(d,i){
  const todo=TODO.includes(d.status),waiting=d.status==='waiting'||d.status==='drafting',r=d.result||{};
  const draft=edits[editKey(d)]??r.draft??'';
  let h=`<article class="card fbcard" data-id="${esc(d.id)}"><h2>${esc(d.name)}<span class="chip">${esc(d.label)}</span></h2><p class="muted">${esc(STATUS[d.status]||d.status)}${d.note?' · '+esc(d.note):''}</p>`;
  if(d.context)h+=`<p class="context"><b>所屬貼文：</b>${esc(d.context.slice(0,200))}</p>`;
  h+=`<div class="messages">${d.messages.map(m=>`<div class="message ${m.role==='assistant'?'ours':''}"><small>${m.role==='assistant'?'我們':'客人'} · ${esc(when(m.time))}</small>${esc(m.text)}</div>`).join('')||'<p class="muted">沒有訊息內容</p>'}</div>`;
  if(/^https:\/\//.test(d.url||''))h+=`<p><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">在 Meta 收件匣打開這則</a></p>`;
  if(todo){
    if(r.staff_note)h+=`<p class="note">AI 的提醒：${esc(r.staff_note)}</p>`;
    h+=(r.flags||[]).map(x=>`<p class="error">${esc(x)}</p>`).join('');
    h+=`<label for="fb-draft-${i}">草稿（可以直接改字）</label><textarea id="fb-draft-${i}" data-draft maxlength="2000" ${waiting?'disabled':''}>${esc(draft)}</textarea>`;
    if(!d.is_public&&canReview){
      const have=new Set((d.media||[]).map(m=>m.name)),sug=(r.cards||[]).filter(c=>!have.has(c.name));
      h+=`<div class="attach"><div class="files">${files(d.media,true)}</div>`;
      if(sug.length)h+=`<div class="suggest"><b>AI 建議附圖</b><div class="files">${sug.map(c=>`<figure><img src="/desk/api/fb/library/${esc(c.id)}" alt="" loading="lazy"><span>${esc(c.name)}</span><button data-act="lib-attach" data-lib="${esc(c.id)}">附上</button></figure>`).join('')}</div></div>`;
      h+=`<div class="actions"><label class="pick"><input type="file" data-file accept="image/jpeg,image/png,video/mp4" hidden>＋ 加圖片／影片</label><button data-act="lib-open">從常用圖片挑</button></div><div class="libgrid" hidden></div><small class="muted">會跟文字一起送出。JPEG／PNG 每張 10MB、MP4 影片 25MB 以內，最多 3 個。</small></div>`;
    }
    h+=`<div class="actions"><button class="primary" data-act="send" ${canReview&&!waiting?'':'disabled'}>發送</button><button data-act="edit" ${waiting?'disabled':''}>修改</button><button data-act="skip" ${canReview?'':'disabled'}>不發送</button></div>`;
    h+=`<div class="editbox" hidden><label for="fb-ins-${i}">要改哪裡？</label><textarea id="fb-ins-${i}" data-instruction maxlength="2000" placeholder="例如：不要問縣市、價格寫 18,000、語氣再輕鬆一點"></textarea>${canReview?'<label class="check"><input type="checkbox" data-remember>以後都照這樣寫（記住這條指示，會影響之後所有草稿）</label>':''}<div class="actions"><button class="primary" data-act="revise">照這樣重寫</button><button data-act="edit-cancel">取消</button></div></div>`;
  }else if(d.status==='approved'){
    const a=d.attempts.find(x=>x.status==='approved');
    h+=`<label>已核准的內容</label><div class="preview">${esc(a?a.text:'')}</div>${(d.media||[]).length?`<div class="attach"><div class="files">${files(d.media,false)}</div></div>`:''}<p class="muted">下一輪排程會照這段文字送出（最慢約 60 分鐘）。</p><div class="actions"><button data-act="cancel" ${canReview?'':'disabled'}>取消核准</button></div>`;
  }else{
    const a=d.attempts.find(x=>x.status==='sent');
    if(a)h+=`<label>已送出的內容（${esc(a.time)}）</label><div class="preview">${esc(a.text)}</div>`;
  }
  return h+'</article>';
}
document.addEventListener('change',async e=>{
  if(!e.target.matches('[data-file]'))return;const p=pick(e.target),file=e.target.files[0];if(!p||!file)return;
  const limit=file.type==='video/mp4'?25e6:10e6;if(!['image/jpeg','image/png','video/mp4'].includes(file.type)){say('只接受 JPEG、PNG 圖片或 MP4 影片。',true);return}
  if(file.size>limit){say(file.type==='video/mp4'?'影片超過 25MB，請改貼 YouTube 連結。':'圖片超過 10MB，請先壓縮。',true);return}
  busy=true;say('上傳中，請稍候…');
  try{const body=new FormData();body.append('thread_id',p.d.id);body.append('file',file);
    const r=await fetch('/desk/api/fb/media',{method:'POST',headers:{'X-CSRF-Token':csrf},body});let d={};try{d=await r.json()}catch(x){}
    if(!r.ok)throw Error(d.error||'上傳失敗，請再試一次');say(`已加入「${file.name}」，會跟回覆一起送出。`)}
  catch(x){say(x.message,true)}
  finally{busy=false}
  await load(true)});
function editing(){const a=document.activeElement;return busy||f('confirm').open||(a&&a.tagName==='TEXTAREA')||!!document.querySelector('.editbox:not([hidden]),.libgrid:not([hidden])')}
let library=null;
async function libraryGrid(box){
  if(!library)library=(await api('library')).cards;
  if(!library.length){box.innerHTML='<p class="muted">常用圖片還沒匯入。</p>';return}
  const groups={};library.forEach(c=>(groups[c.topic||'其他']??=[]).push(c));
  box.innerHTML=Object.entries(groups).map(([t,list])=>`<h3>${esc(t)}</h3><div class="files">${list.map(c=>`<figure title="${esc(c.use_when)}"><img src="/desk/api/fb/library/${esc(c.id)}" alt="" loading="lazy"><span>${esc(c.name)}</span><small class="muted">${esc(c.use_when)}</small><button data-act="lib-attach" data-lib="${esc(c.id)}">附上</button></figure>`).join('')}</div>`).join('');
}
function draw(d){
  items=d.items;f('n-todo').textContent=d.todo?`（${d.todo}）`:'';f('n-approved').textContent=d.approved?`（${d.approved}）`:'';
  f('prev').disabled=page===0;f('next').disabled=!d.has_more;
  const empty={todo:'目前沒有等你決定的訊息。',approved:'目前沒有等待送出的回覆。',done:'還沒有處理過的紀錄。'}[tab];
  f('list').innerHTML=items.length?items.map(card).join(''):`<p class="muted">${empty}</p>`;
}
async function load(force){
  if(!force&&polling)return; // a slow answer must not be discarded by the next timer tick
  const v=++listVersion;polling=true;
  try{const d=await api(`list?channel=${encodeURIComponent(channel)}&tab=${tab}&page=${page}`);if(v!==listVersion)return;const sig=JSON.stringify(d);if(!force&&(sig===shown||editing()))return;shown=sig;draw(d)}
  catch(e){if(v===listVersion&&force)say(e.message,true)}
  finally{if(v===listVersion)polling=false}
}
function remember_rule(id,instruction){const box=f('rules');if(!box)return;const row=document.createElement('div');row.className='rule';row.innerHTML=`<p>${esc(instruction)}</p><small class="muted">剛剛記住</small><button data-rule-id="${Number(id)}" data-active="true">停用</button>`;box.querySelector('.rule')?box.insertBefore(row,box.querySelector('.rule')):box.appendChild(row);box.querySelector('.none')?.remove();box.querySelector('summary').textContent=`我記住的修改指示（${box.querySelectorAll('.rule').length} 條）`}
function pick(button){const el=button.closest('article');const d=items.find(x=>x.id===el?.dataset.id);return d?{d,el}:null}
document.addEventListener('input',e=>{if(e.target.matches('[data-draft]')){const p=pick(e.target);if(p)edits[editKey(p.d)]=e.target.value}});
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b||busy)return;
  try{
    if(b.dataset.fbTab!==undefined){tab=b.dataset.fbTab;page=0;document.querySelectorAll('[data-fb-tab]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));say('');await load(true);return}
    if(b.dataset.fbChannel!==undefined){channel=b.dataset.fbChannel;page=0;document.querySelectorAll('[data-fb-channel]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));await load(true);return}
    if(b.id==='fb-prev'||b.id==='fb-next'){page=Math.max(0,page+(b.id==='fb-prev'?-1:1));await load(true);window.scrollTo(0,0);return}
    if(b.id==='fb-confirm-cancel'){f('confirm').close();confirmation=null;return}
    if(b.id==='fb-confirm-ok'&&confirmation){
      const snap=confirmation;if(snap.is_public&&!f('public-check').checked)return;
      busy=true;b.disabled=true;
      try{await api('send',{thread_id:snap.thread_id,version:snap.version,text:snap.text,retry_key:snap.retry_key,confirmed:true,public_confirmed:snap.is_public,media_ids:snap.media_ids});delete edits[snap.key];say(`已核准給「${snap.name}」的回覆，下一輪排程送出。`)}
      catch(e){say(e.message,true)}
      finally{busy=false;b.disabled=false;f('confirm').close();confirmation=null}
      await load(true);return}
    if(b.dataset.ruleId){const active=b.dataset.active!=='true';b.disabled=true;await api('rules/'+b.dataset.ruleId,{active});b.dataset.active=String(active);b.textContent=active?'停用':'重新啟用';b.closest('.rule').classList.toggle('off',!active);b.disabled=false;return}
    const act=b.dataset.act;if(!act)return;const p=pick(b);if(!p)return;const {d,el}=p;
    if(act==='send'){
      const text=el.querySelector('[data-draft]').value;if(!text.trim()){say('草稿是空的，請先寫內容或按「修改」。',true);return}
      confirmation=Object.freeze({thread_id:d.id,version:d.version,text,name:d.name,is_public:d.is_public,key:editKey(d),retry_key:crypto.randomUUID(),media_ids:(d.media||[]).map(m=>m.id)});
      f('links').hidden=true;f('links').innerHTML='';
      api('links',{text}).then(out=>{if(!confirmation||confirmation.text!==text||!out.links.length)return;const bad=out.links.some(l=>l.state==='broken');
        f('links').innerHTML='<b>連結會開到：</b>'+out.links.map(l=>`<div>${l.state==='broken'?'❌ 打不開':l.state==='ok'?'✅':'⚠️ 沒查到'} ${esc(l.url)}<br><small>→ ${esc(l.title||l.target||'（無法確認）')}</small></div>`).join('');
        f('links').hidden=false;if(bad){f('confirm-ok').disabled=true;f('links').insertAdjacentHTML('beforeend','<p class="error">有連結打不開，請先返回改掉。</p>')}}).catch(()=>{});
      f('attach-note').hidden=!(d.media||[]).length;f('attach-note').textContent=(d.media||[]).length?`另附 ${d.media.length} 個檔案：${d.media.map(m=>m.name).join('、')}`:'';
      f('recipient').textContent=`發送給「${d.name}」（${d.label}）`;f('preview').textContent=text;f('public-warning').hidden=f('public-label').hidden=!d.is_public;f('public-check').checked=false;f('confirm-ok').disabled=d.is_public;f('confirm').showModal();return}
    if(act==='lib-open'){const box=el.querySelector('.libgrid');box.hidden=!box.hidden;b.textContent=box.hidden?'從常用圖片挑':'收起常用圖片';if(!box.hidden&&!box.innerHTML){box.innerHTML='<p class="muted">讀取中…</p>';try{await libraryGrid(box)}catch(e){box.innerHTML='';say(e.message,true)}}return}
    if(act==='lib-attach'){busy=true;b.disabled=true;try{const out=await api('media/library',{thread_id:d.id,library_id:b.dataset.lib});say(`已附上「${out.media.name}」，會跟回覆一起送出。`)}catch(e){say(e.message,true);b.disabled=false}finally{busy=false}const box=el.querySelector('.libgrid');if(box)box.hidden=true;await load(true);return}
    if(act==='media-remove'){busy=true;b.disabled=true;try{await api('media/'+b.dataset.media+'/remove',{thread_id:d.id});say('已移除附件。')}catch(e){say(e.message,true)}finally{busy=false}await load(true);return}
    if(act==='edit'){const box=el.querySelector('.editbox');box.hidden=!box.hidden;if(!box.hidden)box.querySelector('[data-instruction]').focus();return}
    if(act==='edit-cancel'){el.querySelector('.editbox').hidden=true;return}
    if(act==='revise'){
      const instruction=el.querySelector('[data-instruction]').value.trim();if(!instruction){say('請寫下要改哪裡。',true);return}
      const remember=el.querySelector('[data-remember]')?.checked??false;
      busy=true;b.disabled=true;
      try{const out=await api('redraft',{thread_id:d.id,version:d.version,instruction,original_draft:el.querySelector('[data-draft]').value,remember});delete edits[editKey(d)];if(out.rule_id)remember_rule(out.rule_id,instruction);say(out.remembered?'已記住這條指示，AI 正在重寫，約 1 分鐘。':'AI 正在重寫，約 1 分鐘。')}
      catch(e){say(e.message,true)}
      finally{busy=false}
      await load(true);return}
    if(act==='skip'||act==='cancel'){
      if(act==='skip'&&!b.dataset.armed){b.dataset.armed='1';b.textContent='再按一次確定';setTimeout(()=>{if(b.isConnected){delete b.dataset.armed;b.textContent='不發送'}},4000);return}
      busy=true;b.disabled=true;
      try{await api(act,{thread_id:d.id,version:d.version});delete edits[editKey(d)];say(act==='skip'?`「${d.name}」已標成不回覆。`:'已取消核准，回到待你決定。')}
      catch(e){say(e.message,true)}
      finally{busy=false}
      await load(true);return}
  }catch(e){say(e.message,true);b.disabled=false}
});
f('public-check').addEventListener('change',()=>{f('confirm-ok').disabled=!f('public-check').checked});
f('confirm').addEventListener('cancel',e=>{if(busy)e.preventDefault();else confirmation=null});
void load(true);setInterval(()=>load(false),8000);
