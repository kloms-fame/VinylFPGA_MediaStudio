"use strict";
const $ = id => document.getElementById(id);
const S = {projectId:null,project:null,projects:[],profile:null,track:0,releases:[],release:null,releaseId:null,
  dirty:false,tab:"editor",page:"play",job:null,disks:[],disk:null,frameBusy:false,frameAgain:false,lyricsDirty:false,uploads:0,saving:false};
const TOKEN = window.STUDIO_TOKEN;
let fixedDisplayState={},displayedFrameState=null;
const esc = x => String(x ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const size = bytes => bytes >= 1073741824 ? (bytes/1073741824).toFixed(2)+" GiB" : (bytes/1048576).toFixed(1)+" MiB";
const pad = n => String(n).padStart(2,"0");
function clock(seconds,detail=false){seconds=Math.max(0,Number(seconds)||0);return `${pad(Math.floor(seconds/60))}:${pad(Math.floor(seconds%60))}`+(detail?"."+String(Math.floor(seconds*1000)%1000).padStart(3,"0"):"");}
function toast(message){$("toast").textContent=message;$("toast").classList.remove("hidden");clearTimeout(toast.timer);toast.timer=setTimeout(()=>$("toast").classList.add("hidden"),8000);}
async function api(path,method="GET",data){
  const options={method,headers:{"X-Studio-Token":TOKEN}};
  if(data!==undefined){options.headers["Content-Type"]="application/json";options.body=JSON.stringify(data);}
  const response=await fetch(path,options);
  const result=await response.json();
  if(!response.ok)throw new Error(result.error||`请求失败 ${response.status}`);
  return result;
}
const projectURL = () => `/api/projects/${S.projectId}`;
const releaseURL = () => `${projectURL()}/releases/${S.releaseId}`;
const assetURL = path => `${projectURL()}/asset?path=${encodeURIComponent(path)}&token=${encodeURIComponent(TOKEN)}`;
const guarded = action => async event => {try{await action(event);}catch(error){toast(error.message||String(error));}};
function changed(){S.dirty=true;$("saveState").textContent="有未保存修改";$("saveProject").disabled=false;if(S.release){$("previewSource").textContent="已构建版本预览 · 编辑尚未应用";$("compatibility").textContent="当前显示已构建镜像。新编辑尚未生成镜像，保存项目也不会更新此画面；请重新构建后预览。";$("compatibility").className="compatibility warning";}updateBadge();}
function track(){return S.project.tracks[S.track];}
function collect(){
  if(!S.project)return;
  const t=track();t.title=$("trackTitle").value;t.artist=$("trackArtist").value;
  t.cover_fit=$("coverFit").value;t.crop=[Number($("cropX").value),Number($("cropY").value)];t.lyric_offset_ms=Number($("lyricOffset").value)||0;
  S.project.name=$("albumName").value||"我的唱片";S.project.mode=$("firmwareMode").value;S.project.image_size_mib=Number($("imageSize").value);
}
function updateBadge(){
  const custom=S.project?.mode==="custom";$("modeBadge").textContent=custom?"自定义 ROM · 需配套固件":"当前冻结固件";
  $("firmwareHelp").textContent=custom?"将生成配套字库与歌词 ROM。导出的新文字需要在 TD 中编译并烧录对应固件，单独写卡不会更新歌词。":"使用当前已验收 ROM。预览中的中文标题、歌手和歌词以冻结 ROM 为准；候选编辑差异会在构建报告中列出。";
}
async function refreshState(){const d=await api("/api/state");S.projects=d.projects;S.profile=d.profile;$("version").textContent=`Media Studio ${d.version} · Windows Portable`;renderProjects();}
function renderProjects(){
  const root=$("projectList");root.replaceChildren();
  for(const p of S.projects){const b=document.createElement("button");b.textContent="◉  "+p.name;b.className=p.id===S.projectId?"active":"";b.onclick=guarded(()=>openProject(p.id));root.append(b);}
}
async function newProject(){
  $("newDialog").showModal();$("newName").focus();$("newName").select();
}
$("newDialog").addEventListener("close",guarded(async()=>{
  if($("newDialog").returnValue!=="create")return;
  if(S.uploads||S.saving||S.job)throw new Error("当前操作仍在进行，请等待完成后再创建项目");
  if(S.dirty&&!confirm("当前项目有未保存修改，继续创建新项目？"))return;
  const result=await api("/api/projects","POST",{name:$("newName").value});
  await refreshState();await openProject(result.id,true);
}));
async function openProject(id,force=false){
  if(S.uploads)throw new Error("正在导入素材，请等待完成后再切换项目");
  if(S.saving||S.job)throw new Error("当前操作仍在进行，请等待完成后再切换项目");
  if(!force&&S.dirty&&!confirm("当前项目有未保存修改，切换项目将丢弃这些修改。继续？"))return;
  $("audio").pause();const result=await api(`/api/projects/${id}`);
  S.projectId=id;S.project=result.project;S.track=0;S.releases=result.releases;S.dirty=false;S.lyricsDirty=false;
  $("projectName").textContent=S.project.name;$("albumName").value=S.project.name;$("firmwareMode").value=S.project.mode;$("imageSize").value=S.project.image_size_mib||0;
  $("emptyState").classList.add("hidden");$("saveProject").disabled=false;$("saveState").textContent="已保存";
  renderProjects();renderTrackCards();renderEditor();renderReleases();showTab(S.tab);updateBadge();
}
function renderTrackCards(){
  if(!S.project)return;
  $("trackCount").textContent=`${pad(S.project.tracks.length)} / 04`;$("addTrack").disabled=S.project.tracks.length>=4;
  $("removeTrack").disabled=S.project.tracks.length<=1;
  const root=$("trackList");root.replaceChildren();
  S.project.tracks.forEach((t,index)=>{
    const button=document.createElement("button");button.className="track-card"+(index===S.track?" active":"");
    button.innerHTML=`<span class="track-number">${pad(index+1)}</span>${t.cover?`<img class="track-art" src="${esc(assetURL(t.cover))}" alt="">`:'<span class="track-art"></span>'}<span class="track-copy"><strong>${esc(t.title||"未命名曲目")}</strong><small>${esc(t.artist||"等待导入素材")}</small></span><span class="track-state">${t.audio&&t.cover?"READY":"DRAFT"}</span>`;
    button.onclick=guarded(async()=>{collect();S.track=index;renderTrackCards();renderEditor();selectRelease(S.releaseId);});
    root.append(button);
  });
}
function renderEditor(){
  $("lyricAnalysis").textContent="";
  const t=track();$("editingTitle").textContent=`编辑 TRACK ${pad(S.track+1)}`;$("trackTitle").value=t.title||"";$("trackArtist").value=t.artist||"";
  $("moveTrackUp").disabled=S.track===0;$("moveTrackDown").disabled=S.track===S.project.tracks.length-1;
  $("coverFit").value=t.cover_fit||"cover";$("cropX").value=t.crop?.[0]??.5;$("cropY").value=t.crop?.[1]??.5;$("lyricOffset").value=t.lyric_offset_ms||0;
  const assets=[["audio","♫","歌曲音频","MP3 / WAV / FLAC / AAC / 视频音轨","audio/*,video/*,.pcm"],["cover","▧","专辑封面","JPG / PNG / BMP / WEBP","image/*"],["mv","▷","音乐视频（可选）","MP4 / MKV / MOV / WEBM","video/*"],["lyrics","≡","歌词（可选）","LRC / SRT · 可手动校准时间",".lrc,.srt,.txt"]];
  $("assetRows").replaceChildren();
  for(const [key,symbol,title,hint,accept] of assets){
    const row=document.createElement("div");row.className="asset-row";
    row.innerHTML=`<span class="asset-symbol">${symbol}</span><span class="asset-description"><strong>${title}</strong><small>${esc(t[key]?"已导入 · "+(t[key+"_name"]||t[key].split("/").pop()):hint)}</small></span><button data-action="import">${t[key]?"替换":"导入"}</button>${t[key]?'<button class="clear-asset" data-action="clear" aria-label="清除素材">×</button>':""}`;
    row.querySelector('[data-action="import"]').onclick=()=>{S.pendingAsset=key;$("assetInput").accept=accept;$("assetInput").value="";$("assetInput").click();};
    const clear=row.querySelector('[data-action="clear"]');if(clear)clear.onclick=()=>{t[key]="";changed();renderEditor();renderTrackCards();};
    $("assetRows").append(row);
  }
  updateCrop();loadLyricText();
}
function updateCrop(){const t=track();$("coverThumb").src=t.cover?assetURL(t.cover):"";$("coverThumb").style.visibility=t.cover?"visible":"hidden";$("coverThumb").style.objectPosition=`${Number($("cropX").value)*100}% ${Number($("cropY").value)*100}%`;$("coverThumb").style.objectFit=$("coverFit").value;}
async function loadLyricText(){
  const current=track(),projectId=S.projectId;
  if(typeof current.lyric_draft==="string"){$("lyricsText").value=current.lyric_draft;$("lyricStatus").textContent="歌词草稿已保留，请保存为素材后构建。";S.lyricsDirty=true;return;}
  $("lyricsText").value="";$("lyricStatus").textContent="";S.lyricsDirty=false;
  if(!current.lyrics)return;
  const path=current.lyrics;
  try{const response=await fetch(assetURL(path));if(!response.ok)throw new Error("无法读取歌词素材");const text=await response.text();if(S.projectId===projectId&&track()===current&&current.lyric_draft===undefined)$("lyricsText").value=text;}catch(e){toast(e.message);}
}
function upload(file,projectId=S.projectId){if(S.saving)throw new Error("项目正在保存，请稍后再导入素材");S.uploads++;return new Promise((resolve,reject)=>{
  const xhr=new XMLHttpRequest();xhr.open("POST",`/api/projects/${projectId}/upload?name=${encodeURIComponent(file.name)}`);xhr.setRequestHeader("X-Studio-Token",TOKEN);xhr.setRequestHeader("Content-Type","application/octet-stream");
  xhr.upload.onprogress=e=>{if(e.lengthComputable)$("saveState").textContent=`导入 ${Math.round(e.loaded/e.total*100)}%`;};
  xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{reject(new Error("素材导入失败"));return;}if(xhr.status>=400)reject(new Error(data.error));else resolve(data);};xhr.onerror=()=>reject(new Error("素材上传中断"));xhr.send(file);
}).finally(()=>{S.uploads--;});}
$("assetInput").addEventListener("change",guarded(async()=>{
  const file=$("assetInput").files[0];if(!file)return;const current=track(),projectId=S.projectId,key=S.pendingAsset;
  if(key==="lyrics"&&current.lyric_draft!==undefined&&!confirm("替换歌词素材并放弃当前歌词草稿？"))return;
  const result=await upload(file,projectId);
  if(S.projectId!==projectId)throw new Error("导入已完成，请回到原项目检查素材");
  current[key]=result.path;current[key+"_name"]=file.name;
  if(key==="lyrics")delete current.lyric_draft;
  if(key==="audio"&&!current.title){const text=file.name.replace(/\.[^.]+$/,"");current.title=text.slice(0,16);}
  changed();renderTrackCards();if(track()===current)renderEditor();
}));
async function save(){if(S.uploads||S.saving)throw new Error("导入或保存仍在进行，请稍后重试");collect();S.saving=true;try{const result=await api(projectURL(),"PUT",S.project);S.project=result.project;S.dirty=false;$("saveState").textContent="已保存";$("projectName").textContent=S.project.name;const latest=await api(projectURL());S.releases=latest.releases;const selected=S.releaseId;renderReleases();if(selected)selectRelease(selected);await refreshState();return result;}finally{S.saving=false;}}
$("saveLyrics").onclick=guarded(async()=>{
  const text=$("lyricsText").value;if(!text.trim())throw new Error("请先粘贴歌词或导入 LRC/SRT");
  const current=track(),projectId=S.projectId,format=text.includes("-->")?"srt":"lrc";const result=await upload(new File([text],`track${S.track+1}.${format}`,{type:"text/plain"}),projectId);if(S.projectId!==projectId)throw new Error("歌词已导入原项目，请重新打开原项目");current.lyrics=result.path;current.lyrics_name=`手动编辑 track${S.track+1}.${format}`;delete current.lyric_draft;S.lyricsDirty=false;changed();if(track()===current)renderEditor();$("lyricStatus").textContent="已保存为项目素材；生成镜像时检查拆行、字库与时间范围。";
});
$("lyricsText").oninput=()=>{track().lyric_draft=$("lyricsText").value;S.lyricsDirty=true;changed();$("lyricStatus").textContent="编辑尚未保存为素材，草稿会随项目保留。";};
$("analyzeLyrics").onclick=guarded(async()=>{
  if(S.uploads)throw new Error("请等待素材导入完成");collect();
  const project=S.projectId,slot=S.track;
  const result=await api(`${projectURL()}/analyze`,"POST",S.project);
  if(project!==S.projectId||slot!==S.track)return;
  const lines=result.tracks[slot].lyrics;
  $("lyricAnalysis").textContent=(result.passed?"候选字库容量通过":"候选字库超限，请减少字符")+"\n"+
    result.banks.map(x=>`字库组 ${x.bank+1}：${x.glyphs} / ${x.capacity} 字形`).join("\n")+"\n"+result.scope+"\n\n"+
    `本曲校准后 ${lines.length} 行：\n`+lines.map(([ms,text],i)=>`${String(i+1).padStart(3,"0")}  ${clock(ms/1000,true)}  ${text}`).join("\n");
});
document.addEventListener("input",event=>{if(event.target.closest(".editing-column"))$("lyricAnalysis").textContent="编辑已更改，请重新检查。";});
$("addTrack").onclick=()=>{collect();if(S.project.tracks.length>=4)return;S.project.tracks.push({title:"",artist:"",audio:"",cover:"",mv:"",lyrics:"",lyric_offset_ms:0,crop:[.5,.5],cover_fit:"cover"});S.track=S.project.tracks.length-1;changed();renderTrackCards();renderEditor();};
$("removeTrack").onclick=()=>{if(S.uploads){toast("素材正在导入，请等待完成后再移除曲目");return;}if(S.project.tracks.length<=1)return;if(!confirm("移除此曲目槽位？已导入的源素材会保留在项目中。"))return;S.project.tracks.splice(S.track,1);S.track=Math.max(0,S.track-1);changed();renderTrackCards();renderEditor();};
function moveTrack(delta){collect();const next=S.track+delta;if(next<0||next>=S.project.tracks.length)return;[S.project.tracks[S.track],S.project.tracks[next]]=[S.project.tracks[next],S.project.tracks[S.track]];S.track=next;changed();renderTrackCards();renderEditor();}
$("moveTrackUp").onclick=()=>moveTrack(-1);$("moveTrackDown").onclick=()=>moveTrack(1);
for(const id of ["trackTitle","trackArtist","lyricOffset","albumName","firmwareMode","imageSize","coverFit","cropX","cropY"]){$(id).addEventListener("input",()=>{collect();changed();if(["cropX","cropY","coverFit"].includes(id))updateCrop();if(["trackTitle","trackArtist"].includes(id))renderTrackCards();});}
function showTab(tab){S.tab=tab;document.querySelectorAll("[data-tab]").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));for(const id of ["editor","delivery","comparison"])$(id).classList.toggle("hidden",!S.project||tab!==id);}
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
function renderReleases(){
  const select=$("releaseSelect");select.replaceChildren();if(!S.releases.length){select.innerHTML='<option value="">尚无成功构建的镜像</option>';selectRelease(null);return;}
  for(const entry of S.releases){const option=document.createElement("option");option.value=entry.id;option.textContent=`${entry.release.name} · ${size(entry.release.image.bytes)} · ${entry.id.slice(0,8)}`;select.append(option);}
  selectRelease(S.releases[0].id);
}
function selectRelease(id){
  const entry=S.releases.find(x=>x.id===id);S.releaseId=entry?.id||null;S.release=entry?.release||null;$("releaseSelect").value=S.releaseId||"";
  for(const x of ["verifyImage","openOutput","downloadFrame","simulateWrite"])$(x).disabled=!S.release;
  $("playAudio").disabled=!S.release||S.track>=S.release.tracks.length;$("timeline").disabled=$("playAudio").disabled;
  if(!S.release){$("frame").classList.add("hidden");$("previewEmpty").classList.remove("hidden");$("audio").removeAttribute("src");$("releaseInfo").textContent="先在素材页生成镜像。";return;}
  const r=S.release,firmware=r.firmware;
  const compatible=firmware.status==="frozen"&&firmware.compatible;
  const label=compatible?"与当前冻结 ROM 一致":firmware.status==="requires_build"?"自定义 ROM · 需要编译并烧录配套固件":"候选文字与冻结固件不同 · 预览显示冻结 ROM";
  $("releaseInfo").innerHTML=`<div>${esc(r.name)} · ${r.tracks.length} 曲 · ${size(r.image.bytes)}</div><div class="mono">SHA-256<br>${esc(r.image.sha256)}</div><div class="${compatible?"":"warning"}">${esc(label)}</div>`;
  $("compatibility").className="compatibility "+(compatible?"good":"warning");
  $("compatibility").textContent=label+"。"+(firmware.differences?.length?firmware.differences.slice(0,4).map(x=>typeof x==="string"?x:JSON.stringify(x)).join("；"):"")+" 预览数字画面；SD 服务停帧与显示器缩放以实板为准。";
  $("previewSource").textContent="镜像回读 · "+(firmware.status==="requires_build"?"候选配套 ROM":"冻结 ROM");
  if(S.dirty||entry.project_matches===false){$("previewSource").textContent+=" · 当前项目修改尚未构建";$("compatibility").textContent+=" 当前项目配置与此镜像的构建快照不同，请重新生成镜像以应用编辑。";$("compatibility").className="compatibility warning";}
  const t=r.tracks[Math.min(S.track,r.tracks.length-1)];$("timeline").max=(t.duration_ms/1000)||1;$("timeline").value=0;$("durationTime").textContent=clock(t.duration_ms/1000);
  $("audio").pause();$("audio").src=`${releaseURL()}/audio?track=${Math.min(S.track,r.tracks.length-1)}&token=${encodeURIComponent(TOKEN)}`;
  $("playAudio").textContent="▶";renderFrame();updateDiskState();
}
async function renderFrame(){
  if(!S.release)return;
  if(S.frameBusy){S.frameAgain=true;return;}S.frameBusy=true;
  try{
    const query=new URLSearchParams({track:Math.min(S.track,S.release.tracks.length-1),time_ms:Math.round(Number($("timeline").value)*1000),page:S.page,
      background:$("backgroundMode").value,visual:$("visualMode").value,brightness:$("brightness").value,contrast:$("contrast").value,state:JSON.stringify(fixedDisplayState),token:TOKEN});
    const source=releaseURL();
    const identity={project:S.projectId,release:S.releaseId,image_sha256:S.release.image.sha256,rom_sha256:S.release.rom.files_sha256};
    const response=await fetch(`${source}/frame?${query}`);
    if(!response.ok){const error=await response.json();throw new Error(error.error);}
    if(source!==releaseURL()){S.frameAgain=true;return;}
    const blob=await response.blob();const url=URL.createObjectURL(blob);const previous=$("frame").dataset.url;
    $("frame").src=url;await $("frame").decode();$("frame").dataset.url=url;$("frame").classList.remove("hidden");$("previewEmpty").classList.add("hidden");if(previous)URL.revokeObjectURL(previous);
    const parameters=Object.fromEntries(query);delete parameters.token;parameters.state=JSON.parse(parameters.state);
    displayedFrameState={...identity,...parameters};
  }catch(error){toast(error.message);}finally{S.frameBusy=false;if(S.frameAgain){S.frameAgain=false;renderFrame();}}
}
$("applyDisplayState").onclick=guarded(async()=>{const state=JSON.parse($("displayState").value);if(!state||Array.isArray(state)||typeof state!=="object")throw new Error("请输入 JSON 对象");fixedDisplayState=state;$("audio").pause();await renderFrame();});
$("resetDisplayState").onclick=()=>{fixedDisplayState={};$("displayState").value="{}";renderFrame();};
$("timeline").oninput=()=>{$("currentTime").textContent=clock($("timeline").value,true);if(Number.isFinite($("audio").duration))$("audio").currentTime=Number($("timeline").value);renderFrame();};
$("audio").addEventListener("timeupdate",()=>{$("timeline").value=$("audio").currentTime;$("currentTime").textContent=clock($("audio").currentTime,true);renderFrame();});
$("audio").addEventListener("ended",()=>{$("playAudio").textContent="▶";});
$("playAudio").onclick=guarded(async()=>{const audio=$("audio");if(audio.paused){await audio.play();$("playAudio").textContent="Ⅱ";}else{audio.pause();$("playAudio").textContent="▶";}});
$("pagePlay").onclick=()=>{S.page="play";$("pagePlay").classList.add("active");$("pageBrowse").classList.remove("active");renderFrame();};
$("pageBrowse").onclick=()=>{S.page="browse";$("pageBrowse").classList.add("active");$("pagePlay").classList.remove("active");renderFrame();};
for(const id of ["visualMode","backgroundMode","brightness","contrast"])$(id).onchange=renderFrame;
$("releaseSelect").onchange=()=>selectRelease($("releaseSelect").value);
$("downloadFrame").onclick=()=>{if(!$("frame").src)return;const a=document.createElement("a");a.href=$("frame").src;a.download=`VinylFPGA_${S.page}_track${S.track+1}_${Math.round(Number($("timeline").value)*1000)}ms.png`;a.click();};
async function watchJob(id,title){
  S.job=id;$("jobPanel").classList.remove("hidden");$("jobTitle").textContent=title;$("jobLog").textContent="";$("buildImage").disabled=true;$("cancelJob").disabled=false;
  let last="";try{
    while(true){const j=await api(`/api/jobs/${id}`);$("jobTitle").textContent=`${title} · ${j.stage}`;$("jobProgress").value=j.fraction;
      if(j.message!==last){$("jobLog").textContent+=j.message+"\n";$("jobLog").scrollTop=$("jobLog").scrollHeight;last=j.message;}
      $("cancelJob").disabled=j.kind==="physical-write"||j.status!=="running";
      if(j.status!=="running"){
        if(j.status!=="done")throw new Error(j.error||"任务已取消");
        $("jobTitle").textContent=title+" · 完成";
        if(j.kind==="build"){const data=await api(projectURL());S.releases=data.releases;renderReleases();}
        if(j.kind==="physical-write")$("writeStatus").textContent="写入和完整回读 SHA-256 一致。请安全移除 SD 卡后上板确认新素材。";
        if(j.result?.passed)$("jobLog").textContent+="校验通过。\n";
        return j;
      }
      await new Promise(resolve=>setTimeout(resolve,500));
    }
  }finally{S.job=null;$("buildImage").disabled=false;$("cancelJob").disabled=true;}
}
$("buildImage").onclick=guarded(async()=>{
  if(S.project.tracks.some(t=>t.lyric_draft!==undefined))throw new Error("请先将各曲歌词草稿保存为素材，再生成镜像");
  await save();const d=await api(`${projectURL()}/build`,"POST",{});await watchJob(d.job,"生成镜像");
});
$("cancelJob").onclick=guarded(async()=>{if(S.job)await api(`/api/jobs/${S.job}/cancel`,"POST",{});});
$("verifyImage").onclick=guarded(async()=>{const d=await api(`${releaseURL()}/verify`,"POST",{});await watchJob(d.job,"重新校验");});
$("openOutput").onclick=guarded(()=>api("/api/open-output","POST",{project:S.projectId,release:S.releaseId}));
$("simulateWrite").onclick=guarded(async()=>{const d=await api("/api/disks/simulate","POST",{project:S.projectId,release:S.releaseId});await watchJob(d.job,"模拟写入与回读");});
$("refreshDisks").onclick=guarded(async()=>{
  $("diskInfo").textContent="正在读取 Windows 磁盘与卷信息…";const d=await api("/api/disks");S.disks=d.disks;S.disk=null;
  $("diskSelect").innerHTML='<option value="">选择具体 SD 卡 / 读卡器</option>';
  for(const disk of d.disks){const option=document.createElement("option");option.value=disk.Number;option.disabled=disk.IsSystem||disk.IsBoot||!['USB','SD','MMC'].includes(disk.BusType);option.textContent=`磁盘 ${disk.Number} · ${disk.FriendlyName} · ${size(disk.Size)} · ${disk.BusType}${option.disabled?"（不可写）":""}`;$("diskSelect").append(option);}
  $("diskInfo").textContent="请选择目标；系统盘与启动盘不可选择。";updateDiskState();
});
function updateDiskState(){
  const d=S.disk,r=S.release;$("diagnoseDisk").disabled=!d||!r;
  const confirm=d?`ERASE ${d.Number} ${d.Size}`:"ERASE <磁盘号> <容量字节数>";$("expectedConfirm").textContent=confirm;
  $("writeDisk").disabled=!d||!r||$("diskConfirm").value!==confirm||!!S.job;
}
$("diskSelect").onchange=()=>{S.disk=S.disks.find(d=>String(d.Number)===$("diskSelect").value)||null;$("diskConfirm").value="";$("diskInfo").textContent=S.disk?`型号：${S.disk.FriendlyName} · 序列号：${S.disk.SerialNumber||S.disk.UniqueId} · 容量：${S.disk.Size} 字节 · 逻辑扇区：${S.disk.LogicalSectorSize}`:"";updateDiskState();};
$("diskConfirm").oninput=updateDiskState;
$("diagnoseDisk").onclick=guarded(async()=>{const d=await api("/api/disks/diagnose","POST",{project:S.projectId,release:S.releaseId,disk:S.disk});await watchJob(d.job,"只读设备检查");});
$("writeDisk").onclick=guarded(async()=>{
  if(!S.disk||!S.release)return;
  const mismatch=S.release.firmware.status==="requires_build"||!S.release.firmware.compatible;
  const text=`将清除磁盘 ${S.disk.Number}（${S.disk.FriendlyName}，${size(S.disk.Size)}）的现有内容。`+(mismatch?"\n此镜像的文字/歌词需要对应配套固件；写卡不会更新 FPGA ROM。":"")+"\n确认开始写入并完整回读？";
  if(!confirm(text))return;
  const d=await api("/api/disks/write","POST",{project:S.projectId,release:S.releaseId,disk:S.disk,confirm:$("diskConfirm").value});await watchJob(d.job,"SD 卡写入与回读");
});
$("newProject").onclick=guarded(newProject);$("firstProject").onclick=guarded(newProject);$("saveProject").onclick=guarded(save);
$("exitStudio").onclick=guarded(async()=>{if(S.uploads||S.job)throw new Error("请等待当前导入或任务完成后退出");if((S.dirty||S.lyricsDirty)&&!confirm("仍有未保存修改，确定退出工具？"))return;await api('/api/shutdown','POST',{});S.dirty=false;S.lyricsDirty=false;toast("本机服务已退出，可以关闭此页面。");});
window.addEventListener("beforeunload",event=>{if(S.dirty||S.job||S.lyricsDirty){event.preventDefault();event.returnValue="";}});
let comparisonReference=null,comparisonBoard=null,comparisonReport=null,comparisonReferenceState=null;
let comparisonImportSerial=0;
function compareFrames(){
  if(!comparisonReference||!comparisonBoard)return;
  const result=PixelCompare.compareRgba(comparisonReference,comparisonBoard);
  $("differenceCanvas").getContext("2d").putImageData(new ImageData(result.difference,640,480),0,0);
  comparisonReport={width:640,height:480,total_pixels:result.totalPixels,different_pixels:result.differentPixels,
    maximum_channel_error:result.maximumChannelError,first_difference:result.firstDifference,
    verdict:result.differentPixels===0?"这对图像逐像素完全一致":"图像存在像素差异",scope:"仅针对本次导入的实板PNG与当前参考帧，不证明其他帧",
    reference:comparisonReferenceState};
  $("comparisonResult").textContent=JSON.stringify(comparisonReport,null,2);$("downloadComparison").disabled=false;
}
$("snapshotReference").onclick=guarded(async()=>{
  if(!S.release||!$("frame").naturalWidth)throw new Error("请先生成镜像，并等待预览画面加载");
  $("audio").pause();$("playAudio").textContent="▶";
  if(S.frameBusy)throw new Error("画面仍在生成，请稍候再取参考帧");
  const context=$("referenceCanvas").getContext("2d");context.drawImage($("frame"),0,0);
  comparisonReferenceState=structuredClone(displayedFrameState);
  comparisonReference=context.getImageData(0,0,640,480).data;compareFrames();
});
$("boardFrameInput").onchange=guarded(async()=>{
  const file=$("boardFrameInput").files[0];if(!file)return;
  const serial=++comparisonImportSerial;comparisonBoard=null;comparisonReport=null;
  $("downloadComparison").disabled=true;$("comparisonResult").textContent="正在读取 PNG 原始 RGB 像素……";
  if(file.type!=="image/png")throw new Error("请导入原始 PNG，避免有损压缩引入像素误差");
  const response=await fetch('/api/decode-png',{method:'POST',headers:{'X-Studio-Token':TOKEN,'Content-Type':'application/octet-stream'},body:file});
  if(!response.ok)throw new Error((await response.json()).error);
  const bytes=await response.arrayBuffer();if(serial!==comparisonImportSerial)return;
  comparisonBoard=new Uint8ClampedArray(bytes);
  $("boardCanvas").getContext("2d").putImageData(new ImageData(comparisonBoard,640,480),0,0);compareFrames();
});
$("downloadComparison").onclick=()=>{
  if(!comparisonReport)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(comparisonReport,null,2)],{type:"application/json"}));
  const a=document.createElement("a");a.href=url;a.download="VinylFPGA_逐像素对照.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  const b=document.createElement("a");b.href=$("differenceCanvas").toDataURL("image/png");b.download="VinylFPGA_像素差分.png";b.click();
};
refreshState().catch(error=>toast(error.message));
