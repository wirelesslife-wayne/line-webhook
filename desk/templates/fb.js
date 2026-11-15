const f=id=>document.getElementById('fb-'+id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STATUS={waiting:'AI 擬稿中，約 1 分鐘',drafting:'AI 擬稿中，約 1 分鐘',ready:'草稿好了',blocked:'AI 暫停，請直接在框裡寫',error:'擬稿失敗，請按「請 AI 重寫」重試或直接寫',approved:'已核准，等送出',done:'已結案',skip:'不回覆'};
const TODO=['waiting','drafting','ready','blocked','error'];
const CHIP={waiting:['AI 擬稿中','wait'],drafting:['AI 擬稿中','wait'],ready:['待審核',''],blocked:['需人工回覆','manual'],error:['擬稿失敗',''],approved:['已核准','ok'],done:['已結案','off'],skip:['不回覆','off']};
const BRAND={};document.getElementById('fb-brands').content.querySelectorAll('span').forEach(s=>BRAND[s.dataset.ch]=s.innerHTML);
let openId=null,lastData=null;
let q='',beforeSearch='todo',tab='todo',channel='',page=0,order='old',late=false,items=[],edits={},confirmation=null,busy=false,shown='',listVersion=0,polling=false;
async function api(path,body){const r=await fetch('/desk/api/fb/'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json()}catch(e){}if(r.status===401){location.href='/desk/login';throw Error('請重新登入')}if(!r.ok)throw Error(d.error||'暫時無法處理，請重新載入');return d}
function say(text,bad){const e=f('feedback');e.textContent=text||'';e.hidden=!text;e.classList.toggle('bad',!!bad)}
const editKey=d=>d.id+':'+d.version;
function when(t){const d=new Date(t);if(/^\d{4}-\d\d-\d\dT/.test(t||'')&&!isNaN(d))return d.toLocaleString('zh-TW',{timeZone:'Asia/Taipei',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
  // Meta's own long form 「2026年11月12日 星期四上午7:23」 → 「11/12 上午7:23」 (phone review 11/13: it overlapped the status tag)
  const m=/\d{4}年(\d+)月(\d+)日\s*星期.\s*(上午|下午)?\s*(\d+:\d+)/.exec(t||'');return m?`${m[1]}/${m[2]} ${m[3]||''}${m[4]}`:(t||'')}
const PHONE=matchMedia('(max-width:700px)'),WIDE=matchMedia('(min-width:1100px)');
WIDE.addEventListener('change',()=>{if(lastData&&!editing())draw(lastData)});
const SPLIT='\n[[下一則]]\n',MAXPARTS=3;
const partsOf=t=>String(t??'').split('[[下一則]]').map(x=>x.replace(/^\n+|\n+$/g,''));
const joined=el=>[...el.querySelectorAll('[data-draft]')].map(t=>t.value.replace(/^\n+|\n+$/g,'')).filter((x,n,a)=>x.trim()||a.length===1).join(SPLIT);
function grow(t){if(!t)return;t.style.height='auto';t.style.height=Math.max(t.scrollHeight+2,t.closest('.parts')&&t.closest('.parts').children.length>1?72:150)+'px'}
function files(list,removable){return (list||[]).map(m=>`<figure>${m.type==='image'?`<img src="/desk/api/fb/media/${esc(m.id)}" alt="" loading="lazy">`:'<div class="vid">🎬</div>'}<span>${esc(m.name)}</span>${removable?`<button data-act="media-remove" data-media="${esc(m.id)}">移除</button>`:''}</figure>`).join('')}
// Wayne 11/15: like 待處理 in the LINE back office, so a conversation he still has to check never gets lost.
const HOLD={ours:'待我們確認',customer:'等客人回'};
function holdBlock(d){
  if(!canReview)return d.hold?`<p class="note">${d.hold==='ours'?'📌':'⏳'} ${HOLD[d.hold]}${d.hold_note?'：'+esc(d.hold_note):''}</p>`:'';
  if(d.hold){const other=d.hold==='ours'?'customer':'ours';
    return `<div class="note hold"><p>${d.hold==='ours'?'📌':'⏳'} <b>${HOLD[d.hold]}</b>${d.hold_note?'：'+esc(d.hold_note):''}${d.hold_at?` <small class="muted">（${esc(d.hold_at)} 起）</small>`:''}</p><p class="muted">${d.hold==='ours'?'會一直留在「待處理」，處理好了再按下面的按鈕。':'客人回訊息就會自動回到待審核。'}</p><div class="actions"><button data-act="hold" data-hold="">處理好了</button><button data-act="hold" data-hold="${other}">改成${HOLD[other]}</button></div></div>`}
  return `<div class="actions holdacts"><button class="link" data-act="hold" data-hold="ours">📌 標「待我們確認」</button><button class="link" data-act="hold" data-hold="customer">⏳ 標「等客人回」</button></div>`}
function card(d,i,closed){
  const todo=TODO.includes(d.status),waiting=d.status==='waiting'||d.status==='drafting',r=d.result||{};
  const draft=edits[editKey(d)]??r.draft??'';
  const lastUser=[...d.messages].reverse().find(m=>m.role!=='assistant')||d.messages[d.messages.length-1]||{};
  let [chip,tone]=CHIP[d.status]||[d.status,'off'];const open=openId===d.id&&!closed;
  if(TODO.includes(d.status)&&d.waited>=24){chip=`未回覆 ${Math.floor(d.waited/24)} 天`;tone='late'}
  // Wayne 11/15: in 待處理 (and search) the chip says what we are waiting for, not that the reply went out
  if((tab==='hold'||tab==='search')&&d.hold){chip=d.hold==='ours'?`📌 待確認${d.hold_days?` ${d.hold_days} 天`:''}`:'⏳ 等客人回';tone=d.hold==='ours'?'':'wait'}
  let h=`<article class="item${open?' open':''}${closed&&openId===d.id?' sel':''}" data-id="${esc(d.id)}"><button class="row" data-act="toggle" aria-expanded="${open}"><span class="avatar">${d.avatar?`<img src="/desk/api/fb/avatar?thread_id=${encodeURIComponent(d.id)}&t=${esc(d.avatar)}" alt="" decoding="async">`:USER_ICON}${(BRAND[d.channel]||'').split('<svg class="ic"')[0]}</span><span class="sep"></span><span class="who-said"><b>${d.hold?(d.hold==='ours'?'📌 ':'⏳ '):''}${esc(d.name)}</b><span>${esc((lastUser.text||'').replace(/\s+/g,' '))}</span></span><span class="when">${esc(when(lastUser.time||''))}</span><span class="state-chip ${tone}">${esc(chip)}</span>${CHEV}</button>`;
  if(!open)return h+'</article>';
  h+=`<div class="detail"><p class="muted">${esc(d.label)} · ${esc(STATUS[d.status]||d.status)}${d.note&&d.note!==STATUS[d.status]?' · '+esc(d.note):''}</p>`;
  h+=holdBlock(d);
  if(d.context)h+=`<p class="context"><b>所屬貼文：</b>${esc(d.context.slice(0,200))}</p>`;
  h+=`<div class="messages">${d.messages.map(m=>`<div class="message ${m.role==='assistant'?'ours':''}"><small>${m.role==='assistant'?'我們':'客人'} · ${esc(when(m.time))}</small>${esc(m.text)}</div>`).join('')||'<p class="muted">沒有訊息內容</p>'}</div>`;
  if(d.shot)h+=`<details class="shot" ${PHONE.matches?'':'open'}><summary>原始畫面截圖 ${esc(d.shot.replace('-','/'))}</summary><a href="/desk/api/fb/shot?thread_id=${encodeURIComponent(d.id)}" target="_blank" rel="noopener"><img src="/desk/api/fb/shot?thread_id=${encodeURIComponent(d.id)}&t=${encodeURIComponent(d.shot)}" alt="Meta 收件匣這則對話的截圖" loading="lazy"></a></details>`;
  if(/^https:\/\//.test(d.url||''))h+=`<p><a class="meta-link" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">在 Meta 收件匣打開這則</a></p>`;
  if(todo){
    if(r.staff_note)h+=`<p class="note ainote">💡 ${esc(r.staff_note)}</p>`;
    h+=(r.flags||[]).map(x=>`<p class="error">${esc(x)}</p>`).join('');
    if(!waiting&&open)h+=refsBlock(d);
    const vers=[{label:r.label,draft:r.draft},...(r.alternatives||[])].filter(v=>typeof v.draft==='string'&&v.draft);
    if(vers.length>1&&!waiting)h+=`<p class="muted">AI 寫了 ${vers.length} 個版本，按「用這版」會放進下面的框，可以再改字：</p><div class="versions">${vers.map((v,n)=>{const on=draft===v.draft;return `<section class="ver${on?' on':''}"><h3>版本${'ABC'[n]} <small>${esc(v.label||'')}${n===0?'（推薦）':''}</small></h3><pre class="${v.draft.split('\n').length>9||v.draft.length>260?'clamp':''}">${esc(v.draft)}</pre>${v.draft.split('\n').length>9||v.draft.length>260?'<button class="more" data-act="ver-more">展開全文</button>':''}<button data-act="version" data-v="${n}" aria-pressed="${on}" class="${on?'primary':''}">${on?'✓ 正在用這版':'用這版'}</button></section>`}).join('')}</div>`;
    const parts=partsOf(draft),many=parts.length>1;
    h+=`<label for="fb-draft-${i}-0">${many?`草稿分成 ${parts.length} 則，會一則一則送出（可以直接改字）`:'草稿（可以直接改字）'}</label><div class="parts">${parts.map((t,n)=>`<div class="part">${many?`<div class="part-head"><b>第 ${n+1} 則</b>${n?`<button class="link" data-act="part-merge" data-n="${n}">併回上一則</button>`:''}</div>`:''}<textarea id="fb-draft-${i}-${n}" data-draft data-n="${n}" maxlength="2000" ${waiting?'disabled':''}>${esc(t)}</textarea></div>`).join('')}</div>`;
    if(!waiting&&!d.is_public&&parts.length<MAXPARTS)h+=`<div class="part-tools"><button class="link" data-act="part-cut">✂ 從游標處分成下一則</button><button class="link" data-act="part-add">＋ 加一則</button></div>`;
    if(d.channel!=='ig_comment'&&canReview){
      const have=new Set((d.media||[]).map(m=>m.name)),sug=(r.cards||[]).filter(c=>!have.has(c.name));
      h+=`<div class="attach"><div class="files">${files(d.media,true)}</div>`;
      if(sug.length)h+=`<div class="suggest"><b>AI 建議附圖</b><div class="files">${sug.map(c=>`<figure><img src="/desk/api/fb/library/${esc(c.id)}" alt="" loading="lazy"><span>${esc(c.name)}</span><button data-act="lib-attach" data-lib="${esc(c.id)}">附上</button></figure>`).join('')}</div></div>`;
      h+=`<div class="actions"><label class="pick"><input type="file" data-file accept="image/jpeg,image/png,video/mp4" hidden>＋ 加圖片／影片</label><button data-act="lib-open">從常用圖片挑</button></div><div class="libgrid" hidden></div><small class="muted">${d.is_public?'會跟留言回覆一起送出，所有人都看得到。Facebook 留言只能附 1 個：JPEG／PNG 10MB 或 MP4 影片 25MB 以內。':'會跟文字一起送出（分成多則時，跟最後一則一起送）。JPEG／PNG 每張 10MB、MP4 影片 25MB 以內，最多 3 個。'}</small></div>`;
    }
    h+=`<div class="actions mainacts"><button class="primary" data-act="send" ${canReview&&!waiting?'':'disabled'}>發送</button><button data-act="edit" ${waiting?'disabled':''}>請 AI 重寫</button><button data-act="skip" ${canReview?'':'disabled'}>不發送</button></div>`;
    h+=`<div class="editbox" hidden><label for="fb-ins-${i}">要改哪裡？</label><p class="muted">由原本擬稿的 AI 重寫：它讀過價格表、制式回覆、你核准過的回覆和你記住的指示，會照你這句話改。</p><textarea id="fb-ins-${i}" data-instruction maxlength="2000" placeholder="例如：不要問縣市、價格寫 18,000、語氣再輕鬆一點"></textarea>${canReview?'<label class="check"><input type="checkbox" data-remember>以後都照這樣寫（記住這條指示，會影響之後所有草稿）</label>':''}<div class="actions"><button class="primary" data-act="revise">照這樣重寫</button><button data-act="edit-cancel">取消</button></div></div>`;
  }else if(d.status==='approved'){
    const a=d.attempts.find(x=>x.status==='approved');
    h+=`<p class="closed-by">${a?`${esc(a.staff)} 核准 · ${esc(a.time.slice(5,16))}・`:''}<b>尚未送出</b></p><label>已核准的內容</label><div class="preview">${esc(a?a.text:'')}</div>${(d.media||[]).length?`<div class="attach"><div class="files">${files(d.media,false)}</div></div>`:''}<p class="muted">${d.live?'正在用 Meta 官方 API 送出，幾秒內完成；送不出去會退回待審並寫原因。':'下一輪排程會照這段文字送出（最慢約 60 分鐘）。'}</p><div class="actions"><button data-act="cancel" ${canReview?'':'disabled'}>取消核准</button></div>`;
  }else{
    const a=d.attempts.find(x=>x.status==='sent');
    h+=`<p class="closed-by">${d.status==='skip'?'不回覆':a?`已送出 · ${esc(a.staff)} 核准`:'已結案'} · ${esc(d.closed_at||'')}</p>`;
    if(a)h+=`<label>已送出的內容（${esc(a.time)}）</label><div class="preview">${esc(a.text)}</div>`;
  }
  return h+'</div></article>';
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
function editing(){const a=document.activeElement;return busy||f('confirm').open||f('lookup').open||(a&&a.tagName==='TEXTAREA')||!!document.querySelector('.editbox:not([hidden]),.libgrid:not([hidden])')}
let library=null;
async function libraryGrid(box){
  if(!library)library=(await api('library')).cards;
  if(!library.length){box.innerHTML='<p class="muted">常用圖片還沒匯入。</p>';return}
  const groups={};library.forEach(c=>(groups[c.topic||'其他']??=[]).push(c));
  box.innerHTML=Object.entries(groups).map(([t,list])=>`<h3>${esc(t)}</h3><div class="files">${list.map(c=>`<figure title="${esc(c.use_when)}"><img src="/desk/api/fb/library/${esc(c.id)}" alt="" loading="lazy"><span>${esc(c.name)}</span><small class="muted">${esc(c.use_when)}</small><button data-act="lib-attach" data-lib="${esc(c.id)}">附上</button></figure>`).join('')}</div>`).join('');
}
// Wayne 11/14: what the draft could lean on, found automatically; 依此重寫 is one more tap
const REFS={},REFUSED={};let refsShut=new Set(),lookupFor=null,lkKind='history';
function refKey(d){return d.id+':'+d.version}
const RISKY=/格式化|恢復原廠|原廠設定|重置|reset|刪除/i;  // ChatGPT and Gemini 11/14: steps that wipe a customer's videos
function refCard(x,n,where){return `<div class="ref"><b>${x.kind==='canned'?'📘 制式回覆｜'+esc(x.title):'💬 真人案例｜'+esc(x.date||'')}</b>${x.question?`<p class="q">客人問：${esc(x.question)}</p>`:''}<p>${x.kind==='history'?'同事答：':''}${esc(x.text)}</p>${RISKY.test(x.text)?'<p class="warn">⚠️ 含格式化／重置等會清掉資料的步驟，只能參考，不能依此重寫（11/15 兩家 AI：只靠警告擋不住）</p>':`<button data-act="ref-use" data-where="${where}" data-n="${n}">依此重寫</button>`}</div>`}
function refsBlock(d){
  const r=REFS[refKey(d)];
  if(r===undefined){fetchRefs(d);return '<div class="refs"><p class="muted">參考資料：查詢中…</p></div>'}
  const list=[...(r.canned||[]),...(r.history||[])],shut=refsShut.has(d.id);
  if(!list.length)return '<div class="refs"><p class="muted">參考資料 ⚪ 目前沒有合適的參考資料</p><button class="link" data-act="lookup">查資料 ›</button></div>';
  return `<div class="refs"><button class="link refs-head" data-act="refs-toggle">參考資料 ✅ 找到 ${list.length} 筆 ${shut?'▾':'▴'}</button>${shut?'':list.map((x,n)=>refCard(x,n,'auto')).join('')}<button class="link" data-act="lookup">找不到？查資料 ›</button></div>`}
async function fetchRefs(d){
  const k=refKey(d);if(REFS[k]===null)return;REFS[k]=null;
  try{REFS[k]=await api('references?thread_id='+encodeURIComponent(d.id))}catch(e){REFS[k]={canned:[],history:[]}}
  const box=document.querySelector(`.item[data-id="${CSS.escape(d.id)}"] .detail .refs`);if(box)box.outerHTML=refsBlock(d)}
async function useRef(d,el,x){
  if(x.kind!=='paste'&&!confirm('用這筆參考資料請 AI 重寫草稿嗎？框裡現在的草稿會被換掉。'))return;  // Grok 11/15: one stray tap rewrote with the wrong reference
  const label=x.kind==='canned'?'制式回覆〈'+x.title+'〉':x.kind==='history'?'真人案例 '+(x.date||''):'你貼上的答案';
  const body=(x.question?'客人問：'+x.question+'\n':'')+(x.kind==='history'?'同事答：':'')+x.text;
  const instruction=('請依照這份參考資料重寫草稿（價格、連結仍以價格表和制式回覆為準；參考資料裡的舊價格、舊活動不要照抄'+(RISKY.test(body)?'；格式化、重置這類會清掉資料的步驟，不要叫客人直接做，要先請客人備份重要影片，客人還想保留影片時優先教他保存的方法':'')+'）：\n【'+label+'】\n'+body).slice(0,2000);
  busy=true;try{await api('redraft',{thread_id:d.id,version:d.version,instruction,original_draft:el?joined(el):'',remember:false});delete edits[editKey(d)];REFUSED[d.id]=label;say(`AI 正在依「${label}」重寫，約 1 分鐘。`)}
  catch(e){say(e.message,true)}finally{busy=false}
  await load(true)}
function lkShow(kind){lkKind=kind;document.querySelectorAll('[data-lk]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.lk===kind)));
  f('lk-form').hidden=f('lk-results').hidden=kind==='paste';f('lk-paste').hidden=kind!=='paste';if(kind!=='paste')void lkSearch()}
let lkItems=[];
async function lkSearch(){const q=f('lk-q').value.trim(),box=f('lk-results');if(!q){box.innerHTML='<p class="muted">輸入車款或產品再按搜尋。</p>';return}
  box.innerHTML='<p class="muted">搜尋中…</p>';
  try{lkItems=(await api(`lookup?kind=${lkKind}&q=${encodeURIComponent(q)}`)).items;box.innerHTML=lkItems.length?lkItems.map((x,n)=>refCard(x,n,'panel')).join(''):'<p class="muted">沒找到。換個關鍵字（車款、產品名稱），或改用「貼上答案」。</p>'}
  catch(e){box.innerHTML=`<p class="error">${esc(e.message)}</p>`}}
function draw(d){
  lastData=d;items=d.items;f('n-todo').textContent=d.todo||'';f('n-approved').textContent=d.approved||'';f('n-hold').textContent=d.hold||'';const per=d.per_channel||{},all=Object.values(per).reduce((a,b)=>a+b,0);document.querySelectorAll('[data-cnt]').forEach(x=>{const n=x.dataset.cnt?per[x.dataset.cnt]:all;x.textContent=n?` ${n}`:''});
  f('prev').disabled=page===0;f('next').disabled=!d.has_more;
  f('total').textContent=`共 ${d.total||0} 位`;f('alert').hidden=tab!=='todo'||!(d.overdue||d.manual||late);
  f('late').textContent=late?`只看超過 24 小時未回覆的 ${d.total||0} 則・顯示全部 ›`:d.overdue?`⚠ ${d.overdue} 則超過 24 小時未回覆 ›`:'';f('late').setAttribute('aria-pressed',String(late));
  f('manual').textContent=d.manual?`${d.manual} 則需人工回覆`:'';
  const empty={todo:'目前沒有待審核的訊息。',approved:'目前沒有即將送出的回覆。',hold:'目前沒有待處理的對話。',done:'還沒有結案的紀錄。',search:`沒有找到名字含「${esc(q)}」的客人。`}[tab];
  if(tab==='search')f('total').textContent=`搜尋「${q}」：${d.total||0} 位`;
  const sec=(x,i)=>tab==='hold'&&(i===0||items[i-1].hold!==x.hold)?`<h3 class="holdsec">${x.hold==='ours'?`📌 待我們確認（${d.hold_ours||0}）`:`⏳ 等客人回（${d.hold_customer||0}）`}</h3>`:'';
  if(WIDE.matches&&items.length){
    if(!items.some(x=>x.id===openId))openId=items[0].id;  // two columns always show one conversation
    const n=items.findIndex(x=>x.id===openId);
    f('list').innerHTML=`<div class="split"><div class="rows">${items.map((x,i)=>sec(x,i)+card(x,i,true)).join('')}</div><div class="pane">${card(items[n],n)}</div></div>`;
  }else f('list').innerHTML=items.length?items.map((x,i)=>sec(x,i)+card(x,i)).join(''):`<p class="muted">${empty}</p>`;
  document.querySelectorAll('[data-draft]').forEach(grow);
}
function wipe(){items=[];shown='';openId=null;f('list').innerHTML='<p class="muted">讀取中…</p>'}
async function load(force){
  if(!force&&polling)return; // a slow answer must not be discarded by the next timer tick
  const v=++listVersion;polling=true;
  try{const d=await api(`list?channel=${encodeURIComponent(channel)}&tab=${tab}&page=${page}&order=${order}${late?'&late=1':''}${tab==='search'?'&q='+encodeURIComponent(q):''}`);if(v!==listVersion)return;const sig=JSON.stringify(d);if(!force&&(sig===shown||editing()))return;shown=sig;draw(d)}
  catch(e){if(v===listVersion&&force)say(e.message,true)}
  finally{if(v===listVersion)polling=false}
}
function remember_rule(id,instruction){const box=f('rules');if(!box)return;const row=document.createElement('div');row.className='rule';row.innerHTML=`<p>${esc(instruction)}</p><small class="muted">剛剛記住</small><button data-rule-id="${Number(id)}" data-active="true">停用</button>`;box.querySelector('.rule')?box.insertBefore(row,box.querySelector('.rule')):box.appendChild(row);box.querySelector('.none')?.remove();box.querySelector('summary').textContent=`我記住的修改指示（${box.querySelectorAll('.rule').length} 條）`}
function pick(button){const el=button.closest('article');const d=items.find(x=>x.id===el?.dataset.id);return d?{d,el}:null}
document.addEventListener('click',e=>{const n=e.target.closest('.ainote');if(n)n.classList.toggle('full')});
let lastCaret={};document.addEventListener('selectionchange',()=>{const t=document.activeElement;if(t&&t.matches&&t.matches('[data-draft]'))lastCaret={id:t.id,pos:t.selectionStart}});
document.addEventListener('input',e=>{if(e.target.matches('[data-draft]')){grow(e.target);const p=pick(e.target);if(p)edits[editKey(p.d)]=joined(p.el)}});
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b||busy)return;
  try{
    if(b.id==='fb-order'){order=order==='old'?'new':'old';b.textContent=order==='old'?'排序：等最久優先 ⇅':'排序：最新優先 ⇅';page=0;await load(true);return}
    if(b.id==='fb-late'){late=!late;page=0;wipe();await load(true);return}
    if(b.id==='fb-search-open'){const open=f('search').hidden;f('search').hidden=!open;b.setAttribute('aria-expanded',String(open));if(open)f('q').focus();else if(tab==='search')endSearch();return}
    if(b.id==='fb-search-close'){f('search').hidden=true;f('search-open').setAttribute('aria-expanded','false');if(tab==='search')endSearch();return}
    if(b.dataset.fbTab!==undefined){q='';f('search').hidden=true;f('search-open').setAttribute('aria-expanded','false');tab=b.dataset.fbTab;page=0;late=false;document.querySelectorAll('[data-fb-tab]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));f('order').hidden=tab==='done'||tab==='hold';say('');wipe();await load(true);return}
    if(b.dataset.fbChannel!==undefined){channel=b.dataset.fbChannel;page=0;document.querySelectorAll('[data-fb-channel]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));wipe();await load(true);return}
    if(b.id==='fb-prev'||b.id==='fb-next'){page=Math.max(0,page+(b.id==='fb-prev'?-1:1));wipe();await load(true);window.scrollTo(0,0);return}
    if(b.dataset.lk){lkShow(b.dataset.lk);return}
    if(b.id==='fb-lookup-close'){f('lookup').close();return}
    if(b.dataset.act==='ref-use'&&b.dataset.where==='panel'&&lookupFor){const x=lkItems[Number(b.dataset.n)],d=lookupFor;if(!x)return;f('lookup').close();await useRef(d,document.querySelector(`.item[data-id="${CSS.escape(d.id)}"]`),x);return}
    if(b.id==='fb-lk-copy'&&lookupFor){const last=[...lookupFor.messages].reverse().find(m=>m.role!=='assistant');try{await navigator.clipboard.writeText(last?last.text:'');say('已複製客人問題，可以貼到 NotebookLM。')}catch(e){say('手機不讓網頁複製，請長按客人訊息自己複製。',true)}return}
    if(b.id==='fb-lk-use-paste'&&lookupFor){const text=f('lk-answer').value.trim(),d=lookupFor;if(!text){say('請先貼上查到的答案。',true);return}
      if(f('lk-submit').checked){const last=[...d.messages].reverse().find(m=>m.role!=='assistant');
        try{await api('knowledge',{user_id:d.id,question:(last?last.text:'').slice(0,5000)||'（客人問題）',answer:text,summary:text.split('\n')[0].slice(0,120),scope:''})}catch(e){say('送交知識審核失敗：'+e.message,true);return}}
      f('lookup').close();await useRef(d,document.querySelector(`.item[data-id="${CSS.escape(d.id)}"]`),{kind:'paste',title:'貼上的答案',text});
      if(f('lk-submit').checked)say('已送交知識審核，AI 也正在依這個答案重寫，約 1 分鐘。');return}
    if(b.id==='fb-confirm-cancel'){f('confirm').close();confirmation=null;return}
    if(b.id==='fb-confirm-ok'&&confirmation){
      const snap=confirmation;if(linkGate.pending||linkGate.bad)return;if(snap.is_public&&!f('public-check').checked)return;const lc=document.getElementById('fb-links-check');if(lc&&!lc.checked)return;
      busy=true;b.disabled=true;
      try{const out=await api('send',{thread_id:snap.thread_id,version:snap.version,text:snap.text,retry_key:snap.retry_key,confirmed:true,public_confirmed:snap.is_public,links_confirmed:!!(lc&&lc.checked),media_ids:snap.media_ids});delete edits[snap.key];say(out.live?`給「${snap.name}」的回覆正在即時送出。`:`已核准給「${snap.name}」的回覆，下一輪排程送出。`)}
      catch(e){say(e.message,true)}
      finally{busy=false;b.disabled=false;f('confirm').close();confirmation=null}
      await load(true);return}
    if(b.dataset.ruleId){const active=b.dataset.active!=='true';b.disabled=true;await api('rules/'+b.dataset.ruleId,{active});b.dataset.active=String(active);b.textContent=active?'停用':'重新啟用';b.closest('.rule').classList.toggle('off',!active);b.disabled=false;return}
    const act=b.dataset.act;if(!act)return;const p=pick(b);if(!p)return;const {d,el}=p;
    if(act==='toggle'&&WIDE.matches){if(openId!==d.id){openId=d.id;if(lastData)draw(lastData);document.querySelector('.pane')?.scrollTo(0,0)}return}
    if(act==='toggle'){openId=openId===d.id?null:d.id;if(lastData)draw(lastData);if(openId)document.querySelector(`.item[data-id="${CSS.escape(openId)}"]`)?.scrollIntoView({block:'nearest'});return}
    if(act==='hold'){busy=true;try{await api('hold',{thread_id:d.id,hold:b.dataset.hold,note:b.dataset.hold?d.hold_note||'':''});say(b.dataset.hold?`已標「${HOLD[b.dataset.hold]}」，會留在「待處理」直到你按處理好了。`:'已從「待處理」移除。')}catch(e){say(e.message,true)}finally{busy=false}await load(true);return}
    if(act==='refs-toggle'){refsShut.has(d.id)?refsShut.delete(d.id):refsShut.add(d.id);const box=el.querySelector('.refs');if(box)box.outerHTML=refsBlock(d);return}
    if(act==='ref-use'&&b.dataset.where==='auto'){const r=REFS[refKey(d)]||{},x=[...(r.canned||[]),...(r.history||[])][Number(b.dataset.n)];if(x){b.disabled=true;await useRef(d,el,x)}return}
    if(act==='lookup'){lookupFor=d;f('lk-q').value=((REFS[refKey(d)]||{}).query||'');f('lk-answer').value='';f('lk-submit').checked=false;f('lookup').showModal();lkShow(f('lk-q').value?'history':'canned');return}
    if(act==='ver-more'){const pre=b.previousElementSibling,open=pre.classList.toggle('clamp');b.textContent=open?'展開全文':'收合';return}
    if(act==='version'){const r=d.result||{},vers=[{draft:r.draft},...(r.alternatives||[])],v=vers[Number(b.dataset.v)];if(!v)return;const cur=edits[editKey(d)];if(cur!==undefined&&!vers.some(x=>x.draft===cur)&&!confirm('框裡改過的字會被換掉，確定換版本嗎？'))return;edits[editKey(d)]=v.draft;draw(lastData);return}
    if(act==='part-cut'||act==='part-add'||act==='part-merge'){const ps=[...el.querySelectorAll('[data-draft]')].map(t=>t.value);
      if(act==='part-merge'){const n=Number(b.dataset.n);ps[n-1]=ps[n-1].replace(/\n+$/,'')+'\n\n'+ps[n].replace(/^\n+/,'');ps.splice(n,1)}
      else if(act==='part-add')ps.push('');
      else{const t=lastCaret.id&&el.querySelector('#'+CSS.escape(lastCaret.id));if(!t){say('先在框裡點一下要切開的位置，再按「從游標處分成下一則」。',true);return}const n=Number(t.dataset.n),pos=lastCaret.pos;if(!ps[n].slice(0,pos).trim()||!ps[n].slice(pos).trim()){say('游標要放在文字中間（前後都要有字）才能切開。',true);return}ps.splice(n,1,ps[n].slice(0,pos),ps[n].slice(pos))}
      edits[editKey(d)]=ps.map(x=>x.replace(/^\n+|\n+$/g,'')).join(SPLIT);lastCaret={};draw(lastData);return}
    if(act==='send'){
      const text=joined(el);if(partsOf(text).some(x=>!x.trim())){say('有一則是空的，請填內容或按「併回上一則」。',true);return}if(!text.trim()){say('草稿是空的，請先寫內容或按「請 AI 重寫」。',true);return}
      confirmation=Object.freeze({thread_id:d.id,version:d.version,text,name:d.name,is_public:d.is_public,live:!!d.live,key:editKey(d),retry_key:crypto.randomUUID(),media_ids:(d.media||[]).map(m=>m.id)});
      f('links').hidden=true;f('links').innerHTML='';linkGate={pending:true,bad:false};
      // Grok 11/15: the confirm button worked before the link check came back, so a wrong link could slip through
      api('links',{text}).then(out=>{if(!confirmation||confirmation.text!==text)return;linkGate.pending=false;if(!out.links.length){gate();return}const bad=out.links.some(l=>l.state==='broken'),wrong=out.links.some(l=>l.verdict==='mismatch');
        f('links').innerHTML='<b>連結檢查：</b>'+out.links.map(l=>`<div class="${l.verdict==='mismatch'||l.state==='broken'?'error':''}">${l.state==='broken'?'❌ 打不開':l.verdict==='mismatch'?'❌ 不相符':l.state==='ok'?'✅':'⚠️ 沒查到'} ${l.label?'「'+esc(l.label)+'」':''} ${esc(l.url)}<br><small>實際開到：${esc(l.title||l.target||'（無法確認）')}${l.verdict==='mismatch'?'（這是'+esc(l.goes.join('、'))+'，前面寫的是'+esc(l.says.join('、'))+'）':''}</small></div>`).join('');
        f('links').hidden=false;if(bad){linkGate.bad=true;f('links').insertAdjacentHTML('beforeend','<p class="error">有連結打不開，請先返回改掉。</p>')}
        else if(wrong)f('links').insertAdjacentHTML('beforeend','<p class="error">有連結開到別的產品，請按「返回」改掉。</p><label class="check"><input id="fb-links-check" type="checkbox">連結我核對過了，沒有貼錯</label>');
        gate()}).catch(()=>{if(!confirmation||confirmation.text!==text)return;linkGate.pending=false;
        f('links').innerHTML='<p class="error">⚠️ 連結檢查沒跑完（網路或伺服器問題），請自己點開連結確認。</p><label class="check"><input id="fb-links-check" type="checkbox">連結我自己點開確認過了</label>';f('links').hidden=false;gate()});
      f('refs-used').hidden=!REFUSED[d.id];f('refs-used').textContent=REFUSED[d.id]?`本稿參考：${REFUSED[d.id]}（只有你看得到，不會發給客人）`:'';f('attach-note').hidden=!(d.media||[]).length;f('attach-note').textContent=(d.media||[]).length?`另附 ${d.media.length} 個檔案：${d.media.map(m=>m.name).join('、')}`:'';
      f('recipient').textContent=`回覆「${d.name}」（${d.label}）`;f('when').textContent=d.live?'按下確認後，會用 Meta 官方 API 立刻送出。':'按下確認後，下一輪排程會照這段文字送出（最慢約 60 分鐘），送出前都可以在「即將送出」取消。';f('confirm-ok').textContent=d.live?'確認發送':'確認核准';const ps=partsOf(text);f('preview').textContent=ps.length>1?ps.map((x,n)=>`【第 ${n+1} 則】\n${x}`).join('\n\n'):text;f('public-warning').hidden=f('public-label').hidden=!d.is_public;f('public-check').checked=false;gate();f('confirm').showModal();return}
    if(act==='lib-open'){const box=el.querySelector('.libgrid');box.hidden=!box.hidden;b.textContent=box.hidden?'從常用圖片挑':'收起常用圖片';if(!box.hidden&&!box.innerHTML){box.innerHTML='<p class="muted">讀取中…</p>';try{await libraryGrid(box)}catch(e){box.innerHTML='';say(e.message,true)}}return}
    if(act==='lib-attach'){busy=true;b.disabled=true;try{const out=await api('media/library',{thread_id:d.id,library_id:b.dataset.lib});say(`已附上「${out.media.name}」，會跟回覆一起送出。`)}catch(e){say(e.message,true);b.disabled=false}finally{busy=false}const box=el.querySelector('.libgrid');if(box)box.hidden=true;await load(true);return}
    if(act==='media-remove'){busy=true;b.disabled=true;try{await api('media/'+b.dataset.media+'/remove',{thread_id:d.id});say('已移除附件。')}catch(e){say(e.message,true)}finally{busy=false}await load(true);return}
    if(act==='edit'){const box=el.querySelector('.editbox');box.hidden=!box.hidden;if(!box.hidden)box.querySelector('[data-instruction]').focus();return}
    if(act==='edit-cancel'){el.querySelector('.editbox').hidden=true;return}
    if(act==='revise'){
      const instruction=el.querySelector('[data-instruction]').value.trim();if(!instruction){say('請寫下要改哪裡。',true);return}
      const remember=el.querySelector('[data-remember]')?.checked??false;
      busy=true;b.disabled=true;
      try{const out=await api('redraft',{thread_id:d.id,version:d.version,instruction,original_draft:joined(el),remember});delete edits[editKey(d)];if(out.rule_id)remember_rule(out.rule_id,instruction);say(out.remembered?'已記住這條指示，AI 正在重寫，約 1 分鐘。':'AI 正在重寫，約 1 分鐘。')}
      catch(e){say(e.message,true)}
      finally{busy=false}
      await load(true);return}
    if(act==='skip'||act==='cancel'){
      if(act==='skip'&&!b.dataset.armed){b.dataset.armed='1';b.textContent='再按一次確定';setTimeout(()=>{if(b.isConnected){delete b.dataset.armed;b.textContent='不發送'}},4000);return}
      busy=true;b.disabled=true;
      try{await api(act,{thread_id:d.id,version:d.version});delete edits[editKey(d)];say(act==='skip'?`「${d.name}」已標成不回覆。`:'已取消核准，回到待審核。')}
      catch(e){say(e.message,true)}
      finally{busy=false}
      await load(true);return}
  }catch(e){say(e.message,true);b.disabled=false}
});
f('lk-form').addEventListener('submit',e=>{e.preventDefault();void lkSearch()});
// Wayne 11/15: find a customer by name in every tab and channel
f('search').addEventListener('submit',async e=>{e.preventDefault();const v=f('q').value.trim();if(!v)return;q=v;if(tab!=='search')beforeSearch=tab;tab='search';page=0;late=false;
  document.querySelectorAll('[data-fb-tab]').forEach(x=>x.setAttribute('aria-pressed','false'));f('order').hidden=true;say('');wipe();await load(true)});
async function endSearch(){q='';f('q').value='';tab=beforeSearch;page=0;document.querySelectorAll('[data-fb-tab]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.fbTab===tab)));
  f('order').hidden=tab==='done'||tab==='hold';wipe();await load(true)}
let linkGate={pending:false,bad:false};
function gate(){const ok=f('confirm-ok'),lc=document.getElementById('fb-links-check');
  ok.disabled=linkGate.pending||linkGate.bad||!!(lc&&!lc.checked)||!!(confirmation&&confirmation.is_public&&!f('public-check').checked);
  ok.textContent=linkGate.pending?'連結檢查中…':confirmation&&confirmation.live?'確認發送':'確認核准'}
f('public-check').addEventListener('change',gate);
document.addEventListener('change',e=>{if(e.target.id==='fb-links-check')gate()});
f('confirm').addEventListener('cancel',e=>{if(busy)e.preventDefault();else confirmation=null});
void load(true);setInterval(()=>load(false),8000);
