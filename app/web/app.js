'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['completed','failed','busy','no_answer','cancelled']);
let selected = new URL(location.href).searchParams.get('call_id'), call = null, calls = [], source, lastSeen = 0, pending = false;
let seen = new Set(), turns = [], buffered = [], loadVersion = 0;
let inboxCursor = null;
let ownerContactEnabled=false;
let humanCaptions=new Map();
let recordingRenderKey='';
const contactDrafts=new Map();
const csrf = document.querySelector('meta[name="csrf-token"]').content;
function connection(text, healthy=false) { $('connectionText').textContent=text; $('connection').className='connection '+(healthy?'live':'warn'); }
async function api(path, options={}) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),path.endsWith('/generate')?40000:20000);
  let response;
  try { response=await fetch(path, {credentials:'same-origin', cache:'no-store', ...options, signal:controller.signal}); }
  finally {clearTimeout(timer);}
  if (response.status === 401) { location.replace('/live/login'); throw new Error('Browser pairing expired'); }
  if (!response.ok) { const data=await response.json().catch(()=>({})); throw new Error(Array.isArray(data.detail)?data.detail.map(e=>e.msg).join('; '):(data.detail || 'Request failed. Try again.')); }
  return response.status === 204 ? null : response.json();
}
function clock(seconds) { return Math.floor(seconds/60)+':'+String(Math.floor(seconds%60)).padStart(2,'0'); }
function renderCallerBook(rows=[]) {
  callerRows=rows;
  refreshContactPickers();
  const target=$('callerBook'); target.replaceChildren();
  if (!rows.length) {const note=document.createElement('p');note.textContent='No saved caller preferences yet.';target.append(note);return;}
  const query=$('contactSearch').value.toLocaleLowerCase().trim();
  rows.filter(row=>!query || [row.name,row.preferred_name,row.phone_number].join(' ').toLocaleLowerCase().includes(query)).forEach((row,index)=>{
    const entry=document.createElement('details');entry.className='caller-entry';
    const key=row.contact_id || row.phone_number;entry.open=openCallerCards.has(key);
    entry.ontoggle=()=>{if(entry.open) openCallerCards.add(key);else openCallerCards.delete(key);};
    const title=document.createElement('strong');title.textContent=row.name || 'Incoming caller';
    const number=document.createElement('p');number.className='caption-note';number.textContent=row.phone_number || 'No phone number';
    const label=document.createElement('label');label.htmlFor='caller-language-'+index;label.textContent='Language code';
    const input=document.createElement('input');input.id=label.htmlFor;input.value=row.language || 'en';
    input.maxLength=12;input.pattern='[a-z]{2,3}(-[A-Z]{2})?';input.required=true;input.autocomplete='off';input.spellcheck=false;
    input.setAttribute('aria-label','Language for '+(row.name || row.phone_number));
    const button=document.createElement('button');button.className='quiet';button.textContent='Save';button.disabled=!row.phone_number;
    button.onclick=async()=>{
      if (!input.reportValidity()) return;
      button.disabled=true;$('languageError').textContent='';
      try {await api('/live/caller-language',{method:'PUT',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
        body:JSON.stringify({phone_number:row.phone_number,language:input.value})});button.textContent='Saved';}
      catch(error) {$('languageError').textContent=error.message;}
      finally {button.disabled=!row.phone_number;}
    };
    const edit=document.createElement('div');edit.className='language-edit';edit.append(label,input,button);
    const editContact=document.createElement('button');editContact.className='quiet';editContact.textContent=row.contact_id?'Edit contact':'Save as contact';
    editContact.onclick=()=>openContact(row);
    const summary=document.createElement('summary');summary.append(title,number);
    entry.append(summary,edit,editContact);addContactTesting(entry,row);addContactPersonality(entry,row);addContactAnswering(entry,row);target.append(entry);
  });
}
function showCall() {
  showRecordings();
  $('saveCaller').disabled=!call?.phone_number || !!call?.forwarding?.caller_identity_ambiguous;
  $('status').textContent=call ? call.status.replaceAll('_',' ') : '—';
  $('status').className='badge'+(call && !terminal.has(call.status)?' active':'');
  $('objective').textContent=call?.objective || 'Select a call to view its conversation.';
  $('testModeState').textContent=call?.test_mode?.enabled?'Test contact · additional testing instructions are active for this call.':'';
  $('hangup').disabled=!call || terminal.has(call.status) || pending;
  $('hangup').textContent=pending?'Ending call…':'Hang up call';
  $('reachOwner').disabled=!ownerContactEnabled || !call || call.status!=='in_progress' || !!call.owner_transfer || pending;
  const transfer=call?.owner_transfer;
  $('transferState').textContent=transfer?'Owner contact: '+transfer.status.replaceAll('_',' ')+
    (['conference','bridge'].includes(transfer.strategy)?' · Transcription: '+transfer.transcription_state+' · '+
      (transfer.mode==='whisper'?'Private coaching on':transfer.mode==='transcript'?'Coaching off':'Choose mode when answering'):
      ' · Captions pause during human conversation.')+(transfer.error_code?' · '+transfer.error_code.replaceAll('_',' '):''):'';
  if (call) {
    let duration=call.duration_seconds;
    if (!terminal.has(call.status) && call.started_at) duration=Math.max(0,(Date.now()-Date.parse(call.started_at))/1000);
    $('duration').textContent=clock(duration);
  }
}
function showRecordings() {
  const data=call?.audio_recording;
  $('forwardingState').textContent=call?.direction==='inbound'?
    (call.forwarding?.forwarded_from_owner_mobile?'Missed call · Forwarded from your mobile · Caller ID: '+(call.phone_number || 'unavailable'):
      call.forwarding?.detected?'Forwarded call · Caller ID: '+(call.phone_number || 'unavailable'):
      'Incoming call · Carrier forwarding details not supplied. Caller ID: '+(call.phone_number || 'unavailable')):'';
  $('stopRecording').hidden=!data?.policy?.recording_requested_at || !!data.policy.stop_requested_at || !call || terminal.has(call.status);
  const key=JSON.stringify([call?.call_id,data]);
  if(key===recordingRenderKey) return;
  recordingRenderKey=key;$('recordings').replaceChildren();
  const rows=data?.recordings || [];
  if(!rows.length) {
    const p=document.createElement('p');p.className='caption-note';
    p.textContent=data?.last_event?.state==='unavailable'?'Audio recording unavailable; the call can continue.':
      data?.policy?.recording_requested_at?'Recording requested. Waiting for the provider status.':'No audio recording for this call.';
    $('recordings').append(p);
  }
  for(const r of rows) {
    const p=document.createElement('p');p.className='caption-note';
    p.textContent=r.status==='completed'?'Saved audio · '+clock(r.duration_seconds || 0):'Audio: '+r.status.replaceAll('-',' ');
    $('recordings').append(p);
    if(r.status==='completed' && /^RE[0-9a-fA-F]{32}$/.test(r.recording_id)) {
      const audio=document.createElement('audio');audio.controls=true;audio.preload='none';
      audio.src='/live/calls/'+encodeURIComponent(call.call_id)+'/recordings/'+r.recording_id+'/audio';
      $('recordings').append(audio);
    }
  }
  if(data?.policy?.stop_requested_at) {
    const p=document.createElement('p');p.className='caption-note';
    p.textContent=data.policy.stop_confirmed_at?'Audio recording stopped. Earlier audio may remain saved.':'Recording stop requested; confirmation pending.';
    $('recordings').append(p);
  }
}
function showList() {
  $('calls').replaceChildren();
  for (const c of calls) {
    const option=document.createElement('option'); option.value=c.call_id;
    const date=new Date(c.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
    option.textContent=(c.direction==='inbound'?'Incoming · '+(c.phone_number || 'Unknown caller'):(c.recipient || c.phone_number))+' · '+date;
    $('calls').append(option);
  }
  if (!calls.length) { const option=document.createElement('option'); option.textContent='No calls yet'; $('calls').append(option); }
  $('calls').value=selected || '';
}
function addText(event) {
  if (event.source_event_id?.startsWith('human-')) {
    if (!event.text) return;
    let item=humanCaptions.get(event.source_event_id);
    if (!item) {
      if (!turns.length && !humanCaptions.size) $('transcript').replaceChildren();
      const div=document.createElement('div');div.className='turn '+event.speaker;
      const label=document.createElement('div');label.className='speaker';
      const name=document.createElement('span');name.textContent=event.speaker==='owner'?'You':(call?.direction==='inbound'?'Caller':'Recipient');
      const stamp=document.createElement('time');stamp.textContent=clock(event.timestamp);
      label.append(name,stamp);item=document.createElement('div');item.className='words';div.append(label,item);
      humanCaptions.set(event.source_event_id,item);$('transcript').append(div);
    }
    item.textContent=event.text;
    if ($('follow').checked) window.scrollTo({top:document.body.scrollHeight,behavior:'instant'});
    return;
  }
  if (event.source_event_id && seen.has(event.source_event_id)) return;
  if (event.source_event_id) seen.add(event.source_event_id);
  if (!event.text) return;
  if (!turns.length) $('transcript').replaceChildren();
  // Track both speakers independently: fragments can arrive late or overlap.
  let turn=[...turns].reverse().find(t=>t.speaker===event.speaker && event.timestamp>=t.start && event.timestamp-t.end<=2);
  if (!turn) {
    const div=document.createElement('div'); div.className='turn '+event.speaker;
    const label=document.createElement('div'); label.className='speaker';
    const name=document.createElement('span'); name.textContent=event.speaker==='assistant'?(call?.presentation?.mode==='owner_first_person'?'Voice representative · '+call.presentation.spoken_name:'Your assistant'):(call?.direction==='inbound'?'Caller':'Recipient');
    const stamp=document.createElement('time'); stamp.textContent=clock(event.timestamp);
    label.append(name,stamp); const words=document.createElement('div'); words.className='words'; div.append(label,words);
    turn={speaker:event.speaker,start:event.timestamp,end:event.end_timestamp,words}; turns.push(turn); $('transcript').append(div);
  }
  turn.words.append(document.createTextNode(event.text)); turn.end=Math.max(turn.end,event.end_timestamp);
  if ($('follow').checked) window.scrollTo({top:document.body.scrollHeight,behavior:'instant'});
}
async function loadCall(id) {
  const version=++loadVersion;
  selected=id; call=null; pending=false; seen=new Set(); turns=[]; humanCaptions=new Map(); $('transcript').replaceChildren();
  buffered=[];
  const empty=document.createElement('p'); empty.className='empty'; empty.textContent='Waiting for conversation…'; $('transcript').append(empty);
  showCall();
  if (!id) return;
  const data=await api('/live/bootstrap?call_id='+encodeURIComponent(id));
  renderVoice(data);
  renderPersonalities(data.personalities);
  // The selector may have changed during the request.
  if (selected!==id || version!==loadVersion) return;
  call=data.call; for (const fragment of call.transcript) addText(fragment); showCall();
  for (const event of buffered) applyEvent(event);
  buffered=[];
  const url=new URL(location.href); url.searchParams.set('call_id',id); history.replaceState(null,'',url);
  if (terminal.has(call.status)) $('actionState').textContent='Call ended. Transcript is saved.';
  else $('actionState').textContent='Hang up sends a direct command to the phone service.';
}
async function handleEvent(event) {
  lastSeen=Date.now(); connection('Live event stream connected',true);
  const data=JSON.parse(event.data);
  if (data.type.startsWith('call.supervisor_') || data.type==='call.context_update') {
    const snapshot=await api('/live/bootstrap'); renderRequests(snapshot);renderCallerBook(snapshot.caller_book);
  }
  if (data.type==='caller.language_changed') renderCallerBook((await api('/live/bootstrap')).caller_book);
  if (data.type.startsWith('message.')) {
    await refreshInbox();
    if (data.type==='message.received' || data.type==='message.updated') $('inbox').open=true;
  }
  if (data.type==='call.created') {
    const snapshot=await api('/live/bootstrap'); calls=snapshot.calls; renderRequests(snapshot);
    if (!call || terminal.has(call.status)) { selected=data.call_id; await loadCall(selected); }
    showList();
  }
  if (data.call_id!==selected) return;
  if(data.type==='call.recording') {
    const snapshot=await api('/live/bootstrap?call_id='+encodeURIComponent(selected));
    if(snapshot.call?.call_id===selected && call) {call.audio_recording=snapshot.call.audio_recording;showCall();}
  }
  if (!call) {buffered.push(data);return;}
  applyEvent(data);
}
function applyEvent(data) {
  if (data.type==='call.owner_transfer') {
    call.owner_transfer={...(call.owner_transfer || {}),...data.data};showCall();
  }
  if (data.type==='transcript.delta') addText(data.data);
  else if (data.type==='call.status') { Object.assign(call,data.data); showCall(); if (terminal.has(call.status)) $('actionState').textContent='Call ended. Transcript is saved.'; }
  else if (data.type==='call.hangup_requested') $('actionState').textContent='Outgoing audio muted. Waiting for Twilio to disconnect…';
  else if (data.type==='call.keypad') $('actionState').textContent='Keypad: '+data.data.digits+' · '+data.data.status;
}
async function start() {
  const data=await api('/live/bootstrap'); calls=data.calls;
  renderVoice(data);
  renderCallerBook(data.caller_book);
  renderInbox(data.inbox);
  renderRequests(data);
  selected=selected || calls.find(c=>!terminal.has(c.status))?.call_id || calls[0]?.call_id;
  showList(); await loadCall(selected);
  source=new EventSource('/live/events?cursor='+data.cursor);
  source.addEventListener('connected',()=>{lastSeen=Date.now();connection('Live event stream connected',true);});
  source.addEventListener('heartbeat',()=>{lastSeen=Date.now();connection('Live event stream connected',true);});
  source.addEventListener('call_event',event=>handleEvent(event).catch(error=>connection(error.message)));
  source.addEventListener('expired',()=>{source.close();connection('Pairing expired. Sign in again.');location.replace('/live/login');});
  source.onerror=()=>connection('Stream disconnected · reconnecting. Hang-up remains available.');
}
$('stopRecording').onclick=async()=>{
  if(!call) return;
  $('stopRecording').disabled=true;
  try {
    await api('/live/calls/'+encodeURIComponent(call.call_id)+'/recordings/stop',
      {method:'POST',headers:{'X-CSRF-Token':csrf}});
    const snapshot=await api('/live/bootstrap?call_id='+encodeURIComponent(selected));
    if(snapshot.call?.call_id===selected && call) {call.audio_recording=snapshot.call.audio_recording;showCall();}
  } catch(error) {connection(error.message);}
  finally {$('stopRecording').disabled=false;}
};

function renderInbox(data, append=false) {
  $('unreadCount').textContent=data.unread_count+' new';
  if (data.unread_count && !append) $('inbox').open=true;
  if (!append) $('messages').replaceChildren();
  for (const message of data.messages) {
    const card=document.createElement('article'); card.className='message-card '+message.status;
    const heading=document.createElement('h3'); heading.textContent=message.caller_name || 'Incoming caller';
    const stamp=document.createElement('p'); stamp.className='caption-note';
    stamp.textContent=new Date(message.created_at).toLocaleString()+' · '+message.status;
    const identity=document.createElement('p'); identity.className='caption-note';
    identity.textContent='Caller ID (unverified): '+(message.caller_id || 'Withheld/unknown')+
      (message.callback_number?' · Caller-supplied callback: '+message.callback_number:'');
    const quality=document.createElement('p'); quality.className='message-quality';
    quality.textContent=message.capture_method==='transcript_fallback'?
      'Unconfirmed caller transcript — review the conversation. Message capture may be incomplete.':
      'Saved by the assistant from the conversation. Caller details are unverified.';
    const body=document.createElement('p'); body.className='message-body'; body.textContent=message.body;
    const actions=document.createElement('div'); actions.className='message-actions';
    const open=document.createElement('button'); open.className='quiet'; open.textContent='View conversation';
    open.onclick=()=>loadCall(message.call_id).then(()=>{showList();$('calls').scrollIntoView({block:'center'});}).catch(e=>connection(e.message));
    actions.append(open);
    if (message.status==='new') {
      const read=document.createElement('button'); read.className='quiet'; read.textContent='Mark read';
      read.onclick=async()=>{
        read.disabled=true;
        try {await api('/live/messages/'+message.message_id+'/read',{method:'POST',headers:{'X-CSRF-Token':csrf}});await refreshInbox();}
        catch(e) {connection(e.message);read.disabled=false;}
      };
      actions.append(read);
    }
    card.append(heading,stamp,identity,quality,body,actions); $('messages').append(card);
  }
  if (!data.messages.length && !append) {const p=document.createElement('p');p.className='empty';p.textContent='No messages yet.';$('messages').append(p);}
  inboxCursor=data.next_cursor; $('olderMessages').hidden=!inboxCursor;
}
async function refreshInbox() {renderInbox(await api('/live/messages'));}
function renderRequests(data) {
  renderVoice(data);
  $('supervisorPanel').hidden=!data.concierge?.enabled || data.concierge.mode!=='concierge';
  $('supervisorState').textContent=data.concierge?.live_supervisor?
    'Verified live supervisor subscription is active. Replies still require actual client processing.':
    'No active live supervisor subscription. Fresh saved context can answer routine questions; other requests become messages. Automatic Rocky supervision is not connected.';
  const context=data.concierge;
  $('contextState').textContent=(context?.role?context.role+' · ':'')+(context?.availability_fresh?
    context.availability_windows+' cached free windows · expires '+new Date(context.availability_expires_at).toLocaleTimeString():
    'No fresh cached availability. A supervisor is needed for time options.');
  $('requests').replaceChildren();
  for (const request of data.requests || []) {
    const item=document.createElement('article'); item.className='message-card';
    const text=document.createElement('p');text.className='message-body';text.textContent=request.question;
    const stamp=document.createElement('p');stamp.className='caption-note';
    stamp.textContent='Waiting for verified help · expires '+new Date(request.expires_at).toLocaleTimeString();
    item.append(text,stamp);$('requests').append(item);
  }
}
let backendCatalog=null, backendDirty=false, recordingSaved=null, recordingDirty=false, noticeRows=[], noticeDirty=false;
const writeOptions=body=>({method:'PUT',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:JSON.stringify(body)});
function options(select,items,value,inherit=false) {
  select.replaceChildren();
  if(inherit) {const o=document.createElement('option');o.value='';o.textContent='Default / inherit';select.append(o);}
  for(const item of items) {const o=document.createElement('option');o.value=item.id;o.textContent=item.label+(item.configured===false?' · unavailable':'');o.disabled=item.configured===false;select.append(o);}
  select.value=value ?? '';
}
function selectionText(s) {
  const p=backendCatalog?.providers.find(p=>p.id===s.provider),m=p?.models.find(m=>m.id===s.model);
  const voice=m?.voices.find(v=>v.id===s.voice)?.label || s.voice;
  return (p?.label || s.provider)+' · '+(m?.label || s.model)+' · '+voice;
}
function populateAnswering(provider,model,voice) {
  const p=backendCatalog.providers.find(p=>p.id===provider);
  const d=backendCatalog.provider_defaults[provider];
  options($('answerModel'),p?.models || [],model || d.model);
  const m=p?.models.find(m=>m.id===$('answerModel').value);
  options($('answerVoice'),m?.voices || [],voice || d.voice);
}
function renderVoice(data) {
  if(data.voice_backend) {
    backendCatalog=data.voice_backend;
    $('providerAvailability').textContent=backendCatalog.providers.filter(p=>!p.configured).map(p=>p.id==='gemini'?'Gemini Live is unavailable until its API credentials and provider checks are complete.':p.label+' is unavailable until its server credentials and voice are configured.').join(' ');
    if(!backendDirty) {
      options($('voiceBackend'),backendCatalog.providers,backendCatalog.provider);
      populateAnswering(backendCatalog.provider,backendCatalog.model,backendCatalog.voice);
      $('voiceBackendState').textContent='Saved: '+selectionText(backendCatalog)+' · new calls';
    }
  }
  if(data.recording_settings) {
    recordingSaved=data.recording_settings;
    if(!recordingDirty) {
      $('recordNewCalls').checked=recordingSaved.recording_enabled;
      $('announceNewCalls').checked=recordingSaved.announcement_enabled;
      $('recordingSettingsState').textContent='Saved: recording '+(recordingSaved.recording_enabled?'on':'off')+' · announcement preference '+(recordingSaved.announcement_enabled?'on':'off')+' · new calls';
    }
  }
  if(data.recording_notices && !noticeDirty) {noticeRows=data.recording_notices;renderNotice();}
  if(data.models) $('modelState').textContent='Separate lookup model: '+data.models.backend;
  if(Object.hasOwn(data,'owner_contact_enabled')) ownerContactEnabled=!!data.owner_contact_enabled;
}
function answeringChanged() {backendDirty=true;$('voiceBackendState').textContent='Unsaved edits · saved: '+selectionText(backendCatalog);}
$('voiceBackend').onchange=()=>{populateAnswering($('voiceBackend').value);answeringChanged();};
$('answerModel').onchange=()=>{populateAnswering($('voiceBackend').value,$('answerModel').value);answeringChanged();};
$('answerVoice').onchange=answeringChanged;
$('refreshVoiceLibrary').onclick=async()=>{
  $('refreshVoiceLibrary').disabled=true;
  try {const r=await api('/live/voice/refresh-library',{method:'POST',headers:{'X-CSRF-Token':csrf}});backendCatalog=r;populateAnswering($('voiceBackend').value,$('answerModel').value,$('answerVoice').value);$('voiceBackendState').textContent='Library voices refreshed. '+(backendDirty?'Unsaved answering edits remain.':'Saved: '+selectionText(r));}
  catch(e) {$('voiceBackendState').textContent=e.message;} finally {$('refreshVoiceLibrary').disabled=false;}
};
$('saveVoiceBackend').onclick=async()=>{
  $('saveVoiceBackend').disabled=true;
  try {
    const result=await api('/live/voice/backend',writeOptions({provider:$('voiceBackend').value,model:$('answerModel').value,voice:$('answerVoice').value}));
    backendDirty=false;renderVoice({voice_backend:result});
    renderCallerBook((await api('/live/bootstrap')).caller_book);
  } catch(e) {$('voiceBackendState').textContent='Not saved: '+e.message;} finally {$('saveVoiceBackend').disabled=false;}
};
for(const id of ['recordNewCalls','announceNewCalls']) $(id).onchange=()=>{recordingDirty=true;$('recordingSettingsState').textContent='Unsaved edits · applies only after Save';};
$('saveRecordingSettings').onclick=async()=>{
  $('saveRecordingSettings').disabled=true;
  try {const saved=await api('/live/recording-settings',writeOptions({recording_enabled:$('recordNewCalls').checked,announcement_enabled:$('announceNewCalls').checked}));recordingDirty=false;renderVoice({recording_settings:saved});}
  catch(e) {$('recordingSettingsState').textContent='Not saved: '+e.message;} finally {$('saveRecordingSettings').disabled=false;}
};
function currentNotice() {return noticeRows.find(n=>n.language===$('noticeLanguage').value);}
function renderNotice() {
  const row=currentNotice(); if(!row) return;
  $('noticeText').value=row.draft?.text || row.text;
  $('noticeSavedText').textContent='Active text: '+row.text+(row.prepared?'':' · clip not prepared');
  $('previewActiveNotice').disabled=!row.prepared;
  $('noticeDraft').hidden=!row.draft;
  $('noticeDraftText').textContent=row.draft?'Prepared replacement (not active): '+row.draft.text:'';
}
$('noticeLanguage').onchange=()=>{noticeDirty=false;renderNotice();$('noticePreview').hidden=true;$('noticePreview').pause();};
$('noticeText').oninput=()=>{noticeDirty=true;$('noticeState').textContent='Unsaved text · Generate or Upload to prepare a replacement; then preview and activate';};
function previewNotice(asset) {
  const audio=$('noticePreview');audio.src='/live/recording-notices/'+$('noticeLanguage').value+'/audio'+(asset?'?asset_id='+encodeURIComponent(asset):'');audio.hidden=false;audio.play().catch(()=>{});
}
$('previewActiveNotice').onclick=()=>previewNotice(currentNotice()?.active_asset?.asset_id);
$('previewDraftNotice').onclick=()=>previewNotice(currentNotice()?.draft?.asset_id);
async function noticeAction(action,options) {
  const lang=$('noticeLanguage').value;
  for(const id of ['generateNotice','uploadNotice','activateNotice','noticeLanguage','noticeText']) $(id).disabled=true;
  $('noticeState').textContent=action==='generate'?'Generating and verifying speech…':'Saving…';
  try {noticeRows=await api('/live/recording-notices/'+lang+'/'+action,options);noticeDirty=false;renderNotice();$('noticeState').textContent=action==='activate'?'Replacement activated. New notices will use this clip.':'Replacement prepared. Preview it, then Activate replacement.';}
  catch(e) {$('noticeState').textContent='Previous active clip preserved. '+e.message;}
  finally {for(const id of ['generateNotice','uploadNotice','activateNotice','noticeLanguage','noticeText']) $(id).disabled=false;}
}
$('generateNotice').onclick=()=>noticeAction('generate',{...writeOptions({text:$('noticeText').value}),method:'POST'});
$('activateNotice').onclick=()=>noticeAction('activate',{...writeOptions({asset_id:currentNotice()?.draft?.asset_id}),method:'POST'});
$('uploadNotice').onclick=async()=>{
  const clip=$('noticeUpload').files[0];
  if(!clip || clip.size>2*1024*1024) {$('noticeState').textContent='Choose a WAV clip of at most 2 MiB.';return;}
  await noticeAction('upload?text='+encodeURIComponent($('noticeText').value),{method:'POST',headers:{'X-CSRF-Token':csrf,'Content-Type':'audio/wav'},body:clip});
};
function addContactAnswering(entry,row) {
  if(!row.contact_id || !backendCatalog || !row.answering) return;
  const box=document.createElement('div');box.className='settings-grid';
  const provider=document.createElement('select'),model=document.createElement('select'),voice=document.createElement('select');
  for(const [label,select] of [['Answering provider',provider],['Model',model],['Voice',voice]]) {const l=document.createElement('label');l.textContent=label;l.append(select);box.append(l);}
  const pref=contactDrafts.get(row.contact_id) || row.answering.preference;
  options(provider,backendCatalog.providers,pref.provider,true);
  function localOptions(m,v) {
    const p=backendCatalog.providers.find(p=>p.id===(provider.value || backendCatalog.provider));
    const d=backendCatalog.provider_defaults[p.id];
    options(model,p.models,m,true);model.disabled=!provider.value;
    options(voice,p.models.find(item=>item.id===(model.value || d.model))?.voices || [],v,true);voice.disabled=!provider.value;
  }
  localOptions(pref.model,pref.voice);
  const state=document.createElement('p');state.className='caption-note';state.setAttribute('role','status');
  state.textContent='Saved effective: '+selectionText(row.answering.effective);
  const dirty=()=>{
    contactDrafts.set(row.contact_id,{provider:provider.value || null,model:model.value || null,voice:voice.value || null});
    const p=provider.value || backendCatalog.provider,d=backendCatalog.provider_defaults[p];
    state.textContent='Unsaved · effective after Save: '+selectionText({provider:p,model:model.value || d.model,voice:voice.value || d.voice});
  };
  provider.onchange=()=>{localOptions(null,null);dirty();};model.onchange=()=>{localOptions(model.value,null);dirty();};voice.onchange=dirty;
  if(contactDrafts.has(row.contact_id)) dirty();
  const save=document.createElement('button');save.className='quiet';save.textContent='Save contact answering';
  save.onclick=async()=>{
    save.disabled=true;
    try {const r=await api('/live/contacts/'+row.contact_id+'/voice',writeOptions({provider:provider.value || null,model:model.value || null,voice:voice.value || null}));contactDrafts.delete(row.contact_id);state.textContent='Saved effective: '+selectionText(r.effective);renderCallerBook((await api('/live/bootstrap')).caller_book);}
    catch(e) {state.textContent='Not saved: '+e.message;} finally {save.disabled=false;}
  };
  entry.append(box,save,state);
}
$('reachOwner').onclick=async()=>{
  if (!call) return;
  $('reachOwner').disabled=true;
  try {call.owner_transfer=await api('/live/calls/'+selected+'/reach-owner',{method:'POST',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:JSON.stringify({reason:'The owner asked to join this call.'})});showCall();}
  catch(e) {$('transferState').textContent=e.message;}
};
$('olderMessages').onclick=async()=>{
  if (!inboxCursor) return;
  const button=$('olderMessages');button.disabled=true;
  try {renderInbox(await api('/live/messages?before='+encodeURIComponent(inboxCursor)),true);}
  catch(e) {connection(e.message);} finally {button.disabled=false;}
};
$('calls').onchange=()=>loadCall($('calls').value).catch(error=>connection(error.message));
$('hangup').onclick=async()=>{
  if (!call || pending) return;
  pending=true; showCall(); $('actionState').textContent='Sending hang-up command…';
  try {
    const result=await api('/live/calls/'+selected+'/hangup',{method:'POST',headers:{'X-CSRF-Token':csrf}});
    Object.assign(call,result); $('actionState').textContent=terminal.has(result.status)?'Call ended. Transcript is saved.':'Command sent. Waiting for disconnect confirmation…';
  } catch(error) { $('actionState').textContent=error.name==='AbortError'?'Hang-up request timed out. Disconnect is unconfirmed; retry or check Twilio.':error.message; }
  finally {pending=false;showCall();}
};
$('logout').onclick=async()=>{await api('/live/logout',{method:'POST',headers:{'X-CSRF-Token':csrf}});source?.close();location.replace('/live/login');};
$('pairMobile').onclick=async()=>{
  try { const result=await api('/live/pairing-code',{method:'POST',headers:{'X-CSRF-Token':csrf}});
    $('pairCode').textContent=result.code; $('pairing').hidden=false;
  } catch(error) {connection(error.message);}
};
$('closePairing').onclick=()=>{$('pairing').hidden=true;$('pairCode').textContent='';};
$('callerBookPanel').addEventListener('toggle',async()=>{
  if (!$('callerBookPanel').open) return;
  try {const data=await api('/live/bootstrap');renderPersonalities(data.personalities);renderCallerBook(data.caller_book);} catch(error) {$('languageError').textContent=error.message;}
});
setInterval(()=>{showCall(); if (lastSeen && Date.now()-lastSeen>12000) connection('No recent heartbeat · connection may be stale. Hang-up remains available.');},1000);
// Both deferred scripts must finish defining the control-page editors first.
document.addEventListener('DOMContentLoaded',()=>start().catch(error=>connection(error.message)),{once:true});
