'use strict';
let outboundSettings=null, outboundDirty=false, outboundBusy=false;
let outboundRequest=sessionStorage.getItem('phone-outbound-pending') || null;
const outboundFields=['outboundNumber','outboundRecipient','outboundCallerId','outboundObjective','outboundContext','outboundConstraints','outboundMinutes','outboundIvr','outboundContact','outboundPersonality','outboundProvider','outboundModel','outboundVoice'];
function outboundControls() {
  for(const id of [...outboundFields,'placeOutbound','useOutboundDefaults','saveOutboundDefaults']) $(id).disabled=outboundBusy || !!outboundRequest || !outboundSettings;
  $('checkOutbound').hidden=!outboundRequest;$('checkOutbound').disabled=outboundBusy;
  $('newOutbound').disabled=outboundBusy || !!outboundRequest || !outboundSettings;
  if(typeof renderCallModels==='function') renderCallModels();
}
function outboundRequestId() {
  if(typeof crypto.randomUUID==='function') return crypto.randomUUID();
  // randomUUID requires HTTPS; getRandomValues also supports the private HTTP portal.
  const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const value=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  return value.slice(0,8)+'-'+value.slice(8,12)+'-'+value.slice(12,16)+'-'+value.slice(16,20)+'-'+value.slice(20);
}
function renderOutboundSettings(data) {
  outboundSettings=data;
  const select=$('outboundCallerId'),chosen=select.value || 'default';
  select.replaceChildren();
  const def=document.createElement('option');def.value='default';
  const current=data.caller_ids.find(r=>r.id===data.defaults.caller_id);
  def.textContent='Default · '+(current?.label || data.defaults.caller_id)+(current?.number?' · '+current.number:'');select.append(def);
  for(const row of data.caller_ids) {const option=document.createElement('option');option.value=row.id;option.textContent=row.label+(row.number?' · '+row.number:' · not configured');option.disabled=!row.available;select.append(option);}
  select.value=chosen;
  refreshOutboundContacts();
  $('outboundDefaultNote').textContent=data.note;
}
function refreshOutboundContacts() {
  const contacts=$('outboundContact'),contactChoice=contacts.value;
  contacts.replaceChildren();const enter=document.createElement('option');enter.value='';enter.textContent='Enter a number';contacts.append(enter);
  for(const row of callerRows.filter(r=>r.phone_number)) {const option=document.createElement('option');option.value=row.phone_number;option.textContent=(row.name || row.phone_number)+' · '+row.phone_number;option.dataset.name=row.name || '';contacts.append(option);}
  contacts.value=contactChoice;
}
function useOutboundDefaults() {
  const d=outboundSettings.defaults;
  $('outboundObjective').value=d.objective;$('outboundContext').value=d.context;
  $('outboundConstraints').value=d.constraints.join('\n');$('outboundMinutes').value=d.max_duration_minutes;
  $('outboundIvr').checked=d.ivr_mode;$('outboundCallerId').value='default';outboundDirty=false;
}
async function loadOutboundSettings() {
  renderOutboundSettings(await api('/live/outbound/settings'));
  if(!outboundDirty && !outboundRequest) useOutboundDefaults();
  outboundControls();
}
$('outboundPanel').addEventListener('toggle',()=>{
  if($('outboundPanel').open) loadOutboundSettings().catch(e=>{$('outboundState').textContent=e.message;});
});
for(const id of outboundFields) $(id).addEventListener('input',()=>{outboundDirty=true;});
$('outboundContact').onchange=()=>{
  if(!$('outboundContact').value) return;
  $('outboundNumber').value=$('outboundContact').value;
  $('outboundRecipient').value=$('outboundContact').selectedOptions[0].dataset.name;
  outboundDirty=true;
};
function outboundBody() {
  return {phone_number:$('outboundNumber').value.trim(),recipient:$('outboundRecipient').value.trim(),
    objective:$('outboundObjective').value.trim(),context:$('outboundContext').value,
    constraints:$('outboundConstraints').value.split('\n').map(s=>s.trim()).filter(Boolean),
    max_duration_minutes:Number($('outboundMinutes').value),ivr_mode:$('outboundIvr').checked,
    caller_id:$('outboundCallerId').value || 'default',personality_id:$('outboundPersonality').value || null,
    voice_provider:$('outboundProvider').value || null,voice_model:$('outboundProvider').value?$('outboundModel').value:null,
    voice:$('outboundProvider').value?$('outboundVoice').value:null};
}
function forgetOutboundRequest() {outboundRequest=null;sessionStorage.removeItem('phone-outbound-pending');}
async function showOutboundResult(result) {
  selected=result.call_id;
  const snapshot=await api('/live/bootstrap?call_id='+encodeURIComponent(selected));
  calls=snapshot.calls;showList();await loadCall(selected);
  if(typeof showPage==='function') showPage('calls');
  const failed=result.status==='failed';
  $('outboundState').textContent=failed?'Call creation failed or is unconfirmed. Inspect this call and Twilio before placing another call.':
    'Call '+result.status.replaceAll('_',' ')+'. The live transcript and Hang up control are below.';
}
$('outboundForm').onsubmit=async event=>{
  event.preventDefault();if(outboundBusy || outboundRequest) return;
  const body=outboundBody();if(!body.objective) {$('outboundState').textContent='Enter a call objective.';return;}
  outboundRequest=outboundRequestId();sessionStorage.setItem('phone-outbound-pending',outboundRequest);
  outboundBusy=true;outboundControls();$('outboundState').textContent='Submitting one call…';
  try {
    const result=await api('/live/outbound/calls',{...writeOptions({...body,request_id:outboundRequest}),method:'POST'});
    await showOutboundResult(result);forgetOutboundRequest();
  } catch(e) {
    if([400,401,403,409,422].includes(e.status)) {
      forgetOutboundRequest();$('outboundState').textContent='Call not placed: '+e.message;
    } else $('outboundState').textContent='Submission needs checking: '+e.message+' No automatic retry. Use Check submitted call or inspect call history.';
  } finally {outboundBusy=false;outboundControls();}
};
$('checkOutbound').onclick=async()=>{
  if(!outboundRequest) return;
  outboundBusy=true;outboundControls();
  try {await showOutboundResult(await api('/live/outbound/requests/'+encodeURIComponent(outboundRequest)));forgetOutboundRequest();}
  catch(e) {$('outboundState').textContent=e.message+' Check again or inspect history; no call is retried.';}
  finally {outboundBusy=false;outboundControls();}
};
$('newOutbound').onclick=()=>{
  $('outboundForm').reset();outboundDirty=false;useOutboundDefaults();$('outboundState').textContent='New call draft.';
};
$('useOutboundDefaults').onclick=()=>{useOutboundDefaults();$('outboundDefaultsState').textContent='Saved defaults loaded into this draft.';};
$('saveOutboundDefaults').onclick=async()=>{
  outboundBusy=true;outboundControls();
  const {phone_number,recipient,personality_id,voice_provider,voice_model,voice,...body}=outboundBody();
  if(body.caller_id==='default') body.caller_id=outboundSettings.defaults.caller_id;
  try {renderOutboundSettings(await api('/live/outbound/settings',writeOptions({...body,revision:outboundSettings.defaults.revision})));
    $('outboundDefaultsState').textContent='Defaults saved. No call placed.';}
  catch(e) {$('outboundDefaultsState').textContent='Not saved: '+e.message;}
  finally {outboundBusy=false;outboundControls();}
};
outboundControls();
