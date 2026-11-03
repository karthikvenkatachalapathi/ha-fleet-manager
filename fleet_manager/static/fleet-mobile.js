/* Progressive mobile layout: preserve existing controls, IDs and action handlers. */
(function(){
 let expanded=false,queued=false;
 function enhance(){
  queued=false;
  const content=document.getElementById('content');if(!content)return;
  if(!matchMedia('(max-width:760px)').matches){content.querySelectorAll('.mobileFilters').forEach(detail=>{const parent=detail.parentElement;[...detail.querySelectorAll('select')].forEach(select=>parent.insertBefore(select,detail));detail.remove()});return;}
  content.querySelectorAll('table').forEach(table=>{
   const headings=[...table.querySelectorAll('thead th')].map(x=>x.textContent.replace(/[▲▼]/g,'').trim());
   table.querySelectorAll('tbody tr').forEach(row=>[...row.cells].forEach((cell,i)=>{
    if(cell.colSpan>1){cell.classList.add('emptyCell');return;}
    if(!cell.hasAttribute('data-label'))cell.dataset.label=headings[i]||'';
    if(cell.classList.contains('actionsCell')||cell.classList.contains('selectCell'))return;
    if(!cell.querySelector(':scope > .mobileCellValue')){
     const value=document.createElement('div');value.className='mobileCellValue';while(cell.firstChild)value.appendChild(cell.firstChild);cell.appendChild(value);
    }
   }));
  });
  const selection=content.querySelector('.selectionContext');
  if(selection&&!selection.querySelector('.mobileSelectVisible')){const button=document.createElement('button');button.className='mobileSelectVisible';button.textContent='Select visible';button.addEventListener('click',()=>window.toggleAll(true));selection.appendChild(button)}
  content.querySelectorAll('.selectCell input[type=checkbox]').forEach(input=>{if(input.parentElement.tagName!=='LABEL'){const label=document.createElement('label');label.className='mobileCheckbox';label.setAttribute('aria-label','Select update');input.before(label);label.appendChild(input);input.setAttribute('aria-label','Select update')}});
  const tabs=content.querySelector('.settingsTabs');
  if(tabs&&!tabs.querySelector('.mobileSettingsChooser')){
   const chooser=document.createElement('select');chooser.className='mobileSettingsChooser';chooser.setAttribute('aria-label','Settings section');
   tabs.querySelectorAll('.settingsTab').forEach(button=>{const option=document.createElement('option');option.value=button.textContent.trim();option.textContent=option.value;option.selected=button.classList.contains('active');chooser.appendChild(option)});
   chooser.addEventListener('change',()=>window.setSettingsTab(chooser.value));tabs.appendChild(chooser);
  }
  const filters=content.querySelector('.filters:has(#fSearch)');
  if(filters&&!filters.querySelector('.mobileFilters')){
   const detail=document.createElement('details');detail.className='mobileFilters';detail.open=expanded;
   const summary=document.createElement('summary');summary.textContent='Filter updates';detail.appendChild(summary);
   const controls=document.createElement('div');controls.className='mobileFilterControls';
   [...filters.querySelectorAll('select')].forEach(x=>controls.appendChild(x));detail.appendChild(controls);filters.appendChild(detail);
   detail.addEventListener('toggle',()=>{expanded=detail.open});
  }
 }
 function start(){const content=document.getElementById('content');if(!content)return;new MutationObserver(()=>{if(!queued){queued=true;queueMicrotask(enhance)}}).observe(content,{childList:true,subtree:true});enhance();matchMedia('(max-width:760px)').addEventListener('change',enhance)}
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
