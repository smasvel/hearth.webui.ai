(() => {
  const el = id => document.getElementById(id);
  let selected = null, task = null, pendingSignature = '', timer;
  const active = new Set(['queued', 'running', 'waiting_input', 'verifying', 'pausing', 'stopping']);
  async function api(path, body) {
    const r = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await r.json(); if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`); return data;
  }
  function node(tag, text, parent) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (parent) parent.append(n); return n; }
  function button(label, parent, action) { const n = node('button', label, parent); n.className = 'panel-btn'; n.type = 'button'; n.addEventListener('click', action); return n; }
  function show(taskData) {
    task = taskData; el('agent-detail').hidden = false;
    el('agent-title').textContent = task.title;
    el('agent-status').textContent = `${task.status} · ${task.assignee} · ${task.cycles}/${task.max_cycles} cycles`;
    el('agent-target').textContent = `${task.project} · ${task.model.providerID}/${task.model.id}`;
    el('agent-goal').textContent = task.goal + (task.criteria ? '\nAcceptance criteria: '+task.criteria : '');
    el('agent-error').textContent = task.error || '';
    el('agent-pause').disabled = !active.has(task.status);
    el('agent-resume').disabled = !['paused','blocked','interrupted'].includes(task.status);
    el('agent-send-instructions').disabled = task.status !== 'running';
    el('agent-stop').disabled = ['stopped','completed'].includes(task.status);
    el('agent-delete').disabled = active.has(task.status);
    el('agent-download').hidden = task.status !== 'completed';
    el('agent-result').textContent = (task.completion || task.summary || 'Work is in progress.') +
      (task.verification ? '\n\n'+JSON.stringify(task.verification,null,2) : '');
    el('agent-activity').textContent = task.activity.map(a => `${new Date(a.time*1000).toLocaleTimeString()} ${a.text}`).join('\n');
    const messages = el('agent-messages'); messages.replaceChildren();
    for (const m of task.messages) {
      if (!m.content.length) continue;
      node('strong',m.type, messages);
      for (const p of m.content) node('pre',p.type === 'text' ? p.text : `${p.name} · ${p.status}\n${p.input}`,messages);
    }
    renderPrompts();
  }
  async function reply(kind,id,value) {
    try { show(await api(`/api/agent-tasks/${task.id}/reply`,{kind,id,value})); el('agent-network').textContent=''; }
    catch(e) { el('agent-network').textContent=e.message; }
  }
  function renderPrompts() {
    const signature = JSON.stringify([task.id,task.status,task.forms,task.permissions]);
    if (signature === pendingSignature) return; pendingSignature = signature;
    const container = el('agent-prompts'); container.replaceChildren();
    if (task.status !== 'waiting_input') return;
    for (const permission of task.permissions) {
      const card = node('div',undefined,container); card.className='console-card';
      node('h3','Permission required',card); node('pre',`${permission.action}\n${permission.resources.join('\n')}\n${permission.message || ''}`,card);
      button('Allow once',card,()=>reply('permission',permission.id,'once'));
      button('Allow always',card,()=>reply('permission',permission.id,'always'));
      button('Reject',card,()=>reply('permission',permission.id,'reject'));
    }
    for (const prompt of task.forms) {
      const form = node('form',undefined,container); form.className='console-card agent-form'; node('h3',prompt.title || 'Agent question',form);
      const readers = [];
      for (const field of prompt.fields) {
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
    }
  }
  async function refresh() {
    clearTimeout(timer);
    try {
      const {tasks}=await api('/api/agent-tasks');
      const list=el('agent-list');list.replaceChildren();
      for(const t of tasks) {
        const cycleText=`${t.cycles || 0}/${t.max_cycles || 0} cycles`;
        const latest=t.latest_activity ? ` · ${t.latest_activity}` : '';
        button(`${t.title} · ${t.status} · ${cycleText}${latest}`,list,async()=>{selected=t.id;await refresh();});
      }
      if(!tasks.length) node('p','No assignments yet.',list);
      if(!selected && tasks.length) selected=tasks[0].id;
      if(selected && tasks.some(t=>t.id===selected)) show(await api('/api/agent-tasks/'+selected));
      else if(selected) { selected=null;task=null;el('agent-detail').hidden=true; }
      if(el('agent-network').textContent.endsWith('— reconnecting…')) el('agent-network').textContent='';
    } catch(e) { el('agent-network').textContent=e.message+' — reconnecting…'; }
    timer=setTimeout(refresh,2500);
  }
  el('agent-create').addEventListener('submit',async e=>{
    e.preventDefault();const form=e.currentTarget;const values=Object.fromEntries(new FormData(form));
    values.model=JSON.parse(el('agent-model').value);values.max_cycles=Number(values.max_cycles);values.verification_timeout=Number(values.verification_timeout);values.shell_mode=form.elements.shell_mode.value;
    el('agent-assign').disabled=true;
    try { const created=await api('/api/agent-tasks',values);selected=created.id;show(created);el('agent-create-status').textContent='Agent assigned. Work continues when this page is closed.';await refresh(); }
    catch(error){el('agent-create-status').textContent=error.message;}
    finally{el('agent-assign').disabled=false;}
  });
  el('agent-steer').addEventListener('submit',async e=>{
    e.preventDefault();
    try { show(await api(`/api/agent-tasks/${task.id}/steer`,{text:el('agent-steering').value}));el('agent-steering').value=''; }
    catch(error) { el('agent-network').textContent=error.message; }
  });
  for(const action of ['pause','resume','stop']) el('agent-'+action).addEventListener('click',async()=>{
    try { show(await api(`/api/agent-tasks/${task.id}/control`,{action})); }
    catch(e){el('agent-network').textContent=e.message;}
  });
  el('agent-delete').addEventListener('click',async()=>{
    if(!task || !window.confirm(`Delete “${task.title}” and its assignment history?`)) return;
    try { await fetch(`/api/agent-tasks/${task.id}`,{method:'DELETE',headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||'Delete failed');return d;});selected=null;task=null;el('agent-detail').hidden=true;await refresh(); }
    catch(e){el('agent-network').textContent=e.message;}
  });
  el('agent-download').addEventListener('click',()=>{if(task) window.location.assign(`/api/agent-tasks/${task.id}/download`);});
  api('/api/agent-tasks/models').then(({models})=>{
    for(const m of models) { const option=node('option',m.name+(m.usable === false ? ' — needs larger context' : ''),el('agent-model'));option.value=JSON.stringify({providerID:m.providerID,id:m.id});option.disabled=m.usable === false;option.title=m.reason || ''; }
    const available=[...el('agent-model').options].filter(o=>!o.disabled);
    const loaded=models.find(m=>m.loaded && m.usable !== false);
    const preferred=loaded ? available.find(o=>{const value=JSON.parse(o.value);return value.id===loaded.id && value.providerID===loaded.providerID;}) : null;
    (preferred || available[0] || {}).selected=true;
    if(!models.some(m=>m.usable !== false)) throw new Error('Configure a local model in OpenCode first.');
  }).catch(e=>{el('agent-create-status').textContent=e.message;el('agent-assign').disabled=true;});
  refresh();
})();
