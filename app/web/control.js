'use strict';
let appPage='calls',rulesData=null,rulesDirty=false,rulesJsonDirty=false,ruleDraft=null;
let controlCallId=null,liveBusy=false,liveUpdateId=null,approvalKey='';
const controlPages={calls:['callSummary','transferState','liveControlPanel','conversationPanel','audioPanel','inbox','supervisorPanel','ownerJoin'],
  call:['outboundPanel'],contacts:['callerBookPanel'],rules:['rulesPanel'],
  settings:['personalitiesPanel','answeringPanel','recordingSettingsPanel','ownerContextPanel','instructionsPanel','accountPanel']};
for(const [name,ids] of Object.entries(controlPages)) {
  const panel=document.createElement('section');panel.id='page-'+name;panel.className='page-view';panel.setAttribute('role','tabpanel');
  panel.setAttribute('aria-labelledby','nav-'+name);panel.hidden=name!=='calls';
  for(const id of ids) panel.append($(id));$('pageViews').append(panel);
  const tab=document.querySelector('[data-open-page="'+name+'"]');tab.id='nav-'+name;tab.setAttribute('aria-controls',panel.id);
  tab.onclick=()=>showPage(name);
}
function showPage(name) {
  if(!controlPages[name]) name='calls';appPage=name;
  for(const page of Object.keys(controlPages)) {$('page-'+page).hidden=page!==name;
    const button=$('nav-'+page);button.setAttribute('aria-selected',String(page===name));button.tabIndex=page===name?0:-1;}
  $('pageTitle').textContent={calls:'Calls',call:'New call',contacts:'Contacts',rules:'Rules',settings:'Settings'}[name];
  const url=new URL(location.href);url.hash=name;history.replaceState(null,'',url);
  if(name==='call') {$('outboundPanel').open=true;renderCallChoices();}
  if(name==='contacts') $('callerBookPanel').open=true;
  if(name==='rules' && !rulesData) loadRules().catch(e=>{$('ruleState').textContent=e.message;});
  window.scrollTo({top:0,behavior:'instant'});
}
document.querySelector('.app-nav').addEventListener('keydown',e=>{
  if(!['ArrowLeft','ArrowRight'].includes(e.key)) return;e.preventDefault();
  const names=Object.keys(controlPages),index=(names.indexOf(appPage)+(e.key==='ArrowRight'?1:-1)+names.length)%names.length;
  showPage(names[index]);$('nav-'+names[index]).focus();
});
function shouldFollowLive() {return appPage==='calls' && !call?.approvals?.some(r=>r.actionable) && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName);}
function updateActiveBar() {
  const active=!!call && !terminal.has(call.status);$('activeCallBar').hidden=!active;
  document.body.classList.toggle('has-active-call',active);
  if(active) $('activeCallLabel').textContent=(call.recipient || (call.direction==='inbound'?'Incoming call':'Outgoing call'))+' · '+$('duration').textContent;
  renderLiveControls();
}
function controlOptions(select,rows,value,empty) {
  select.replaceChildren();if(empty) {const o=document.createElement('option');o.value='';o.textContent=empty;select.append(o);}
  for(const row of rows) {const o=document.createElement('option');o.value=row.id;o.textContent=row.label || row.name || row.id;
    o.disabled=row.configured===false;select.append(o);}
  select.value=value || '';if(select.selectedIndex<0) select.selectedIndex=0;
}
function renderCallChoices() {
  const profiles=personalityCatalog?.personalities || [];
  controlOptions($('outboundPersonality'),profiles.map(p=>({id:p.personality_id,label:p.name+(p.presentation_mode==='owner_first_person'?' · speak as me':'')})),
    $('outboundPersonality').value,'Contact / routing / default');
  if(!backendCatalog) return;
  controlOptions($('outboundProvider'),backendCatalog.providers,$('outboundProvider').value,'Contact / routing / default');
  renderCallModels();
}
function renderCallModels() {
  const id=$('outboundProvider').value,p=backendCatalog?.providers.find(p=>p.id===id),d=backendCatalog?.provider_defaults[id];
  controlOptions($('outboundModel'),p?.models || [],$('outboundModel').value || d?.model);
  const model=p?.models.find(m=>m.id===$('outboundModel').value);
  controlOptions($('outboundVoice'),model?.voices || [],$('outboundVoice').value || d?.voice);
  $('outboundModel').disabled=!id || outboundBusy || !!outboundRequest;
  $('outboundVoice').disabled=!id || outboundBusy || !!outboundRequest;
}
$('outboundProvider').addEventListener('change',()=>{
  $('outboundModel').value='';$('outboundVoice').value='';renderCallModels();outboundDirty=true;
});
$('outboundModel').addEventListener('change',()=>{$('outboundVoice').value='';renderCallModels();outboundDirty=true;});

