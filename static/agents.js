(() => {
  const el = id => document.getElementById(id);
  const app=el('agent-detail')?.closest('.agent-app') || document.querySelector('.agent-app');
  const page=app?.dataset.page || 'list';
  const initialTaskId=app?.dataset.taskId || '';
  let selected = null, task = null, pendingSignature = '', timer;
  let rerunAcceptedAt = 0;
  let agentEventSource=null, agentEventTaskId=null;
  const selectedShareUsers = new Map();
  let shareTaskId = null, shareSearchTimer;
  const active = new Set(['queued', 'running', 'waiting_input', 'verifying', 'pausing', 'stopping']);
  async function api(path, body, method='POST') {
    const r = await fetch(path, body === undefined ? {} : {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await r.json(); if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`); return data;
  }
  function node(tag, text, parent) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (parent) parent.append(n); return n; }
  function agentDate(value) {
    if(value && typeof value==='object')value=value.completed ?? value.streamed ?? value.created ?? value.updated ?? value.time ?? null;
    if(typeof value==='string' && /^\d+(\.\d+)?$/.test(value))value=Number(value);
    if(typeof value==='number')return new Date(value>1e12 ? value : value*1000);
    return value ? new Date(value) : null;
  }
  function displayTime(value, options={timeStyle:'short'}) {
    const date=agentDate(value);
    return date && Number.isFinite(date.getTime()) ? date.toLocaleString(undefined,options) : '';
  }
  function newestFirst(events) {
    return [...(events || [])].sort((a,b)=>{
      const at=agentDate(a.time)?.getTime() || 0, bt=agentDate(b.time)?.getTime() || 0;
      return bt-at;
    });
  }
  function button(label, parent, action) { const n = node('button', label, parent); n.className = 'panel-btn'; n.type = 'button'; n.addEventListener('click', action); return n; }
  function show(taskData) {
    if (!el('agent-detail')) return;
    if(task && task.id===taskData.id && rerunAcceptedAt && Date.now()-rerunAcceptedAt<12000 &&
       ['queued','running','verifying'].includes(task.status) && (taskData.run_count||0)<(task.run_count||0)) {
      taskData=task;
    }
    task = taskData; el('agent-detail').hidden = false;
    if(el('agent-rerun-status') && task.status==='queued' && task.run_count>1) {
      el('agent-rerun-status').textContent='Rerun queued. The supervisor will start it shortly.';
    } else if(el('agent-rerun-status') && rerunAcceptedAt && task.run_count>1 && ['running','verifying'].includes(task.status)) {
      el('agent-rerun-status').textContent=task.status==='verifying'?'Rerun is checking its result.':'Rerun started. The agent is working.';
    }
    if(page==='execution') {
      if(active.has(task.status) && agentEventTaskId!==task.id) connectAgentEvents(task.id);
      else if(!active.has(task.status) && agentEventTaskId===task.id) {
        agentEventSource?.close();agentEventSource=null;agentEventTaskId=null;
      }
    }
    const owner = !task.shared;
    if (el('agent-sharing')) el('agent-sharing').hidden = !owner;
    if(el('agent-model-change'))el('agent-model-change').hidden=!['pausing','paused','blocked','interrupted'].includes(task.status);
    for (const id of ['agent-pause','agent-resume','agent-stop','agent-delete','agent-send-instructions']) if(el(id))el(id).hidden = false;
    if(el('agent-rerun'))el('agent-rerun').hidden=task.status!=='completed';
    if(el('agent-reset-cycles'))el('agent-reset-cycles').hidden=!['running','waiting_input','paused','blocked','interrupted'].includes(task.status);
    if(el('agent-prompts'))el('agent-prompts').hidden = false;
    if(el('agent-title'))el('agent-title').textContent = task.title;
    if(el('agent-status'))el('agent-status').textContent = `${task.status}${task.shared ? ' · shared access' : ''} · ${task.assignee} · ${task.cycles}/${task.max_cycles} cycles`;
    const recentActivity=newestFirst(task.activity);
    const latestActivity=recentActivity[0] || null;
    const latestTime=el('agent-latest-time');
    if(latestTime)latestTime.textContent=displayTime(latestActivity?.time,{dateStyle:'medium',timeStyle:'short'}) || 'No updates yet';
    const liveIndicator=el('agent-live-indicator');
    if(liveIndicator)liveIndicator.hidden=!['queued','running','verifying','waiting_input'].includes(task.status);
    if(el('agent-target'))el('agent-target').textContent = `${task.shared ? 'Shared project' : task.project} · ${task.model.providerID}/${task.model.id}`;
    if (owner && shareTaskId !== task.id) {
      shareTaskId = task.id;
      selectedShareUsers.clear();
      renderShareSelection();
      const ids = task.shared_with || [];
      if (ids.length) {
        const params = new URLSearchParams({ids: ids.join(',')});
        api(`/api/agent-tasks/share-targets?${params}`).then(({users})=>{
          if (shareTaskId !== task.id) return;
          selectedShareUsers.clear();
          for (const user of users) selectedShareUsers.set(user.id,user.username);
          renderShareSelection();
      }).catch(e=>{if(el('agent-share-status'))el('agent-share-status').textContent=e.message;});
      }
    }
    if(el('agent-goal'))el('agent-goal').textContent = task.goal + (task.criteria ? '\nAcceptance criteria: '+task.criteria : '');
    if(el('agent-error'))el('agent-error').textContent = task.error || '';
    if(el('agent-pause'))el('agent-pause').disabled = !active.has(task.status);
    if(el('agent-resume'))el('agent-resume').disabled = !['paused','blocked','interrupted'].includes(task.status);
    if(el('agent-send-instructions'))el('agent-send-instructions').disabled = task.status !== 'running';
    if(el('agent-stop'))el('agent-stop').disabled = ['stopped','completed'].includes(task.status);
    if(el('agent-delete'))el('agent-delete').disabled = active.has(task.status);
    if(el('agent-download'))el('agent-download').hidden = task.status !== 'completed';
    const resultHeading=el('agent-result-heading');
    if(resultHeading)resultHeading.textContent=task.completion?'Completed result':task.summary?'Latest agent response':task.error?'No result returned':'Agent response';
    if(el('agent-result-meta'))el('agent-result-meta').textContent=[task.result_label,({web:'Web page · HTML',document:'Document',general:'General result'})[task.result_platform||'general']].filter(Boolean).join(' · ');
    const previewWrap=el('agent-html-preview-wrap'), preview=el('agent-html-preview');
    if(previewWrap&&preview){
      const showPreview=task.result_platform==='web' && task.status==='completed';
      previewWrap.hidden=!showPreview;
      const src=showPreview?`/api/agent-tasks/${encodeURIComponent(task.id)}/preview`:'';
      if(preview.dataset.src!==src){preview.dataset.src=src;preview.src=src || 'about:blank';}
    }
    if(el('agent-result')) {
      const resultParts=[];
      if(task.summary)resultParts.push(task.summary);
      if(task.completion && task.completion!==task.summary)resultParts.push(task.completion);
      if(!resultParts.length)resultParts.push(task.error || 'Work is in progress.');
      if(task.verification)resultParts.push(JSON.stringify(task.verification,null,2));
      el('agent-result').textContent=resultParts.join('\n\n');
    }
    if(el('agent-activity'))el('agent-activity').textContent = recentActivity.map(a => `${displayTime(a.time)} ${a.text}`).join('\n');
    const activityFeed=el('agent-activity-feed');
    if(activityFeed) {
      const events=recentActivity;
      const eventSignature=JSON.stringify(events.map(a=>[a.time,a.text]));
      if(activityFeed.dataset.signature!==eventSignature) {
        activityFeed.dataset.signature=eventSignature;
        activityFeed.replaceChildren();
        if(events.length)for(const item of events.slice(0,40)) {
          const row=node('div',undefined,activityFeed);row.className='agent-activity-item';
          node('time',displayTime(item.time) || '—',row);
          node('span',item.text,row);
        } else node('p','The agent will post updates here as it works.',activityFeed);
        activityFeed.scrollTop=0;
      }
      if(task.status==='running' && events.length)activityFeed.dataset.live='true';
    }
    const messages = el('agent-messages'); if(!messages)return;
    const displayMessages=task.messages.slice(-30).reverse();
    const signature=JSON.stringify(displayMessages.map(m=>[m.id,m.content]));
    if(messages.dataset.signature===signature){renderPrompts();return;}
    messages.dataset.signature=signature;
    messages.replaceChildren();
    for (const m of displayMessages) {
      if (!m.content.length) continue;
      const head=node('div',undefined,messages);head.className=`agent-message-heading ${m.type==='assistant'?'agent-message-agent':'agent-message-user'}`;
      node('strong',m.type==='assistant'?'Agent':m.type==='user'?'You':m.type,head);
      const messageTime=displayTime(m.time);
      if(messageTime)node('time',messageTime,head);
      for (const p of m.content) {
        if(p.type==='text' && p.text) node('pre',p.text,messages);
        else if(p.type==='tool') {
          const activity=node('details',undefined,messages);activity.className='agent-container-activity';activity.open=true;
          const toolTime=displayTime(p.time || m.time);
          node('summary',`${p.name || 'Container tool'} · ${p.status || p.state || 'update'}${toolTime ? ` · ${toolTime}` : ''}`,activity);
          if(p.input)node('pre',`Input\n${p.input}`,activity);
          if(p.output)node('pre',`Output\n${p.output}`,activity);
          if(!p.input && !p.output)node('p','The container has not returned output for this step yet.',activity);
        }
      }
    }
    messages.scrollTop=0;
    renderPrompts();
  }
  function connectAgentEvents(taskId) {
    agentEventSource?.close();
    agentEventTaskId=taskId;
    agentEventSource=new EventSource(`/api/agent-tasks/${taskId}/events`);
    agentEventSource.onmessage=event=>{
      try {
        const payload=JSON.parse(event.data);
        if(!task || task.id!==payload.id)return;
        if(Array.isArray(task.messages) && task.messages.length &&
           (!Array.isArray(payload.messages) || !payload.messages.length)) {
          payload.messages=task.messages;
        }
        if(Array.isArray(task.activity) && task.activity.length &&
           (!Array.isArray(payload.activity) || !payload.activity.length)) {
          payload.activity=task.activity;
        }
        show({...task,...payload,event_streamed:true});
      } catch(error) { console.warn('Could not read the agent activity event.',error); }
    };
    agentEventSource.addEventListener('access-revoked',()=>{
      agentEventSource?.close();agentEventSource=null;agentEventTaskId=null;
      if(el('agent-network'))el('agent-network').textContent='You no longer have access to this task.';
    });
  }
  async function reply(kind,id,value) {
    try { show(await api(`/api/agent-tasks/${task.id}/reply`,{kind,id,value})); el('agent-network').textContent=''; }
    catch(e) { el('agent-network').textContent=e.message; }
  }
  function renderPrompts() {
    if(!el('agent-prompts'))return;
    const forms = Array.isArray(task.forms) ? task.forms : [];
    const permissions = Array.isArray(task.permissions) ? task.permissions : [];
    const signature = JSON.stringify([task.id,task.status,forms,permissions]);
    if (signature === pendingSignature) return; pendingSignature = signature;
    const container = el('agent-prompts'); container.replaceChildren();
    if (task.status !== 'waiting_input') return;
    let rendered = 0;
    for (const permission of permissions) {
      if (!permission || !permission.id) continue;
      const card = node('div',undefined,container); card.className='console-card';
      node('h3','Permission required',card); node('pre',`${permission.action || 'The agent requests permission'}\n${(Array.isArray(permission.resources) ? permission.resources : []).join('\n')}\n${permission.message || ''}`.trim(),card);
      button('Allow once',card,()=>reply('permission',permission.id,'once'));
      button('Allow always',card,()=>reply('permission',permission.id,'always'));
      button('Reject',card,()=>reply('permission',permission.id,'reject'));
      rendered++;
    }
    for (const prompt of forms) {
      if (!prompt || !prompt.id) continue;
      const form = node('form',undefined,container); form.className='console-card agent-form'; node('h3',prompt.title || 'Agent question',form);
      const readers = [];
      for (const field of (Array.isArray(prompt.fields) ? prompt.fields : [])) {
        if (field.hidden) continue;
        const label = node('div',undefined,form); label.className='agent-field'; node('span',field.title || field.key,label);
        if (field.description) node('span',field.description,label).className='hint';
        let input;
        if (field.type === 'multiselect') {
          const choices=[];
          for (const option of field.options || []) {
            const row=node('label',undefined,label); const check=node('input',undefined,row); check.type='checkbox';check.checked=(field.default || []).includes(option.value); check.setAttribute('aria-label',option.label);
            node('span',option.label+(option.description ? ' — '+option.description : ''),row);
            choices.push([check,option.value]);
          }
          const custom=field.custom ? node('input',undefined,label) : null;
          if(custom) { custom.className='panel-input'; custom.placeholder='Custom choices, separated by commas'; }
          readers.push(()=>{
            const values=choices.filter(([n])=>n.checked).map(([,v])=>v);
            if(custom && custom.value.trim()) values.push(...custom.value.split(',').map(v=>v.trim()).filter(Boolean));
            if((field.required || field.minItems) && values.length < (field.minItems || 1)) throw new Error('Choose an answer for '+field.key);
            if(field.maxItems !== undefined && values.length > field.maxItems) throw new Error('Too many choices for '+field.key);
            return [field.key,[...new Set(values)]];
          });
          continue;
        }
        if (field.options && !field.custom) {
          input=node('select',undefined,label); node('option','Choose…',input).value='';
          for(const option of field.options) node('option',option.label,input).value=option.value;
        } else {
          input=node('input',undefined,label);
          input.type=field.type === 'boolean' ? 'checkbox' : ['number','integer'].includes(field.type) ? 'number' : 'text';
          if(field.type === 'integer') input.step='1';
          if(field.options) {
            const list=node('datalist',undefined,label);list.id=`choices-${prompt.id}-${readers.length}`;input.setAttribute('list',list.id);
            for(const option of field.options) node('option',option.label,list).value=option.value;
          }
        }
        if(field.type === 'boolean') input.checked=field.default === true;
        else if(field.default !== undefined) input.value=String(field.default);
        if(field.minimum !== undefined) input.min=field.minimum;
        if(field.maximum !== undefined) input.max=field.maximum;
        if(field.minLength !== undefined) input.minLength=field.minLength;
        if(field.maxLength !== undefined) input.maxLength=field.maxLength;
        input.setAttribute('aria-label',field.title || field.key); input.className='panel-input'; if(field.type !== 'boolean') input.required=!!field.required;
        readers.push(()=>{
          if(field.type !== 'boolean' && input.value === '' && !field.required) return null;
          return [field.key,field.type === 'boolean' ? input.checked : ['number','integer'].includes(field.type) ? Number(input.value) : input.value];
        });
      }
      const submit=node('button','Answer and continue',form);submit.type='submit';submit.className='panel-btn';
      button('Dismiss question',form,()=>reply('dismiss',prompt.id,null));
      form.addEventListener('submit',e=>{e.preventDefault();try { reply('form',prompt.id,Object.fromEntries(readers.map(f=>f()).filter(Boolean))); } catch(error) { el('agent-network').textContent=error.message; }});
      rendered++;
    }
    if (!rendered) {
      const card=node('section',undefined,container); card.className='console-card';
      node('h3','The agent is waiting for your input',card);
      node('p','Hearth has not received the question or permission details yet. Refresh this assignment to check for the request.',card);
      button('Refresh request',card,async()=>{try { show(await api(`/api/agent-tasks/${task.id}`)); } catch(e) { el('agent-network').textContent=e.message; }});
    }
  }
  async function refresh() {
    clearTimeout(timer);
    try {
      const list=el('agent-list');
      if(page==='execution' && initialTaskId) {
        const detail=await api('/api/agent-tasks/'+initialTaskId);
        if(task?.id===initialTaskId && task.event_streamed) {
          if(active.has(detail.status) && Array.isArray(task.messages) && task.messages.length) {
            detail.messages=task.messages;
          } else if(!Array.isArray(detail.messages) || !detail.messages.length) {
            detail.messages=Array.isArray(task.messages) ? task.messages : [];
          }
          if((!Array.isArray(detail.activity) || !detail.activity.length) &&
             Array.isArray(task.activity) && task.activity.length) {
            detail.activity=task.activity;
          }
          detail.event_streamed=true;
        }
        show(detail);
      } else if(list) {
        const {tasks}=await api('/api/agent-tasks');
        list.replaceChildren();
        const count=el('agent-count'); if(count)count.textContent=`${tasks.length} ${tasks.length===1?'assignment':'assignments'}`;
        for(const t of tasks) {
          const cycleText=`${t.cycles || 0}/${t.max_cycles || 0} cycles`;
          const latest=t.latest_activity ? ` · ${t.latest_activity}` : '';
          const card=node('a',undefined,list); card.className='agent-task-card'; card.href=`/agents/${t.id}`;
          const top=node('div',undefined,card); top.className='agent-task-card-top';
          node('strong',t.title,top); const badge=node('span',friendlyStatus(t.status),top); badge.className=`agent-status-badge status-${t.status}`;
          node('p',`${t.schedule_id ? 'Scheduled · ' : ''}${cycleText}${latest}`,card).className='agent-task-meta';
        }
        if(!tasks.length) { const empty=node('div',undefined,list);empty.className='agent-empty';node('strong','No assignments yet',empty);node('p','Create an agent and its progress will appear here.',empty);node('a','Create an agent',empty).href='/agents/new'; }
      }
      if(el('agent-network')?.textContent.endsWith('— reconnecting…')) el('agent-network').textContent='';
    } catch(e) { if(el('agent-network'))el('agent-network').textContent=e.message+' — reconnecting…'; }
    if(page==='execution')timer=setTimeout(refresh,2500);
    else if(el('agent-list'))timer=setTimeout(refresh,10000);
  }
  function friendlyStatus(status) {
    return ({queued:'Starting',running:'Working',waiting_input:'Needs your help',verifying:'Checking the result',pausing:'Pausing',stopping:'Stopping',paused:'Paused',blocked:'Needs review',interrupted:'Interrupted',completed:'Done',stopped:'Stopped'})[status] || status;
  }
  function localDate(value) { return value ? new Date(value*1000).toLocaleString() : 'Not run yet'; }
  async function refreshSchedules() {
    try {
      const {schedules}=await api('/api/agent-schedules');
      const list=el('agent-schedule-list'); if(!list)return; list.replaceChildren();
      for (const schedule of schedules) {
        const card=node('div',undefined,list); card.className='agent-task-card';
        const scheduledTitle=schedule.task_title || schedule.project_name || schedule.assignment?.title || 'Agent assignment';
        node('strong',scheduledTitle,card);
        const timeLabel=schedule.run_time ? ` · at ${schedule.run_time} ${schedule.timezone}` : '';
        node('p',`${schedule.enabled ? 'Enabled' : 'Paused'} · every ${schedule.interval_value} ${schedule.interval_unit}${timeLabel} · next run ${localDate(schedule.next_run_at)}`,card).className='model-meta';
        if(schedule.last_run_at) node('p',`Last run ${localDate(schedule.last_run_at)}${schedule.last_task_id ? ` · assignment ${schedule.last_task_id.slice(0,8)}` : ''}`,card).className='model-meta';
        if(schedule.last_error) node('p',schedule.last_error,card).className='test-result-fail';
        button(schedule.enabled ? 'Pause schedule' : 'Resume schedule',card,async()=>{
          try { await api(`/api/agent-schedules/${schedule.id}`,{enabled:!schedule.enabled},'PATCH'); await refreshSchedules(); }
          catch(error) { el('agent-schedule-status').textContent=error.message; }
        });
        button('Delete schedule',card,async()=>{
          if(!window.confirm(`Delete recurring schedule for “${scheduledTitle}”? The agent assignment will remain.`)) return;
          try { await api(`/api/agent-schedules/${schedule.id}`,{},'DELETE'); await refreshSchedules(); }
          catch(error) { el('agent-schedule-status').textContent=error.message; }
        });
      }
      if(!schedules.length) node('p','No recurring schedules yet.',list);
    } catch(error) { el('agent-schedule-status').textContent=error.message; }
  }
  async function loadScheduleProjects() {
    const select=el('agent-schedule-project'); if(!select)return;
    try {
      const {tasks}=await api('/api/agent-tasks');
      const eligible=tasks.filter(item=>!item.shared && ['queued','running','waiting_input','verifying','completed'].includes(item.status));
      for(const task of eligible) {
        const option=document.createElement('option'); option.value=task.id; option.textContent=`${task.title} · ${friendlyStatus(task.status)}`; select.append(option);
      }
      if(!eligible.length) {
        const option=document.createElement('option'); option.value=''; option.textContent='Create an assignment first'; option.disabled=true; select.append(option);
      }
    } catch(error) { if(el('agent-schedule-status'))el('agent-schedule-status').textContent=error.message; }
  }
  el('agent-schedule-form')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const form=event.currentTarget, values=Object.fromEntries(new FormData(form));
    values.timezone=Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const multiplier={minutes:60,hours:3600,days:86400,weeks:604800}[values.interval_unit];
    values.interval_value=Number(values.interval_value); values.interval_seconds=values.interval_value*multiplier;
    if(!Number.isInteger(values.interval_seconds) || values.interval_seconds<300 || values.interval_seconds>31536000) {
      el('agent-schedule-status').textContent='Choose a repeat interval from 5 minutes through 365 days.'; return;
    }
    const submit=el('agent-schedule-create'); submit.disabled=true;
    try {
      const created=await api('/api/agent-schedules',values);
      el('agent-schedule-status').textContent=`Schedule created. First run: ${localDate(created.next_run_at)}.`;
      form.reset(); if(timezoneInput)timezoneInput.value=browserTimezone; await refreshSchedules();
    } catch(error) { el('agent-schedule-status').textContent=error.message; }
    finally { submit.disabled=false; }
  });
  const browserTimezone=Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const timezoneInput=document.querySelector('#agent-schedule-form [name="timezone"]');
  if(timezoneInput)timezoneInput.value=browserTimezone;
  const intervalSelect=document.querySelector('#agent-schedule-form [name="interval_unit"]');
  const runTimeInput=document.querySelector('#agent-schedule-form [name="run_time"]');
  if(intervalSelect && runTimeInput) {
    const updateRunTime=()=>{const eligible=['days','weeks'].includes(intervalSelect.value);runTimeInput.disabled=!eligible;runTimeInput.title=eligible?'Optional local time of day for the recurring run.':'Choose days or weeks to set a time of day.';};
    intervalSelect.addEventListener('change',updateRunTime);updateRunTime();
  }
  el('agent-create')?.addEventListener('submit',async e=>{
    e.preventDefault();const form=e.currentTarget;const values=Object.fromEntries(new FormData(form));
    values.enable_thinking=form.elements.enable_thinking.checked;
    values.model=JSON.parse(el('agent-model').value);values.max_cycles=Number(values.max_cycles);values.verification_timeout=Number(values.verification_timeout);values.shell_mode=form.elements.shell_mode.value;
    el('agent-assign').disabled=true;
    try { const created=await api('/api/agent-tasks',values);window.location.assign(`/agents/${created.id}`); }
    catch(error){el('agent-create-status').textContent=error.message;}
    finally{if(el('agent-assign'))el('agent-assign').disabled=false;}
  });
  el('agent-steer')?.addEventListener('submit',async e=>{
    e.preventDefault();
    try { show(await api(`/api/agent-tasks/${task.id}/steer`,{text:el('agent-steering').value}));el('agent-steering').value=''; }
    catch(error) { el('agent-network').textContent=error.message; }
  });
  for(const action of ['pause','resume','stop','reset-cycles']) el('agent-'+action)?.addEventListener('click',async()=>{
    if(action==='reset-cycles' && !window.confirm('Reset this assignment’s work-cycle count to zero? Review the latest response before continuing.'))return;
    try { show(await api(`/api/agent-tasks/${task.id}/control`,{action})); }
    catch(e){el('agent-network').textContent=e.message;}
  });
  el('agent-rerun')?.addEventListener('click',async()=>{
    if(!task || task.status!=='completed' || !window.confirm('Run this assignment again from its latest saved project files? The original completion and activity will remain in its history.'))return;
    const button=el('agent-rerun');
    button.disabled=true;
    const status=el('agent-rerun-status');
    if(status)status.textContent='Submitting rerun…';
    try {
      const rerun=await api(`/api/agent-tasks/${task.id}/rerun`,{},'POST');
      rerunAcceptedAt=Date.now();
      if(status)status.textContent=rerun.status==='queued'?'Rerun queued. The supervisor will start it shortly.':`Rerun accepted (${rerun.status}).`;
      show(rerun);
    }
    catch(e){if(status)status.textContent=`Rerun could not start: ${e.message}`;}
    finally {button.disabled=false;}
  });
  el('agent-change-model')?.addEventListener('click',async()=>{
    try {
      const model=JSON.parse(el('agent-resume-model').value);
      show(await api(`/api/agent-tasks/${task.id}/model`,{model},'PATCH'));
      el('agent-model-status').textContent='Model saved. Continue the assignment when ready.';
    } catch(error) { el('agent-model-status').textContent=error.message; }
  });
  el('agent-delete')?.addEventListener('click',async()=>{
    if(!task || !window.confirm(`Delete “${task.title}” and its assignment history?`)) return;
    try { await fetch(`/api/agent-tasks/${task.id}`,{method:'DELETE',headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||'Delete failed');return d;});selected=null;task=null;el('agent-detail').hidden=true;await refresh(); }
    catch(e){el('agent-network').textContent=e.message;}
  });
  el('agent-download')?.addEventListener('click',()=>{if(task) window.location.assign(`/api/agent-tasks/${task.id}/download`);});
  if(el('agent-model') || el('agent-resume-model')) api('/api/agent-tasks/models').then(({models})=>{
    for(const m of models) {
      const value=JSON.stringify({providerID:m.providerID,id:m.id});
      for(const select of [el('agent-model'),el('agent-resume-model')].filter(Boolean)) {
        const option=node('option',m.name+(m.usable === false ? ' — needs larger context' : ''),select);
        option.value=value; option.disabled=m.usable === false; option.title=m.reason || '';
      }
    }
    const availableSelect=el('agent-model') || el('agent-resume-model');
    const available=availableSelect ? [...availableSelect.options].filter(o=>!o.disabled) : [];
    const loaded=models.find(m=>m.loaded && m.usable !== false);
    const preferred=loaded ? available.find(o=>{const value=JSON.parse(o.value);return value.id===loaded.id && value.providerID===loaded.providerID;}) : null;
    if(el('agent-model'))(preferred || available[0] || {}).selected=true;
    if(el('agent-resume-model')) {
      const current=JSON.stringify(task?.model || {});
      el('agent-resume-model').value=(preferred || ([...el('agent-resume-model').options].find(o=>o.value===current && !o.disabled)) || available[0] || {}).value || '';
    }
    if(!models.some(m=>m.usable !== false)) throw new Error('Configure a local model in OpenCode first.');
  }).catch(e=>{if(el('agent-create-status'))el('agent-create-status').textContent=e.message;if(el('agent-assign'))el('agent-assign').disabled=true;if(el('agent-schedule-create'))el('agent-schedule-create').disabled=true;});
  function renderShareSelection() {
    const selectedBox=el('agent-share-selected');
    if (!selectedBox) return;
    selectedBox.replaceChildren();
    for (const [id,username] of selectedShareUsers) {
      const chip=button(`${username} ×`,selectedBox,()=>{selectedShareUsers.delete(id);renderShareSelection();});
      chip.className='panel-btn';
      chip.setAttribute('aria-label',`Remove ${username}`);
    }
  }
  if (el('agent-save-shares') && el('agent-share-search') && el('agent-share-results') && el('agent-share-status')) {
    const search=el('agent-share-search'), results=el('agent-share-results');
    const searchUsers=async()=>{
      const q=search.value.trim(); results.replaceChildren();
      if(q.length<2){node('span','Type at least 2 characters to search.',results);return;}
      try {
        const params=new URLSearchParams({q});
        const {users}=await api(`/api/agent-tasks/share-targets?${params}`);
        if(search.value.trim()!==q)return;
        if(!users.length){node('span','No matching users with Agent access.',results);return;}
        let matches=0;
        for(const user of users){
          if(selectedShareUsers.has(user.id))continue;
          matches++;
          button(user.username,results,()=>{selectedShareUsers.set(user.id,user.username);renderShareSelection();searchUsers();});
        }
        if(!matches)node('span','All matching users are already selected.',results);
      } catch(e) { el('agent-share-status').textContent=e.message; }
    };
    search.addEventListener('input',()=>{clearTimeout(shareSearchTimer);shareSearchTimer=setTimeout(searchUsers,250);});
    el('agent-save-shares').addEventListener('click',async()=>{
      if (!task || task.shared) return;
      const user_ids=[...selectedShareUsers.keys()];
      try {
        const response=await fetch(`/api/agent-tasks/${task.id}/shares`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_ids})});
        const data=await response.json(); if(!response.ok) throw new Error(data.error || 'Could not save sharing');
        selectedShareUsers.clear(); for(const user of data.shared_with)selectedShareUsers.set(user.id,user.username); renderShareSelection();
        el('agent-share-status').textContent=data.shared_with.length ? `Shared with ${data.shared_with.map(user=>user.username).join(', ')}.` : 'Sharing removed.';
      } catch(error) { el('agent-share-status').textContent=error.message; }
    });
  }
  if(el('agent-schedule-list')) { loadScheduleProjects(); refreshSchedules(); setInterval(refreshSchedules,30000); }
  if(el('agent-list') || page==='execution') refresh();
})();
