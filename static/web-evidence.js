/* Web Evidence UI — redesigned modal + inline result cards for the agentic
 * web-evidence feature. Loaded from index.html after signal-card.js. Globals
 * (showWebEvidenceModal / closeWebEvidenceModal / renderWebResult) are called
 * from inline onclick handlers and from the main dashboard script.
 *
 * IMPORTANT: All layout is done with inline `style` attributes because the
 * Tailwind CDN only generates classes that exist in the initial DOM. Anything
 * built purely in JS (this whole modal) would otherwise be unstyled. */

(function () {
    'use strict';

    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const ICONS = {
        globe: '<svg style="width:20px;height:20px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/></svg>',
        check: '<svg style="width:12px;height:12px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>',
        external: '<svg style="width:12px;height:12px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5h5v5M19 5l-9 9M19 13v6H5V5h6"/></svg>',
        refresh: '<svg style="width:14px;height:14px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.5m15-.5V4m0 0h-5m5 0l-4 4M4 20v-5h.5m15 .5V20m0 0h-5m5 0l-4-4"/></svg>',
        search: '<svg style="width:28px;height:28px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z"/></svg>',
        spinner: '<svg style="width:20px;height:20px" class="animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6l3 2"/></svg>',
        file: '<svg style="width:16px;height:16px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m-7-8h2m0 0V4h8v16H4V8h5zm0 0l5-5"/></svg>',
        paper: '<svg style="width:12px;height:12px" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.6 2A9 9 0 1112 3a9 9 0 018.6 9z"/></svg>',
    };

    function scoreOf(e) {
        const cls = e.classification || {};
        if (cls.relevance_score != null) return cls.relevance_score;
        if (e.relevance_score != null) return e.relevance_score;
        return null;
    }

    function domainOf(e) {
        return e.source || (e.url ? (e.url.split('/')[2] || '') : '');
    }

    const badge = (text, bg, fg, bold) =>
        '<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:9999px;' +
        'font-size:11px;font-weight:' + (bold ? 700 : 600) + ';line-height:1.4;background:' + bg + ';color:' + fg + ';white-space:nowrap">' + text + '</span>';

    function relevanceBadgeHtml(score) {
        if (score == null) return badge('UNSCORED', 'rgba(51,65,85,0.6)', '#94a3b8', false);
        if (score >= 70) return badge('RELEVANT', 'rgba(20,184,166,0.2)', '#5eead4', false);
        if (score >= 50) return badge('PARTIAL', 'rgba(245,158,11,0.2)', '#fcd34d', false);
        return badge('NOT RELEVANT', 'rgba(51,65,85,0.6)', '#64748b', false);
    }

    function statusBadgeHtml(status) {
        if (status === 'fetched') return badge(ICONS.check + 'Fetched', 'rgba(16,185,129,0.15)', '#6ee7b7', false);
        if (status === 'failed') return badge('Fetch failed', 'rgba(239,68,68,0.15)', '#fca5a5', false);
        if (status === 'blocked') return badge('Blocked', 'rgba(245,158,11,0.15)', '#fcd34d', false);
        return badge(esc(status || 'Queued'), 'rgba(51,65,85,0.6)', '#64748b', false);
    }

    function relevanceBarHtml(score) {
        if (score == null) return '';
        const color = score >= 70 ? '#2dd4bf' : score >= 50 ? '#fbbf24' : '#64748b';
        const text = score >= 70 ? '#5eead4' : score >= 50 ? '#fcd34d' : '#94a3b8';
        const width = Math.max(5, Math.min(100, score));
        return '<div style="display:flex;align-items:center;gap:8px;max-width:220px">' +
            '<span style="font-size:11px;font-weight:700;color:' + text + ';width:36px;flex-shrink:0;text-align:right">' + score + '%</span>' +
            '<div style="flex:1;height:6px;border-radius:9999px;background:#1e293b;overflow:hidden">' +
            '<div style="height:100%;border-radius:9999px;background:' + color + ';transition:width .5s;width:' + width + '%"></div></div></div>';
    }

    function webEvidenceItemHtml(e, last) {
        const score = scoreOf(e);
        const cls = e.classification || {};
        const paper = !!(cls.is_paper_qms || e.is_paper_qms);
        const summary = (cls.summary || e.summary || '').trim();
        const snippet = (e.snippet || '').trim();
        const title = e.title || e.url;
        const domain = domainOf(e);

        const badges = [
            paper ? badge(ICONS.paper + 'PAPER-QMS', 'rgba(234,179,8,0.2)', '#fde047', true) : '',
            relevanceBadgeHtml(score),
            statusBadgeHtml(e.fetch_status),
        ].filter(Boolean).join(' ');

        return '<article style="display:block;margin-bottom:' + (last ? '0' : '10px') + ';padding:16px;' +
            'border:1px solid rgba(51,65,85,0.8);border-radius:12px;background:rgba(30,41,59,0.4)">' +
            '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">' +
            '<a href="' + esc(e.url) + '" target="_blank" rel="noopener noreferrer" ' +
            'style="font-size:14px;font-weight:600;color:#5eead4;text-decoration:none;line-height:1.4;overflow-wrap:break-word">' + esc(title) + '</a>' +
            '<span style="font-size:11px;color:#64748b;flex-shrink:0;margin-top:4px">' + esc(domain) + '</span></div>' +
            '<div style="margin-top:10px;display:flex;flex-wrap:wrap;align-items:center;gap:6px">' + badges + '</div>' +
            (score != null ? '<div style="margin-top:12px">' + relevanceBarHtml(score) + '</div>' : '') +
            (summary ? '<p style="margin:12px 0 0;font-size:13px;line-height:1.6;color:#cbd5e1">' + esc(summary) + '</p>'
                : (snippet ? '<p style="margin:12px 0 0;font-size:13px;line-height:1.6;color:#64748b">' + esc(snippet.slice(0, 240)) + '…</p>' : '')) +
            '<div style="margin-top:12px;display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:11px;color:#64748b">' +
            '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(e.url) + '</span>' +
            '<a href="' + esc(e.url) + '" target="_blank" rel="noopener noreferrer" ' +
            'style="flex-shrink:0;display:inline-flex;align-items:center;gap:4px;color:#2dd4bf;font-weight:600;text-decoration:none">Open ' + ICONS.external + '</a>' +
            '</div></article>';
    }

    function loadingHtml() {
        return '<div style="display:flex;align-items:center;justify-content:center;gap:12px;padding:64px 0;color:#5eead4">' +
            ICONS.spinner + '<span style="font-size:14px;color:#94a3b8">Fetching evidence…</span></div>';
    }

    function emptyStateHtml() {
        return '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:64px 24px;text-align:center">' +
            '<div style="width:56px;height:56px;border-radius:9999px;background:#1e293b;display:flex;align-items:center;justify-content:center;margin-bottom:16px;color:#475569">' + ICONS.search + '</div>' +
            '<p style="font-size:14px;font-weight:600;color:#cbd5e1;margin:0">No web evidence yet</p>' +
            '<p style="font-size:12px;color:#64748b;margin:6px auto 0;max-width:288px;line-height:1.6">Run an agentic web search for this company to fetch regulatory alerts, recalls and news reports.</p></div>';
    }

    function closeWebEvidenceModal() {
        const m = document.getElementById('webModal');
        if (m) m.remove();
        document.body.style.overflow = '';
    }

    async function showWebEvidenceModal(eventId) {
        const existing = document.getElementById('webModal');
        if (existing) existing.remove();

        const cached = (window.__webEv || {})[eventId];
        const reRunHtml = window.VIEW_ONLY ? '' :
            '<button onclick="searchWeb(\'' + esc(eventId) + '\')" ' +
            'style="font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;border:0;cursor:pointer;' +
            'background:rgba(13,148,136,0.2);color:#5eead4;display:inline-flex;align-items:center;gap:6px">' + ICONS.refresh + 'Re-run search</button>';

        const overlay = document.createElement('div');
        overlay.id = 'webModal';
        overlay.style.cssText =
            'position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;' +
            'padding:16px;background:rgba(0,0,0,0.75);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);';
        overlay.innerHTML =
            '<div style="width:100%;max-width:672px;max-height:85vh;display:flex;flex-direction:column;' +
            'background:#0f172a;border:1px solid #334155;border-radius:16px;overflow:hidden;' +
            'box-shadow:0 25px 50px -12px rgba(0,0,0,0.8)">' +
            '<header style="flex-shrink:0;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;' +
            'padding:16px 20px;border-bottom:1px solid #1e293b">' +
            '<div style="display:flex;align-items:flex-start;gap:12px">' +
            '<div style="width:40px;height:40px;border-radius:12px;background:rgba(20,184,166,0.15);' +
            'display:flex;align-items:center;justify-content:center;flex-shrink:0;color:#2dd4bf">' + ICONS.globe + '</div>' +
            '<div><h3 style="font-size:16px;font-weight:700;color:#fff;line-height:1.25;margin:0">Web Evidence</h3>' +
            '<p style="font-size:12px;color:#94a3b8;margin:2px 0 0">Fetched regulatory &amp; news sources for this company</p></div></div>' +
            '<button onclick="closeWebEvidenceModal()" aria-label="Close" ' +
            'style="color:#94a3b8;background:transparent;border:0;border-radius:8px;width:32px;height:32px;' +
            'display:flex;align-items:center;justify-content:center;font-size:20px;line-height:1;cursor:pointer">×</button>' +
            '</header>' +
            '<div id="webModalBody" style="padding:16px;overflow-y:auto;flex:1;min-height:0"></div>' +
            '<footer style="flex-shrink:0;display:flex;align-items:center;justify-content:space-between;gap:12px;' +
            'padding:12px 20px;border-top:1px solid #1e293b">' +
            '<span id="webModalCount" style="font-size:12px;color:#64748b"></span>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
            reRunHtml +
            '<button onclick="closeWebEvidenceModal()" ' +
            'style="font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;border:0;cursor:pointer;' +
            'background:#1e293b;color:#cbd5e1">Close</button>' +
            '</div></footer></div>';
        document.body.appendChild(overlay);
        document.body.style.overflow = 'hidden';
        overlay.addEventListener('click', ev => { if (ev.target === overlay) closeWebEvidenceModal(); });

        const body = document.getElementById('webModalBody');
        const count = document.getElementById('webModalCount');

        function renderItems(ev) {
            count.textContent = ev.length + (ev.length === 1 ? ' source' : ' sources');
            body.innerHTML = ev.length ? ev.map((it, i) => webEvidenceItemHtml(it, i === ev.length - 1)).join('') : emptyStateHtml();
        }

        if (cached && cached.length) {
            renderItems(cached);
            return;
        }

        body.innerHTML = loadingHtml();
        try {
            const res = await fetch('/api/v1/records/' + eventId + '/web-evidence');
            const data = await res.json();
            renderItems(data.evidence || []);
        } catch (err) {
            body.innerHTML = '<p style="font-size:14px;color:#f87171">Failed to load web evidence: ' + esc(err.message) + '</p>';
        }
    }

    async function renderWebResult(box, eventId) {
        try {
            const res = await fetch('/api/v1/records/' + eventId + '/web-evidence');
            const data = await res.json();
            const ev = data.evidence || [];
            if (!ev.length) {
                box.innerHTML = '<p style="font-size:12px;color:#94a3b8">No web evidence found for this event.</p>';
                return;
            }
            box.innerHTML = ev.map((it, i) => webEvidenceItemHtml(it, i === ev.length - 1)).join('');
        } catch (e) {
            box.innerHTML = '<p style="font-size:12px;color:#f87171">Failed to load web evidence: ' + esc(e.message) + '</p>';
        }
    }

    window.showWebEvidenceModal = showWebEvidenceModal;
    window.closeWebEvidenceModal = closeWebEvidenceModal;
    window.renderWebResult = renderWebResult;
})();
