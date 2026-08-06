/* Web Evidence UI — redesigned modal + inline result cards for the agentic
 * web-evidence feature. Loaded from index.html after signal-card.js. Globals
 * (showWebEvidenceModal / closeWebEvidenceModal / renderWebResult) are called
 * from inline onclick handlers and from the main dashboard script. */

(function () {
    'use strict';

    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const ICONS = {
        globe: '<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/></svg>',
        check: '<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>',
        external: '<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5h5v5M19 5l-9 9M19 13v6H5V5h6"/></svg>',
        refresh: '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.5m15-.5V4m0 0h-5m5 0l-4 4M4 20v-5h.5m15 .5V20m0 0h-5m5 0l-4-4"/></svg>',
        search: '<svg class="w-7 h-7" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z"/></svg>',
        spinner: '<svg class="w-5 h-5 text-teal-400 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6l3 2"/></svg>',
        file: '<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m-7-8h2m0 0V4h8v16H4V8h5zm0 0l5-5"/></svg>',
        paper: '<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.6 2A9 9 0 1112 3a9 9 0 018.6 9z"/></svg>',
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

    function relevanceBadgeHtml(score) {
        if (score == null) return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold bg-slate-700/60 text-slate-400">UNSCORED</span>';
        if (score >= 70) return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold bg-teal-500/20 text-teal-300">RELEVANT</span>';
        if (score >= 50) return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold bg-amber-500/20 text-amber-300">PARTIAL</span>';
        return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold bg-slate-700/60 text-slate-500">NOT RELEVANT</span>';
    }

    function statusBadgeHtml(status) {
        if (status === 'fetched') return '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/15 text-emerald-300">' + ICONS.check + 'Fetched</span>';
        if (status === 'failed') return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-red-500/15 text-red-300">Fetch failed</span>';
        if (status === 'blocked') return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/15 text-amber-300">Blocked</span>';
        return '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-700/60 text-slate-500">' + esc(status || 'Queued') + '</span>';
    }

    function relevanceBarHtml(score) {
        if (score == null) return '';
        const color = score >= 70 ? 'bg-teal-400' : score >= 50 ? 'bg-amber-400' : 'bg-slate-500';
        const text = score >= 70 ? 'text-teal-300' : score >= 50 ? 'text-amber-300' : 'text-slate-400';
        const width = Math.max(5, Math.min(100, score));
        return '<div class="flex items-center gap-2 max-w-[220px]">' +
            '<span class="text-[11px] font-bold ' + text + ' w-9 shrink-0 text-right">' + score + '%</span>' +
            '<div class="flex-1 h-1.5 rounded-full bg-slate-800 overflow-hidden">' +
            '<div class="h-full ' + color + ' rounded-full transition-all duration-500" style="width:' + width + '%"></div></div></div>';
    }

    function webEvidenceItemHtml(e) {
        const score = scoreOf(e);
        const cls = e.classification || {};
        const paper = !!(cls.is_paper_qms || e.is_paper_qms);
        const summary = (cls.summary || e.summary || '').trim();
        const snippet = (e.snippet || '').trim();
        const title = e.title || e.url;
        const domain = domainOf(e);

        const badges = [
            paper ? '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-yellow-500/20 text-yellow-300">' + ICONS.paper + 'PAPER-QMS</span>' : '',
            relevanceBadgeHtml(score),
            statusBadgeHtml(e.fetch_status),
        ].filter(Boolean).join(' ');

        return '<article class="group rounded-xl border border-slate-700/80 bg-slate-800/40 hover:border-teal-500/50 hover:bg-slate-800/70 transition-colors p-4">' +
            '<div class="flex items-start justify-between gap-3">' +
            '<a href="' + esc(e.url) + '" target="_blank" rel="noopener noreferrer" ' +
            'class="text-sm font-semibold text-teal-300 hover:text-teal-200 hover:underline leading-snug break-words">' + esc(title) + '</a>' +
            '<span class="text-[11px] text-slate-500 shrink-0 mt-1">' + esc(domain) + '</span></div>' +
            '<div class="mt-2.5 flex flex-wrap items-center gap-1.5">' + badges + '</div>' +
            (score != null ? '<div class="mt-3">' + relevanceBarHtml(score) + '</div>' : '') +
            (summary ? '<p class="mt-3 text-[13px] leading-relaxed text-slate-300">' + esc(summary) + '</p>'
                : (snippet ? '<p class="mt-3 text-[13px] leading-relaxed text-slate-500">' + esc(snippet.slice(0, 240)) + '…</p>' : '')) +
            '<div class="mt-3 flex items-center justify-between gap-3 text-[11px] text-slate-500">' +
            '<span class="truncate">' + esc(e.url) + '</span>' +
            '<a href="' + esc(e.url) + '" target="_blank" rel="noopener noreferrer" class="shrink-0 inline-flex items-center gap-1 text-teal-400 hover:text-teal-300 font-semibold group-hover:gap-1.5 transition-all">Open ' + ICONS.external + '</a>' +
            '</div></article>';
    }

    function loadingHtml() {
        return '<div class="flex items-center justify-center gap-3 py-16">' + ICONS.spinner + '<span class="text-sm text-slate-400">Fetching evidence…</span></div>';
    }

    function emptyStateHtml() {
        return '<div class="flex flex-col items-center justify-center py-16 text-center px-6">' +
            '<div class="w-14 h-14 rounded-full bg-slate-800 flex items-center justify-center mb-4"><span class="text-slate-600">' + ICONS.search + '</span></div>' +
            '<p class="text-sm font-semibold text-slate-300">No web evidence yet</p>' +
            '<p class="text-xs text-slate-500 mt-1.5 max-w-xs leading-relaxed">Run an agentic web search for this company to fetch regulatory alerts, recalls and news reports.</p></div>';
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

        const overlay = document.createElement('div');
        overlay.id = 'webModal';
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm';
        overlay.innerHTML =
            '<div class="w-full max-w-2xl max-h-[85vh] flex flex-col rounded-2xl border border-slate-700 bg-slate-900 shadow-2xl overflow-hidden">' +
            '<header class="px-5 py-4 border-b border-slate-800 flex items-start justify-between gap-4 shrink-0">' +
            '<div class="flex items-start gap-3">' +
            '<div class="w-10 h-10 rounded-xl bg-teal-500/15 flex items-center justify-center shrink-0 text-teal-400">' + ICONS.globe + '</div>' +
            '<div><h3 class="text-base font-bold text-white leading-tight">Web Evidence</h3>' +
            '<p class="text-xs text-slate-400 mt-0.5">Fetched regulatory &amp; news sources for this company</p></div></div>' +
            '<button onclick="closeWebEvidenceModal()" aria-label="Close" class="text-slate-400 hover:text-white hover:bg-slate-800 rounded-lg w-8 h-8 flex items-center justify-center transition-colors text-xl leading-none">×</button>' +
            '</div>' +
            '<div id="webModalBody" class="p-4 space-y-2.5 overflow-y-auto"></div>' +
            '<footer class="px-5 py-3 border-t border-slate-800 flex items-center justify-between gap-3 shrink-0">' +
            '<span id="webModalCount" class="text-xs text-slate-500"></span>' +
            '<div class="flex items-center gap-2">' +
            '<button onclick="searchWeb(\'' + esc(eventId) + '\')" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-teal-600/20 text-teal-300 hover:bg-teal-600/30 transition-colors inline-flex items-center gap-1.5">' + ICONS.refresh + 'Re-run search</button>' +
            '<button onclick="closeWebEvidenceModal()" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:bg-slate-700 transition-colors">Close</button>' +
            '</div></footer></div>';
        document.body.appendChild(overlay);
        document.body.style.overflow = 'hidden';
        overlay.addEventListener('click', ev => { if (ev.target === overlay) closeWebEvidenceModal(); });

        const body = document.getElementById('webModalBody');
        const count = document.getElementById('webModalCount');

        function renderItems(ev) {
            count.textContent = ev.length + (ev.length === 1 ? ' source' : ' sources');
            body.innerHTML = ev.length ? ev.map(webEvidenceItemHtml).join('') : emptyStateHtml();
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
            body.innerHTML = '<p class="text-sm text-red-400">Failed to load web evidence: ' + esc(err.message) + '</p>';
        }
    }

    async function renderWebResult(box, eventId) {
        try {
            const res = await fetch('/api/v1/records/' + eventId + '/web-evidence');
            const data = await res.json();
            const ev = data.evidence || [];
            if (!ev.length) {
                box.innerHTML = '<p class="text-xs text-slate-400">No web evidence found for this event.</p>';
                return;
            }
            box.innerHTML = '<div class="space-y-2">' + ev.map(webEvidenceItemHtml).join('') + '</div>';
        } catch (e) {
            box.innerHTML = '<p class="text-xs text-red-400">Failed to load web evidence: ' + esc(e.message) + '</p>';
        }
    }

    window.showWebEvidenceModal = showWebEvidenceModal;
    window.closeWebEvidenceModal = closeWebEvidenceModal;
    window.renderWebResult = renderWebResult;
})();
