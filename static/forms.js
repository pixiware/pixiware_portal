window.PixiForms = (function () {
    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function fieldNode(field, fillable) {
        const wrap = document.createElement('div');
        wrap.className = 'form_field';
        const label = document.createElement('div');
        label.className = 'form_field__label';
        label.innerHTML = esc(field.label) + (field.required ? ' <span class="form_field__req">*</span>' : '');
        wrap.appendChild(label);

        if (field.type === 'textarea') {
            const t = document.createElement('textarea');
            t.placeholder = field.placeholder || '';
            t.dataset.fid = field.id;
            t.disabled = !fillable;
            wrap.appendChild(t);
        } else if (field.type === 'file') {
            const lab = document.createElement('label');
            lab.className = 'form_field__file';
            const inp = document.createElement('input');
            inp.type = 'file';
            inp.multiple = true;
            inp.dataset.fid = field.id;
            inp.hidden = true;
            inp.disabled = !fillable;
            const span = document.createElement('span');
            span.textContent = 'Choose file(s)';
            inp.addEventListener('change', () => {
                span.textContent = inp.files.length ? Array.from(inp.files).map((f) => f.name).join(', ') : 'Choose file(s)';
            });
            lab.append(inp, span);
            wrap.appendChild(lab);
            if (field.folder) {
                const hint = document.createElement('div');
                hint.className = 'form_field__hint';
                hint.textContent = '→ saved to "' + field.folder + '" in PixiVault';
                wrap.appendChild(hint);
            }
        } else {
            const t = document.createElement('input');
            t.type = 'text';
            t.placeholder = field.placeholder || '';
            t.dataset.fid = field.id;
            t.disabled = !fillable;
            wrap.appendChild(t);
        }
        return wrap;
    }

    function answerNode(a) {
        const wrap = document.createElement('div');
        wrap.className = 'form_field';
        const label = document.createElement('div');
        label.className = 'form_field__label';
        label.textContent = a.label;
        wrap.appendChild(label);
        const val = document.createElement('div');
        val.className = 'form_answer';
        if (a.type === 'file') {
            val.textContent = (a.files && a.files.length) ? (a.files.join(', ') + (a.folder ? ' → ' + a.folder : '')) : '—';
        } else {
            val.textContent = a.value || '—';
        }
        wrap.appendChild(val);
        return wrap;
    }

    // mode: 'fill' (client can submit), 'sent' (read-only), 'preview' (builder)
    function buildFormCard(form, opts) {
        opts = opts || {};
        const mode = opts.mode || 'preview';
        const submitted = !!form.submitted;
        const fillable = mode === 'fill' && !submitted;

        const card = document.createElement('div');
        card.className = 'form_card' + (submitted ? ' form_card--submitted' : '');

        const head = document.createElement('div');
        head.className = 'form_card__head';
        head.innerHTML = '<span class="form_card__tag">Form</span><span class="form_card__title">' + esc(form.name || 'Form') + '</span>';
        card.appendChild(head);

        const body = document.createElement('div');
        body.className = 'form_card__body';
        const fields = form.fields || [];
        if (submitted && form.answers) {
            if (!form.answers.length) body.innerHTML = '<p class="form_card__empty">No answers.</p>';
            form.answers.forEach((a) => body.appendChild(answerNode(a)));
        } else if (!fields.length) {
            body.innerHTML = '<p class="form_card__empty">This form has no fields yet.</p>';
        } else {
            fields.forEach((f) => body.appendChild(fieldNode(f, fillable)));
        }
        card.appendChild(body);

        if (submitted) {
            const st = document.createElement('div');
            st.className = 'form_card__status';
            st.textContent = '✓ Submitted';
            card.appendChild(st);
        } else if (fillable) {
            const err = document.createElement('div');
            err.className = 'form_card__error';
            err.hidden = true;
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'form_card__submit';
            btn.textContent = 'Submit';
            btn.addEventListener('click', () => submitForm(card, form, opts, btn, err));
            card.append(err, btn);
        } else if (mode === 'sent') {
            const st = document.createElement('div');
            st.className = 'form_card__status';
            st.textContent = 'Sent · awaiting response';
            card.appendChild(st);
        }
        return card;
    }

    async function submitForm(card, form, opts, btn, err) {
        const fd = new FormData();
        let missing = null;
        (form.fields || []).forEach((f) => {
            const el = card.querySelector('[data-fid="' + f.id + '"]');
            if (!el) return;
            if (f.type === 'file') {
                if (f.required && !el.files.length && !missing) missing = f.label;
                Array.from(el.files).forEach((file) => fd.append(f.id, file));
            } else {
                if (f.required && !el.value.trim() && !missing) missing = f.label;
                fd.append(f.id, el.value);
            }
        });
        if (missing) { err.textContent = missing + ' is required'; err.hidden = false; return; }

        btn.disabled = true;
        btn.textContent = 'Submitting…';
        err.hidden = true;
        try {
            const res = await fetch('/chat/' + opts.chatId + '/form/' + opts.messageId + '/submit', { method: 'POST', body: fd });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || 'Could not submit');
            btn.textContent = 'Submitted';
            if (window.__chatReload) window.__chatReload();
        } catch (e) {
            err.textContent = e.message;
            err.hidden = false;
            btn.disabled = false;
            btn.textContent = 'Submit';
        }
    }

    // Build a chat conversation row containing a form card.
    function chatCard(message) {
        const item = document.createElement('div');
        item.className = 'conversation_item conversation_item--form' + (message.sender === 'you' ? ' conversation_item--you' : '');
        item.dataset.messageId = message.id;
        const mode = window.IS_AGENCY ? 'sent' : 'fill';
        item.appendChild(buildFormCard(message.form, { mode: mode, chatId: window.CHAT_ID, messageId: message.id }));
        return item;
    }

    // ---- chat composer wiring (only on the chat page) ----
    function initChat() {
        const formBtn = document.getElementById('message-form-btn');
        if (!formBtn) return;
        const picker = document.getElementById('form-picker');
        const list = document.getElementById('form-picker-list');
        const closeBtn = document.getElementById('form-picker-close');

        async function openPicker() {
            picker.hidden = false;
            list.innerHTML = '<p class="fp__sub">Loading…</p>';
            try {
                const res = await fetch('/chat/' + window.CHAT_ID + '/forms');
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'Could not load forms');
                if (!data.forms.length) {
                    list.innerHTML = '<div class="fp__empty">No forms yet. <a href="/forms">Build one in the Form Builder</a>.</div>';
                    return;
                }
                list.innerHTML = '';
                data.forms.forEach((f) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'fp__item';
                    btn.innerHTML = '<span class="fp__item-name">' + esc(f.name) + '</span><span class="fp__item-meta">' + f.fields + ' field' + (f.fields === 1 ? '' : 's') + '</span>';
                    btn.addEventListener('click', () => sendForm(f.id));
                    list.appendChild(btn);
                });
            } catch (e) {
                list.innerHTML = '<div class="fp__empty">' + esc(e.message) + '</div>';
            }
        }

        async function sendForm(formItemId) {
            try {
                const res = await fetch('/chat/' + window.CHAT_ID + '/form/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ form_item_id: formItemId }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.error || 'Could not send');
                picker.hidden = true;
                if (window.__chatReload) window.__chatReload();
            } catch (e) {
                alert(e.message);
            }
        }

        formBtn.addEventListener('click', openPicker);
        closeBtn.addEventListener('click', () => { picker.hidden = true; });
        picker.addEventListener('click', (e) => { if (e.target === picker) picker.hidden = true; });
    }

    // Hydrate server-rendered form placeholders.
    function hydrate() {
        document.querySelectorAll('.conversation_item--form[data-form]').forEach((el) => {
            let form;
            try { form = JSON.parse(el.dataset.form); } catch (_) { return; }
            const mode = window.IS_AGENCY ? 'sent' : 'fill';
            el.innerHTML = '';
            el.appendChild(buildFormCard(form, { mode: mode, chatId: window.CHAT_ID, messageId: el.dataset.messageId }));
            el.removeAttribute('data-form');
        });
    }

    if (document.readyState !== 'loading') { initChat(); hydrate(); }
    else document.addEventListener('DOMContentLoaded', () => { initChat(); hydrate(); });

    return { buildFormCard: buildFormCard, chatCard: chatCard };
})();
