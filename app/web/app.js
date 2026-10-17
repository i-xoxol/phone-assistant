'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['completed','failed','busy','no_answer','cancelled']);
let selected = new URL(location.href).searchParams.get('call_id'), call = null, calls = [], source, lastSeen = 0, pending = false;
let seen = new Set(), turns = [], buffered = [], loadVersion = 0;
let inboxCursor = null;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
function connection(text, healthy=false) { $('connectionText').textContent=text; $('connection').className='connection '+(healthy?'live':'warn'); }
async function api(path, options={}) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),20000);
  let response;
  try { response=await fetch(path, {credentials:'same-origin', cache:'no-store', ...options, signal:controller.signal}); }
  finally {clearTimeout(timer);}
  if (response.status === 401) { location.replace('/live/login'); throw new Error('Browser pairing expired'); }
  if (!response.ok) { const data=await response.json().catch(()=>({})); throw new Error(data.detail || 'Request failed. Try again.'); }
  return response.status === 204 ? null : response.json();
}
function clock(seconds) { return Math.floor(seconds/60)+':'+String(Math.floor(seconds%60)).padStart(2,'0'); }
function showCall() {
  $('status').textContent=call ? call.status.replaceAll('_',' ') : '—';
  $('status').className='badge'+(call && !terminal.has(call.status)?' active':'');
  $('objective').textContent=call?.objective || 'Select a call to view its conversation.';
  $('hangup').disabled=!call || terminal.has(call.status) || pending;
  $('hangup').textContent=pending?'Ending call…':'Hang up call';
  if (call) {
    let duration=call.duration_seconds;
    if (!terminal.has(call.status) && call.started_at) duration=Math.max(0,(Date.now()-Date.parse(call.started_at))/1000);
    $('duration').textContent=clock(duration);
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
  if (event.source_event_id && seen.has(event.source_event_id)) return;
  if (event.source_event_id) seen.add(event.source_event_id);
  if (!event.text) return;
  if (!turns.length) $('transcript').replaceChildren();
  // Track both speakers independently: fragments can arrive late or overlap.
  let turn=[...turns].reverse().find(t=>t.speaker===event.speaker && event.timestamp>=t.start && event.timestamp-t.end<=2);
  if (!turn) {
    const div=document.createElement('div'); div.className='turn '+event.speaker;
    const label=document.createElement('div'); label.className='speaker';
    const name=document.createElement('span'); name.textContent=event.speaker==='assistant'?'Your assistant':(call?.direction==='inbound'?'Caller':'Recipient');
    const stamp=document.createElement('time'); stamp.textContent=clock(event.timestamp);
    label.append(name,stamp); const words=document.createElement('div'); words.className='words'; div.append(label,words);
    turn={speaker:event.speaker,start:event.timestamp,end:event.end_timestamp,words}; turns.push(turn); $('transcript').append(div);
  }
  turn.words.append(document.createTextNode(event.text)); turn.end=Math.max(turn.end,event.end_timestamp);
  if ($('follow').checked) window.scrollTo({top:document.body.scrollHeight,behavior:'instant'});
}
async function loadCall(id) {
  const version=++loadVersion;
  selected=id; call=null; pending=false; seen=new Set(); turns=[]; $('transcript').replaceChildren();
  buffered=[];
  const empty=document.createElement('p'); empty.className='empty'; empty.textContent='Waiting for conversation…'; $('transcript').append(empty);
  showCall();
  if (!id) return;
  const data=await api('/live/bootstrap?call_id='+encodeURIComponent(id));
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
    const snapshot=await api('/live/bootstrap'); renderRequests(snapshot);
  }
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
  if (!call) {buffered.push(data);return;}
  applyEvent(data);
}
function applyEvent(data) {
  if (data.type==='transcript.delta') addText(data.data);
  else if (data.type==='call.status') { Object.assign(call,data.data); showCall(); if (terminal.has(call.status)) $('actionState').textContent='Call ended. Transcript is saved.'; }
  else if (data.type==='call.hangup_requested') $('actionState').textContent='Outgoing audio muted. Waiting for Twilio to disconnect…';
  else if (data.type==='call.keypad') $('actionState').textContent='Keypad: '+data.data.digits+' · '+data.data.status;
}
async function start() {
  const data=await api('/live/bootstrap'); calls=data.calls;
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
setInterval(()=>{showCall(); if (lastSeen && Date.now()-lastSeen>12000) connection('No recent heartbeat · connection may be stale. Hang-up remains available.');},1000);
start().catch(error=>connection(error.message));
