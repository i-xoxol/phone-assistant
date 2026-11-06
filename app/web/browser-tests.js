'use strict';
let browserTest=null,browserStarting=false,browserAttempt=0;

function renderBrowserChoices() {
  controlOptions($('browserContact'),(typeof callerRows==='undefined'?[]:callerRows).filter(r=>r.phone_number)
    .map(r=>({id:r.phone_number,label:(r.name || 'Saved contact')+' · '+r.phone_number})),
    $('browserContact').value,'Enter a number or leave caller ID hidden');
  controlOptions($('browserPersonality'),(personalityCatalog?.personalities || []).map(p=>({id:p.personality_id,label:p.name})),
    $('browserPersonality').value,'Contact / routing / default');
  if(backendCatalog) {
    controlOptions($('browserProvider'),backendCatalog.providers,$('browserProvider').value,'Contact / routing / default');
    renderBrowserModels();
  }
}
function renderBrowserModels() {
  const id=$('browserProvider').value,p=backendCatalog?.providers.find(p=>p.id===id),d=backendCatalog?.provider_defaults[id];
  controlOptions($('browserModel'),p?.models || [],$('browserModel').value || d?.model);
  const model=p?.models.find(m=>m.id===$('browserModel').value);
  controlOptions($('browserVoice'),model?.voices || [],$('browserVoice').value || d?.voice);
  $('browserModel').disabled=!id;$('browserVoice').disabled=!id;
}
function browserScenarioChanged() {
  const outgoing=$('browserScenario').value==='outbound',forwarded=$('browserScenario').value==='forwarded';
  $('browserOutboundFields').hidden=!outgoing;$('browserForwardedField').hidden=!forwarded;
  $('browserForwarded').required=forwarded;$('browserNumber').required=outgoing;$('browserObjective').required=outgoing;
  $('browserNumberLabel').textContent=outgoing?'Recipient number the assistant thinks it called':'Caller ID the assistant sees';
}
$('browserScenario').onchange=browserScenarioChanged;
$('browserContact').onchange=()=>{$('browserNumber').value=$('browserContact').value;};
$('browserNumber').oninput=()=>{$('browserContact').value=$('browserNumber').value;};
$('browserProvider').onchange=()=>{$('browserModel').value='';$('browserVoice').value='';renderBrowserModels();};
$('browserModel').onchange=()=>{$('browserVoice').value='';renderBrowserModels();};
$('browserTestPanel').addEventListener('toggle',()=>{if($('browserTestPanel').open) renderBrowserChoices();});
$('browserCopyBrief').onclick=()=>{
  for(const [target,source] of [['browserNumber','outboundNumber'],['browserObjective','outboundObjective'],['browserContext','outboundContext'],
    ['browserConstraints','outboundConstraints'],['browserCallerId','outboundCallerId']]) $(target).value=$(source).value;
  $('browserIvr').checked=$('outboundIvr').checked;
};
function browserBody() {
  const outgoing=$('browserScenario').value==='outbound';
  return {request_id:crypto.randomUUID(),scenario:$('browserScenario').value,phone_number:$('browserNumber').value.trim(),
    forwarded_from:$('browserScenario').value==='forwarded'?$('browserForwarded').value.trim():'',
    objective:outgoing?$('browserObjective').value.trim():'',context:outgoing?$('browserContext').value.trim():'',
    constraints:outgoing?$('browserConstraints').value.split('\n').map(v=>v.trim()).filter(Boolean):[],
    max_duration_minutes:Number($('browserMinutes').value),ivr_mode:outgoing && $('browserIvr').checked,
    caller_id:outgoing?$('browserCallerId').value:'default',personality_id:$('browserPersonality').value || null,
    voice_provider:$('browserProvider').value || null,voice_model:$('browserModel').disabled?null:$('browserModel').value || null,
    voice:$('browserVoice').disabled?null:$('browserVoice').value || null};
}
function browserSend(test,data) {if(test.socket?.readyState===WebSocket.OPEN) test.socket.send(JSON.stringify(data));}
function clearBrowserPlayback(test) {
  for(const node of test.playing) {try {node.stop();} catch(_) {}}
  test.playing.clear();
  for(const timer of test.timers) clearTimeout(timer);
  test.timers.clear();test.nextAudio=test.audio.currentTime;
}
function playBrowserAudio(test,payload) {
  const raw=atob(payload),buffer=test.audio.createBuffer(1,raw.length,8000),samples=buffer.getChannelData(0);
  for(let i=0;i<raw.length;i++) {
    const value=raw.charCodeAt(i)^255,magnitude=(((value&15)<<3)+132)<<((value&112)>>4);
    samples[i]=((value&128)?132-magnitude:magnitude-132)/32768;
  }
  const node=test.audio.createBufferSource();node.buffer=buffer;node.connect(test.audio.destination);
  test.nextAudio=Math.max(test.nextAudio,test.audio.currentTime+0.015);
  if(test.nextAudio-test.audio.currentTime>4) throw new Error('Browser playback fell behind. Start a new test.');
  test.playing.add(node);node.onended=()=>test.playing.delete(node);
  node.start(test.nextAudio);test.nextAudio+=buffer.duration;
}
function acknowledgeBrowserMark(test,mark,at=test.nextAudio) {
  const timer=setTimeout(()=>{
    test.timers.delete(timer);
    if(test.closed) return;
    if(test.audio.currentTime+0.005<at) {acknowledgeBrowserMark(test,mark,at);return;}
    browserSend(test,{event:'mark',mark});
  },Math.max(20,(at-test.audio.currentTime)*1000));
  test.timers.add(timer);
}
async function cleanupBrowserTest(test,message) {
  if(test.closed) return;test.closed=true;
  if(test.id) fetch('/live/calls/'+encodeURIComponent(test.id)+'/hangup',{
    method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf},keepalive:true}).catch(()=>{});
  clearTimeout(test.connectTimer);test.microphone?.disconnect();test.input?.disconnect();
  test.stream?.getTracks().forEach(t=>t.stop());clearBrowserPlayback(test);
  test.socket?.close(1000);await test.audio.close().catch(()=>{});
  if(browserTest===test) {
    browserTest=null;browserStarting=false;$('browserSession').hidden=true;$('browserStart').disabled=false;
    $('browserTestState').textContent=message;
  }
  if(test.id && selected===test.id) await loadCall(test.id).catch(()=>{});
}
async function endBrowserTest(message='Browser test ended. Its transcript is saved.') {
  browserAttempt++;
  if(browserTest) await cleanupBrowserTest(browserTest,message);
}
$('browserEnd').onclick=()=>endBrowserTest();
$('browserMute').onclick=()=>{
  if(!browserTest) return;browserTest.muted=!browserTest.muted;
  $('browserMute').setAttribute('aria-pressed',String(browserTest.muted));
  $('browserMute').textContent=browserTest.muted?'Unmute microphone':'Mute microphone';
};
$('browserTestForm').onsubmit=async event=>{
  event.preventDefault();if(browserStarting || browserTest) return;
  if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    $('browserTestState').textContent='Microphone testing needs HTTPS or localhost and a browser with AudioWorklet support.';return;
  }
  const body=browserBody(),attempt=++browserAttempt;
  browserStarting=true;$('browserStart').disabled=true;
  const test={audio:new AudioContext(),playing:new Set(),timers:new Set(),nextAudio:0,muted:false,closed:false,samples:0};
  browserTest=test;$('browserSession').hidden=false;$('browserMute').disabled=true;
  $('browserSessionState').textContent='Waiting for microphone permission…';$('browserActionState').textContent='';
  $('browserMute').textContent='Mute microphone';$('browserMute').setAttribute('aria-pressed','false');
  try {
    // Resume during the user gesture so mobile browsers permit assistant playback.
    await test.audio.resume();
    test.stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(attempt!==browserAttempt || test.closed) {test.stream.getTracks().forEach(t=>t.stop());return;}
    await test.audio.audioWorklet.addModule('/live/browser-audio-worklet.js');
    if(test.closed) return;
    test.microphone=new AudioWorkletNode(test.audio,'browser-call-microphone');
    test.input=test.audio.createMediaStreamSource(test.stream);test.input.connect(test.microphone);
    // The processor produces silent output; only remote assistant audio is played.
    test.microphone.connect(test.audio.destination);
    $('browserSessionState').textContent='Preparing the selected call scenario…';
    const result=await api('/live/browser-tests',{...writeOptions(body),method:'POST'});test.id=result.call_id;
    if(attempt!==browserAttempt || test.closed) {
      await api('/live/calls/'+test.id+'/hangup',{method:'POST',headers:{'X-CSRF-Token':csrf}}).catch(()=>{});return;
    }
    const snapshot=await api('/live/bootstrap?call_id='+encodeURIComponent(test.id));calls=snapshot.calls;showList();await loadCall(test.id);showPage('calls');
    if(test.closed) return;
    test.socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/live/browser-tests/'+test.id+'/audio');
    test.connectTimer=setTimeout(()=>cleanupBrowserTest(test,'Audio connection timed out. Start a new browser test.'),15000);
    test.socket.onopen=()=>browserSend(test,{event:'start',csrf});
    test.microphone.port.onmessage=event=>{
      if(!test.ready || test.closed) return;
      if(test.socket.bufferedAmount>64000) {cleanupBrowserTest(test,'Microphone connection fell behind. Start a new test.');return;}
      const packet=test.muted?new Uint8Array(160).fill(255):event.data;
      browserSend(test,{event:'media',media:{payload:btoa(String.fromCharCode(...packet)),timestamp:test.samples/8}});test.samples+=packet.length;
    };
    test.socket.onmessage=event=>{
      try {
        const data=JSON.parse(event.data);
        if(data.event==='ready') {
          clearTimeout(test.connectTimer);test.ready=true;$('browserMute').disabled=false;
          $('browserSessionState').textContent='Microphone connected · '+body.scenario+' · speak as the '+(body.scenario==='outbound'?'recipient':'caller')+'.';
        } else if(data.event==='media') playBrowserAudio(test,data.media.payload);
        else if(data.event==='mark') acknowledgeBrowserMark(test,data.mark);
        else if(data.event==='clear') clearBrowserPlayback(test);
        else if(data.event==='test_announcement') {
          test.announcement=data.text;$('browserActionState').textContent='Office-hours announcement: '+data.text;
          if(data.ending) test.endMessage='Office closed: '+data.text+' No assistant voice session was started.';
        }
        else if(data.event==='test_keypad') $('browserActionState').textContent='Simulated assistant keypad: '+data.digits+' · no telephone tones sent.';
        else if(data.event==='test_transfer') $('browserActionState').textContent='Simulated owner transfer: unavailable. No phone was dialed.';
        else if(data.event==='test_error') cleanupBrowserTest(test,data.message);
      } catch(error) {cleanupBrowserTest(test,error.message);}
    };
    test.socket.onclose=()=>cleanupBrowserTest(test,test.endMessage || 'Browser test ended. Its transcript is saved.');
    test.socket.onerror=()=>cleanupBrowserTest(test,'Browser audio connection failed. Check the call status and start a new test.');
  } catch(error) {
    await cleanupBrowserTest(test,error.name==='NotAllowedError'?'Microphone access was denied. Allow it in your browser settings, then start again.':error.message);
  } finally {if(attempt===browserAttempt) {browserStarting=false;if(!browserTest) $('browserStart').disabled=false;}}
};
window.addEventListener('pagehide',()=>{if(browserTest) cleanupBrowserTest(browserTest,'Browser test closed.');});
browserScenarioChanged();renderBrowserChoices();
