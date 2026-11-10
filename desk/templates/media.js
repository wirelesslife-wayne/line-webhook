let selectedMedia=[],uploading=false;
function mediaMarkup(item){
 if(typeof item==='string')item={type:'image',url:item,preview_url:item,name:'原確認圖片'};
 return `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer"><img src="${esc(item.preview_url)}" alt="${esc(item.name)}" loading="lazy"><span>${item.type==='video'?'影片':'圖片'} · ${esc(item.name)}</span></a>`;
}
function renderMedia(){
 const node=el('uploads');if(!node)return;
 const locked=!!current?.pending||!!retrySnapshot;
 node.innerHTML=selectedMedia.map((item,index)=>`<div class="upload-item">${mediaMarkup(item)}${!locked?`<button type="button" data-remove-media="${index}" aria-label="移除 ${esc(item.name)}" ${uploading?'disabled':''}>移除</button>`:''}</div>`).join('');
 const input=el('media-input');if(input)input.disabled=uploading||locked||sending;
 el('open-confirm').disabled=uploading||(!!current?.pending&&!current.pending.can_retry);
 el('upload-status').textContent=uploading?'正在處理附件，請稍候…':selectedMedia.length?`已加入 ${selectedMedia.length}／4 個檔案`:'可選 JPEG、PNG 圖片或 MP4 影片，最多 4 個檔案。';
}
function canvasBlob(canvas,type='image/jpeg',quality=.85){
 return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(Error('無法產生圖片，請改選其他檔案')),type,quality));
}
function drawFrame(source,width,height,max){
 const scale=Math.min(1,max/Math.max(width,height));
 const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
 const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(source,0,0,canvas.width,canvas.height);return canvas;
}
async function prepareMedia(file){
 if(!['image/jpeg','image/png','video/mp4'].includes(file.type))throw Error('只接受 JPEG、PNG 圖片或 MP4 影片');
 if(file.type==='video/mp4'&&file.size>50_000_000)throw Error('影片太大，請改傳 YouTube 連結');
 if(!file.size)throw Error('檔案內容是空的');
 const url=URL.createObjectURL(file);
 try{
  let source,width,height;
  if(file.type==='video/mp4'){
   source=document.createElement('video');source.preload='auto';source.muted=true;source.playsInline=true;
   await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{source.removeAttribute('src');source.load();reject(Error('影片無法讀取第一格畫面，請改傳相容 MP4 或 YouTube 連結'))},20000);
    source.onloadeddata=()=>{clearTimeout(timer);resolve()};source.onerror=()=>{clearTimeout(timer);reject(Error('影片無法讀取，請改傳相容 MP4 或 YouTube 連結'))};source.src=url;
   });
   width=source.videoWidth;height=source.videoHeight;
  }else{
   source=new Image();source.src=url;
   try{await source.decode()}catch(_){throw Error('圖片無法讀取，請改用 JPEG／PNG')}
   width=source.naturalWidth;height=source.naturalHeight;
  }
  if(!width||!height)throw Error('無法讀取檔案尺寸');
  const preview=await canvasBlob(drawFrame(source,width,height,240));
  let upload=file;
  if(file.type!=='video/mp4'&&file.size>10_000_000){
   const canvas=drawFrame(source,width,height,2048);
   for(const quality of [.9,.75,.6,.45]){
    upload=await canvasBlob(canvas,'image/jpeg',quality);
    if(upload.size<=10_000_000)break;
   }
   if(upload.size>10_000_000)throw Error('圖片壓縮後仍超過 10MB，請改選較小圖片');
   upload=new File([upload],file.name.replace(/\.[^.]*$/,'')+'.jpg',{type:'image/jpeg'});
  }
  return {file:upload,preview:new File([preview],'preview.jpg',{type:'image/jpeg'})};
 }finally{URL.revokeObjectURL(url)}
}
document.addEventListener('change',async event=>{
 if(event.target.id!=='media-input'||uploading||sending||current?.pending||retrySnapshot||confirmation)return;
 const input=event.target,files=[...input.files],uid=selected,v=selectionVersion;
 input.value='';if(!files.length)return;
 if(selectedMedia.length+files.length>4){actionFeedback('最多只能加入 4 個檔案，請先移除不用的附件',true);return}
 uploading=true;renderMedia();actionFeedback('');
 try{
  for(const file of files){
   const prepared=await prepareMedia(file);if(!matches(uid,v))return;
   const form=new FormData();form.append('user_id',uid);form.append('file',prepared.file);form.append('preview',prepared.preview);
   const response=await fetch('/desk/api/media',{method:'POST',headers:{'X-CSRF-Token':csrf},body:form});
   const data=await response.json();if(!response.ok)throw Error(data.error||'上傳失敗，請重新選取');
   if(!matches(uid,v))return;
   selectedMedia.push(data.media);retrySnapshot=null;renderMedia();
  }
 }catch(error){if(matches(uid,v))actionFeedback(error.message,true)}
 finally{uploading=false;if(current)renderMedia()}
});
