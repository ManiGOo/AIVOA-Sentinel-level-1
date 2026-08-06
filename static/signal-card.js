/* SignalCard — standalone <signal-card> web component.
   Encapsulates everything the SDR lead card renders: score breakdown,
   company / drug / issue, AI root-cause + classification tags, the paper-QMS
   Evidence Class panel, external enrichment chips, and the action buttons.

   Uses light DOM (no shadow root) so the existing global id-based helpers
   (approveCampaign, enrichCard) keep working unchanged.
*/
(function () {
    'use strict';

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function clean(s) {
        return String(s ?? '').replace(/\s+/g, ' ').trim();
    }

    const PLACEHOLDER_MFR = /^(under investigation|not known|unknown|not available|nil|n\/a|na|not disclosed)$/i;

    function splitManufacturer(m) {
        const s = String(m || '').trim();
        if (!s) return { name: 'Unknown Manufacturer', address: '' };
        if (PLACEHOLDER_MFR.test(s)) return { name: 'Manufacturer under investigation', address: 'Identity withheld by regulator' };
        const i = s.indexOf(',');
        if (i > 0) return { name: s.slice(0, i).trim(), address: s.slice(i + 1).trim() };
        return { name: s, address: '' };
    }

    function getClassificationTags(a) {
        const tags = [];
        if (a?.is_paper_failure) tags.push({ label: 'Paper QMS', cls: 'bg-yellow-900/50 text-yellow-300 border-yellow-700/50' });
        if (a?.violates_rule_96) tags.push({ label: 'Rule 96', cls: 'bg-red-900/50 text-red-300 border-red-700/50' });
        if (a?.violates_sub_rule_7) tags.push({ label: 'Sub-Rule 7', cls: 'bg-orange-900/50 text-orange-300 border-orange-700/50' });
        if (a?.violates_schedule_h2) tags.push({ label: 'Schedule H2', cls: 'bg-purple-900/50 text-purple-300 border-purple-700/50' });
        const SMG = {
            process_control: 'Schedule M gap · Process Control',
            contamination_control: 'Schedule M gap · Contamination Control',
            stability: 'Schedule M gap · Stability',
            labeling_packaging: 'Schedule M gap · Labeling/Packaging',
            data_integrity: 'Schedule M gap · Data Integrity',
        };
        if (a?.schedule_m_gap && SMG[a.schedule_m_gap]) tags.push({ label: SMG[a.schedule_m_gap], cls: 'bg-teal-900/50 text-teal-300 border-teal-700/50' });
        return tags;
    }

    const PROXY_LABELS = {
        manual_failure_mode: 'manual failure mode (dissolution/assay)',
        failure_mode_neutral: 'unclassified failure mode',
        failure_mode_unknown: 'failure mode unknown',
        formulation_failure_mode: 'formulation-type failure',
        llm_formulation: 'formulation-type failure',
        llm_unclear: 'generic failure text',
        sme_revenue_tier: 'SME revenue tier',
        release_gap: 'release gap (caught by state lab)',
        release_gap_unknown: 'release gap (not confirmed)',
        explicit_regulator_quote: 'regulator quote on paper records',
        not_sme_confirmed: 'not SME-tier',
    };

    function paperAssessmentHtml(signal) {
        const pa = signal.paper_assessment || {};
        if (!pa.class || pa.class === 'none') return '';
        const badge = {
            explicit: { label: 'Category 1 · Explicit Evidence', cls: 'bg-green-900/40 text-green-300 border-green-700/50', dot: 'bg-green-400' },
            deductive: { label: `Category 2 · Deductive (${pa.confidence ?? 0}% conf)`, cls: 'bg-amber-900/40 text-amber-300 border-amber-700/50', dot: 'bg-amber-400' },
        }[pa.class];
        if (!badge) return '';
        const detail = pa.proxies || [];
        const proxyTxt = detail.map(p => PROXY_LABELS[p] || p).join(' · ');
        return `
            <div class="mb-4">
                <p class="text-xs text-slate-500 uppercase tracking-wider mb-1">Evidence Class</p>
                <div class="flex items-center gap-2">
                    <span class="px-2 py-0.5 text-[10px] rounded border ${badge.cls} flex items-center gap-1.5">
                        <span class="w-1.5 h-1.5 rounded-full ${badge.dot}"></span>
                        ${badge.label}
                    </span>
                </div>
                <p class="text-[11px] text-slate-400 mt-1.5 leading-relaxed">${esc(clean(pa.basis || ''))}</p>
                ${proxyTxt ? `<p class="text-[10px] text-slate-500 mt-1">${esc(proxyTxt)}</p>` : ''}
                ${pa.sales_message ? `
                <p class="text-[11px] mt-2 p-2 bg-slate-800/60 rounded border border-slate-700 text-slate-300 italic leading-relaxed" title="${esc(pa.sales_message)}">💼 ${esc(pa.sales_message)}</p>` : ''}
            </div>`;
    }

    function enrichmentHtml(signal) {
        const en = signal.enrichment || {};
        const checks = en.checks || {};
        const evidence = en.evidence || [];
        if (!evidence.length && !Object.keys(checks).length) return '';
        const src = s => ({ FDA: 'FDA', EudraGMDP: 'EudraGMDP' }[s] || s);
        let html = '<div class="mb-5 pt-2 border-t border-slate-700/60">';
        html += '<p class="text-xs text-slate-500 uppercase tracking-wider mb-1">External Enrichment</p>';
        html += '<div class="flex flex-wrap gap-1.5">';
        evidence.forEach(e => {
            const q = e.paper_qms_score > 0 ? ' <span class="text-yellow-300">⚠ Paper-QMS</span>' : '';
            html += `<a target="_blank" rel="noopener" href="${esc(e.url)}" class="px-2 py-0.5 text-[10px] rounded border border-green-700/50 bg-green-900/20 text-green-300 hover:bg-green-800/30">${esc(src(e.source))} · ${esc(e.finding_date || '')}${q}</a>`;
        });
        Object.entries(checks).forEach(([source, c]) => {
            const date = (c.checked_at || '').slice(0, 10);
            const f = c.findings_count || 0;
            if (c.status === 'error') {
                html += `<span class="px-2 py-0.5 text-[10px] rounded border border-red-700/50 bg-red-900/20 text-red-300">${esc(src(source))} ✗ error${date ? ' (' + date + ')' : ''}</span>`;
            } else if (c.status === 'skipped') {
                html += `<span class="px-2 py-0.5 text-[10px] rounded border border-slate-700 bg-slate-800/50 text-slate-400">${esc(src(source))} — not searchable</span>`;
            } else if (f > 0) {
                html += `<span class="px-2 py-0.5 text-[10px] rounded border border-green-700/50 bg-green-900/20 text-green-300">${esc(src(source))} ✓ ${f} finding${f > 1 ? 's' : ''}${date ? ' (' + date + ')' : ''}</span>`;
            } else {
                html += `<span class="px-2 py-0.5 text-[10px] rounded border border-slate-700 bg-slate-800/50 text-slate-400">${esc(src(source))} ✓ no findings${date ? ' (' + date + ')' : ''}</span>`;
            }
        });
        html += '</div></div>';
        return html;
    }

    function scoreBreakdownHtml(signal) {
        const sb = signal.score_breakdown || {};
        const flagLabels = { violates_rule_96: 'Rule 96', violates_sub_rule_7: 'Sub-Rule 7', violates_schedule_h2: 'Schedule H2' };
        const mandateTxt = (sb.mandate_flags || []).map(f => flagLabels[f] || f).join(', ');
        const fmt = (w) => (w == null ? '—' : '×' + Number(w).toFixed(1));
        return `
            <div class="pointer-events-none absolute right-0 top-full mt-2 z-50 hidden group-hover:block w-64 p-3 bg-slate-900 border border-slate-700 rounded-lg shadow-xl">
                <p class="text-xs text-slate-400 uppercase tracking-wider mb-2">Score Breakdown</p>
                <div class="space-y-1 text-xs text-slate-300">
                    <div class="flex justify-between gap-2"><span>Base (${signal.event_type})</span><span class="text-white">+${sb.base ?? 0}</span></div>
                    <div class="flex justify-between gap-2"><span>Paper QMS (${sb.paper_bonus_class ?? 'none'})</span><span class="text-white">+${sb.paper_bonus ?? 0}</span></div>
                    <div class="flex justify-between gap-2"><span>2026 Mandate${mandateTxt ? ' · ' + mandateTxt : ''}</span><span class="text-white">+${sb.mandate_bonus ?? 0}</span></div>
                    <div class="flex justify-between gap-2"><span>Recency</span><span class="text-white">${fmt(sb.recency_weight)}</span></div>
                    <div class="flex justify-between gap-2"><span>Repeat offender (${sb.prior_events ?? 0} prior)</span><span class="text-white">+${sb.repeat_offender_bonus ?? 0}</span></div>
                    <div class="flex justify-between gap-2"><span>Web Evidence (${sb.web_evidence_sources ?? 0} src)</span><span class="text-white">+${sb.web_evidence_bonus ?? 0}</span></div>
                    <div class="border-t border-slate-700 mt-2 pt-1 flex justify-between gap-2 font-bold text-white"><span>Total</span><span>${signal.score}</span></div>
                </div>
            </div>
        `;
    }

    class SignalCard extends HTMLElement {
        constructor() {
            super();
            this._signal = null;
            this._viewOnly = false;
            this._selectedIdx = -1;
        }

        set signal(value) {
            this._signal = value;
            if (this.isConnected) this.render();
        }

        set viewOnly(value) {
            this._viewOnly = !!value;
        }

        connectedCallback() {
            this.render();
        }

        selectEvent(value) {
            this._selectedIdx = parseInt(value, 10) || 0;
            if (this._selectedIdx === 0 && value !== '0') this._selectedIdx = -1;
            this.render();
        }

        _displaySignal() {
            const base = this._signal || {};
            const events = base.events || [];
            if (events.length && this._selectedIdx >= 0) {
                const idx = Math.min(this._selectedIdx, events.length - 1);
                return { ...base, ...events[idx], events: base.events, event_count: base.event_count };
            }
            return base;
        }

        render() {
            const base = this._signal || {};
            const signal = this._displaySignal();
            if (!signal.event_id) {
                this.innerHTML = '<div class="glass-panel p-5 border border-slate-700 text-slate-500">No signal data.</div>';
                return;
            }

            const groupEvents = base.events || [];
            const isGroup = (base.event_count || 1) > 1;

            window.__webEv = window.__webEv || {};
            if (base.web_evidence && base.web_evidence.length) {
                window.__webEv[signal.event_id] = base.web_evidence;
            }

            const raw = signal.raw_details || {};
            const drugName = raw.drug_name || 'Unknown Drug';
            const reason = clean(raw.reason || 'No reason provided');
            const rootCause = signal.llm_analysis?.root_cause_summary || 'Analysis pending';
            const { name: fallbackName, address } = splitManufacturer(raw.manufacturer || 'Unknown Manufacturer');
            const company = signal.company_name || base.company_name || fallbackName || 'Unknown Manufacturer';
            const tags = getClassificationTags(signal.llm_analysis);

            let scoreColor = 'text-green-400';
            if (signal.score >= 85) scoreColor = 'text-red-400';
            else if (signal.score >= 65) scoreColor = 'text-yellow-400';

            this.className = 'glass-panel p-5 signal-card border border-slate-700 flex flex-col justify-between h-full';

            this.innerHTML = `
                <div>
                    <div class="flex justify-between items-start mb-3">
                        <span class="px-3 py-1 bg-blue-900/50 text-blue-300 text-xs rounded-full font-medium border border-blue-700/50">
                            ${esc(signal.event_type)}
                        </span>
                        <div class="relative flex flex-col items-end group cursor-help">
                            <span class="text-3xl font-bold ${scoreColor}">${signal.score}</span>
                            <span class="text-xs text-slate-500 uppercase tracking-wide">Lead Score</span>
                            ${scoreBreakdownHtml(signal)}
                        </div>
                    </div>

                    <h3 class="font-bold text-xl mb-1 text-white">${esc(company)}</h3>
                    ${address ? `<p class="text-xs text-slate-500 mb-3 leading-relaxed">${esc(address)}</p>` : ''}
                    ${isGroup ? `
                    <div class="mb-3">
                        <div class="flex items-center gap-2">
                            <span class="text-[10px] uppercase tracking-wider text-slate-500 shrink-0">Incidents · ${base.event_count}</span>
                            <select onchange="this.closest('signal-card').selectEvent(this.value)" class="flex-1 min-w-0 px-2 py-1.5 bg-slate-800/70 border border-slate-700 rounded-lg text-xs text-slate-300 focus:outline-none focus:border-blue-500">
                                <option value="-1" ${this._selectedIdx < 0 ? 'selected' : ''}>Current · ${esc(signal.raw_details?.drug_name || 'Unknown Drug')} · ${esc(signal.event_date || 'n/a')} · ${signal.score}</option>
                                ${groupEvents.map((e, i) => `<option value="${i}" ${this._selectedIdx === i ? 'selected' : ''}>${esc(e.raw_details?.drug_name || 'Unknown Drug')} · ${esc(e.event_date || 'n/a')} · ${e.score}</option>`).join('')}
                            </select>
                        </div>
                    </div>` : ''}
                    <p class="text-slate-400 text-sm mb-4">${esc(drugName)}</p>

                    ${tags.length ? `
                    <div class="mb-4 flex gap-1.5 flex-wrap">
                        ${tags.map(t => `<span class="px-2 py-0.5 text-[10px] rounded border font-medium ${t.cls}">${t.label}</span>`).join('')}
                    </div>` : ''}

                    <div class="mb-4">
                        <p class="text-xs text-slate-500 uppercase tracking-wider mb-1">Issue</p>
                        <p class="text-sm text-slate-300 line-clamp-2" title="${esc(reason)}">${esc(reason)}</p>
                        <p class="text-[10px] text-slate-500 mt-2">Reported: ${esc(signal.event_date || 'n/a')}</p>
                    </div>

                    <div class="mb-5 p-3 bg-slate-800/50 rounded-lg border border-slate-700">
                        <p class="text-xs text-slate-500 uppercase tracking-wider mb-1">AI Root Cause Summary</p>
                        <p class="text-sm text-slate-300 line-clamp-2">${esc(rootCause)}</p>
                    </div>

                    ${paperAssessmentHtml(signal)}
                    ${enrichmentHtml(signal)}
                </div>

                <div class="mt-auto space-y-2">
                    ${this._viewOnly ? '' : `
                    <button onclick="approveCampaign('${signal.event_id}')" class="w-full py-2 bg-slate-700 hover:bg-green-600 transition-colors rounded text-sm font-semibold flex items-center justify-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                        </svg>
                        Approve & Dispatch
                    </button>
                    <button id="enrichBtn-${signal.event_id}" onclick="enrichCard('${signal.event_id}')" class="w-full py-2 bg-indigo-800 hover:bg-indigo-600 transition-colors rounded text-sm font-semibold flex items-center justify-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
                        </svg>
                        Check FDA + EudraGMDP
                    </button>
                    <div id="enrichResult-${signal.event_id}" class="hidden"></div>
                    <button id="webBtn-${signal.event_id}" onclick="searchWeb('${signal.event_id}')" class="w-full py-2 bg-teal-800 hover:bg-teal-600 transition-colors rounded text-sm font-semibold flex items-center justify-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" />
                        </svg>
                        Search Web
                    </button>
                    <div id="webResult-${signal.event_id}" class="hidden"></div>`}
                    ${signal.web_evidence && signal.web_evidence.length ? `
                    <button onclick="showWebEvidenceModal('${signal.event_id}')" class="w-full py-2 bg-slate-800 hover:bg-slate-600 border border-slate-700 transition-colors rounded text-sm font-semibold flex items-center justify-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10a1 1 0 001 1h14a1 1 0 001-1V7a1 1 0 00-1-1H5a1 1 0 00-1 1zm2 4h12M4 7l8-3 8 3" />
                        </svg>
                        Web Evidence (${signal.web_evidence.length})
                    </button>` : ''}
                </div>
            `;
        }
    }

    customElements.define('signal-card', SignalCard);
})();
