'use strict';
let callerRows=[], personalityCatalog=null, editingPersonality=null, personalityDirty=false;
let editingContact=null, contactSourceCall=null, ownerContextDirty=false, contextNote=null;
let editingRevision=null;
let ownerContextRevision=null;
const openCallerCards=new Set(),personalityAssignmentDrafts=new Map();
const personalityDrafts=new Map();
const testContactDrafts=new Map();
const personalityFields={name:'personalityName',instructions:'personalityInstructions',context:'personalityContext',
  greeting:'personalityGreeting',role:'personalityRole',presentation_mode:'personalityPresentation',
  spoken_name:'personalitySpokenName',outbound_greeting:'personalityOutboundGreeting'};
function personalityBody() {return Object.fromEntries(Object.entries(personalityFields).map(([key,id])=>[key,$(id).value]));}
function presentationFields() {
  $('personalitySpokenName').disabled=$('personalityPresentation').value!=='owner_first_person';
  $('personalitySpokenName').placeholder=personalityCatalog?.owner_name || 'Your configured name';
}
function profileOptions() {return (personalityCatalog?.personalities || []).map(p=>({id:p.personality_id,label:p.name}));}
function currentProfile() {return personalityCatalog?.personalities.find(p=>p.personality_id===editingPersonality);}
function rememberPersonalityDraft() {
  if(!personalityDirty) return;
  personalityDrafts.set(editingPersonality || '', {...personalityBody(),revision:editingRevision});
}
function editPersonality(id) {
  rememberPersonalityDraft();editingPersonality=id || null;personalityDirty=false;
  const draft=personalityDrafts.get(id || ''), row=draft || currentProfile() || {};
  editingRevision=row.revision || null;
  for(const [key,field] of Object.entries(personalityFields)) $(field).value=row[key] || (key==='presentation_mode'?'assistant':'');
  presentationFields();
  $('personalityPicker').value=id || '';$('duplicatePersonality').disabled=!id;
  personalityDirty=!!draft;
  $('personalityState').textContent=draft?'Unsaved draft retained.':id?'Saved revision '+row.revision+' · changes apply to new calls.':'New profile · save before assigning it.';
}
function renderPersonalities(data) {
  if(!data) return;
  personalityCatalog=data;
  const edit=editingPersonality || data.default_personality_id;
  options($('defaultPersonality'),profileOptions(),data.default_personality_id);
  options($('personalityPicker'),profileOptions(),edit);
  if(!personalityDirty) editPersonality(edit);
  $('defaultPersonalityState').textContent='Saved default: '+(data.personalities.find(p=>p.personality_id===data.default_personality_id)?.name || '');
  refreshContactPickers();
}
async function refreshPeople() {
  const snapshot=await api('/live/bootstrap');renderPersonalities(snapshot.personalities);renderCallerBook(snapshot.caller_book);
}
$('personalityPicker').onchange=()=>editPersonality($('personalityPicker').value);
$('personalitiesPanel').addEventListener('toggle',async()=>{
  if(!$('personalitiesPanel').open) return;
  try {renderPersonalities(await api('/live/personalities'));} catch(e) {$('personalityState').textContent=e.message;}
});
$('newPersonality').onclick=()=>editPersonality(null);
$('reloadPersonality').onclick=async()=>{
  personalityDrafts.delete(editingPersonality || '');personalityDirty=false;
  try {await refreshPeople();$('personalityState').textContent='Loaded saved profile. Previous unsaved draft discarded.';}
  catch(e) {$('personalityState').textContent=e.message;}
};
for(const id of Object.values(personalityFields)) $(id).oninput=()=>{
  presentationFields();personalityDirty=true;$('personalityState').textContent='Unsaved edits · active calls are unaffected.';
};
$('personalityForm').onsubmit=async event=>{
  event.preventDefault();$('savePersonality').disabled=true;
  const body={...personalityBody(),revision:editingRevision};
  try {
    const r=await api('/live/personalities'+(editingPersonality?'/'+editingPersonality:''),{...writeOptions(body),method:editingPersonality?'PUT':'POST'});
    personalityDrafts.delete(editingPersonality || '');personalityDirty=false;editingPersonality=r.personality_id;
    await refreshPeople();$('personalityState').textContent='Saved '+r.name+' · revision '+r.revision+' · new calls only.';
  } catch(e) {$('personalityState').textContent='Not saved: '+e.message;} finally {$('savePersonality').disabled=false;}
};
$('duplicatePersonality').onclick=async()=>{
  const row=currentProfile();if(!row) return;
  $('duplicatePersonality').disabled=true;
  try {
    const r=await api('/live/personalities/'+row.personality_id+'/duplicate',{...writeOptions({name:(row.name+' copy').slice(0,80)}),method:'POST'});
    rememberPersonalityDraft();personalityDirty=false;editingPersonality=r.personality_id;await refreshPeople();
    $('personalityState').textContent='Independent copy saved. Tweak it below; default and callers are unchanged.';
  } catch(e) {$('personalityState').textContent=e.message;} finally {$('duplicatePersonality').disabled=false;}
};
$('saveDefaultPersonality').onclick=async()=>{
  try {await api('/live/default-personality',writeOptions({personality_id:$('defaultPersonality').value}));await refreshPeople();}
  catch(e) {$('defaultPersonalityState').textContent=e.message;}
};
function refreshContactPickers() {
  const contacts=callerRows.filter(r=>r.contact_id).map(r=>({id:r.contact_id,label:r.name+' · '+r.phone_number}));
  for(const id of ['previewContact','contextNoteContact']) {const v=$(id).value;options($(id),contacts,v,true);}
  const value=$('previewPersonality').value;options($('previewPersonality'),profileOptions(),value,true);
}
function addContactPersonality(entry,row) {
  if(!row.contact_id || !personalityCatalog) return;
  const label=document.createElement('label');label.className='editor-field';label.textContent='Personality';
  const select=document.createElement('select');select.setAttribute('aria-label','Personality for '+row.name);
  options(select,profileOptions(),personalityAssignmentDrafts.has(row.contact_id)?personalityAssignmentDrafts.get(row.contact_id):row.personality_id,true);label.append(select);
  const save=document.createElement('button');save.className='quiet';save.textContent='Save contact personality';
  const state=document.createElement('p');state.className='caption-note';state.setAttribute('role','status');
  const effective=personalityCatalog.personalities.find(p=>p.personality_id===(row.personality_id || personalityCatalog.default_personality_id));
  state.textContent='Saved: '+(row.personality_id?'':'Default → ')+(effective?.name || '');
  if(personalityAssignmentDrafts.has(row.contact_id)) state.textContent='Unsaved personality choice.';
  select.onchange=()=>{personalityAssignmentDrafts.set(row.contact_id,select.value);state.textContent='Unsaved personality choice.';};
  save.onclick=async()=>{
    save.disabled=true;
    try {await api('/live/contacts/'+row.contact_id+'/personality',writeOptions({personality_id:select.value || null}));personalityAssignmentDrafts.delete(row.contact_id);await refreshPeople();}
    catch(e) {state.textContent='Not saved: '+e.message;} finally {save.disabled=false;}
  };
  entry.append(label,save,state);
}
function openContact(row={},sourceCall=null) {
  editingContact=row.contact_id || null;contactSourceCall=sourceCall;
  $('callerBookPanel').open=true;$('contactForm').hidden=false;$('contactState').textContent='';
  $('contactFormTitle').textContent=editingContact?'Edit contact':'New contact';
  $('contactName').value=row.name || '';$('contactNumber').value=row.phone_number || '';
  $('contactNickname').value=row.preferred_name || '';$('contactLanguage').value=row.language || 'en';
  $('contactStyle').value=row.conversation_style || 'formal';$('contactForm').scrollIntoView({block:'center'});
}
function addContactTesting(entry,row) {
  if(!row.contact_id || !row.test_mode) return;
  const saved=row.test_mode,draft=testContactDrafts.get(row.contact_id);
  const box=document.createElement('fieldset');box.className='editor-card';
  const legend=document.createElement('legend');legend.textContent='Test contact';
  const label=document.createElement('label');label.className='check-setting';
  const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=draft?.enabled ?? saved.enabled;
  checkbox.setAttribute('aria-label','Test contact for '+row.name);label.append(checkbox,'Trust this contact for cooperative testing');
  const note=document.createElement('p');note.className='caption-note';
  note.textContent='New calls only. Allows general conversation and harmless supported tests using available approved information. Server permissions remain enforced.';
  const instructionLabel=document.createElement('label');instructionLabel.className='editor-field';instructionLabel.textContent='Extra test instructions';
  const instructions=document.createElement('textarea');instructions.rows=6;instructions.maxLength=3000;
  instructions.setAttribute('aria-label','Test instructions for '+row.name);
  instructions.value=draft?.instructions ?? (saved.instructions || saved.default_instructions);instructionLabel.append(instructions);
  instructionLabel.hidden=!checkbox.checked;
  const state=document.createElement('p');state.className='caption-note';state.setAttribute('role','status');
  state.textContent=draft?'Unsaved test settings.':saved.enabled?(saved.active_for_number?'Saved: test mode on for new calls.':'Saved flag needs resaving: the number changed or is duplicated.'):'Saved: ordinary contact.';
  function changed() {instructionLabel.hidden=!checkbox.checked;testContactDrafts.set(row.contact_id,{enabled:checkbox.checked,instructions:instructions.value});state.textContent='Unsaved test settings · click Save test settings.';}
  checkbox.onchange=changed;instructions.oninput=changed;
  const save=document.createElement('button');save.className='quiet';save.textContent='Save test settings';
  save.onclick=async()=>{
    save.disabled=true;
    try {await api('/live/contacts/'+row.contact_id+'/test-mode',writeOptions({enabled:checkbox.checked,instructions:instructions.value}));testContactDrafts.delete(row.contact_id);await refreshPeople();}
    catch(e) {state.textContent='Not saved: '+e.message;} finally {save.disabled=false;}
  };
  box.append(legend,label,note,instructionLabel,save,state);entry.append(box);
}
$('newContact').onclick=()=>openContact();
$('contactSearch').oninput=()=>renderCallerBook(callerRows);
$('cancelContact').onclick=()=>{$('contactForm').hidden=true;};
$('saveCaller').onclick=()=>{
  if(!call?.phone_number) return;
  const existing=callerRows.filter(r=>r.contact_id && r.phone_number===call.phone_number);
  if(existing.length>1) {$('callerBookPanel').open=true;$('languageError').textContent='Multiple contacts use this number. Edit the correct saved contact.';return;}
  openContact(existing[0] || {phone_number:call.phone_number,name:call.direction==='outbound'?call.recipient:''},call.call_id);
};
$('contactForm').onsubmit=async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  const body={name:$('contactName').value,phone_number:$('contactNumber').value,preferred_name:$('contactNickname').value,
    preferred_language:$('contactLanguage').value,conversation_style:$('contactStyle').value};
  if(editingContact) body.contact_id=editingContact;
  if(contactSourceCall && body.phone_number===call?.phone_number) body.source_call_id=contactSourceCall;
  try {const saved=await api('/live/contacts',writeOptions(body));openCallerCards.add(saved.contact_id);$('contactForm').hidden=true;await refreshPeople();}
  catch(e) {$('contactState').textContent='Not saved: '+e.message;} finally {button.disabled=false;}
};
async function loadOwnerContext() {
  const data=await api('/live/owner-context');
  if(!ownerContextDirty) {$('ownerBackground').value=data.profile.context;ownerContextRevision=data.profile.updated_at;}
  $('ownerContextSnapshot').textContent=JSON.stringify(data,null,2);
  const container=$('contextNotes');container.replaceChildren();
  for(const [kind,rows] of [['expected',data.expected_calls],['fact',data.knowledge.facts]]) for(const row of rows) {
    const card=document.createElement('div');card.className='editor-card';
    const title=document.createElement('strong');title.textContent=(kind==='expected'?'Expected call · ':'Fact · ')+(row.title || row.topic);
    const text=document.createElement('p');text.textContent=row.context || row.text;text.className='message-body';
    const expiry=document.createElement('p');expiry.className='caption-note';expiry.textContent='Expires '+new Date(row.expires_at).toLocaleString()+(row.contact_name?' · '+row.contact_name:'');
    const edit=document.createElement('button');edit.className='quiet';edit.textContent='Edit';edit.onclick=()=>openContextNote(kind,row);
    const retire=document.createElement('button');retire.className='quiet';retire.textContent='Retire';
    retire.onclick=async()=>{retire.disabled=true;try {await api('/live/'+(kind==='expected'?'expected-calls/':'facts/')+(row.context_id || row.fact_id)+'/retire',{method:'POST',headers:{'X-CSRF-Token':csrf}});await loadOwnerContext();} catch(e) {$('ownerContextState').textContent=e.message;retire.disabled=false;}};
    card.append(title,text,expiry,edit,retire);container.append(card);
  }
}
$('ownerContextPanel').addEventListener('toggle',()=>{if($('ownerContextPanel').open) loadOwnerContext().catch(e=>{$('ownerContextState').textContent=e.message;});});
$('reloadOwnerContext').onclick=async()=>{ownerContextDirty=false;try {await loadOwnerContext();$('ownerContextState').textContent='Loaded current saved context.';} catch(e) {$('ownerContextState').textContent=e.message;}};
for(const id of ['ownerBackground']) $(id).oninput=()=>{ownerContextDirty=true;$('ownerContextState').textContent='Unsaved context edits.';};
$('ownerContextForm').onsubmit=async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try {await api('/live/owner-context',writeOptions({context:$('ownerBackground').value,expected_updated_at:ownerContextRevision}));ownerContextDirty=false;await loadOwnerContext();$('ownerContextState').textContent='Saved. Active incoming calls are notified to refresh facts; their presentation is unchanged.';}
  catch(e) {$('ownerContextState').textContent='Not saved: '+e.message;} finally {button.disabled=false;}
};
function localDateTime(value) {const d=new Date(value);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function openContextNote(kind,row={}) {
  contextNote={kind,...row};$('contextNoteForm').hidden=false;$('contextNoteState').textContent='';
  $('contextNoteHeading').textContent=kind==='expected'?'Expected call':'Approved fact';
  $('contextNoteTitle').value=row.title || row.topic || '';$('contextNoteText').value=row.context || row.text || '';
  $('contextNoteNumber').value=row.phone_number || '';$('contextNoteContact').value=row.contact_id || '';
  $('contextNoteSource').value=row.source || 'Owner portal';
  $('contextNoteExpiry').value=localDateTime(row.expires_at || Date.now()+24*60*60*1000);
  $('contextNoteText').maxLength=kind==='expected'?1200:600;
  $('contextNoteNumber').parentElement.hidden=kind!=='expected';
  $('contextNoteContact').parentElement.hidden=kind!=='fact';$('contextNoteSource').parentElement.hidden=kind!=='fact';
  $('contextNoteForm').scrollIntoView({block:'center'});
}
$('newExpected').onclick=()=>openContextNote('expected');$('newFact').onclick=()=>openContextNote('fact');
$('cancelContextNote').onclick=()=>{$('contextNoteForm').hidden=true;};
$('contextNoteForm').onsubmit=async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  const expiry=new Date($('contextNoteExpiry').value).toISOString(),kind=contextNote.kind;
  const body=kind==='expected'?{title:$('contextNoteTitle').value,context:$('contextNoteText').value,phone_number:$('contextNoteNumber').value,expires_at:expiry}:
    {topic:$('contextNoteTitle').value,text:$('contextNoteText').value,source:$('contextNoteSource').value,contact_id:$('contextNoteContact').value || null,expires_at:expiry,approved_for_callers:true};
  if(contextNote.context_id) body.context_id=contextNote.context_id;if(contextNote.fact_id) body.fact_id=contextNote.fact_id;
  try {await api('/live/'+(kind==='expected'?'expected-calls':'facts'),writeOptions(body));$('contextNoteForm').hidden=true;await loadOwnerContext();}
  catch(e) {$('contextNoteState').textContent='Not saved: '+e.message;} finally {button.disabled=false;}
};
async function showInstructions(forSelected) {
  if(forSelected && !selected) {$('instructionsState').textContent='Select a call first.';return;}
  const body=forSelected?{call_id:selected}:{contact_id:$('previewContact').value || null,personality_id:$('previewPersonality').value || null};
  $('instructionsState').textContent='Building preview locally…';
  try {
    const result=await api('/live/instructions-preview',{...writeOptions(body),method:'POST'});
    $('instructionsState').textContent=result.basis+' '+result.policy;
    const output=$('instructionsOutput');output.replaceChildren();
    for(const [label,value] of [['Personality, presentation and answering',{personality:result.personality,presentation:result.presentation,answering:result.answering,test_mode:result.test_mode}],
      ['Effective greeting',result.greeting],
      ['Voice instructions',result.voice_instructions],['Tool planner instructions',result.planner_instructions],
      ['One-time startup input',result.startup_input],['Tool definitions',result.tool_definitions],['Caller context',result.context],['Shared owner context',result.owner_context]]) {
      const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=label;
      const text=document.createElement('pre');text.className='instruction-preview';text.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);
      details.append(summary,text);output.append(details);
    }
  } catch(e) {$('instructionsState').textContent='Preview failed: '+e.message;}
}
$('previewNewCall').onclick=()=>showInstructions(false);$('previewSelectedCall').onclick=()=>showInstructions(true);
