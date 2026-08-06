/* Company views — sidebar leaderboard, the /companies directory page and the
 * individual /companies/{slug} company page. A tiny path-based router keeps
 * the SPA on the single index.html shell that FastAPI serves for /companies/*.
 *
 * IMPORTANT: Tailwind CDN only generates classes present in the initial DOM.
 * Every class used in the JS-generated markup below is also listed inside the
 * hidden #classPreloader div in index.html so it gets compiled.
 *
 * All layout-critical styling uses Tailwind classes from that preloader; the
 * sidebar shell itself lives in index.html (so its classes always compile).
 */
(function () {
    'use strict';

    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const DIR_PAGE_SIZE = 30;
    const SIDEBAR_PAGE_SIZE = 10;

    function scoreColor(score) {
        if (score >= 85) return 'text-red-400';
        if (score >= 65) return 'text-yellow-400';
        return 'text-green-400';
    }

    function rankBadge(rank) {
        if (rank === 1) return 'bg-gradient-to-br from-amber-300 to-yellow-600 text-slate-900';
        if (rank === 2) return 'bg-gradient-to-br from-slate-200 to-slate-400 text-slate-800';
        if (rank === 3) return 'bg-gradient-to-br from-orange-400 to-amber-700 text-slate-900';
        return 'bg-slate-700 text-slate-300';
    }

    function parsePath() {
        const parts = location.pathname.split('/').filter(Boolean);
        if (parts[0] === 'companies') {
            if (parts.length === 1) return { view: 'directory' };
            return { view: 'company', slug: decodeURIComponent(parts[1]) };
        }
        return { view: 'dashboard' };
    }

    function currentSlug() {
        const p = parsePath();
        return p.view === 'company' ? p.slug : null;
    }

    /* ------------------------- sidebar leaderboard ------------------------- */

    let sidebarPage = 1, sidebarTotal = 0, sidebarLoaded = 0;

    function sidebarItemHtml(it, rank) {
        const url = '/companies/' + encodeURIComponent(it.slug);
        const active = currentSlug() === it.slug;
        return `<a href="${url}" data-nav="${url}" class="flex items-center gap-3 p-2.5 rounded-lg border cursor-pointer transition-colors ${active ? 'border-teal-600/50 bg-slate-800/70' : 'border-slate-700/50 bg-slate-800/40 hover:bg-slate-800 hover:border-slate-600'}">
            <span class="w-7 h-7 shrink-0 rounded-full text-xs font-bold flex items-center justify-center ${rankBadge(rank)}">${rank}</span>
            <span class="min-w-0 flex-1">
                <span class="block text-sm font-medium text-slate-100 truncate">${esc(it.name)}</span>
                <span class="block text-[11px] text-slate-500">${it.event_count} incident${it.event_count === 1 ? '' : 's'} · avg ${it.avg_score}</span>
            </span>
            <span class="ml-auto text-sm font-bold ${scoreColor(it.score)}">${it.score}</span>
        </a>`;
    }

    async function loadSidebarPage(reset) {
        const list = document.getElementById('sidebarList');
        const moreBtn = document.getElementById('sidebarMoreBtn');
        if (!list) return;
        if (reset) {
            sidebarPage = 1;
            list.innerHTML = '<div class="text-sm text-slate-500 py-6 text-center">Loading leaderboard…</div>';
        }
        try {
            const res = await fetch(`/api/v1/companies/ranking?page=${sidebarPage}&page_size=${SIDEBAR_PAGE_SIZE}`);
            const data = await res.json();
            sidebarTotal = data.total || 0;
            const start = (sidebarPage - 1) * SIDEBAR_PAGE_SIZE + 1;
            const html = (data.items || []).map((it, i) => sidebarItemHtml(it, start + i)).join('');
            if (reset) list.innerHTML = html; else list.insertAdjacentHTML('beforeend', html);
            sidebarLoaded = start + (data.items || []).length - 1;
            sidebarPage++;
            if (moreBtn) moreBtn.classList.toggle('hidden', sidebarLoaded >= sidebarTotal);
        } catch (e) {
            if (reset) list.innerHTML = '<div class="text-sm text-red-400 py-6 text-center">Failed to load leaderboard.</div>';
        }
    }

    function refreshSidebarActive() {
        const slug = currentSlug();
        const list = document.getElementById('sidebarList');
        if (!list) return;
        list.querySelectorAll('a').forEach(a => {
            const s = a.getAttribute('data-nav') || '';
            if (slug && s.endsWith('/' + encodeURIComponent(slug))) {
                a.classList.add('border-teal-600/50', 'bg-slate-800/70');
                a.classList.remove('border-slate-700/50', 'bg-slate-800/40');
            } else {
                a.classList.remove('border-teal-600/50', 'bg-slate-800/70');
                a.classList.add('border-slate-700/50', 'bg-slate-800/40');
            }
        });
    }

    /* --------------------------- directory page --------------------------- */

    let dirPage = 1, dirPages = 1, dirQ = '', dirSearchTimer = null;

    function dirTileHtml(it, rank) {
        const url = '/companies/' + encodeURIComponent(it.slug);
        const pct = Math.min(100, Math.max(2, it.score));
        return `<a href="${url}" data-nav="${url}" class="glass-panel p-5 border border-slate-700 hover:border-teal-500/60 hover:-translate-y-0.5 transition-all cursor-pointer block">
            <div class="flex items-start justify-between gap-3 mb-3">
                <span class="w-8 h-8 shrink-0 rounded-full text-sm font-bold flex items-center justify-center ${rankBadge(rank)}">${rank}</span>
                <span class="text-right text-sm font-bold ${scoreColor(it.score)}">${it.score}</span>
            </div>
            <h3 class="font-bold text-base text-white leading-snug mb-1 truncate">${esc(it.name)}</h3>
            <p class="text-xs text-slate-500 mb-3">${it.event_count} incident${it.event_count === 1 ? '' : 's'} · avg ${it.avg_score} · latest ${esc(it.latest_date || 'n/a')}</p>
            <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden mb-3"><div class="h-full rounded-full bg-gradient-to-r from-teal-500 to-purple-500 transition-all" style="width:${pct}%"></div></div>
            <div class="flex flex-wrap gap-1.5">
                <span class="px-2 py-0.5 text-[10px] rounded border border-yellow-800/50 bg-yellow-900/20 text-yellow-300">${it.paper_count} paper-QMS</span>
                <span class="px-2 py-0.5 text-[10px] rounded border border-red-800/50 bg-red-900/20 text-red-300">${it.mandate_count} mandate</span>
                <span class="px-2 py-0.5 text-[10px] rounded border border-slate-700 bg-slate-800/50 text-slate-400">${esc((it.regulators || []).join(', '))}</span>
            </div>
        </a>`;
    }

    async function loadDirPage(page, reset) {
        const grid = document.getElementById('dirGrid');
        if (!grid) return;
        if (reset) grid.innerHTML = '<div class="col-span-full text-center py-12 text-slate-500">Loading…</div>';
        try {
            const res = await fetch(`/api/v1/companies/ranking?page=${page}&page_size=${DIR_PAGE_SIZE}&q=${encodeURIComponent(dirQ)}`);
            const d = await res.json();
            dirPage = d.page || page;
            dirPages = Math.max(d.pages || 1, 1);
            const countEl = document.getElementById('dirCount');
            if (countEl) countEl.textContent = `${d.total} companies ranked by peak lead score`;
            const info = document.getElementById('dirPageInfo');
            if (info) info.textContent = `Page ${dirPage} / ${dirPages}`;
            const prev = document.getElementById('dirPrev');
            const next = document.getElementById('dirNext');
            if (prev) prev.disabled = dirPage <= 1;
            if (next) next.disabled = dirPage >= dirPages;
            const items = d.items || [];
            grid.innerHTML = items.length
                ? items.map((it, i) => dirTileHtml(it, (dirPage - 1) * DIR_PAGE_SIZE + i + 1)).join('')
                : '<div class="col-span-full text-center py-12 text-slate-500">No companies match.</div>';
        } catch (e) {
            grid.innerHTML = '<div class="col-span-full text-center py-12 text-red-400">Failed to load companies.</div>';
        }
    }

    function renderDirectory() {
        const main = document.getElementById('directoryView');
        if (!main) return;
        main.innerHTML = `
            <div class="glass-panel p-6">
                <div class="flex flex-wrap items-center justify-between gap-3 mb-6">
                    <div>
                        <h2 class="text-2xl font-semibold">Company Leaderboard</h2>
                        <p class="text-sm text-slate-400 mt-1">Every manufacturer, ranked by its highest-scoring signal</p>
                    </div>
                    <input id="dirSearch" oninput="dirSearch()" placeholder="Filter companies…" class="w-full sm:w-72 px-3 py-2 bg-slate-800/70 border border-slate-700 rounded-lg text-sm text-slate-300 focus:outline-none focus:border-teal-500">
                </div>
                <div id="dirCount" class="text-xs text-slate-400 mb-4"></div>
                <div id="dirGrid" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
                    <div class="col-span-full text-center py-12 text-slate-500">Loading…</div>
                </div>
                <div class="flex items-center justify-center gap-2 mt-8">
                    <button id="dirPrev" onclick="dirPageMove(-1)" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg text-sm font-semibold disabled:opacity-40 disabled:cursor-not-allowed">‹ Prev</button>
                    <span id="dirPageInfo" class="text-sm text-slate-400 px-2"></span>
                    <button id="dirNext" onclick="dirPageMove(1)" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg text-sm font-semibold disabled:opacity-40 disabled:cursor-not-allowed">Next ›</button>
                </div>
            </div>`;
        loadDirPage(1, true);
    }

    /* --------------------------- company page ----------------------------- */

    const VIEW_ONLY_PROMISE = fetch('/api/v1/config')
        .then(r => r.json())
        .then(c => { window.VIEW_ONLY = !!c.view_only; })
        .catch(() => { window.VIEW_ONLY = window.VIEW_ONLY || false; });

    function statHtml(label, value) {
        return `<div class="glass-panel p-4"><p class="text-[11px] uppercase tracking-wider text-slate-500 mb-1">${esc(label)}</p><p class="text-lg font-bold text-white truncate" title="${esc(value)}">${esc(value)}</p></div>`;
    }

    function companyHeroHtml(c) {
        const years = (c.years || []).join(', ') || 'n/a';
        return `
        <div class="mb-4 text-sm">
            <a href="/" data-nav="/" class="text-slate-400 hover:text-teal-300 transition-colors">← Dashboard</a>
            <span class="text-slate-600 mx-2">/</span>
            <a href="/companies" data-nav="/companies" class="text-slate-400 hover:text-teal-300 transition-colors">Companies</a>
            <span class="text-slate-600 mx-2">/</span>
            <span class="text-slate-200">${esc(c.name)}</span>
        </div>
        <div class="glass-panel p-6 mb-6">
            <div class="flex flex-wrap items-end justify-between gap-6">
                <div class="min-w-0">
                    <p class="text-[11px] uppercase tracking-wider text-slate-500 mb-2">Company Profile</p>
                    <h2 class="text-3xl font-bold text-white leading-tight">${esc(c.name)}</h2>
                    <p class="text-sm text-slate-400 mt-2">${c.event_count} regulatory incident${c.event_count === 1 ? '' : 's'} across ${esc(years)}</p>
                    <div class="flex flex-wrap gap-1.5 mt-3">
                        <span class="px-2 py-0.5 text-[10px] rounded border border-yellow-800/50 bg-yellow-900/20 text-yellow-300">${c.paper_count} paper-QMS</span>
                        <span class="px-2 py-0.5 text-[10px] rounded border border-red-800/50 bg-red-900/20 text-red-300">${c.mandate_count} 2026-mandate</span>
                        <span class="px-2 py-0.5 text-[10px] rounded border border-slate-700 bg-slate-800/50 text-slate-400">${c.web_evidence_count} web evidence</span>
                        <span class="px-2 py-0.5 text-[10px] rounded border border-slate-700 bg-slate-800/50 text-slate-400">${c.evidence_count} external findings</span>
                        ${(c.regulators || []).map(r => `<span class="px-2 py-0.5 text-[10px] rounded border border-slate-700 bg-slate-800/50 text-slate-400">${esc(r)}</span>`).join('')}
                    </div>
                </div>
                <div class="text-right">
                    <span class="text-5xl font-bold ${scoreColor(c.score)}">${c.score}</span>
                    <span class="text-xl font-semibold text-slate-500"> / ${c.max_possible_score || 100}</span>
                    <p class="text-xs text-slate-500 uppercase tracking-wide mt-1">Peak Lead Score</p>
                </div>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6 pt-6 border-t border-slate-700/60">
                ${statHtml('Incidents', c.event_count)}
                ${statHtml('Average Score', c.avg_score)}
                ${statHtml('Latest Report', c.latest_date || 'n/a')}
                ${statHtml('Regulators', (c.regulators || []).join(', ') || 'n/a')}
            </div>
        </div>`;
    }

    async function renderCompany(slug) {
        const main = document.getElementById('companyView');
        if (!main) return;
        main.innerHTML = '<div class="glass-panel p-6 text-center text-slate-500 py-12">Loading company…</div>';
        await VIEW_ONLY_PROMISE;
        try {
            const res = await fetch(`/api/v1/companies/${encodeURIComponent(slug)}/signals`);
            if (!res.ok) throw new Error('Company not found');
            const d = await res.json();
            const c = d.company;
            const cards = [{ ...d.card, event_count: 1, events: [] }, ...(d.card.events || [])];
            main.innerHTML = companyHeroHtml(c) + '<div id="companySignals" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6"></div>';
            const grid = document.getElementById('companySignals');
            cards.forEach(signal => {
                const el = document.createElement('signal-card');
                el.signal = signal;
                el.viewOnly = window.VIEW_ONLY;
                grid.appendChild(el);
            });
        } catch (e) {
            main.innerHTML = `<div class="glass-panel p-6 text-center py-12">
                <p class="text-red-400 mb-2">${esc(e.message)}</p>
                <a href="/companies" data-nav="/companies" class="inline-block px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg text-sm font-semibold">← Back to companies</a>
            </div>`;
        }
    }

    /* ------------------------ collapsible sidebar -------------------------- */

    const SIDEBAR_KEY = 'sentinelSidebarCollapsed';

    function applySidebarState() {
        let collapsed = true;
        try { collapsed = localStorage.getItem(SIDEBAR_KEY) !== '0'; } catch (e) { /* ignore */ }
        const aside = document.getElementById('companySidebar');
        const btn = document.getElementById('sidebarToggle');
        const label = document.getElementById('sidebarToggleLabel');
        const icon = document.getElementById('sidebarToggleIcon');
        if (aside) aside.classList.toggle('hidden', collapsed);
        if (label) label.textContent = collapsed ? 'Show ranking' : 'Hide ranking';
        if (icon) icon.innerHTML = collapsed
            ? '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />'
            : '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />';
        if (btn) btn.title = collapsed ? 'Show company ranking sidebar' : 'Hide company ranking sidebar';
    }

    window.toggleSidebar = function () {
        let collapsed = true;
        try { collapsed = localStorage.getItem(SIDEBAR_KEY) !== '0'; } catch (e) { /* ignore */ }
        try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '0' : '1'); } catch (e) { /* ignore */ }
        applySidebarState();
    };

    /* ------------------------------ router -------------------------------- */

    async function route() {
        const p = parsePath();
        const dv = document.getElementById('dashboardView');
        const dir = document.getElementById('directoryView');
        const co = document.getElementById('companyView');
        if (dv) dv.classList.toggle('hidden', p.view !== 'dashboard');
        if (dir) dir.classList.toggle('hidden', p.view !== 'directory');
        if (co) co.classList.toggle('hidden', p.view !== 'company');
        if (p.view === 'directory') renderDirectory();
        if (p.view === 'company') renderCompany(p.slug);
        refreshSidebarActive();
        window.scrollTo({ top: 0 });
    }

    window.navigateTo = function (event, path) {
        if (event && event.preventDefault) event.preventDefault();
        history.pushState({}, '', path);
        route();
        return false;
    };

    window.dirSearch = function () {
        clearTimeout(dirSearchTimer);
        dirSearchTimer = setTimeout(() => {
            dirQ = (document.getElementById('dirSearch').value || '').trim();
            loadDirPage(1, true);
        }, 300);
    };

    window.dirPageMove = function (delta) {
        const next = Math.min(Math.max(dirPage + delta, 1), dirPages);
        if (next === dirPage) return;
        loadDirPage(next, true);
    };

    document.addEventListener('click', (ev) => {
        const a = ev.target.closest ? ev.target.closest('a[data-nav]') : null;
        if (a && !ev.metaKey && !ev.ctrlKey && !ev.shiftKey && !ev.defaultPrevented) {
            ev.preventDefault();
            window.navigateTo(ev, a.getAttribute('data-nav'));
        }
    });

    window.addEventListener('popstate', route);

    const moreBtn = document.getElementById('sidebarMoreBtn');
    if (moreBtn) moreBtn.addEventListener('click', () => loadSidebarPage(false));

    loadSidebarPage(true);
    applySidebarState();
    route();
})();
