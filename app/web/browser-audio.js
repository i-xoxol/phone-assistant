'use strict';
let liveAudio=null,liveAudioLoading=false,liveAudioChecked=0,liveAudioCheckedId=null,liveAudioStatus=null;
function renderLiveAudio() {
  const active=call?.status==='in_progress';
  const localTest=typeof browserTest!=='undefined' && browserTest?.id===selected;
  const current=liveAudio && !liveAudio.closed;
  $('listenLive').disabled=!active || !!current || localTest;
  $('joinLive').disabled=!current || !liveAudio.ready || liveAudio.joining || liveAudio.joined || !liveAudio.canJoin;
  $('liveAudioHint').textContent=localTest?'This tab is already the test caller. Open the call in another paired tab to listen or join.':
    !active?'Select a call while the assistant is connected.':
    liveAudioStatus?.available===false?'Live audio is available while the assistant voice bridge is connected. Ringing, keypad navigation and telephone handoffs are excluded.':
    'Listen to both sides live. Join shares your microphone with the caller; the assistant receives your separately labelled captions. Headphones recommended.';
  if(current) {
    $('liveAudioSession').hidden=false;
    $('liveAudioCall').textContent=liveAudio.label;
    $('liveAudioMute').hidden=!liveAudio.joined;
    $('liveAudioLeave').hidden=!liveAudio.joined && !liveAudio.joining;
    $('liveAudioMute').textContent=liveAudio.muted?'Unmute microphone':'Mute microphone';
    $('liveAudioMute').setAttribute('aria-pressed',String(liveAudio.muted));
  }
  if(active && (selected!==liveAudioCheckedId || Date.now()-liveAudioChecked>3000) && !liveAudioLoading) {
    liveAudioLoading=true;const id=selected;liveAudioCheckedId=id;liveAudioChecked=Date.now();
    api('/live/calls/'+id+'/live-audio').then(data=>{if(id===selected) liveAudioStatus=data;}).catch(()=>{})
      .finally(()=>{liveAudioLoading=false;});
  }
  // Selecting another call never moves a microphone to it.
  if(current && calls.some(c=>c.call_id===liveAudio.id && terminal.has(c.status))) stopLiveAudio('The call ended. Browser audio stopped.');
}
function liveAudioSend(session,message) {
  if(!session.closed && session.socket?.readyState===WebSocket.OPEN) session.socket.send(JSON.stringify(message));
}
function clearLiveAudioSource(session,source) {
  for(const node of session.playing.get(source) || []) {try{node.stop();}catch{}}
  session.playing.delete(source);session.next.delete(source);
}
function playLiveAudio(session,source,payload) {
  if(!['caller','assistant','owner'].includes(source)) return;
  const bytes=Uint8Array.from(atob(payload),c=>c.charCodeAt(0));
  const buffer=session.audio.createBuffer(1,bytes.length,8000),samples=buffer.getChannelData(0);
  bytes.forEach((byte,index)=>{const value=byte^255,magnitude=(((value&15)<<3)+132)<<((value&112)>>4);samples[index]=((value&128)?132-magnitude:magnitude-132)/32768;});
  const now=session.audio.currentTime;
  if((session.next.get(source)||0)-now>4) throw new Error('Live audio fell behind. Listen again to reconnect.');
  const node=session.audio.createBufferSource();node.buffer=buffer;node.connect(session.audio.destination);
  if(!session.playing.has(source)) session.playing.set(source,new Set());session.playing.get(source).add(node);
  node.onended=()=>session.playing.get(source)?.delete(node);
  const at=Math.max(now+.025,session.next.get(source)||0);node.start(at);session.next.set(source,at+buffer.duration);
}
function releaseLiveMicrophone(session) {
  session.joined=false;session.joining=false;session.muted=false;
  if(session.microphone) {session.microphone.port.onmessage=null;session.microphone.disconnect();}
  session.input?.disconnect();session.stream?.getTracks().forEach(track=>track.stop());
  session.microphone=null;session.input=null;session.stream=null;clearTimeout(session.joinTimer);
}
async function stopLiveAudio(message='Browser audio stopped. The phone call continues.') {
  const session=liveAudio;if(!session) return;liveAudioSend(session,{event:'stop'});liveAudio=null;session.closed=true;
  releaseLiveMicrophone(session);clearTimeout(session.timer);
  session.socket?.close();for(const source of session.playing.keys()) clearLiveAudioSource(session,source);
  await session.audio.close().catch(()=>{});$('liveAudioSession').hidden=true;$('liveAudioState').textContent=message;renderLiveAudio();
}
$('listenLive').onclick=async()=>{
  if(liveAudio || !selected) return;
  const session={id:selected,label:call?.objective || 'Selected call',audio:new AudioContext(),playing:new Map(),next:new Map(),closed:false,ready:false,joined:false,joining:false,muted:false};
  liveAudio=session;$('liveAudioState').textContent='Connecting live audio…';$('liveAudioSessionState').textContent='Connecting…';renderLiveAudio();
  try {
    await session.audio.resume();if(session.closed) return;
    session.socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/live/calls/'+session.id+'/live-audio');
    session.timer=setTimeout(()=>stopLiveAudio('Live audio timed out. The assistant may be connecting or handing off.'),15000);
    session.socket.onopen=()=>liveAudioSend(session,{event:'start',csrf});
    session.socket.onmessage=event=>{
      if(session.closed) return;
      try {
        const data=JSON.parse(event.data);
        if(data.event==='ready') {
          clearTimeout(session.timer);session.ready=true;session.canJoin=data.join_available;
          $('liveAudioSessionState').textContent='Listening live · microphone off';
          $('liveAudioState').textContent=data.join_available?'Live audio connected.':'Listening connected. Joining needs a configured transcription provider for owner captions.';
        } else if(data.event==='audio') playLiveAudio(session,data.source,data.payload);
        else if(data.event==='clear') clearLiveAudioSource(session,data.source);
        else if(data.event==='joined') {
          clearTimeout(session.joinTimer);session.joined=true;session.joining=false;
          $('liveAudioSessionState').textContent='Microphone live · caller hears you · assistant receives owner captions';
        } else if(data.event==='left') {
          releaseLiveMicrophone(session);$('liveAudioSessionState').textContent='Listening live · microphone off';
        } else if(data.event==='error') {
          releaseLiveMicrophone(session);$('liveAudioState').textContent=data.message;
        }
        renderLiveAudio();
      } catch(error) {stopLiveAudio(error.message);}
    };
    session.socket.onclose=()=>{if(!session.closed) stopLiveAudio('Live audio disconnected. Your microphone is off; the call continues if still active.');};
    session.socket.onerror=()=>{if(!session.closed) stopLiveAudio('Live audio could not connect. Check the call status and try again.');};
  } catch(error) {await stopLiveAudio(error.message);}
};
$('joinLive').onclick=async()=>{
  const session=liveAudio;if(!session?.ready || session.joining || session.joined) return;
  if(!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {$('liveAudioState').textContent='Microphone joining needs HTTPS and AudioWorklet support.';return;}
  if(typeof browserTest!=='undefined' && browserTest) {$('liveAudioState').textContent='This tab is already using its microphone for a test call.';return;}
  session.joining=true;renderLiveAudio();$('liveAudioSessionState').textContent='Waiting for microphone permission…';
  try {
    const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(session.closed || !session.joining) {stream.getTracks().forEach(track=>track.stop());return;}
    session.stream=stream;await session.audio.audioWorklet.addModule('/live/browser-audio-worklet.js');
    if(session.closed || !session.joining) return;
    session.microphone=new AudioWorkletNode(session.audio,'browser-call-microphone');session.input=session.audio.createMediaStreamSource(stream);
    session.input.connect(session.microphone);session.microphone.connect(session.audio.destination);
    session.microphone.port.onmessage=event=>{
      if(!session.joined || session.closed) return;
      if(session.socket.bufferedAmount>16000) {releaseLiveMicrophone(session);liveAudioSend(session,{event:'leave'});$('liveAudioState').textContent='Microphone connection fell behind. Rejoin to retry.';renderLiveAudio();return;}
      const bytes=session.muted?new Uint8Array(160).fill(255):event.data;
      liveAudioSend(session,{event:'media',payload:btoa(String.fromCharCode(...bytes))});
    };
    $('liveAudioSessionState').textContent='Connecting owner captions…';liveAudioSend(session,{event:'join'});
    session.joinTimer=setTimeout(()=>{releaseLiveMicrophone(session);liveAudioSend(session,{event:'leave'});$('liveAudioState').textContent='Microphone setup timed out. Listening continues.';renderLiveAudio();},15000);
  } catch(error) {
    releaseLiveMicrophone(session);$('liveAudioSessionState').textContent='Listening live · microphone off';
    $('liveAudioState').textContent=error.name==='NotAllowedError'?'Microphone permission denied. Listening continues.':error.message;renderLiveAudio();
  }
};
$('liveAudioMute').onclick=()=>{
  if(!liveAudio?.joined) return;liveAudio.muted=!liveAudio.muted;
  $('liveAudioSessionState').textContent=liveAudio.muted?'Listening live · microphone muted':'Microphone live · caller hears you · assistant receives owner captions';renderLiveAudio();
};
$('liveAudioLeave').onclick=()=>{
  if(!liveAudio) return;releaseLiveMicrophone(liveAudio);liveAudioSend(liveAudio,{event:'leave'});
  $('liveAudioSessionState').textContent='Listening live · microphone off';renderLiveAudio();
};
$('stopLiveAudio').onclick=()=>stopLiveAudio();
$('pageViews').before($('liveAudioSession'));
window.addEventListener('pagehide',()=>{if(liveAudio) stopLiveAudio();});
renderLiveAudio();
