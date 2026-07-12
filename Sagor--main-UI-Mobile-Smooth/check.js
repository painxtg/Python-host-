
    const API = window.location.origin;
    const REFRESH_MS = 18000;
    let authToken = sessionStorage.getItem('sagor_token') || '';
    let overview = null;
    let apps = [];
    let selectedFile = null;
    let fileModalState = { app: '', path: '', items: [], current: '' };
    let lastAppsSnapshot = '';
    let lastOverviewSnapshot = '';
    const expandedApps = new Set(JSON.parse(localStorage.getItem('sagor_expanded') || '[]'));

    function authHeaders(){ return authToken ? { 'X-Auth-Token': authToken } : {}; }
    function showToast(msg, kind='ok'){
      const box = document.getElementById('toastbox');
      const el = document.createElement('div');
      el.className = `toast ${kind}`;
      el.innerHTML = msg;
      box.appendChild(el);
      requestAnimationFrame(() => {
        setTimeout(() => {
          el.style.opacity='0';
          el.style.transform='translateY(10px)';
          setTimeout(()=>el.remove(),250);
        }, 2600);
      });
    }
    function showLogin(){
      document.getElementById('login-screen').classList.remove('hidden');
      document.getElementById('dashboard').classList.add('hidden');
      document.getElementById('logout-btn').classList.add('hidden');
    }
    function showDash(){
      document.getElementById('login-screen').classList.add('hidden');
      document.getElementById('dashboard').classList.remove('hidden');
      document.getElementById('logout-btn').classList.remove('hidden');
    }

    async function login(){
      const password = document.getElementById('password').value.trim();
      if(!password) return showToast('Enter password','warn');
      try{
        const res = await fetch(`${API}/auth`, {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({password})
        });
        const data = await res.json();
        if(res.ok && data.token){
          authToken = data.token;
          sessionStorage.setItem('sagor_token', authToken);
          showDash();
          showToast('Access granted','ok');
          await refreshAll();
        }else{
          showToast(data.message || 'Login failed','bad');
        }
      }catch(e){
        showToast('Connection failed','bad');
      }
    }

    function logout(){
      sessionStorage.removeItem('sagor_token');
      authToken = '';
      apps = [];
      overview = null;
      lastAppsSnapshot = '';
      lastOverviewSnapshot = '';
      showLogin();
    }

    function previewFile(fileObj){
      const file = fileObj || document.getElementById('file').files[0];
      selectedFile = file || null;
      document.getElementById('selected-file').innerText = file ? `${file.name} (${Math.round(file.size/1024)} KB)` : 'No file selected';
    }

    function dropFile(ev){
      ev.preventDefault();
      const file = ev.dataTransfer.files[0];
      if(file){
        selectedFile = file;
        document.getElementById('selected-file').innerText = `${file.name} (${Math.round(file.size/1024)} KB)`;
      }
    }

    async function uploadBot(){
      const file = selectedFile || document.getElementById('file').files[0];
      if(!file) return showToast('Select a file first','warn');
      const fd = new FormData();
      fd.append('file', file);
      try{
        const res = await fetch(`${API}/upload`, {method:'POST', headers:authHeaders(), body:fd});
        const data = await res.json();
        if(res.ok){
          showToast(data.message || 'Deployed','ok');
          document.getElementById('file').value = '';
          selectedFile = null;
          document.getElementById('selected-file').innerText = 'No file selected';
          await refreshAll();
        }else{
          showToast(data.error || 'Deploy failed','bad');
        }
      }catch(e){
        showToast('Deploy failed','bad');
      }
    }

    async function requestAction(appName, action){
      if(action === 'delete' && !confirm(`Delete ${appName}?`)) return;
      try{
        const res = await fetch(`${API}/action`, {
          method:'POST',
          headers:{'Content-Type':'application/json', ...authHeaders()},
          body:JSON.stringify({app_name: appName, action})
        });
        const data = await res.json();
        if(res.ok){
          showToast(data.message || `${action} done`, 'ok');
          await refreshAll();
        }else{
          showToast(data.error || 'Action failed','bad');
        }
      }catch(e){
        showToast('Action failed','bad');
      }
    }

    async function refreshOverview(silent=false){
      try{
        const res = await fetch(`${API}/api/overview`, {
          method:'POST',
          headers:{'Content-Type':'application/json', ...authHeaders()},
          body:JSON.stringify({token:authToken})
        });
        if(res.status === 403){
          logout();
          if(!silent) showToast('Session expired','bad');
          return null;
        }
        const data = await res.json();
        const snapshot = JSON.stringify(data);
        overview = data;
        if(snapshot !== lastOverviewSnapshot){
          lastOverviewSnapshot = snapshot;
          document.getElementById('server-pill').innerText = `Uptime: ${data.server_uptime}`;
          document.getElementById('metrics').innerHTML = [
            {t:'Total Bots', v:data.total_apps, i:'fa-layer-group'},
            {t:'Running Bots', v:data.running_apps, i:'fa-circle-play'},
            {t:'Sessions', v:data.sessions, i:'fa-key'},
            {t:'Server CPU', v:`${data.cpu}%`, i:'fa-microchip'},
          ].map(x=>`<div class="card card-pad metric"><div><h3>${x.t}</h3><div class="val">${x.v}</div></div><div class="ico"><i class="fa-solid ${x.i}"></i></div></div>`).join('');
          document.getElementById('system-metrics').innerHTML = [
            {t:'Memory', v:`${data.memory.used_gb} / ${data.memory.total_gb} GB (${data.memory.percent}%)`, i:'fa-memory'},
            {t:'Disk', v:`${data.disk.used_gb} / ${data.disk.total_gb} GB (${data.disk.percent}%)`, i:'fa-hard-drive'},
            {t:'Server Uptime', v:data.server_uptime, i:'fa-hourglass-half'},
            {t:'Auto Restart', v:'Enabled', i:'fa-rotate'},
          ].map(x=>`<div class="card card-pad metric"><div><h3>${x.t}</h3><div class="val" style="font-size:1rem">${x.v}</div></div><div class="ico"><i class="fa-solid ${x.i}"></i></div></div>`).join('');
          document.getElementById('quick-info').innerHTML = `
            <span class="pill">Render ready</span>
            <span class="pill">Auto restart watch</span>
            <span class="pill">Live logs</span>
            <span class="pill">File manager</span>`;
        }
        return data;
      }catch(e){
        if(!silent) showToast('Overview refresh failed','bad');
        return null;
      }
    }

    async function refreshApps(silent=false){
      try{
        const res = await fetch(`${API}/my_apps`, {
          method:'POST',
          headers:{'Content-Type':'application/json', ...authHeaders()},
          body:JSON.stringify({token:authToken})
        });
        if(res.status === 403){
          logout();
          if(!silent) showToast('Session expired','bad');
          return null;
        }
        const data = await res.json();
        const snapshot = JSON.stringify(data.apps || []);
        if(snapshot !== lastAppsSnapshot){
          lastAppsSnapshot = snapshot;
          apps = data.apps || [];
          renderApps();
        }
        return data;
      }catch(e){
        if(!silent) showToast('Bot list refresh failed','bad');
        return null;
      }
    }

    function isMobile(){ return window.innerWidth <= 768; }

    function renderApps(){
      const query = (document.getElementById('search').value || '').toLowerCase().trim();
      const box = document.getElementById('apps');
      const list = apps.filter(a => a.name.toLowerCase().includes(query));

      if(!list.length){
        box.innerHTML = `<div class="card card-pad muted center">No bots found.</div>`;
        return;
      }

      const mobile = isMobile();
      box.innerHTML = list.map(app => {
        const open = !mobile || expandedApps.has(app.name);
        const logs = String(app.logs || '').slice(-1200);
        return `
          <div class="card app">
            <div class="app-top">
              <div>
                <div class="app-name">${escapeHtml(app.name)}</div>
                <div class="meta">
                  <span><i class="fa-regular fa-clock"></i> ${escapeHtml(app.uptime || '0s')}</span>
                  <span><i class="fa-solid fa-folder"></i> ${app.files_count || 0} files</span>
                  <span><i class="fa-solid fa-rotate"></i> ${app.restart_count || 0} restarts</span>
                </div>
              </div>
              <span class="badge ${app.running ? 'on' : 'off'}">${app.running ? 'RUNNING' : 'STOPPED'}</span>
            </div>

            <div class="details ${open ? 'open' : ''}">
              <div class="meta">
                <span><i class="fa-solid fa-memory"></i> ${app.memory_mb || 0} MB</span>
                <span><i class="fa-solid fa-gauge-high"></i> ${app.cpu_percent || 0}% CPU</span>
                ${app.start_time ? `<span><i class="fa-solid fa-play"></i> ${new Date(app.start_time).toLocaleString()}</span>` : ''}
              </div>
              <div class="btns" style="margin-top:2px">
                <button class="btn btn-ok" onclick="requestAction('${escapeJs(app.name)}','start')"><i class="fa-solid fa-play"></i> Start</button>
                <button class="btn btn-warn" onclick="requestAction('${escapeJs(app.name)}','stop')"><i class="fa-solid fa-stop"></i> Stop</button>
                <button class="btn btn-pri" onclick="requestAction('${escapeJs(app.name)}','restart')"><i class="fa-solid fa-rotate"></i> Restart</button>
                <button class="btn btn-ghost" onclick="openFiles('${escapeJs(app.name)}')"><i class="fa-solid fa-folder-open"></i> Files</button>
                <button class="btn btn-bad" onclick="requestAction('${escapeJs(app.name)}','delete')"><i class="fa-solid fa-trash"></i> Delete</button>
              </div>
              <div class="log">${escapeHtml(logs || 'No logs yet.')}</div>
            </div>

            ${mobile ? `<div class="space" style="height:10px"></div>
              <button class="btn btn-ghost details-toggle" onclick="toggleDetails('${escapeJs(app.name)}')">
                <i class="fa-solid ${open ? 'fa-chevron-up' : 'fa-chevron-down'}"></i>
                ${open ? 'Hide details' : 'Show details'}
              </button>` : ''}
          </div>
        `;
      }).join('');
    }

    function toggleDetails(name){
      if(expandedApps.has(name)) expandedApps.delete(name);
      else expandedApps.add(name);
      localStorage.setItem('sagor_expanded', JSON.stringify([...expandedApps]));
      renderApps();
    }

    function escapeHtml(s){
      return String(s || '').replace(/[&<>'"]/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;',"'" :'&#39;','"':'&quot;'
      }[c]));
    }
    function escapeJs(s){ return String(s || '').replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }

    async function openFiles(appName, path=''){
      fileModalState = { app: appName, path, items: [], current: '' };
      const root = document.getElementById('modal-root');
      root.innerHTML = `
        <div class="modal-backdrop" onclick="closeModal(event)">
          <div class="modal card card-pad" onclick="event.stopPropagation()">
            <div class="toolbar">
              <h2><i class="fa-solid fa-folder-tree"></i> ${escapeHtml(appName)} / <span id="file-path">${escapeHtml(path || '')}</span></h2>
              <button class="btn btn-ghost" onclick="closeModal()"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div class="modal-grid">
              <div>
                <div class="btns" style="margin-bottom:10px">
                  <button class="btn btn-ghost" onclick="upOne()"><i class="fa-solid fa-turn-up"></i> Up</button>
                  <button class="btn btn-pri" onclick="mkdirPrompt()"><i class="fa-solid fa-folder-plus"></i> New Folder</button>
                </div>
                <div id="file-list" class="file-list"></div>
              </div>
              <div>
                <div class="btns" style="margin-bottom:10px">
                  <button class="btn btn-ok" onclick="saveFile()"><i class="fa-solid fa-floppy-disk"></i> Save</button>
                  <button class="btn btn-bad" onclick="deleteCurrent()"><i class="fa-solid fa-trash"></i> Delete</button>
                </div>
                <input id="file-name" placeholder="path/to/file.py" />
                <div class="space"></div>
                <textarea id="file-content" placeholder="Select a text file to edit"></textarea>
              </div>
            </div>
          </div>
        </div>`;
      await loadFileList();
    }

    function closeModal(e){
      if(e && e.target !== e.currentTarget) return;
      document.getElementById('modal-root').innerHTML = '';
    }

    async function loadFileList(){
      const res = await fetch(`${API}/files/list`, {
        method:'POST',
        headers:{'Content-Type':'application/json', ...authHeaders()},
        body:JSON.stringify({token:authToken, app_name:fileModalState.app, path:fileModalState.path})
      });
      const data = await res.json();
      const box = document.getElementById('file-list');
      if(!res.ok){
        box.innerHTML = `<div class="muted">${escapeHtml(data.error || 'Unable to load')}</div>`;
        return;
      }
      document.getElementById('file-path').innerText = data.path || '';
      fileModalState.items = data.items || [];
      box.innerHTML = fileModalState.items.map(item => `
        <div class="file-item" onclick="openItem('${escapeJs(item.path)}','${item.type}')">
          <div><i class="fa-solid fa-${item.type === 'dir' ? 'folder' : 'file-lines'}"></i> ${escapeHtml(item.name)}</div>
          <div class="muted">${item.type}</div>
        </div>`).join('') || `<div class="muted" style="padding:12px">Empty folder</div>`;
    }

    async function openItem(path, type){
      if(type === 'dir'){
        fileModalState.path = path;
        await loadFileList();
        return;
      }
      const res = await fetch(`${API}/files/read`, {
        method:'POST',
        headers:{'Content-Type':'application/json', ...authHeaders()},
        body:JSON.stringify({token:authToken, app_name:fileModalState.app, path})
      });
      const data = await res.json();
      if(!res.ok) return showToast(data.error || 'Cannot open file', 'bad');
      document.getElementById('file-name').value = path;
      document.getElementById('file-content').value = data.content || '';
    }

    async function saveFile(){
      const path = document.getElementById('file-name').value.trim();
      const content = document.getElementById('file-content').value;
      if(!path) return showToast('Enter file path','warn');
      const res = await fetch(`${API}/files/write`, {
        method:'POST',
        headers:{'Content-Type':'application/json', ...authHeaders()},
        body:JSON.stringify({token:authToken, app_name:fileModalState.app, path, content})
      });
      const data = await res.json();
      if(res.ok){
        showToast('Saved','ok');
        await loadFileList();
        await refreshApps(true);
      }else{
        showToast(data.error || 'Save failed','bad');
      }
    }

    async function deleteCurrent(){
      const path = document.getElementById('file-name').value.trim();
      if(!path) return showToast('Select a file or folder first','warn');
      if(!confirm(`Delete ${path}?`)) return;
      const res = await fetch(`${API}/files/delete`, {
        method:'POST',
        headers:{'Content-Type':'application/json', ...authHeaders()},
        body:JSON.stringify({token:authToken, app_name:fileModalState.app, path})
      });
      const data = await res.json();
      if(res.ok){
        showToast('Deleted','ok');
        document.getElementById('file-name').value='';
        document.getElementById('file-content').value='';
        await loadFileList();
        await refreshApps(true);
      }else{
        showToast(data.error || 'Delete failed','bad');
      }
    }

    async function mkdirPrompt(){
      const name = prompt('Folder name or path (e.g. logs/archive)');
      if(!name) return;
      const res = await fetch(`${API}/files/mkdir`, {
        method:'POST',
        headers:{'Content-Type':'application/json', ...authHeaders()},
        body:JSON.stringify({token:authToken, app_name:fileModalState.app, path:name})
      });
      const data = await res.json();
      if(res.ok){
        showToast('Folder created','ok');
        await loadFileList();
      }else{
        showToast(data.error || 'Create failed','bad');
      }
    }

    async function upOne(){
      if(!fileModalState.path) return;
      const parts = fileModalState.path.split('/').filter(Boolean);
      parts.pop();
      fileModalState.path = parts.join('/');
      await loadFileList();
    }

    async function refreshAll(){
      if(!authToken) return;
      await Promise.all([refreshOverview(true), refreshApps(true)]);
    }

    let refreshTimer = null;
    function startAutoRefresh(){
      if(refreshTimer) clearInterval(refreshTimer);
      refreshTimer = setInterval(() => {
        if(!authToken || document.hidden) return;
        refreshOverview(true);
        refreshApps(true);
      }, REFRESH_MS);
    }

    window.addEventListener('resize', () => renderApps());
    document.addEventListener('visibilitychange', () => {
      if(!document.hidden && authToken){
        refreshOverview(true);
        refreshApps(true);
      }
    });

    window.addEventListener('DOMContentLoaded', async () => {
      if(authToken){
        showDash();
        await refreshAll();
      }else{
        showLogin();
      }
      startAutoRefresh();
    });
  