'use strict';
let characterBiographyKey=null,characterBiographyId=null,characterBiographyRevision=0,characterBiographyEntries=[],characterBiographyDirty=false;
const characterBiographyDrafts=new Map();
function keepCharacterBiographyDraft() {
  if(characterBiographyId && characterBiographyDirty) characterBiographyDrafts.set(characterBiographyId,{revision:characterBiographyRevision,entries:structuredClone(characterBiographyEntries)});
}
function markCharacterBiographyDirty() {
  characterBiographyDirty=true;$('characterBiographyState').textContent='Unsaved biography edits.';
}
function drawCharacterBiography() {
  const list=$('characterBiographyEntries');list.replaceChildren();
  for(const row of characterBiographyEntries) {
    const card=document.createElement('div');card.className='editor-card';
    const label=document.createElement('label');label.className='editor-field';label.textContent='Topic';
    const topic=document.createElement('input');topic.value=row.key.replaceAll('_',' ');topic.maxLength=80;topic.setAttribute('aria-label','Fact topic');
    topic.oninput=()=>{row.key=topic.value;markCharacterBiographyDirty();};label.append(topic);
    const detail=document.createElement('label');detail.className='editor-field';detail.textContent='Personal detail';
    const value=document.createElement('textarea');value.rows=3;value.maxLength=500;value.value=row.value;value.setAttribute('aria-label','Personal detail');
    value.oninput=()=>{row.value=value.value;markCharacterBiographyDirty();};detail.append(value);
    const enabledLabel=document.createElement('label');enabledLabel.className='follow';
    const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=row.enabled;enabled.setAttribute('aria-label','Use this detail');
    enabled.onchange=()=>{row.enabled=enabled.checked;markCharacterBiographyDirty();};enabledLabel.append(enabled,document.createTextNode(' Use this detail'));
    const source=document.createElement('p');source.className='caption-note';source.textContent=row.source==='character'?'Developed during conversation':'Provided or edited in settings';
    card.append(label,detail,enabledLabel,source);list.append(card);
  }
  if(!characterBiographyEntries.length) {const p=document.createElement('p');p.className='caption-note';p.textContent='No established details yet. Add known biography here; when development is enabled, missing details can be saved during conversations.';list.append(p);}
}
async function renderCharacterBiography(force=false) {
  const visible=$('personalityPresentation').value==='character';
  $('characterBiographyPanel').hidden=!visible;
  const id=editingPersonality,key=visible?String(id):null;
  if(!force && key===characterBiographyKey) return;
  keepCharacterBiographyDraft();characterBiographyKey=key;characterBiographyId=id;characterBiographyDirty=false;
  $('saveCharacterBiography').disabled=!id;$('addCharacterFact').disabled=!id;$('reloadCharacterBiography').disabled=!id;
  characterBiographyEntries=[];drawCharacterBiography();
  if(!visible) return;
  if(!id) {$('characterBiographyState').textContent='Save the character profile before adding its biography.';return;}
  const draft=characterBiographyDrafts.get(id);
  if(draft && !force) {characterBiographyRevision=draft.revision;characterBiographyEntries=structuredClone(draft.entries);characterBiographyDirty=true;drawCharacterBiography();$('characterBiographyState').textContent='Unsaved biography draft retained.';return;}
  $('saveCharacterBiography').disabled=true;$('addCharacterFact').disabled=true;
  $('characterBiographyState').textContent='Loading saved details…';
  try {
    const data=await api('/live/personalities/'+id+'/biography');
    if(key!==characterBiographyKey) return;
    characterBiographyRevision=data.revision;characterBiographyEntries=data.entries;drawCharacterBiography();
    $('saveCharacterBiography').disabled=false;$('addCharacterFact').disabled=false;
    $('characterBiographyState').textContent='Saved revision '+data.revision+' · '+(data.allow_invention?'Character development enabled.':'Character development off.');
  } catch(error) {if(key===characterBiographyKey) $('characterBiographyState').textContent=error.message;}
}
$('addCharacterFact').onclick=()=>{characterBiographyEntries.push({key:'',value:'',enabled:true,source:'owner'});markCharacterBiographyDirty();drawCharacterBiography();};
$('saveCharacterBiography').onclick=async()=>{
  const id=characterBiographyId;if(!id) return;
  $('saveCharacterBiography').disabled=true;
  try {
    const data=await api('/live/personalities/'+id+'/biography',{...writeOptions({revision:characterBiographyRevision,facts:characterBiographyEntries.map(({key,value,enabled})=>({key,value,enabled}))}),method:'PUT'});
    characterBiographyDrafts.delete(id);
    if(id!==characterBiographyId) return;
    characterBiographyDirty=false;characterBiographyRevision=data.revision;characterBiographyEntries=data.entries;drawCharacterBiography();
    $('characterBiographyState').textContent='Biography saved · revision '+data.revision+'.';
  } catch(error) {$('characterBiographyState').textContent='Not saved: '+error.message;}
  finally {if(id===characterBiographyId) $('saveCharacterBiography').disabled=false;}
};
$('reloadCharacterBiography').onclick=()=>{if(characterBiographyId) characterBiographyDrafts.delete(characterBiographyId);characterBiographyDirty=false;renderCharacterBiography(true);};
renderCharacterBiography();
