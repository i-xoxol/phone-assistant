'use strict';
const guidanceDrafts=new Map(),openGuidanceContacts=new Set();

function guidanceElement(tag,text,className='') {
  const element=document.createElement(tag);if(text) element.textContent=text;if(className) element.className=className;return element;
}
function guidanceField(label,control) {
  const field=guidanceElement('label',label,'editor-field');control.setAttribute('aria-label',label);field.append(control);return field;
}
function guidanceSelect(label,choices,value,change) {
  const select=document.createElement('select');
  for(const [id,text] of choices) {const option=guidanceElement('option',text);option.value=id;select.append(option);}
  select.value=value;select.onchange=()=>change(select.value);return guidanceField(label,select);
}
function localGuidanceTime(value) {
  if(!value) return '';const date=new Date(value);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
function addContactGuidance(entry,row) {
  if(!row.contact_id) return;
  const cid=row.contact_id,details=document.createElement('details'),summary=guidanceElement('summary','Instructions and facts');
  details.dataset.contactGuidance=cid;details.open=openGuidanceContacts.has(cid);
  const root=document.createElement('div');details.append(summary,root);entry.append(details);
  if(!guidanceDrafts.has(cid)) guidanceDrafts.set(cid,{loaded:false,loading:false,dirty:false,revision:0,entries:[]});
  const draft=guidanceDrafts.get(cid);
  async function load() {
    if(draft.loading) return;draft.loading=true;render();
    try {
      const saved=await api('/live/contacts/'+cid+'/guidance');
      draft.entries=saved.entries;draft.revision=saved.revision;draft.loaded=true;draft.dirty=false;draft.error='';
    } catch(error) {draft.error=error.message;}
    finally {draft.loading=false;render();}
  }
  function changed() {draft.dirty=true;state.textContent='Unsaved contact guidance · new calls only.';preview.disabled=true;}
  let state,preview;
  function render() {
    root.replaceChildren();
    root.append(guidanceElement('p','Instructions and facts stay with '+row.name+'. Choose all profiles or a selected set, for incoming calls, outgoing calls, or both. More-specific guidance overrides general guidance.','caption-note'));
    state=guidanceElement('p',draft.error || (draft.loading?'Loading saved guidance…':draft.dirty?'Unsaved contact guidance · new calls only.':'Saved revision '+draft.revision+' · new calls only.'),'caption-note');state.setAttribute('role','status');
    if(!draft.loaded) {
      root.append(state);if(!draft.loading) {const retry=guidanceElement('button','Load guidance','quiet');retry.type='button';retry.onclick=load;root.append(retry);}return;
    }
    const editor=document.createElement('fieldset');editor.className='guidance-editor';editor.disabled=!!draft.saving || draft.loading;
    for(const [kind,title,note] of [
      ['instruction','Instructions','Behavior for this contact. Opening words are optional and apply once at the beginning of the call.'],
      ['fact','Facts approved to share with this contact','Use neutral factual wording, one short fact per entry (250 UTF-8 bytes). Facts stay available until changed or disabled, with an optional expiry. They are not shared with other callers.']]) {
      const section=document.createElement('section');section.append(guidanceElement('h3',title),guidanceElement('p',note,'caption-note'));
      const rows=draft.entries.filter(item=>item.kind===kind);
      if(!rows.length) section.append(guidanceElement('p','No '+(kind==='instruction'?'instructions':'facts')+' saved.','caption-note'));
      for(const item of rows) {
        const card=guidanceElement('fieldset','','editor-card guidance-entry');card.dataset.entryId=item.entry_id;
        card.append(guidanceElement('legend',kind==='instruction'?'Instruction':'Approved fact'));
        const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=item.enabled;
        const enabledLabel=guidanceElement('label','Enabled','check-setting');enabledLabel.prepend(enabled);enabled.onchange=()=>{item.enabled=enabled.checked;changed();};card.append(enabledLabel);
        const titleInput=document.createElement('input');titleInput.value=item.title;titleInput.maxLength=120;titleInput.required=true;
        titleInput.oninput=()=>{item.title=titleInput.value;changed();};card.append(guidanceField('Title',titleInput));
        const text=document.createElement('textarea');text.rows=4;text.maxLength=kind==='fact'?250:3000;text.required=true;text.value=item.text;
        text.oninput=()=>{item.text=text.value;text.setCustomValidity(kind==='fact' && new TextEncoder().encode(text.value).length>250?'Each fact must fit 250 UTF-8 bytes. Split longer notes.':'');changed();};card.append(guidanceField(kind==='instruction'?'Instruction text':'Fact text',text));
        const profileBox=guidanceElement('div','','guidance-profile-options');profileBox.hidden=item.profile_scope!=='selected';
        for(const profile of personalityCatalog?.personalities || []) {
          const check=document.createElement('input');check.type='checkbox';check.value=profile.personality_id;check.checked=item.profile_ids.includes(profile.personality_id);
          const name=profile.spoken_name || (profile.presentation_mode==='owner_first_person'?personalityCatalog.owner_name:profile.presentation_mode==='character'?profile.name:personalityCatalog.assistant_name);
          const label=guidanceElement('label',profile.name+' · '+name,'check-setting');label.prepend(check);
          check.onchange=()=>{item.profile_ids=check.checked?[...item.profile_ids,profile.personality_id]:item.profile_ids.filter(id=>id!==profile.personality_id);changed();};profileBox.append(label);
        }
        card.append(guidanceSelect('Apply to profiles',[['all','All profiles · including future profiles'],['selected','Selected profiles']],item.profile_scope,value=>{
          item.profile_scope=value;if(value==='all') {item.profile_ids=[];for(const check of profileBox.querySelectorAll('input')) check.checked=false;}
          profileBox.hidden=value!=='selected';changed();
        }),profileBox);
        card.append(guidanceSelect('Call direction',[['both','Incoming and outgoing'],['inbound','Incoming only'],['outbound','Outgoing only']],item.direction,value=>{item.direction=value;changed();}));
        if(kind==='instruction') {
          const opening=document.createElement('textarea');opening.rows=2;opening.maxLength=1000;opening.value=item.greeting;
          opening.oninput=()=>{item.greeting=opening.value;changed();};card.append(guidanceField('Opening words (optional)',opening));
        } else {
          const expiry=document.createElement('input');expiry.type='datetime-local';expiry.value=localGuidanceTime(item.expires_at);
          expiry.oninput=()=>{item.expires_at=expiry.value?new Date(expiry.value).toISOString():null;changed();};card.append(guidanceField('Expires at (optional · your local time)',expiry));
          if(item.expires_at && Date.parse(item.expires_at)<=Date.now()) card.append(guidanceElement('p','Expired · not used for calls.','caption-note'));
        }
        const remove=guidanceElement('button','Remove '+(kind==='instruction'?'instruction':'fact'),'quiet');remove.type='button';
        remove.onclick=()=>{draft.entries=draft.entries.filter(e=>e!==item);changed();render();};card.append(remove);section.append(card);
      }
      const add=guidanceElement('button',kind==='instruction'?'Add instruction':'Add approved fact','quiet');add.type='button';
      add.onclick=()=>{draft.entries.push({entry_id:crypto.randomUUID(),kind,title:'',text:'',enabled:true,profile_scope:'all',profile_ids:[],direction:'both',greeting:'',expires_at:null});changed();render();};
      section.append(add);editor.append(section);
    }
    const actions=guidanceElement('div','','message-actions'),save=guidanceElement('button',draft.saving?'Saving…':'Save instructions and facts','quiet'),reload=guidanceElement('button','Reload saved guidance','quiet');
    save.type='button';reload.type='button';save.disabled=!!draft.saving || draft.loading;reload.disabled=!!draft.saving || draft.loading;
    save.onclick=async()=>{
      for(const input of editor.querySelectorAll('input,textarea,select')) if(!input.reportValidity()) return;
      if(draft.entries.some(e=>e.profile_scope==='selected' && !e.profile_ids.length)) {state.textContent='Choose at least one profile for each selected-profile entry.';return;}
      draft.saving=true;render();
      try {
        const saved=await api('/live/contacts/'+cid+'/guidance',writeOptions({revision:draft.revision,entries:draft.entries}));
        draft.entries=saved.entries;draft.revision=saved.revision;draft.dirty=false;draft.error='';
      } catch(error) {draft.error='Not saved: '+error.message;}
      finally {draft.saving=false;render();}
    };
    reload.onclick=()=>{draft.dirty=false;draft.loaded=false;load();};actions.append(save,reload);root.append(editor,actions,state);
    const previewBox=guidanceElement('details','','editor-card'),previewTitle=guidanceElement('summary','Preview saved guidance');previewBox.append(previewTitle);
    const choice=document.createElement('select');choice.setAttribute('aria-label','Preview profile');
    controlOptions(choice,profileOptions(),row.personality_id || personalityCatalog?.default_personality_id);
    let direction='inbound';
    previewBox.append(guidanceField('Preview profile',choice),guidanceSelect('Preview call direction',[['inbound','Incoming'],['outbound','Outgoing']],direction,value=>{direction=value;}));
    preview=guidanceElement('button','Show effective guidance','quiet');preview.type='button';preview.disabled=draft.dirty || !!draft.saving;
    const output=document.createElement('div');output.setAttribute('aria-live','polite');
    preview.onclick=async()=>{
      preview.disabled=true;output.replaceChildren(guidanceElement('p','Loading saved guidance…','caption-note'));
      try {
        const result=await api('/live/instructions-preview',{...writeOptions({contact_id:cid,personality_id:choice.value || null,direction}),method:'POST'});
        const effective=result.contact_guidance;output.replaceChildren(guidanceElement('p',result.personality.name+' · speaks as '+result.presentation.spoken_name+' · '+(direction==='inbound'?'incoming':'outgoing')));
        for(const [title,entries] of [['Instructions',effective.instructions],['Facts approved for this call',effective.facts]]) {
          output.append(guidanceElement('h4',title));
          if(!entries.length) output.append(guidanceElement('p','None for this selection.','caption-note'));
          for(const item of entries) output.append(guidanceElement('strong',item.title),guidanceElement('p',item.text));
        }
        if(effective.greeting) output.append(guidanceElement('strong','Opening words'),guidanceElement('p',effective.greeting));
      } catch(error) {output.replaceChildren(guidanceElement('p',error.message,'caption-note'));}
      finally {preview.disabled=draft.dirty;}
    };
    previewBox.append(guidanceElement('p','Save edits first. This preview does not place a call.','caption-note'),preview,output);root.append(previewBox);
  }
  details.ontoggle=()=>{
    if(details.open) {openGuidanceContacts.add(cid);if(!draft.loaded && !draft.loading) load();}
    else openGuidanceContacts.delete(cid);
  };
  render();if(details.open && !draft.loaded && !draft.loading) load();
}
if(callerRows.length) renderCallerBook(callerRows);
