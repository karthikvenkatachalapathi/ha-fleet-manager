/* UI-only adapter for the independently tested API-access controller. */
(function(root){'use strict';
function mount({host,api,instances}){
 let controller,disposed=false,editing=null,mode='create';
 const names=new Map(instances.map(i=>[Number(i.id),i.friendly_name]));
 const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n};
 host.innerHTML=`<style>
 .accessCards{display:grid;gap:12px}.accessCard{border:1px solid var(--line,#526070);border-radius:10px;padding:14px;min-width:0;overflow-wrap:anywhere}.accessActions{display:flex;gap:8px;flex-wrap:wrap}.accessActions button{min-height:44px}.accessMeta{font-size:.9em;opacity:.8}.accessDialog{background:var(--panel,#182330);color:inherit;border:1px solid #526070;border-radius:12px;width:min(560px,calc(100vw - 32px));max-height:85vh;overflow:auto;padding:20px;box-sizing:border-box}.accessDialog::backdrop{background:#0009}.accessDialog label{display:block;margin:10px 0 4px}.accessDialog input:not([type=checkbox]),.accessDialog select,.accessDialog textarea{display:block;width:100%;box-sizing:border-box;min-width:0}.accessDialog textarea{min-height:100px;overflow-wrap:anywhere}.accessChoices label,.accessCheck{display:flex!important;align-items:center;gap:10px}.accessChoices input,.accessCheck input{width:18px!important;min-width:18px!important;min-height:18px!important}.accessToken{border-top:1px solid #526070;margin-top:12px;padding-top:10px}.accessError{color:var(--danger,#ffaaaa);overflow-wrap:anywhere}.accessDialog h3{margin-top:0}
 </style><h2>API access</h2><p>Create separate access for each person, agent or integration. Store its one-time token securely in your preferred password or secrets manager.</p><p id="accessSecureWarning" class="accessError" role="alert"></p><div class="accessActions"><button id="accessNew">Create API access</button><button id="accessRefresh">Refresh</button></div><p id="accessStatus" role="status"></p><div id="accessIdentities" class="accessCards"></div>
 <dialog id="accessEditor" class="accessDialog" aria-labelledby="accessEditorTitle"><form id="accessEditorForm"><h3 id="accessEditorTitle">Create API access</h3><div id="accessIdentityFields"><label for="accessName">Name</label><input id="accessName" maxlength="120" required><label for="accessPurpose">Purpose</label><input id="accessPurpose" maxlength="500" required></div><div id="accessScopeFields"><label for="accessRole">Role</label><select id="accessRole"><option value="read">Read</option><option value="admin">Admin</option></select><p id="accessAdminWarning" class="accessError" hidden>Admin can change configuration and request managed actions. Existing risk checks and approvals still apply.</p><fieldset><legend>Permitted instances</legend><div id="accessInstanceChoices" class="accessChoices"></div><label class="accessCheck"><input id="accessAll" type="checkbox">All current instances</label><label class="accessCheck"><input id="accessFuture" type="checkbox">Also include future instances</label><p class="accessMeta">Fleet-wide settings require explicit current-and-future access.</p></fieldset></div><div id="accessExpiryField"><label for="accessExpiry">Expires in days</label><input id="accessExpiry" type="number" min="1" max="365" value="90" required></div><p id="accessRotateWarning" class="accessError" hidden>Rotation immediately revokes the old token. Update secure storage with the new token.</p><label for="accessPassword">Confirm your password</label><input id="accessPassword" type="password" autocomplete="current-password" required><p id="accessFormError" role="alert" class="accessError"></p><div class="accessActions"><button type="submit" id="accessSave">Create token</button><button type="button" id="accessCancel">Cancel</button></div></form></dialog>
 <dialog id="accessSecretDialog" class="accessDialog" aria-labelledby="accessSecretTitle"><h3 id="accessSecretTitle">Copy your API token now</h3><p>Shown once. Store it securely in your preferred password or secrets manager. Use Authorization: Bearer with the token; never put it in a URL.</p><textarea id="accessSecretValue" readonly aria-label="New API token" spellcheck="false"></textarea><p id="accessCopyStatus" role="status"></p><div class="accessActions"><button id="accessCopy" data-keep-enabled>Copy token</button><button id="accessDone" data-keep-enabled>Done</button></div></dialog>`;
 const $=id=>host.querySelector('#'+id),editor=$('accessEditor'),secretDialog=$('accessSecretDialog');
 const secure=location.protocol==='https:';
 if(!secure)$('accessSecureWarning').textContent='HTTPS is required before creating or using API tokens.';
 for(const i of instances){const label=el('label');const box=el('input',undefined,{type:'checkbox',value:i.id});label.append(box,el('span',i.friendly_name));$('accessInstanceChoices').append(label)}
 function scopeChanged(){const all=$('accessAll').checked;$('accessFuture').disabled=!all;if(!all)$('accessFuture').checked=false;host.querySelectorAll('#accessInstanceChoices input').forEach(x=>x.disabled=all);$('accessAdminWarning').hidden=$('accessRole').value!=='admin'}
 function closeEditor(){editor.close();$('accessPassword').value='';$('accessFormError').textContent=''}
 function openEditor(next,identity=null,token=null){
  if(disposed||!secure||controller.getState().busy)return;
  editing={identity,token};mode=next;$('accessEditorForm').reset();$('accessFormError').textContent='';
  $('accessEditorTitle').textContent=next==='create'?'Create API access':next==='rotate'?'Rotate token':next==='enable'?'Enable API access':'Edit role and scope';
  for(const field of ['Name','Purpose']){const input=$('access'+field);input.value=identity?.[field.toLowerCase()]||'';input.disabled=next!=='create'}
  $('accessIdentityFields').hidden=next!=='create';$('accessScopeFields').hidden=next==='rotate';$('accessExpiryField').hidden=!['create','rotate'].includes(next);$('accessExpiry').disabled=!['create','rotate'].includes(next);
  $('accessRole').value=identity?.role||'read';$('accessExpiry').value=identity?.role==='admin'?'30':'90';$('accessAll').checked=!!identity?.all_instances;$('accessFuture').checked=!!identity?.include_future_instances;
  host.querySelectorAll('#accessInstanceChoices input').forEach(x=>x.checked=(identity?.instance_ids||[]).includes(Number(x.value)));
  $('accessRotateWarning').hidden=next!=='rotate';$('accessSave').textContent=next==='create'?'Create token':next==='rotate'?'Rotate and revoke old token':'Save changes';scopeChanged();editor.showModal();$('accessPassword').value='';(next==='create'?$('accessName'):$('accessPassword')).focus();
 }
 function button(text,action){const b=el('button',text);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(action).catch(e=>{$('accessStatus').textContent=e.message}));return b}
 function render(){
  if(disposed||!controller)return;const s=controller.getState();$('accessStatus').textContent=s.error|| (s.loading?'Loading API access…':s.busy?'Saving…':'');
  const cards=$('accessIdentities');cards.replaceChildren();
  if(!s.identities.length&&!s.loading)cards.append(el('p','No API identities yet.'));
  for(const identity of s.identities){
   const card=el('article',undefined,{class:'accessCard','data-identity-id':identity.id});card.append(el('h3',identity.name),el('p',identity.purpose),el('p',`${identity.role==='admin'?'Admin':'Read'} · ${identity.enabled?'Enabled':'Disabled'}`));
   const scope=identity.all_instances?(identity.include_future_instances?'All current and future instances':'All instances at time of grant'):(identity.instance_ids||[]).map(id=>names.get(id)||`Instance ${id}`).join(', ');card.append(el('p',scope,{class:'accessMeta'}));
   const actions=el('div',undefined,{class:'accessActions'});actions.append(button('Edit role/scope',()=>openEditor('edit',identity)),button(identity.enabled?'Disable':'Enable',async()=>{if(identity.enabled){if(root.confirm(`Disable API access for ${identity.name}?`))await controller.change(identity.id,{enabled:false})}else openEditor('enable',identity)}));card.append(actions);
   for(const token of identity.tokens||[]){const item=el('div',undefined,{class:'accessToken','data-token-id':token.id});item.append(el('p',`Token ${token.id}`),el('p',`Created ${token.created_at||'—'} · expires ${token.expires_at||'—'} · last used ${token.last_used_at||'never'}`,{class:'accessMeta'}));if(token.revoked_at)item.append(el('p','Revoked'));else{const buttons=el('div',undefined,{class:'accessActions'});buttons.append(button('Rotate',()=>openEditor('rotate',identity,token)),button('Revoke',async()=>{if(root.confirm('Revoke this token immediately?'))await controller.revoke(token.id)}));item.append(buttons)}card.append(item)}cards.append(card);
  }
  host.querySelectorAll('button:not([data-keep-enabled])').forEach(b=>b.disabled=s.busy||(!secure&&b.id!=='accessRefresh'&&b.id!=='accessCancel'));
  if(s.secret){$('accessSecretValue').value=s.secret;if(!secretDialog.open){$('accessCopyStatus').textContent='';secretDialog.showModal()}}else{$('accessSecretValue').value='';if(secretDialog.open)secretDialog.close()}
 }
 controller=root.FleetAccess.createController({api,onChange:render});
 $('accessNew').onclick=()=>openEditor('create');$('accessRefresh').onclick=()=>controller.load().catch(()=>{});$('accessCancel').onclick=closeEditor;
 $('accessRole').onchange=()=>{$('accessExpiry').value=$('accessRole').value==='admin'?'30':'90';scopeChanged()};$('accessAll').onchange=scopeChanged;
 editor.addEventListener('close',()=>{const input=$('accessPassword');if(input)input.value=''});
 $('accessEditorForm').onsubmit=async event=>{
  event.preventDefault();if(controller.getState().busy)return;
  const password=$('accessPassword').value;$('accessPassword').value='';$('accessFormError').textContent='';
  try{const role=$('accessRole').value,all=$('accessAll').checked;const ids=all?[]:Array.from(host.querySelectorAll('#accessInstanceChoices input:checked')).map(x=>Number(x.value));
   if(mode!=='rotate'&&!all&&!ids.length)throw Error('Select at least one instance.');
   if(role==='admin'&&editing?.identity?.role!=='admin'&&!root.confirm('Grant Admin access within this scope?'))return;
   const scope={role,instance_ids:ids,all_instances:all,include_future_instances:all&&$('accessFuture').checked};const days=Number($('accessExpiry').value);
   if(mode==='create')await controller.create({name:$('accessName').value.trim(),purpose:$('accessPurpose').value.trim(),...scope,expires_in_days:days},password);
   else if(mode==='rotate')await controller.rotate(editing.token.id,days,password);
   else await controller.change(editing.identity.id,{...scope,...(mode==='enable'?{enabled:true}:{})},password);
   if(!disposed)closeEditor();
  }catch(e){if(!disposed)$('accessFormError').textContent=e.message}
 };
 $('accessCopy').onclick=async()=>{try{await navigator.clipboard.writeText($('accessSecretValue').value);$('accessCopyStatus').textContent='Copied. Store it securely in your preferred password or secrets manager.'}catch(e){$('accessSecretValue').focus();$('accessSecretValue').select();$('accessCopyStatus').textContent='Clipboard unavailable. Copy the selected token manually.'}};
 $('accessDone').onclick=()=>controller.clearSecret();secretDialog.addEventListener('close',()=>controller.clearSecret());
 controller.load().catch(()=>{});render();
 return {deactivate(){if(disposed)return;disposed=true;controller.deactivate();$('accessPassword').value='';$('accessSecretValue').value='';if(editor.open)editor.close();if(secretDialog.open)secretDialog.close();host.replaceChildren()},controller};
}
root.FleetAccessUI={mount};
})(window);
