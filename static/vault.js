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
    let currentFolder = null; // null = root
    let selectedId = null;
    let loaded = false;
    let openMenu = null;
    let renaming = false;
    let lastTap = { id: null, t: 0 };

    const byId = (id) => items.find((i) => i.id === id);
    const childrenOf = (pid) => items.filter((i) => i.parent_id === pid);
    const now = () => (window.performance && performance.now ? performance.now() : Date.now());

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

    // ---- icons ----
    function folderIcon() {
        return '<svg viewBox="0 0 48 40" fill="none" aria-hidden="true">'
            + '<path d="M2 9a4 4 0 0 1 4-4h11l4 5h19a4 4 0 0 1 4 4v20a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V9Z" fill="currentColor"/>'
            + '<path d="M2 13h44v-1a4 4 0 0 0-4-4H21l-4-5H6a4 4 0 0 0-4 4v6Z" fill="#fff" opacity="0.18"/></svg>';
    }
    function fileIcon() {
        return '<svg viewBox="0 0 40 48" fill="none" aria-hidden="true">'
            + '<path d="M6 4a3 3 0 0 1 3-3h17l8 8v32a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3V4Z" fill="currentColor"/>'
            + '<path d="M26 1v6a2 2 0 0 0 2 2h6" fill="#000" opacity="0.18"/></svg>';
    }
    function icon(item) {
        if (item.kind === 'file' && item.mime && item.mime.indexOf('image/') === 0) {
            return `<img class="vault_item__thumb" src="${escapeHtml(item.url)}" alt="" draggable="false">`;
        }
        return item.kind === 'folder' ? folderIcon() : fileIcon();
    }

    // ---- rendering ----
    function renderItem(item) {
        const el = document.createElement('div');
        el.className = 'vault_item vault_item--' + item.kind;
        if (item.id === selectedId) el.classList.add('vault_item--selected');
        el.dataset.id = item.id;
        el.style.left = item.x + 'px';
        el.style.top = item.y + 'px';

        const iconEl = document.createElement('div');
        iconEl.className = 'vault_item__icon';
        iconEl.innerHTML = icon(item);

        const nameEl = document.createElement('div');
        nameEl.className = 'vault_item__name';
        nameEl.textContent = item.name;

        el.append(iconEl, nameEl);
        canvas.appendChild(el);
        attach(el, item);
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
        btn.dataset.folderId = folderId == null ? '' : folderId;
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
            sep.textContent = '›';
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
        } else if (item.mime && item.mime.indexOf('image/') === 0) {
            openLightbox(item);
        } else {
            window.open(item.url, '_blank', 'noopener');
        }
    }

    function openLightbox(item) {
        const ov = document.createElement('div');
        ov.className = 'vault_lightbox';
        ov.innerHTML =
            '<div class="vault_lightbox__bar">' +
            '<span class="vault_lightbox__name"></span>' +
            '<a class="vault_lightbox__open" target="_blank" rel="noopener">Open ↗</a>' +
            '<button type="button" class="vault_lightbox__close" aria-label="Close">✕</button>' +
            '</div>' +
            '<div class="vault_lightbox__stage"><img alt=""></div>';
        ov.querySelector('.vault_lightbox__name').textContent = item.name;
        ov.querySelector('.vault_lightbox__open').href = item.url;
        const stage = ov.querySelector('.vault_lightbox__stage');
        const img = ov.querySelector('img');
        // cache-bust so a stale/expired cached response is never shown
        img.src = item.url + (item.url.indexOf('?') === -1 ? '?' : '&') + 't=' + Date.now();
        img.onerror = () => {
            stage.innerHTML = '<p class="vault_lightbox__err">Couldn’t load this image. <a href="' + item.url + '" target="_blank" rel="noopener">Open it directly</a>.</p>';
        };
        function close() { ov.remove(); document.removeEventListener('keydown', onKey); }
        function onKey(e) { if (e.key === 'Escape') close(); }
        ov.addEventListener('click', (e) => { if (e.target === ov || e.target.closest('.vault_lightbox__close')) close(); });
        document.addEventListener('keydown', onKey);
        document.body.appendChild(ov);
    }

    // ---- mutations ----
    async function moveItem(item, payload) {
        const body = { x: payload.x != null ? payload.x : item.x, y: payload.y != null ? payload.y : item.y };
        if ('parent_id' in payload) body.parent_id = payload.parent_id;
        item.x = body.x;
        item.y = body.y;
        if ('parent_id' in payload) item.parent_id = payload.parent_id;
        render();
        try {
            await api(`/chat/${chatId}/vault/item/${item.id}/move`, { method: 'POST', json: body });
        } catch (err) { load(); }
    }

    function moveOut(item) {
        if (item.parent_id == null) return;
        const parent = byId(item.parent_id);
        moveItem(item, { parent_id: parent ? parent.parent_id : null, x: 40, y: 40 });
    }

    function startRename(id) {
        const el = canvas.querySelector(`.vault_item[data-id="${id}"]`);
        const item = byId(id);
        if (!el || !item) return;
        const nameEl = el.querySelector('.vault_item__name');
        if (!nameEl) return;

        renaming = true;
        const input = document.createElement('input');
        input.className = 'vault_item__rename';
        input.value = item.name;
        nameEl.replaceWith(input);
        input.focus();
        input.select();

        let settled = false;
        const finish = async (save) => {
            if (settled) return;
            settled = true;
            renaming = false;
            const value = input.value.trim();
            if (save && value && value !== item.name) {
                item.name = value;
                try {
                    await api(`/chat/${chatId}/vault/item/${id}/rename`, { method: 'POST', json: { name: value } });
                } catch (err) { load(); return; }
            }
            render();
        };

        input.addEventListener('pointerdown', (e) => e.stopPropagation());
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true); }
            else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
        });
        input.addEventListener('blur', () => finish(true));
    }

    function collectSubtree(id) {
        const ids = new Set([id]);
        let changed = true;
        while (changed) {
            changed = false;
            items.forEach((i) => {
                if (i.parent_id != null && ids.has(i.parent_id) && !ids.has(i.id)) { ids.add(i.id); changed = true; }
            });
        }
        return ids;
    }

    async function deleteItem(item) {
        const extra = item.kind === 'folder' ? ' and everything inside it' : '';
        if (!window.confirm(`Delete "${item.name}"${extra}? This cannot be undone.`)) return;
        const remove = collectSubtree(item.id);
        if (remove.has(currentFolder)) currentFolder = byId(currentFolder)?.parent_id ?? null;
        items = items.filter((i) => !remove.has(i.id));
        if (remove.has(selectedId)) selectedId = null;
        render();
        try {
            await api(`/chat/${chatId}/vault/item/${item.id}/delete`, { method: 'POST' });
        } catch (err) { load(); }
    }

    async function createFolder() {
        try {
            const offset = childrenOf(currentFolder).length * 10 % 120;
            const data = await api(`/chat/${chatId}/vault/folder`, {
                method: 'POST',
                json: { name: 'New folder', parent_id: currentFolder, x: 40 + offset, y: 40 + offset },
            });
            items.push(data.item);
            render();
            selectItem(data.item.id);
            startRename(data.item.id);
        } catch (err) { window.alert(err.message); }
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
        } catch (err) { window.alert(err.message); }
    }

    // ---- drag targets (folders + breadcrumb crumbs) ----
    function dropTargetUnder(ev, excludeId) {
        const stack = document.elementsFromPoint(ev.clientX, ev.clientY);
        for (const node of stack) {
            if (!node.closest) continue;
            const folder = node.closest('.vault_item--folder');
            if (folder && Number(folder.dataset.id) !== excludeId) return { el: folder, parentId: Number(folder.dataset.id) };
            const cr = node.closest('.vault__crumb');
            if (cr) return { el: cr, parentId: cr.dataset.folderId ? Number(cr.dataset.folderId) : null };
        }
        return null;
    }
    function clearDropHighlight() {
        canvas.querySelectorAll('.vault_item--drop-target').forEach((n) => n.classList.remove('vault_item--drop-target'));
        breadcrumb.querySelectorAll('.vault__crumb--drop').forEach((n) => n.classList.remove('vault__crumb--drop'));
    }

    // ---- unified pointer interaction (tap/double-tap/long-press/drag) ----
    function attach(el, item) {
        el.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            e.stopPropagation();
            selectItem(item.id);
            showItemMenu(item, e.clientX, e.clientY);
        });

        el.addEventListener('pointerdown', (e) => {
            if (e.button === 2 || renaming) return;
            const startX = e.clientX, startY = e.clientY;
            const origX = item.x, origY = item.y;
            let moved = false, curX = origX, curY = origY, longPressed = false;
            let lastX = e.clientX, lastY = e.clientY;
            try { el.setPointerCapture(e.pointerId); } catch (_) {}

            const longTimer = setTimeout(() => {
                if (!moved) {
                    longPressed = true;
                    selectItem(item.id);
                    showItemMenu(item, lastX, lastY);
                }
            }, 500);

            function onMove(ev) {
                lastX = ev.clientX; lastY = ev.clientY;
                const dx = ev.clientX - startX, dy = ev.clientY - startY;
                if (!moved && Math.hypot(dx, dy) < 7) return;
                if (!moved) { moved = true; clearTimeout(longTimer); el.classList.add('vault_item--dragging'); el.style.pointerEvents = 'none'; }
                curX = Math.max(0, origX + dx);
                curY = Math.max(0, origY + dy);
                el.style.left = curX + 'px';
                el.style.top = curY + 'px';
                clearDropHighlight();
                const target = dropTargetUnder(ev, item.id);
                if (target) target.el.classList.add(target.el.classList.contains('vault__crumb') ? 'vault__crumb--drop' : 'vault_item--drop-target');
            }

            function cleanup() {
                clearTimeout(longTimer);
                el.removeEventListener('pointermove', onMove);
                el.removeEventListener('pointerup', onUp);
                el.removeEventListener('pointercancel', onCancel);
                try { el.releasePointerCapture(e.pointerId); } catch (_) {}
            }

            function onCancel() { cleanup(); el.classList.remove('vault_item--dragging'); el.style.pointerEvents = ''; clearDropHighlight(); render(); }

            function onUp(ev) {
                cleanup();
                if (moved) {
                    el.classList.remove('vault_item--dragging');
                    el.style.pointerEvents = '';
                    clearDropHighlight();
                    const target = dropTargetUnder(ev, item.id);
                    if (target && target.parentId !== item.parent_id) moveItem(item, { parent_id: target.parentId, x: 40, y: 40 });
                    else if (!target) moveItem(item, { x: curX, y: curY });
                    else render();
                    return;
                }
                if (longPressed) return;
                // a tap: second tap on same item opens it, otherwise select
                const t = now();
                if (lastTap.id === item.id && (t - lastTap.t) < 400) {
                    lastTap = { id: null, t: 0 };
                    openItem(item);
                } else {
                    lastTap = { id: item.id, t: t };
                    selectItem(item.id);
                }
            }

            el.addEventListener('pointermove', onMove);
            el.addEventListener('pointerup', onUp);
            el.addEventListener('pointercancel', onCancel);
        });
    }

    // ---- context menus ----
    function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }

    function buildMenu(entries, x, y) {
        closeMenu();
        const menu = document.createElement('div');
        menu.className = 'vault_menu';
        entries.forEach((entry) => {
            if (entry === '-') {
                const sep = document.createElement('div');
                sep.className = 'vault_menu__sep';
                menu.appendChild(sep);
                return;
            }
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.dataset.act = entry.act;
            btn.textContent = entry.label;
            btn.addEventListener('click', () => { closeMenu(); entry.run(); });
            menu.appendChild(btn);
        });
        document.body.appendChild(menu);
        const rect = menu.getBoundingClientRect();
        menu.style.left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)) + 'px';
        menu.style.top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8)) + 'px';
        openMenu = menu;
        setTimeout(() => document.addEventListener('pointerdown', closeMenu, { once: true }), 0);
    }

    function showItemMenu(item, x, y) {
        const entries = [
            { act: 'open', label: item.kind === 'folder' ? 'Open' : 'Open / download', run: () => openItem(item) },
            { act: 'rename', label: 'Rename', run: () => startRename(item.id) },
        ];
        if (item.parent_id != null) entries.push({ act: 'moveout', label: 'Move out of folder', run: () => moveOut(item) });
        entries.push('-', { act: 'delete', label: item.kind === 'folder' ? 'Delete folder' : 'Delete file', run: () => deleteItem(item) });
        buildMenu(entries, x, y);
    }

    function showCanvasMenu(x, y) {
        buildMenu([
            { act: 'newfolder', label: 'New folder', run: createFolder },
            { act: 'upload', label: 'Upload files…', run: () => fileInput.click() },
        ], x, y);
    }

    // ---- load ----
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

    // ---- wiring ----
    newFolderBtn.addEventListener('click', createFolder);
    uploadBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => { uploadFiles(fileInput.files, { x: 56, y: 56 }); fileInput.value = ''; });

    // empty-canvas: tap to deselect, long-press / right-click for menu
    let canvasLongTimer = null;
    canvas.addEventListener('pointerdown', (e) => {
        if (e.target !== canvas && !e.target.closest('.vault__empty')) return;
        selectItem(null);
        const x = e.clientX, y = e.clientY;
        canvasLongTimer = setTimeout(() => showCanvasMenu(x, y), 500);
    });
    canvas.addEventListener('pointermove', () => { if (canvasLongTimer) { clearTimeout(canvasLongTimer); canvasLongTimer = null; } });
    canvas.addEventListener('pointerup', () => { if (canvasLongTimer) { clearTimeout(canvasLongTimer); canvasLongTimer = null; } });
    canvas.addEventListener('contextmenu', (e) => {
        if (e.target === canvas || e.target.closest('.vault__empty')) { e.preventDefault(); showCanvasMenu(e.clientX, e.clientY); }
    });

    canvas.addEventListener('keydown', (e) => {
        if (renaming || selectedId == null) return;
        if (e.key === 'Delete' || e.key === 'Backspace') { const it = byId(selectedId); if (it) deleteItem(it); }
        else if (e.key === 'Enter') { startRename(selectedId); }
    });

    canvas.addEventListener('dragover', (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') !== -1) { e.preventDefault(); dropzone.hidden = false; }
    });
    canvas.addEventListener('dragleave', (e) => { if (e.target === canvas || e.target === dropzone) dropzone.hidden = true; });
    canvas.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.hidden = true;
        if (!e.dataTransfer || !e.dataTransfer.files.length) return;
        const rect = canvas.getBoundingClientRect();
        uploadFiles(e.dataTransfer.files, { x: e.clientX - rect.left + canvas.scrollLeft, y: e.clientY - rect.top + canvas.scrollTop });
    });

    window.PixiVault = {
        activate() { if (loaded) return; loaded = true; load(); },
    };
})();
