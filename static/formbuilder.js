(function () {
    const canvas = document.getElementById('forms-canvas');
    if (!canvas) return;
    const breadcrumb = document.getElementById('forms-breadcrumb');
    const emptyEl = document.getElementById('forms-empty');
    const newFolderBtn = document.getElementById('forms-new-folder');
    const newFormBtn = document.getElementById('forms-new');

    let items = [];
    let currentFolder = null;
    let selectedId = null;
    let openMenu = null;
    let renaming = false;
    let lastTap = { id: null, t: 0 };

    const root = document.getElementById('forms');
    const byId = (id) => items.find((i) => i.id === id);
    const childrenOf = (pid) => items.filter((i) => i.parent_id === pid);
    const nowTs = () => (window.performance && performance.now ? performance.now() : Date.now());

    // Touch screens jitter, so accidental drags are easy — need a bigger threshold.
    const COARSE = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    const DRAG_THRESHOLD = COARSE ? 12 : 7;
    const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    async function api(path, opts) {
        opts = opts || {};
        const init = { method: opts.method || 'GET', headers: {} };
        if (opts.json) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
        const res = await fetch(path, init);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Request failed');
        return data;
    }

    function folderIcon() {
        return '<svg viewBox="0 0 48 40" fill="none"><path d="M2 9a4 4 0 0 1 4-4h11l4 5h19a4 4 0 0 1 4 4v20a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V9Z" fill="currentColor"/><path d="M2 13h44v-1a4 4 0 0 0-4-4H21l-4-5H6a4 4 0 0 0-4 4v6Z" fill="#fff" opacity="0.18"/></svg>';
    }
    function formIcon() {
        return '<svg viewBox="0 0 40 48" fill="none"><path d="M6 4a3 3 0 0 1 3-3h17l8 8v32a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3V4Z" fill="currentColor"/><path d="M26 1v6a2 2 0 0 0 2 2h6" fill="#000" opacity="0.18"/><rect x="12" y="20" width="16" height="2.4" rx="1.2" fill="#fff"/><rect x="12" y="26" width="16" height="2.4" rx="1.2" fill="#fff"/><rect x="12" y="32" width="10" height="2.4" rx="1.2" fill="#fff"/></svg>';
    }

    function renderItem(item) {
        const el = document.createElement('div');
        el.className = 'vault_item vault_item--' + item.kind;
        if (item.id === selectedId) el.classList.add('vault_item--selected');
        el.dataset.id = item.id;
        el.style.left = item.x + 'px';
        el.style.top = item.y + 'px';
        const iconEl = document.createElement('div');
        iconEl.className = 'vault_item__icon';
        iconEl.innerHTML = item.kind === 'folder' ? folderIcon() : formIcon();
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
        updateActionBar();
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
        while (f != null) { const it = byId(f); if (!it) break; path.unshift(it); f = it.parent_id; }
        breadcrumb.innerHTML = '';
        breadcrumb.appendChild(crumb('Forms', null));
        path.forEach((it) => {
            const sep = document.createElement('span'); sep.className = 'vault__crumb-sep'; sep.textContent = '›';
            breadcrumb.appendChild(sep); breadcrumb.appendChild(crumb(it.name, it.id));
        });
    }

    function selectItem(id) {
        selectedId = id;
        canvas.querySelectorAll('.vault_item').forEach((el) => el.classList.toggle('vault_item--selected', Number(el.dataset.id) === id));
        updateActionBar();
    }

    // Always-visible action bar for the selected item — reliable Rename/Delete on touch.
    let actionBar = null;
    function ensureActionBar() {
        if (actionBar) return actionBar;
        actionBar = document.createElement('div');
        actionBar.className = 'vault_actions';
        actionBar.hidden = true;
        if (root) root.appendChild(actionBar);
        return actionBar;
    }
    function updateActionBar() {
        const bar = ensureActionBar();
        const item = selectedId != null ? byId(selectedId) : null;
        if (!item) { bar.hidden = true; bar.innerHTML = ''; return; }
        bar.innerHTML = '';
        const name = document.createElement('span');
        name.className = 'vault_actions__name';
        name.textContent = item.name;
        bar.appendChild(name);
        const acts = [
            { label: item.kind === 'folder' ? 'Open' : 'Edit', run: () => openItem(item) },
            { label: 'Rename', run: () => startRename(item.id) },
        ];
        if (item.parent_id != null) acts.push({ label: 'Move out', run: () => moveOut(item) });
        acts.push({ label: 'Delete', danger: true, run: () => deleteItem(item) });
        acts.forEach((a) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'vault_actions__btn' + (a.danger ? ' vault_actions__btn--danger' : '');
            b.textContent = a.label;
            b.addEventListener('click', a.run);
            bar.appendChild(b);
        });
        bar.hidden = false;
    }

    function openItem(item) {
        if (item.kind === 'folder') { currentFolder = item.id; selectedId = null; render(); }
        else openBuilder(item.id);
    }

    async function moveItem(item, payload) {
        const body = { x: payload.x != null ? payload.x : item.x, y: payload.y != null ? payload.y : item.y };
        if ('parent_id' in payload) body.parent_id = payload.parent_id;
        item.x = body.x; item.y = body.y;
        if ('parent_id' in payload) item.parent_id = payload.parent_id;
        render();
        try { await api('/forms/item/' + item.id + '/move', { method: 'POST', json: body }); } catch (e) { load(); }
    }
    function moveOut(item) {
        if (item.parent_id == null) return;
        const parent = byId(item.parent_id);
        moveItem(item, { parent_id: parent ? parent.parent_id : null, x: 40, y: 40 });
    }

    function startRename(id) {
        const el = canvas.querySelector('.vault_item[data-id="' + id + '"]');
        const item = byId(id);
        if (!el || !item) return;
        const nameEl = el.querySelector('.vault_item__name');
        renaming = true;
        const input = document.createElement('input');
        input.className = 'vault_item__rename';
        input.value = item.name;
        nameEl.replaceWith(input);
        input.focus();
        try { input.setSelectionRange(0, input.value.length); } catch (_) { input.select(); }
        let settled = false;
        const finish = async (save) => {
            if (settled) return; settled = true; renaming = false;
            const v = input.value.trim();
            if (save && v && v !== item.name) {
                item.name = v;
                try { await api('/forms/item/' + id + '/rename', { method: 'POST', json: { name: v } }); } catch (e) { load(); return; }
            }
            render();
        };
        input.addEventListener('pointerdown', (e) => e.stopPropagation());
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); finish(true); } else if (e.key === 'Escape') { e.preventDefault(); finish(false); } });
        input.addEventListener('blur', () => finish(true));
    }

    function collectSubtree(id) {
        const ids = new Set([id]); let changed = true;
        while (changed) { changed = false; items.forEach((i) => { if (i.parent_id != null && ids.has(i.parent_id) && !ids.has(i.id)) { ids.add(i.id); changed = true; } }); }
        return ids;
    }
    async function deleteItem(item) {
        const extra = item.kind === 'folder' ? ' and everything inside it' : '';
        if (!window.confirm('Delete "' + item.name + '"' + extra + '?')) return;
        const remove = collectSubtree(item.id);
        if (remove.has(currentFolder)) currentFolder = byId(currentFolder)?.parent_id ?? null;
        items = items.filter((i) => !remove.has(i.id));
        if (remove.has(selectedId)) selectedId = null;
        render();
        try { await api('/forms/item/' + item.id + '/delete', { method: 'POST' }); } catch (e) { load(); }
    }

    async function createFolder() {
        try {
            const offset = childrenOf(currentFolder).length * 10 % 120;
            const data = await api('/forms/folder', { method: 'POST', json: { name: 'New folder', parent_id: currentFolder, x: 40 + offset, y: 40 + offset } });
            items.push(data.item); render(); selectItem(data.item.id); startRename(data.item.id);
        } catch (e) { window.alert(e.message); }
    }

    // ---- drag targets ----
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

    function attach(el, item) {
        el.addEventListener('contextmenu', (e) => { e.preventDefault(); e.stopPropagation(); selectItem(item.id); showItemMenu(item, e.clientX, e.clientY); });
        el.addEventListener('pointerdown', (e) => {
            if (e.button === 2 || renaming) return;
            const sx = e.clientX, sy = e.clientY, ox = item.x, oy = item.y;
            let moved = false, cx = ox, cy = oy, longPressed = false, lx = e.clientX, ly = e.clientY;
            try { el.setPointerCapture(e.pointerId); } catch (_) {}
            const longTimer = setTimeout(() => { if (!moved) { longPressed = true; selectItem(item.id); showItemMenu(item, lx, ly); } }, 500);
            function onMove(ev) {
                lx = ev.clientX; ly = ev.clientY;
                const dx = ev.clientX - sx, dy = ev.clientY - sy;
                if (!moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
                if (!moved) { moved = true; clearTimeout(longTimer); el.classList.add('vault_item--dragging'); el.style.pointerEvents = 'none'; }
                cx = Math.max(0, ox + dx); cy = Math.max(0, oy + dy);
                el.style.left = cx + 'px'; el.style.top = cy + 'px';
                clearDropHighlight();
                const t = dropTargetUnder(ev, item.id);
                if (t) t.el.classList.add(t.el.classList.contains('vault__crumb') ? 'vault__crumb--drop' : 'vault_item--drop-target');
            }
            function cleanup() {
                clearTimeout(longTimer);
                el.removeEventListener('pointermove', onMove); el.removeEventListener('pointerup', onUp); el.removeEventListener('pointercancel', onCancel);
                try { el.releasePointerCapture(e.pointerId); } catch (_) {}
            }
            function onCancel() { cleanup(); el.classList.remove('vault_item--dragging'); el.style.pointerEvents = ''; clearDropHighlight(); render(); }
            function onUp(ev) {
                cleanup();
                if (moved) {
                    el.classList.remove('vault_item--dragging'); el.style.pointerEvents = ''; clearDropHighlight();
                    const t = dropTargetUnder(ev, item.id);
                    if (t && t.parentId !== item.parent_id) moveItem(item, { parent_id: t.parentId, x: 40, y: 40 });
                    else if (!t) moveItem(item, { x: cx, y: cy });
                    else render();
                    return;
                }
                if (longPressed) return;
                const t = nowTs();
                if (lastTap.id === item.id && (t - lastTap.t) < 400) { lastTap = { id: null, t: 0 }; openItem(item); }
                else { lastTap = { id: item.id, t: t }; selectItem(item.id); }
            }
            el.addEventListener('pointermove', onMove); el.addEventListener('pointerup', onUp); el.addEventListener('pointercancel', onCancel);
        });
    }

    // ---- menus ----
    function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
    function buildMenu(entries, x, y) {
        closeMenu();
        const menu = document.createElement('div'); menu.className = 'vault_menu';
        entries.forEach((entry) => {
            if (entry === '-') { const s = document.createElement('div'); s.className = 'vault_menu__sep'; menu.appendChild(s); return; }
            const b = document.createElement('button'); b.type = 'button'; b.dataset.act = entry.act; b.textContent = entry.label;
            b.addEventListener('click', () => { closeMenu(); entry.run(); }); menu.appendChild(b);
        });
        document.body.appendChild(menu);
        const r = menu.getBoundingClientRect();
        menu.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + 'px';
        menu.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
        openMenu = menu;
        setTimeout(() => document.addEventListener('pointerdown', closeMenu, { once: true }), 0);
    }
    function showItemMenu(item, x, y) {
        const entries = [{ act: 'open', label: item.kind === 'folder' ? 'Open' : 'Edit', run: () => openItem(item) }, { act: 'rename', label: 'Rename', run: () => startRename(item.id) }];
        if (item.parent_id != null) entries.push({ act: 'moveout', label: 'Move out of folder', run: () => moveOut(item) });
        entries.push('-', { act: 'delete', label: item.kind === 'folder' ? 'Delete folder' : 'Delete form', run: () => deleteItem(item) });
        buildMenu(entries, x, y);
    }
    function showCanvasMenu(x, y) {
        buildMenu([{ act: 'newform', label: 'New form', run: () => openBuilder(null) }, { act: 'newfolder', label: 'New folder', run: createFolder }], x, y);
    }

    // ================= BUILDER =================
    const fb = document.getElementById('fb');
    const fbTitle = document.getElementById('fb-title');
    const fbFields = document.getElementById('fb-fields');
    const fbPreview = document.getElementById('fb-preview');
    let editingId = null;
    let builderParent = null;
    let fields = [];
    let fieldSeq = 0;

    const TYPE_LABEL = { text: 'Short text', textarea: 'Paragraph', file: 'File upload' };

    function openBuilder(formId) {
        closeMenu();
        editingId = formId;
        builderParent = currentFolder;
        if (formId) {
            api('/forms/item/' + formId).then((data) => {
                fbTitle.value = data.name || '';
                fields = ((data.schema && data.schema.fields) || []).map((f) => Object.assign({}, f, { id: f.id || ('f' + (fieldSeq++)) }));
                renderBuilder();
                fb.hidden = false;
            }).catch((e) => window.alert(e.message));
        } else {
            fbTitle.value = '';
            fields = [];
            renderBuilder();
            fb.hidden = false;
        }
    }
    function closeBuilder() { fb.hidden = true; }

    function addField(type) {
        fields.push({ id: 'f' + (fieldSeq++), type: type, label: '', required: false, placeholder: '', folder: type === 'file' ? '' : undefined, to_root: type === 'file' ? true : undefined });
        renderBuilder();
    }

    function fieldEditor(field, index) {
        const card = document.createElement('div');
        card.className = 'fb_field';

        const top = document.createElement('div');
        top.className = 'fb_field__top';
        const tag = document.createElement('span');
        tag.className = 'fb_field__type';
        tag.textContent = TYPE_LABEL[field.type] || field.type;
        const labelInput = document.createElement('input');
        labelInput.className = 'fb_field__label-input';
        labelInput.placeholder = 'Question / label';
        labelInput.value = field.label || '';
        labelInput.addEventListener('input', () => { field.label = labelInput.value; renderPreview(); });
        const btns = document.createElement('div');
        btns.className = 'fb_field__btns';
        const up = iconBtn('↑', 'Move up', () => { if (index > 0) { [fields[index - 1], fields[index]] = [fields[index], fields[index - 1]]; renderBuilder(); } });
        const down = iconBtn('↓', 'Move down', () => { if (index < fields.length - 1) { [fields[index + 1], fields[index]] = [fields[index], fields[index + 1]]; renderBuilder(); } });
        const del = iconBtn('✕', 'Remove', () => { fields.splice(index, 1); renderBuilder(); });
        del.classList.add('fb_field__iconbtn--del');
        btns.append(up, down, del);
        top.append(tag, labelInput, btns);
        card.appendChild(top);

        const row = document.createElement('div');
        row.className = 'fb_field__row';
        const reqLabel = document.createElement('label');
        const req = document.createElement('input');
        req.type = 'checkbox';
        req.checked = !!field.required;
        req.addEventListener('change', () => { field.required = req.checked; renderPreview(); });
        reqLabel.append(req, document.createTextNode(' Required'));
        row.appendChild(reqLabel);

        if (field.type !== 'file') {
            const ph = document.createElement('input');
            ph.type = 'text';
            ph.placeholder = 'Placeholder (optional)';
            ph.value = field.placeholder || '';
            ph.addEventListener('input', () => { field.placeholder = ph.value; renderPreview(); });
            row.appendChild(ph);
        }
        card.appendChild(row);

        if (field.type === 'file') {
            // Where the client's uploaded file is saved in their PixiVault.
            if (field.to_root === undefined) field.to_root = !field.folder;
            const save = document.createElement('div');
            save.className = 'fb_field__save';

            const heading = document.createElement('div');
            heading.className = 'fb_field__save-title';
            heading.textContent = 'Where do uploads get saved?';
            save.appendChild(heading);

            const rootLabel = document.createElement('label');
            rootLabel.className = 'fb_field__check';
            const root = document.createElement('input');
            root.type = 'checkbox';
            root.checked = field.to_root !== false;
            rootLabel.append(root, document.createTextNode(' Save to main vault'));

            const folderLabel = document.createElement('label');
            folderLabel.className = 'fb_field__check';
            const useFolder = document.createElement('input');
            useFolder.type = 'checkbox';
            useFolder.checked = !!field.folder;
            folderLabel.append(useFolder, document.createTextNode(' Save into a folder'));

            const folder = document.createElement('input');
            folder.type = 'text';
            folder.className = 'fb_field__folder';
            folder.placeholder = 'Folder name (e.g. icons)';
            folder.value = field.folder || '';
            folder.hidden = !useFolder.checked;

            const note = document.createElement('p');
            note.className = 'fb_field__note';
            note.textContent = 'The folder is created automatically if it doesn’t exist yet.';
            note.hidden = !useFolder.checked;

            function sync() {
                // A file has to land somewhere — keep at least one box ticked.
                if (!root.checked && !useFolder.checked) { root.checked = true; }
                folder.hidden = !useFolder.checked;
                note.hidden = !useFolder.checked;
                field.to_root = root.checked;
                field.folder = useFolder.checked ? folder.value.trim() : '';
                renderPreview();
            }
            root.addEventListener('change', sync);
            useFolder.addEventListener('change', () => { sync(); if (useFolder.checked) folder.focus(); });
            folder.addEventListener('input', () => { field.folder = folder.value.trim(); renderPreview(); });

            const checks = document.createElement('div');
            checks.className = 'fb_field__checks';
            checks.append(rootLabel, folderLabel);
            save.append(checks, folder, note);
            card.appendChild(save);
        }
        return card;
    }

    function iconBtn(symbol, title, onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'fb_field__iconbtn';
        b.title = title;
        b.textContent = symbol;
        b.addEventListener('click', onClick);
        return b;
    }

    function renderBuilder() {
        fbFields.innerHTML = '';
        if (!fields.length) {
            const hint = document.createElement('p');
            hint.className = 'fb__hint';
            hint.textContent = 'Add fields below to start building your form.';
            fbFields.appendChild(hint);
        }
        fields.forEach((f, i) => fbFields.appendChild(fieldEditor(f, i)));
        renderPreview();
    }

    function renderPreview() {
        fbPreview.innerHTML = '';
        const label = document.createElement('div');
        label.className = 'fb__preview-label';
        label.textContent = 'Live preview';
        fbPreview.appendChild(label);
        const clean = fields.map((f) => ({ id: f.id, type: f.type, label: f.label || 'Untitled', required: f.required, placeholder: f.placeholder, folder: f.folder, to_root: f.to_root }));
        fbPreview.appendChild(window.PixiForms.buildFormCard({ name: fbTitle.value || 'Untitled form', fields: clean }, { mode: 'preview' }));
    }

    async function saveForm() {
        const name = (fbTitle.value || '').trim() || 'Untitled form';
        const schema = { fields: fields.map((f) => {
            const o = { id: f.id, type: f.type, label: (f.label || '').trim() || 'Untitled', required: !!f.required };
            if (f.type === 'file') {
                o.folder = (f.folder || '').trim();
                o.to_root = f.to_root !== false;
                if (!o.to_root && !o.folder) o.to_root = true;
            } else {
                o.placeholder = (f.placeholder || '').trim();
            }
            return o;
        }) };
        try {
            const payload = { id: editingId, name: name, schema: schema, parent_id: builderParent };
            const data = await api('/forms/save', { method: 'POST', json: payload });
            const idx = items.findIndex((i) => i.id === data.item.id);
            if (idx >= 0) items[idx] = data.item; else items.push(data.item);
            render();
            closeBuilder();
        } catch (e) { window.alert(e.message); }
    }

    document.getElementById('fb-save').addEventListener('click', saveForm);
    document.getElementById('fb-cancel').addEventListener('click', closeBuilder);
    fbTitle.addEventListener('input', renderPreview);
    document.querySelectorAll('.fb__add button').forEach((b) => b.addEventListener('click', () => addField(b.dataset.type)));

    // ---- finder wiring ----
    newFolderBtn.addEventListener('click', createFolder);
    newFormBtn.addEventListener('click', () => openBuilder(null));
    canvas.addEventListener('pointerdown', (e) => { if (e.target === canvas) selectItem(null); });
    let canvasLong = null;
    canvas.addEventListener('pointerdown', (e) => {
        if (e.target !== canvas && !e.target.closest('.vault__empty')) return;
        const x = e.clientX, y = e.clientY;
        canvasLong = setTimeout(() => showCanvasMenu(x, y), 500);
    });
    canvas.addEventListener('pointermove', () => { if (canvasLong) { clearTimeout(canvasLong); canvasLong = null; } });
    canvas.addEventListener('pointerup', () => { if (canvasLong) { clearTimeout(canvasLong); canvasLong = null; } });
    canvas.addEventListener('contextmenu', (e) => { if (e.target === canvas || e.target.closest('.vault__empty')) { e.preventDefault(); showCanvasMenu(e.clientX, e.clientY); } });
    canvas.addEventListener('keydown', (e) => {
        if (renaming || selectedId == null) return;
        if (e.key === 'Delete' || e.key === 'Backspace') { const it = byId(selectedId); if (it) deleteItem(it); }
        else if (e.key === 'Enter') { startRename(selectedId); }
    });

    async function load() {
        try { const data = await api('/forms/list'); items = data.items || []; render(); }
        catch (e) { emptyEl.hidden = false; emptyEl.querySelector('p').textContent = 'Could not load forms.'; }
    }
    load();
})();