function renderRules(data) {
  rulesData=data;
  if(!rulesDirty) $('ruleInstructions').value=data.instructions;
  if(!rulesJsonDirty) $('ruleJson').value=JSON.stringify(data.rules,null,2);
  $('ruleState').textContent='Saved revision '+data.revision+' · '+data.precedence;
  for(const option of $('ruleReviewer').options) option.disabled=!data.reviewers[option.value];
  const target=$('activeRules');target.replaceChildren();
  if(!data.rules.length) {const p=document.createElement('p');p.className='caption-note';p.textContent='No routing rules. Saved contact/default choices apply.';target.append(p);}
  for(const rule of data.rules) {
    const card=document.createElement('article');card.className='editor-card';
    const title=document.createElement('strong');title.textContent=rule.priority+' · '+rule.name;
    const label=document.createElement('label');label.className='check-setting';const check=document.createElement('input');
    check.type='checkbox';check.checked=rule.enabled;label.append(check,'Enabled for new calls');
    const details=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('pre');
    summary.textContent='Conditions and choices';text.className='instruction-preview';text.textContent=JSON.stringify({when:rule.when,then:rule.then},null,2);details.append(summary,text);
    check.onchange=async()=>{
      check.disabled=true;
      try {renderRules(await api('/live/rules',writeOptions({revision:rulesData.revision,instructions:rulesData.instructions,
        rules:rulesData.rules.map(r=>r.rule_id===rule.rule_id?{...r,enabled:check.checked}:r)})));}
      catch(e) {$('ruleState').textContent='Not changed: '+e.message;check.checked=rule.enabled;check.disabled=false;}
    };
    card.append(title,label,details);target.append(card);
  }
}
async function loadRules() {renderRules(await api('/live/rules'));}
$('ruleInstructions').oninput=()=>{rulesDirty=true;ruleDraft=null;$('applyRules').disabled=true;$('ruleState').textContent='Unsaved instructions · review before applying.';};
$('ruleJson').oninput=()=>{rulesJsonDirty=true;};
$('reloadRules').onclick=async()=>{
  rulesDirty=false;rulesJsonDirty=false;ruleDraft=null;$('ruleProposal').hidden=true;
  try {await loadRules();} catch(e) {$('ruleState').textContent=e.message;}
};
$('reviewRules').onclick=async()=>{
  if(!rulesData?.reviewers[$('ruleReviewer').value]) {$('ruleState').textContent='This reviewer is not configured. Choose an available reviewer or use the structured editor. API review is billed separately.';return;}
  if(!$('ruleInstructions').value.trim()) {$('ruleState').textContent='Describe the rules first.';return;}
  const sourceText=$('ruleInstructions').value;$('reviewRules').disabled=true;$('ruleState').textContent='Reviewing instructions. Existing rules are still active…';
  try {
    const result=await api('/live/rules/review',{...writeOptions({instructions:sourceText,provider:$('ruleReviewer').value}),method:'POST'});
    ruleDraft=result;$('ruleProposal').hidden=false;$('ruleExplanation').textContent=result.explanation;
    $('ruleProposalJson').textContent=JSON.stringify(result.rules,null,2);$('ruleWarnings').replaceChildren();
    for(const warning of result.unresolved) {const li=document.createElement('li');li.textContent=warning;$('ruleWarnings').append(li);}
    $('applyRules').disabled=!!result.unresolved.length || sourceText!==$('ruleInstructions').value;
    $('ruleState').textContent='Proposal ready. No active rules changed. Review the choices and warnings below.';
  } catch(e) {$('ruleState').textContent=e.message+' Existing rules are unchanged.';}
  finally {$('reviewRules').disabled=false;}
};
$('applyRules').onclick=async()=>{
  if(!ruleDraft) return;$('applyRules').disabled=true;
  try {const result=await api('/live/rules/review/'+ruleDraft.draft_id+'/apply',{...writeOptions({}),method:'POST'});
    rulesDirty=false;rulesJsonDirty=false;renderRules(result);$('ruleProposal').hidden=true;$('ruleState').textContent='Rules applied to future calls · revision '+result.revision+'.';}
  catch(e) {$('ruleState').textContent='Not applied: '+e.message;}
};
$('saveRuleJson').onclick=async()=>{
  $('saveRuleJson').disabled=true;
  try {const rules=JSON.parse($('ruleJson').value);if(!Array.isArray(rules)) throw new Error('Enter an array of rules.');
    const result=await api('/live/rules',writeOptions({revision:rulesData.revision,instructions:$('ruleInstructions').value,rules}));
    rulesDirty=false;rulesJsonDirty=false;renderRules(result);$('ruleState').textContent='Structured rules applied to future calls.';
  } catch(e) {$('ruleState').textContent='Not applied: '+e.message;} finally {$('saveRuleJson').disabled=false;}
};
$('previewRule').onclick=async()=>{
  try {$('rulePreviewOutput').textContent=JSON.stringify(await api('/live/rules/preview',{...writeOptions({
    phone_number:$('rulePreviewNumber').value.trim(),direction:$('rulePreviewDirection').value,
    forwarded_from:$('rulePreviewForwarded').value.trim()}),method:'POST'}),null,2);}
  catch(e) {$('rulePreviewOutput').textContent=e.message;}
};

