// Simple in-browser message notifications.
// No keys, no service worker, no cost. Works while a portal tab is open.
(function () {
    if (!('Notification' in window)) return;

    const POLL_MS = 10000;
    const STORE_KEY = 'pixiware:lastSeenMessageId';
    const baseTitle = document.title;

    // Ask for permission politely — only after the first click/keypress,
    // never on load (browsers ignore ungestured prompts anyway).
    function askPermissionOnce() {
        if (Notification.permission === 'default') {
            Notification.requestPermission().catch(function () {});
        }
    }
    if (Notification.permission === 'default') {
        window.addEventListener('pointerdown', askPermissionOnce, { once: true });
        window.addEventListener('keydown', askPermissionOnce, { once: true });
    }

    function lastSeen() {
        return parseInt(localStorage.getItem(STORE_KEY) || '0', 10) || 0;
    }
    function setLastSeen(id) {
        localStorage.setItem(STORE_KEY, String(id));
    }
    function onThisChat(chatId) {
        return window.location.pathname === '/chat/' + chatId;
    }

    // If the tab is in the background, flash the title as a fallback.
    let flashTimer = null;
    function flashTitle() {
        if (flashTimer) return;
        let on = true;
        flashTimer = setInterval(function () {
            document.title = on ? '💬 New message' : baseTitle;
            on = !on;
        }, 1500);
    }
    function clearFlash() {
        if (flashTimer) { clearInterval(flashTimer); flashTimer = null; }
        document.title = baseTitle;
    }
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) clearFlash();
    });

    async function poll() {
        let data;
        try {
            const res = await fetch('/notifications/poll');
            if (!res.ok) return;
            data = await res.json();
        } catch (e) {
            return;
        }

        const latest = data.latest;
        if (!latest) return;

        const seen = lastSeen();
        // First run on this device: set a baseline, don't notify about old messages.
        if (!seen) { setLastSeen(latest.id); return; }
        if (latest.id <= seen) return;

        setLastSeen(latest.id);

        // Don't interrupt if they're already reading that exact chat.
        if (onThisChat(latest.chat_id) && !document.hidden) return;

        if (Notification.permission === 'granted') {
            const n = new Notification('New message from ' + latest.from, {
                body: latest.snippet,
                tag: 'pixiware-message',
            });
            n.onclick = function () {
                window.focus();
                window.location.href = '/chat/' + latest.chat_id;
                n.close();
            };
        }
        if (document.hidden) flashTitle();
    }

    poll();
    setInterval(poll, POLL_MS);
})();
