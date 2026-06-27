(function () {
    const root = document.getElementById('vault');
    if (!root) return;

    const chatId = root.dataset.chatId;
    const canvas = document.getElementById('vault-canvas');
    const breadcrumb = document.getElementById('vault-breadcrumb');
    const emptyEl = document.getElementById('vault-empty');
    const dropzone = document.getElementById('vault-dropzone');
    const fileInput = document.getElementById('vault-file-input');
    const newFolderBtn = document.getElementById('vault-new-folder');
    const uploadBtn = document.getElementById('vault-upload');

    let items = [];
    let currentFolder = null; // null = root canvas
    let selectedId = null;
    let loaded = false;
    let openMenu = null;

    const byId = (id) => items.find((i) => i.id === id);
    const childrenOf = (pid) => items.filter((i) => i.parent_id === pid);

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async function api(path, opts) {
        opts = opts || {};
        const init = { method: opts.method || 'GET', headers: {} };
        if (opts.json) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(opts.json);
        }
        const res = await fetch(path, init);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Request failed');
        return data;
    }

    function folderIcon() {
        return '<svg viewBox="0 0 48 40" fill="none" aria-hidden="true"><path d="M3 8a4 4 0 0 1 4-4h11l4 5h19a4 4 0 0 1 4 4v22a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V8Z" fill="currentColor"/></svg>';
    }

    function fileIcon() {
        return '<svg viewBox="0 0 40 48" fill="none" aria-hidden="true"><path d="M6 4a3 3 0 0 1 3-3h17l8 8v32a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3V4Z" fill="currentColor"/><path d="M26 1v8h8" fill="rgba(0,0,0,0.18)"/></svg>';
    }

    function renderIcon(item) {
        if (item.kind === 'file' && item.mime && item.mime.indexOf('image/') === 0) {
            return `<img class="vault_item__thumb" src="${escapeHtml(item.url)}" alt="">`;
        }
        return item.kind === 'folder' ? folderIcon() : fileIcon();
    }

    function renderItem(item) {
        const el = document.createElement('div');
        el.className = 'vault_item vault_item--' + item.kind;
        if (item.id === selectedId) el.classList.add('vault_item--selected');
        el.dataset.id = item.id;
        el.style.left = item.x + 'px';
        el.style.top = item.y + 'px';
        el.innerHTML =
            `<div class="vault_item__icon">${renderIcon(item)}</div>` +
            `<div class="vault_item__name">${escapeHtml(item.name)}</div>`;
        canvas.appendChild(el);
        attachInteractions(el, item);
    }

    function render() {
        canvas.querySelectorAll('.vault_item').forEach((n) => n.remove());
        const kids = childrenOf(currentFolder);
        emptyEl.hidden = kids.length > 0;
        kids.forEach(renderItem);
        renderBreadcrumb();
    }

    function crumb(label, folderId) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'vault__crumb';
        btn.textContent = label;
        btn.addEventListener('click', () => { currentFolder = folderId; selectedId = null; render(); });
        return btn;
    }

    function renderBreadcrumb() {
        const path = [];
        let f = currentFolder;
        while (f != null) {
            const it = byId(f);
            if (!it) break;
            path.unshift(it);
            f = it.parent_id;
        }
        breadcrumb.innerHTML = '';
        breadcrumb.appendChild(crumb('PixiVault', null));
        path.forEach((it) => {
            const sep = document.createElement('span');
            sep.className = 'vault__crumb-sep';
            sep.textContent = '/';
            breadcrumb.appendChild(sep);
            breadcrumb.appendChild(crumb(it.name, it.id));
        });
    }

    function selectItem(id) {
        selectedId = id;
        canvas.querySelectorAll('.vault_item').forEach((el) => {
            el.classList.toggle('vault_item--selected', Number(el.dataset.id) === id);
        });
    }

    function openItem(item) {
        if (item.kind === 'folder') {
            currentFolder = item.id;
            selectedId = null;
            render();
        } else {
            window.open(item.url, '_blank', 'noopener');
        }
    }

    async function moveItem(item, payload) {
        const body = { x: payload.x != null ? payload.x : item.x, y: payload.y != null ? payload.y : item.y };
        if ('parent_id' in payload) body.parent_id = payload.parent_id;
        item.x = body.x;
        item.y = body.y;
        if ('parent_id' in payload) item.parent_id = payload.parent_id;
        render();
        try {
            await api(`/chat/${chatId}/vault/item/${item.id}/move`, { method: 'POST', json: body });
        } catch (err) {
            load();
        }
    }

    function folderElUnder(ev, excludeId) {
        const stack = document.elementsFromPoint(ev.clientX, ev.clientY);
        for (const node of stack) {
            const el = node.closest && node.closest('.vault_item--folder');
            if (el && Number(el.dataset.id) !== excludeId) return el;
        }
        return null;
    }

    function attachInteractions(el, item) {
        el.addEventListener('dblclick', (e) => { e.preventDefault(); openItem(item); });
        el.addEventListener('contextmenu', (e) => { e.preventDefault(); showMenu(e, item); });

        el.addEventListener('pointerdown', (e) => {
            if (e.button !== 0) return;
            e.preventDefault();
            selectItem(item.id);
            const startX = e.clientX, startY = e.clientY;
            const origX = item.x, origY = item.y;
            let moved = false;
            let curX = origX, curY = origY;
            el.setPointerCapture(e.pointerId);

            function onMove(ev) {
                const dx = ev.clientX - startX;
                const dy = ev.clientY - startY;
                if (!moved && Math.hypot(dx, dy) < 4) return;
                if (!moved) {
                    moved = true;
                    el.classList.add('vault_item--dragging');
                    el.style.pointerEvents = 'none';
                }
                curX = Math.max(0, origX + dx);
                curY = Math.max(0, origY + dy);
                el.style.left = curX + 'px';
                el.style.top = curY + 'px';

                canvas.querySelectorAll('.vault_item--drop-target').forEach((n) => n.classList.remove('vault_item--drop-target'));
                const folderEl = folderElUnder(ev, item.id);
                if (folderEl) folderEl.classList.add('vault_item--drop-target');
            }

            function onUp(ev) {
                el.releasePointerCapture(e.pointerId);
                el.removeEventListener('pointermove', onMove);
                el.removeEventListener('pointerup', onUp);
                el.classList.remove('vault_item--dragging');
                el.style.pointerEvents = '';
                canvas.querySelectorAll('.vault_item--drop-target').forEach((n) => n.classList.remove('vault_item--drop-target'));
                if (!moved) return;
                const folderEl = folderElUnder(ev, item.id);
                if (folderEl) {
                    moveItem(item, { parent_id: Number(folderEl.dataset.id), x: 40, y: 40 });
                } else {
                    moveItem(item, { x: curX, y: curY });
                }
            }

            el.addEventListener('pointermove', onMove);
            el.addEventListener('pointerup', onUp);
        });
    }

    function closeMenu() {
        if (openMenu) { openMenu.remove(); openMenu = null; }
    }

    function showMenu(e, item) {
        closeMenu();
        const menu = document.createElement('div');
        menu.className = 'vault_menu';
        menu.style.left = e.clientX + 'px';
        menu.style.top = e.clientY + 'px';
        menu.innerHTML =
            '<button type="button" data-act="open">Open</button>' +
            '<button type="button" data-act="rename">Rename</button>' +
            '<button type="button" data-act="delete">Delete</button>';
        document.body.appendChild(menu);
        openMenu = menu;
        menu.addEventListener('click', (ev) => {
            const act = ev.target.dataset.act;
            if (!act) return;
            if (act === 'open') openItem(item);
            if (act === 'rename') startRename(item.id);
            if (act === 'delete') deleteItem(item);
            closeMenu();
        });
        setTimeout(() => document.addEventListener('pointerdown', closeMenu, { once: true }), 0);
    }

    async function startRename(id) {
        const it = byId(id);
        if (!it) return;
        const name = window.prompt('Rename', it.name);
        if (name == null) return;
        const trimmed = name.trim();
        if (!trimmed) return;
        it.name = trimmed;
        render();
        try {
            await api(`/chat/${chatId}/vault/item/${id}/rename`, { method: 'POST', json: { name: trimmed } });
        } catch (err) {
            load();
        }
    }

    async function deleteItem(item) {
        const extra = item.kind === 'folder' ? ' and everything inside it' : '';
        if (!window.confirm(`Delete "${item.name}"${extra}?`)) return;

        const toRemove = new Set([item.id]);
        let changed = true;
        while (changed) {
            changed = false;
            items.forEach((i) => {
                if (i.parent_id != null && toRemove.has(i.parent_id) && !toRemove.has(i.id)) {
                    toRemove.add(i.id);
                    changed = true;
                }
            });
        }
        if (toRemove.has(currentFolder)) currentFolder = byId(currentFolder)?.parent_id ?? null;
        items = items.filter((i) => !toRemove.has(i.id));
        render();
        try {
            await api(`/chat/${chatId}/vault/item/${item.id}/delete`, { method: 'POST' });
        } catch (err) {
            load();
        }
    }

    async function createFolder() {
        try {
            const offset = childrenOf(currentFolder).length * 12;
            const data = await api(`/chat/${chatId}/vault/folder`, {
                method: 'POST',
                json: { name: 'New folder', parent_id: currentFolder, x: 40 + offset, y: 40 + offset },
            });
            items.push(data.item);
            render();
            startRename(data.item.id);
        } catch (err) {
            window.alert(err.message);
        }
    }

    async function uploadFiles(fileList, pos) {
        if (!fileList || !fileList.length) return;
        const fd = new FormData();
        fd.append('x', pos.x);
        fd.append('y', pos.y);
        if (currentFolder != null) fd.append('parent_id', currentFolder);
        Array.from(fileList).forEach((f) => fd.append('files', f));
        try {
            const res = await fetch(`/chat/${chatId}/vault/upload`, { method: 'POST', body: fd });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || 'Upload failed');
            items.push(...(data.items || []));
            render();
        } catch (err) {
            window.alert(err.message);
        }
    }

    async function load() {
        try {
            const data = await api(`/chat/${chatId}/vault`);
            items = data.items || [];
            render();
        } catch (err) {
            emptyEl.hidden = false;
            emptyEl.querySelector('p').textContent = 'Could not load your vault.';
        }
    }

    newFolderBtn.addEventListener('click', createFolder);
    uploadBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
        uploadFiles(fileInput.files, { x: 60, y: 60 });
        fileInput.value = '';
    });

    canvas.addEventListener('pointerdown', (e) => {
        if (e.target === canvas) selectItem(null);
    });

    canvas.addEventListener('keydown', (e) => {
        if ((e.key === 'Delete' || e.key === 'Backspace') && selectedId != null) {
            const it = byId(selectedId);
            if (it) deleteItem(it);
        }
    });

    canvas.addEventListener('dragover', (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') !== -1) {
            e.preventDefault();
            dropzone.hidden = false;
        }
    });
    canvas.addEventListener('dragleave', (e) => {
        if (e.target === canvas || e.target === dropzone) dropzone.hidden = true;
    });
    canvas.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.hidden = true;
        if (!e.dataTransfer || !e.dataTransfer.files.length) return;
        const rect = canvas.getBoundingClientRect();
        uploadFiles(e.dataTransfer.files, { x: e.clientX - rect.left, y: e.clientY - rect.top });
    });

    window.PixiVault = {
        activate() {
            if (loaded) return;
            loaded = true;
            load();
        },
    };
})();