function renderLiveControls() {
  const active=!!call && !terminal.has(call.status);
  $('liveControlState').textContent=active?'Updates stay within this call’s existing identity and permissions. Approval releases only the specified detail for this call.':'Select an active call to send information. Past decisions remain visible below.';
  if(controlCallId!==call?.call_id) {
    controlCallId=call?.call_id || null;liveUpdateId=null;$('liveUpdateText').value='';$('liveUpdateState').textContent='';approvalKey='';
  }
  $('sendLiveUpdate').disabled=!active || liveBusy || !$('liveUpdateText').value.trim() || new TextEncoder().encode($('liveUpdateText').value).length>400;
  $('liveUpdateText').disabled=liveBusy;$('liveUpdateMode').disabled=liveBusy;
  const rows=call?.approvals || [],key=JSON.stringify([call?.call_id,call?.status,rows]);
  if(key===approvalKey) return;approvalKey=key;
  const target=$('callApprovals');target.replaceChildren();const count=rows.filter(r=>r.actionable && active).length;
  if(count) $('liveControlPanel').open=true;
  $('approvalBadge').textContent=count?count+' pending':'';
  $('nav-calls').dataset.pending=count?String(count):'';
  if(!rows.length) {const p=document.createElement('p');p.className='caption-note';p.textContent='No information requests for this call.';target.append(p);}
  for(const row of rows) {
    const card=document.createElement('article');card.className='editor-card approval-card';
    const title=document.createElement('strong');title.textContent=row.topic;
    const value=document.createElement('p');value.className='message-body';value.textContent=row.text;
    const reason=document.createElement('p');reason.className='caption-note';reason.textContent='Request: '+row.reason;
    const status=document.createElement('p');status.className='caption-note';status.textContent=row.actionable && active?'Waiting for your decision · this call only':row.status+' · '+(row.current?'saved decision':'fact changed or expired');
    card.append(title,value,reason,status);
    if(row.actionable && active) {
      const actions=document.createElement('div');actions.className='message-actions';
      for(const [text,approve] of [['Approve for this call',true],['Deny',false]]) {
        const b=document.createElement('button');b.className='quiet';b.textContent=text;
        b.onclick=async()=>{
          for(const child of actions.children) child.disabled=true;
          try {const result=await api('/live/approvals/'+row.approval_id,{...writeOptions({approve}),method:'POST'});
            $('liveUpdateState').textContent=result.status+' · voice update '+result.delivery+'. Check the transcript for the spoken response.';
            await refreshCallApprovals();}
          catch(e) {status.textContent=e.message;for(const child of actions.children) child.disabled=false;}
        };actions.append(b);
      }card.append(actions);
    }target.append(card);
  }
}
async function refreshCallApprovals() {
  if(!selected) return;const id=selected,data=await api('/live/calls/'+id+'/approvals');
  if(selected!==id || !call) return;call.approvals=data.approvals;renderLiveControls();
}
$('liveUpdateText').oninput=()=>{liveUpdateId=null;renderLiveControls();};
$('liveUpdateMode').onchange=()=>{liveUpdateId=null;};
$('liveUpdateForm').onsubmit=async e=>{
  e.preventDefault();if(liveBusy || !call || terminal.has(call.status)) return;
  const id=call.call_id,content=$('liveUpdateText').value.trim();if(!content) return;
  liveBusy=true;liveUpdateId=liveUpdateId || outboundRequestId();renderLiveControls();
  try {const result=await api('/live/calls/'+id+'/updates',{...writeOptions({content,mode:$('liveUpdateMode').value,
    response_required:$('liveUpdateMode').value==='say',update_id:liveUpdateId}),method:'POST'});
    if(selected===id) {$('liveUpdateText').value='';liveUpdateId=null;$('liveUpdateState').textContent='Voice update '+result.status+'. Check the transcript to confirm how it was used.';}
  } catch(error) {$('liveUpdateState').textContent=error.message+' Retry unchanged text to reuse the same update ID.';}
  finally {liveBusy=false;renderLiveControls();}
};
showPage(controlPages[location.hash.slice(1)]?location.hash.slice(1):'calls');renderCallChoices();updateActiveBar();
