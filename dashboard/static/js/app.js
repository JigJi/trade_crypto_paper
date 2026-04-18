/* ═══════════════════════════════════════════════════
   Trading R&D Dashboard - Main App Logic
   Tabs: Paper | Lab | Real
   ═══════════════════════════════════════════════════ */

const API = '';
const INIT_EQUITY = 5000;
let equityChart = null;
let socket = null;
let activeTab = 'paper';
let _coinList = { v3: [], v5: [], v6: [] };
let _modelStats = {};
let _perCoin = [];
let _lastEquityData = [];  // cached equity curve for chart updates

// ══════════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════════

const AUTO_REFRESH_MS = 60 * 60 * 1000;  // 1 hour

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initEquityChart();
    initWebSocket();
    loadAllData();
    setInterval(loadAllData, AUTO_REFRESH_MS);
});

// ══════════════════════════════════════════════════════
// TAB SWITCHING
// ══════════════════════════════════════════════════════

function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;
            if (tab === activeTab) return;

            // Update buttons
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // Update content
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');

            activeTab = tab;

            // Resize chart if switching to paper tab
            if (tab === 'paper' && equityChart) {
                setTimeout(() => equityChart.resize(), 50);
            }

            // Load tab-specific data
            if (tab === 'real') loadExperimentLog();
        });
    });
}

// ══════════════════════════════════════════════════════
// WEBSOCKET
// ══════════════════════════════════════════════════════

function initWebSocket() {
    socket = io();
    socket.on('connect', () => {
        const dot = document.getElementById('hdr-status-dot');
        const txt = document.getElementById('hdr-status-text');
        if (dot) dot.className = 'status-dot live';
        if (txt) txt.textContent = 'LIVE';
    });
    socket.on('disconnect', () => {
        const dot = document.getElementById('hdr-status-dot');
        const txt = document.getElementById('hdr-status-text');
        if (dot) dot.className = 'status-dot down';
        if (txt) txt.textContent = 'offline';
    });
    socket.on('snapshot', (data) => applySnapshot(data));
    socket.on('trading_update', () => loadTradingData());
    socket.on('research_progress', (data) => updateResearchProgress(data));
}

function loadAllData() {
    loadTradingData();
    loadResearchData();
    loadDataHealth();
}

function refreshAll() {
    const btn = document.getElementById('btn-refresh');
    btn.disabled = true;
    btn.style.animation = 'spin 1s linear infinite';
    loadAllData();
    setTimeout(() => {
        btn.disabled = false;
        btn.style.animation = '';
    }, 1500);
}

// ══════════════════════════════════════════════════════
// TAB: PAPER - TRADING ARENA
// ══════════════════════════════════════════════════════

async function loadTradingData() {
    try {
        const [statsRes, tradesRes, equityRes, posRes] = await Promise.all([
            fetch(API + '/api/trading/stats'),
            fetch(API + '/api/trading/trades?limit=50'),
            fetch(API + '/api/trading/equity?limit=1500'),
            fetch(API + '/api/trading/positions'),
        ]);
        const stats = await statsRes.json();
        const trades = await tradesRes.json();
        const equity = await equityRes.json();
        const positions = await posRes.json();

        if (stats.coin_list) _coinList = stats.coin_list;
        if (stats.model_stats) _modelStats = stats.model_stats;
        if (stats.per_coin) _perCoin = stats.per_coin;
        renderHeader(stats);
        renderGauge(stats);
        _lastEquityData = equity.equity || [];
        renderEquityChart(_lastEquityData);
        renderActiveBattles(positions.positions, stats.coin_list);
        renderBattleLog(trades.trades);
        renderCoinWarriors(stats.per_coin, stats.stats && stats.stats.model);
        loadSignalData();
    } catch (e) {
        console.error('Trading data load failed:', e);
    }
}

function renderHeader(data) {
    const s = data.stats || {};
    const latest = data.latest || {};
    const ms = data.model_stats || {};
    // Prefer live exchange balance over SQLite snapshot
    const equity = data.exchange_balance || latest.equity || s.peak_equity || INIT_EQUITY;
    const profitPct = ((equity - INIT_EQUITY) / INIT_EQUITY * 100);

    // Equity + return %
    const eqEl = document.getElementById('hdr-equity');
    eqEl.innerHTML = '$' + fmtNum(equity) + ` <span class="header-stat-pct" style="color:${profitPct >= 0 ? 'var(--green)' : 'var(--red)'}">${(profitPct >= 0 ? '+' : '') + profitPct.toFixed(1)}%</span>`;
    eqEl.style.color = equity >= INIT_EQUITY ? 'var(--green)' : 'var(--red)';

    // v3/v5/v6/old model stats
    _renderModelStat('v3', ms.v3 || {});
    _renderModelStat('v5', ms.v5 || {});
    _renderModelStat('old', ms.old || {});

    document.getElementById('trade-count').textContent = (s.total_trades || 0) + ' trades';
}

function _renderModelStat(model, d) {
    const pnl = d.pnl || 0;
    const pnlEl = document.getElementById('hdr-' + model + '-pnl');
    const detailEl = document.getElementById('hdr-' + model + '-detail');
    if (pnlEl) {
        pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + fmtNum(Math.abs(pnl));
        pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    }
    if (detailEl) {
        detailEl.textContent = (d.trades || 0) + 'T ' + (d.win_rate || 0).toFixed(0) + '%';
    }
}

function renderGauge(data) {
    const latest = data.latest || {};
    const score = Number(latest.btc_score) || 0;
    const pct = Math.max(0, Math.min(100, (score + 10) / 20 * 100));

    // Needle position
    document.getElementById('gauge-needle').style.left = pct + '%';

    // Score value + color
    const valEl = document.getElementById('gauge-value');
    valEl.textContent = (score >= 0 ? '+' : '') + score.toFixed(1);
    valEl.style.color = score > 2 ? 'var(--green)' : score < -2 ? 'var(--red)' : 'var(--text-dim)';

    // Bear/Bull icon
    const iconEl = document.getElementById('mood-icon');
    if (score > 2) {
        iconEl.textContent = '\u{1F402}';  // bull 🐂
        iconEl.className = 'mood-icon bull';
    } else if (score < -2) {
        iconEl.textContent = '\u{1F43B}';  // bear 🐻
        iconEl.className = 'mood-icon bear';
    } else {
        iconEl.textContent = '\u{1F610}';  // neutral 😐
        iconEl.className = 'mood-icon';
    }

    // Fill bar (colored portion showing strength)
    const fillEl = document.getElementById('gauge-fill');
    const strength = Math.min(Math.abs(score) / 10 * 50, 50);  // max 50% width from center
    if (score >= 0) {
        fillEl.className = 'gauge-fill bull';
        fillEl.style.width = strength + '%';
        fillEl.style.left = '50%';
        fillEl.style.right = 'auto';
    } else {
        fillEl.className = 'gauge-fill bear';
        fillEl.style.width = strength + '%';
        fillEl.style.right = '50%';
        fillEl.style.left = 'auto';
    }
}

function renderActiveBattles(positions, coinList) {
    const el = document.getElementById('active-battles');
    const posMap = {};
    if (positions) {
        positions.forEach(p => {
            posMap[p.coin] = p.direction === 1 ? 'long' : 'short';
        });
    }
    const v3 = (coinList && coinList.v3) || [];
    const v5 = (coinList && coinList.v5) || [];
    const v6 = (coinList && coinList.v6) || [];
    const allCoinsSet = new Set([...v3, ...v5, ...v6]);
    const totalCoins = allCoinsSet.size;
    const openCount = Object.keys(posMap).filter(c => allCoinsSet.has(c)).length;

    const countEl = document.getElementById('open-count');
    if (countEl) countEl.textContent = openCount + '/' + totalCoins + ' coins';

    const chipHtml = (coins, model) => coins.map(c => {
        const state = posMap[c];
        if (state) return `<span class="pos-chip ${state}">${c}</span>`;
        return `<span class="pos-chip idle ${model}">${c}</span>`;
    }).join('');

    // Show stale positions from old coins separately
    const extraOpen = Object.keys(posMap).filter(c => !allCoinsSet.has(c));
    const extraHtml = extraOpen.map(c => {
        const state = posMap[c];
        return `<span class="pos-chip ${state}" title="not in config">${c}</span>`;
    }).join('');

    el.innerHTML = '<div class="active-chips">' +
        chipHtml(v3, 'v3') + chipHtml(v5, 'v5') + chipHtml(v6, 'v6') +
        (extraHtml ? ' ' + extraHtml : '') +
    '</div>';
}

function renderBattleLog(trades) {
    const el = document.getElementById('battle-log');
    if (!trades || trades.length === 0) {
        el.innerHTML = '<li class="empty-state">No trades yet</li>';
        return;
    }
    el.innerHTML = trades.map(t => {
        const pnl = Number(t.pnl_net);
        const isWin = pnl > 0;
        const icon = isWin ? '&#x2694;' : '&#x1F480;';
        const dir = t.direction === 1 ? 'long' : 'short';
        const model = t.model || '?';
        const reason = t.exit_reason || '';
        const reasonCls = reason === 'TP' ? 'tp' : (reason === 'SL' || reason === 'SL/TP') ? 'sl' : reason === 'SIGNAL_FLIP' ? 'flip' : reason === 'MAX_HOLD' ? 'hold' : '';
        const reasonLabel = reason === 'SIGNAL_FLIP' ? 'FLIP' : reason === 'SL/TP' ? 'SL/TP' : reason === 'MAX_HOLD' ? 'HOLD' : reason || '';
        const timeStr = t.exit_time ? formatTimeAgo(t.exit_time) : '';
        const margin = Number(t.margin) || 0;
        const lev = Number(t.leverage) || 2;
        const profitPct = Number(t.profit_pct) || 0;
        return `<li class="battle-item">
            <span class="battle-icon">${icon}</span>
            <span class="battle-dir ${dir}">${dir.toUpperCase()}</span>
            <span class="battle-model ${model}">${model}</span>
            <span class="battle-coin">${t.coin}</span>
            <span class="battle-margin">$${margin.toFixed(0)}</span>
            <span class="battle-lev">${lev}x</span>
            <span class="battle-reason ${reasonCls}">${reasonLabel}</span>
            <span class="battle-pnl ${isWin ? 'positive' : 'negative'}">${isWin ? '+' : ''}$${pnl.toFixed(2)}</span>
            <span class="battle-profit ${isWin ? 'positive' : 'negative'}">${isWin ? '+' : ''}${profitPct.toFixed(1)}%</span>
            <span class="battle-time">${timeStr}</span>
        </li>`;
    }).join('');
}

function renderWarriorItem(c) {
    const wr = Number(c.win_rate) || 0;
    const pnl = Number(c.total_pnl) || 0;
    const trades = Number(c.trades) || 0;
    const model = c.model || '?';
    const histTrades = Number(c.hist_trades) || 0;
    const histPnl = Number(c.hist_pnl) || 0;
    // Model badge + historical note if coin changed model
    let modelBadge = `<span class="warrior-model ${model}">${model}</span>`;
    if (histTrades > 0) {
        const histStr = histPnl >= 0 ? `+$${Math.abs(histPnl).toFixed(0)}` : `-$${Math.abs(histPnl).toFixed(0)}`;
        modelBadge += `<span class="warrior-hist" title="Historical: ${histTrades} trades from previous model">(prev ${histTrades}T ${histStr})</span>`;
    }
    return `<li class="warrior-item">
        <span class="warrior-coin">${c.coin}</span>
        <span class="warrior-model-group">${modelBadge}</span>
        <span class="warrior-trades">${trades}T</span>
        <span class="warrior-stars">${getStars(wr)}</span>
        <span class="warrior-wr">${wr}%</span>
        <span class="warrior-pnl ${pnl >= 0 ? 'positive' : 'negative'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(0)}</span>
        <span class="warrior-title">"${getTitle(wr, pnl)}"</span>
    </li>`;
}

function renderCoinWarriors(perCoin, model) {
    const el = document.getElementById('coin-warriors');
    const sumEl = document.getElementById('warrior-summary');
    if (!perCoin || perCoin.length === 0) {
        el.innerHTML = '<li class="empty-state">No data yet</li>';
        if (sumEl) sumEl.textContent = '';
        return;
    }
    const totalTrades = perCoin.reduce((s, c) => s + (Number(c.trades) || 0), 0);
    if (sumEl) sumEl.textContent = `${totalTrades} trades`;

    const allCoins = perCoin.sort((a, b) => (b.total_pnl || 0) - (a.total_pnl || 0));
    el.innerHTML = allCoins.map(renderWarriorItem).join('');
}

function getStars(wr) {
    if (wr >= 65) return '\u2605\u2605\u2605\u2605\u2605';
    if (wr >= 55) return '\u2605\u2605\u2605\u2605';
    if (wr >= 50) return '\u2605\u2605\u2605';
    if (wr >= 40) return '\u2605\u2605';
    return '\u2605';
}

function getLevel(trades) {
    if (trades >= 50) return 5;
    if (trades >= 30) return 4;
    if (trades >= 15) return 3;
    if (trades >= 5) return 2;
    return 1;
}

function getTitle(wr, pnl) {
    if (wr >= 60 && pnl > 50) return 'The Legend';
    if (wr >= 55 && pnl > 0) return 'The Reliable';
    if (wr >= 50 && pnl > 0) return 'The Balanced';
    if (pnl > 0) return 'The Climber';
    if (wr >= 45) return 'Struggling';
    if (pnl > -50) return 'In Training';
    return 'The Rookie';
}

function formatTimeAgo(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr + (isoStr.endsWith('Z') ? '' : 'Z'));
    const now = new Date();
    const diffMs = now - d;
    if (diffMs < 0) return 'just now';
    const diffM = Math.floor(diffMs / 60000);
    if (diffM < 1) return 'just now';
    if (diffM < 60) return diffM + 'm ago';
    const diffH = Math.floor(diffMs / 3600000);
    if (diffH < 24) return diffH + 'h ago';
    const diffD = Math.floor(diffMs / 86400000);
    return diffD + 'd ago';
}

// ══════════════════════════════════════════════════════
// SIGNAL LOG
// ══════════════════════════════════════════════════════

async function loadSignalData() {
    try {
        const res = await fetch(API + '/api/trading/signals?limit=50');
        const data = await res.json();
        renderSignalLog(data.signals || []);
    } catch (e) {
        console.error('Signal data load failed:', e);
    }
}

function renderSignalLog(signals) {
    const el = document.getElementById('signal-log');
    const badge = document.getElementById('signal-count');
    if (!signals || signals.length === 0) {
        el.innerHTML = '<div class="empty-state">No signals yet</div>';
        if (badge) badge.textContent = '0';
        return;
    }
    if (badge) badge.textContent = signals.length + ' recent';

    el.innerHTML = signals.map(sig => {
        const action = sig.action || '';
        const isOpen = action.startsWith('OPEN_');
        const dimClass = isOpen ? '' : ' dim';
        const model = sig.model || '?';
        const score = Number(sig.btc_score) || 0;
        const ts = sig.ts ? formatTimeAgo(sig.ts) : '';
        return `<div class="signal-item${dimClass}">
            <span class="signal-time">${ts}</span>
            <span class="signal-coin">${sig.coin || ''}</span>
            <span class="battle-model ${model}">${model}</span>
            <span class="signal-score" style="color:${score > 0 ? 'var(--green)' : score < 0 ? 'var(--red)' : 'var(--text-dim)'}">${score >= 0 ? '+' : ''}${score.toFixed(1)}</span>
            <span class="signal-action ${isOpen ? 'open' : ''}">${action}</span>
        </div>`;
    }).join('');
}

// ══════════════════════════════════════════════════════
// EQUITY CHART (Daily bars)
// ══════════════════════════════════════════════════════

// Custom plugin: draw value label on top/bottom of every bar
const pnlBarLabelsPlugin = {
    id: 'pnlBarLabels',
    afterDatasetsDraw(chart) {
        const dataset = chart.data.datasets[0];
        const data = dataset.data;
        if (!data || data.length === 0) return;
        const ctx = chart.ctx;
        const meta = chart.getDatasetMeta(0);
        ctx.save();
        ctx.font = "bold 10px 'JetBrains Mono', monospace";
        ctx.textAlign = 'center';
        meta.data.forEach((bar, i) => {
            const val = data[i];
            if (val === 0) return;
            const label = (val >= 0 ? '+' : '-') + '$' + Math.abs(val).toFixed(0);
            ctx.fillStyle = val >= 0 ? '#22c55e' : '#ef4444';
            if (val >= 0) {
                ctx.textBaseline = 'bottom';
                ctx.fillText(label, bar.x, bar.y - 4);
            } else {
                ctx.textBaseline = 'top';
                ctx.fillText(label, bar.x, bar.y + 4);
            }
        });
        ctx.restore();
    }
};

function initEquityChart() {
    const ctx = document.getElementById('equity-chart').getContext('2d');
    equityChart = new Chart(ctx, {
        type: 'bar',
        plugins: [pnlBarLabelsPlugin],
        data: {
            labels: [],
            datasets: [{
                label: 'Daily P&L',
                data: [],
                backgroundColor: [],
                borderRadius: 3,
                borderSkipped: false,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            layout: { padding: { top: 18, bottom: 18 } },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => (ctx.parsed.y >= 0 ? '+$' : '-$') + fmtNum(Math.abs(ctx.parsed.y)),
                        afterBody: (items) => {
                            if (!items.length) return '';
                            const idx = items[0].dataIndex;
                            const cumPnl = items[0].chart._cumPnl;
                            if (cumPnl && cumPnl[idx] != null) {
                                return 'Total: ' + (cumPnl[idx] >= 0 ? '+$' : '-$') + fmtNum(Math.abs(cumPnl[idx]));
                            }
                            return '';
                        }
                    }
                }
            },
            scales: {
                x: {
                    display: true,
                    grid: { display: false },
                    ticks: { color: '#64748b', font: { size: 10, family: "'JetBrains Mono'" } }
                },
                y: {
                    display: false,
                }
            },
            interaction: { intersect: false, mode: 'index' },
        }
    });
}

function renderEquityChart(equityData) {
    if (!equityChart || !equityData || equityData.length === 0) return;

    // Aggregate to daily: take last equity per day
    const dailyMap = {};
    equityData.forEach(e => {
        if (!e.ts) return;
        const d = new Date(e.ts);
        const key = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
        dailyMap[key] = Number(e.equity);
    });

    const allDays = Object.keys(dailyMap).sort();
    const days = allDays.slice(-14);  // last 14 days
    const equities = days.map(d => dailyMap[d]);
    const labels = days.map(d => {
        const parts = d.split('-');
        return parts[1] + '/' + parts[2];
    });

    // Daily PnL (first visible day compares to the day before it, or INIT_EQUITY)
    const prevEquity = allDays.length > days.length ? dailyMap[allDays[allDays.length - days.length - 1]] : INIT_EQUITY;
    const dailyPnl = equities.map((eq, i) => i === 0 ? eq - prevEquity : eq - equities[i-1]);
    const barColors = dailyPnl.map(p => p >= 0 ? 'rgba(34,197,94,0.75)' : 'rgba(239,68,68,0.75)');

    // Cumulative PnL for tooltip
    const cumPnl = [];
    let cum = 0;
    dailyPnl.forEach(p => { cum += p; cumPnl.push(Math.round(cum * 100) / 100); });
    equityChart._cumPnl = cumPnl;

    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = dailyPnl;
    equityChart.data.datasets[0].backgroundColor = barColors;
    equityChart.update('none');
}

// ══════════════════════════════════════════════════════
// TAB: LAB - RESEARCH
// ══════════════════════════════════════════════════════

async function loadResearchData() {
    try {
        const [factorsRes, modelsRes, lbRes, missionsRes] = await Promise.all([
            fetch(API + '/api/research/factors'),
            fetch(API + '/api/research/models'),
            fetch(API + '/api/research/leaderboard'),
            fetch(API + '/api/research/missions'),
        ]);
        const factors = await factorsRes.json();
        const models = await modelsRes.json();
        const lb = await lbRes.json();
        const missions = await missionsRes.json();

        renderFactorInventory(factors);
        renderModelHQ(lb.champion, models.models);
        renderMissions(missions);
    } catch (e) {
        console.error('Research data load failed:', e);
    }
}

const FACTOR_ICONS = {
    liquidation: '\u{1F30A}', funding_rate: '\u{1F4B0}', ob_combined: '\u{1F4D6}',
    etf_flows: '\u{1F3E6}', basis_contrarian: '\u{2696}\uFE0F', tick_liq: '\u{1F3AF}',
    oi_divergence: '\u{1F4C8}', whale_alerts: '\u{1F40B}', stable_supply: '\u{1FA99}',
    macro_risk_off: '\u{1F30D}', cvd_contrarian: '\u{1F4C9}', hashrate: '\u{26CF}\uFE0F',
    dvol_level: '\u{1F4CA}', dvol_change: '\u{1F300}', skew_25d: '\u{1F4D0}',
    put_call_ratio: '\u{260E}\uFE0F', gamma_exposure: '\u{26A1}', max_pain: '\u{1F480}',
    fear_greed: '\u{1F631}', taker_ratio: '\u{1F528}', ls_ratio: '\u{1F4CF}',
    active_addr: '\u{1F465}', dex_ratio: '\u{1F517}', basis_momentum: '\u{1F3C3}',
    news_directional: '\u{1F4F0}', news_contrarian: '\u{1F5DE}\uFE0F',
    displacement: '\u{1F4A5}', fvg: '\u{1F573}\uFE0F', sweep: '\u{1F9F9}',
    btc_dominance: '\u{1F451}',
};

const FACTOR_DESC = {
    liquidation: {
        what: 'Tracks large liquidation cascades on Binance Futures. When over-leveraged positions get force-closed, it creates predictable price movements.',
        why: 'Contrarian signal: large long liquidations = bullish reversal, large short liquidations = bearish reversal. Strongest single factor in the model.',
        tested: 'Mega discovery ablation + stepwise build. Consistently #1 across all test periods (bull & bear). Delta +$10,837 on OOS.'
    },
    funding_rate: {
        what: 'Measures the periodic funding fee between long/short perpetual futures traders. Positive = longs pay shorts, negative = shorts pay longs.',
        why: 'Extreme funding signals crowded positioning. High positive funding = too many longs = contrarian short. Works as a mean-reversion indicator.',
        tested: 'Part of v1 original 8 factors. Survived all pruning rounds. Stable across timeframes.'
    },
    ob_combined: {
        what: 'Combines order book bid/ask imbalance at multiple depth levels (1%, 2%, 5% from mid price). Measures real-time supply/demand pressure.',
        why: 'Contrarian: when bids heavily outweigh asks, smart money is often distributing. Order book walls often break. Best used as fade signal.',
        tested: 'New in v3 (from orderbook factor tests). Tested ob_combined vs ob_contrarian vs ob_vol. Combined performed best at w=2.0.'
    },
    etf_flows: {
        what: 'Tracks daily net inflows/outflows from US spot Bitcoin ETFs (BlackRock IBIT, Fidelity FBTC, etc.). Institutional money flow proxy.',
        why: 'Large inflows = institutional buying pressure, affects spot price which leads futures. Only daily factor that adds value on 15m timeframe.',
        tested: 'Tested at w=0.5-2.0 in mega discovery. Optimal at w=1.0. Adds +$1,654 with minimal correlation to other factors.'
    },
    basis_contrarian: {
        what: 'Measures futures-spot basis (premium/discount). Calculated from Binance futures mark price vs spot index price.',
        why: 'Contrarian: extreme positive basis = overheated longs, fade long. Extreme negative basis = panic shorts, fade short. Mean-reverts reliably.',
        tested: 'New factor from test_new_factors.py. Tested basis_contrarian vs basis_momentum. Contrarian clearly superior (+$1,709 vs -$100).'
    },
    tick_liq: {
        what: 'Measures tick-level liquidity depth from Binance trade stream. Captures how much volume is needed to move price by 1 tick.',
        why: 'Thin liquidity = potential for sharp moves. Thick liquidity = mean-reversion likely. Used as volatility regime filter.',
        tested: 'New in v3. Added +$657. Modest individual contribution but low correlation with other factors, improving diversification.'
    },
    oi_divergence: {
        what: 'Detects divergence between Open Interest changes and price changes. Rising OI + falling price = bearish divergence, and vice versa.',
        why: 'OI divergence signals positioning buildup against price trend. When resolved, creates sharp moves in divergence direction.',
        tested: 'Part of v1 original factors. Optimal weight dropped from 1.0 to 0.5 in v3 (partially redundant with liquidation). Still net positive.'
    },
    whale_alerts: {
        what: 'Tracks large BTC transfers (>100 BTC) detected from blockchain. Aggregated from Whale Alert service via our data collector.',
        why: 'Large exchange inflows may signal selling pressure. Large outflows signal accumulation. Noisy but adds small edge as ensemble member.',
        tested: 'Part of v1 original factors. Smallest contributor (+$214) but consistently positive across test periods. Kept at w=1.5.'
    },
    stable_supply: {
        what: 'Tracks total stablecoin (USDT+USDC) market cap changes. Growing stablecoin supply = more dry powder for crypto buying.',
        why: 'Rising stablecoin supply is structurally bullish for crypto. Acts as macro liquidity indicator.',
        tested: 'Best v4 candidate (+$1,337). Passed individual test but overlaps with short_bias effect. Skipped to avoid overfitting.'
    },
    macro_risk_off: {
        what: 'Combines DXY (US Dollar Index) strength and VIX (equity volatility). High DXY + high VIX = risk-off environment.',
        why: 'Risk-off macro environment is bearish for crypto. Helps filter out long signals during macro stress.',
        tested: 'Phase 1 factor test. +$785 individually but failed stepwise addition to v3 (redundant signal with existing factors).'
    },
    cvd_contrarian: {
        what: 'Cumulative Volume Delta - net buy vs sell volume from Binance trades. Contrarian interpretation: extreme buying = fade.',
        why: 'When retail aggressively market-buys (positive CVD spike), price often reverses. Contrarian CVD captures this.',
        tested: 'Phase 1 factor test. +$667 individually but failed stepwise (redundant with taker_ratio/liquidation signals).'
    },
    hashrate: {
        what: 'Bitcoin network hashrate from blockchain.com. Measures total mining computational power.',
        why: 'Rising hashrate = miner confidence = structurally bullish. Declining hashrate = miners capitulating.',
        tested: 'v4 factor test. +$345 individually but daily data on 15m timeframe has limited utility. Failed stepwise.'
    },
    dvol_level: {
        what: 'Deribit DVOL index - implied volatility of BTC options (like VIX for Bitcoin).',
        why: 'High DVOL = expensive options = fear = potential contrarian buy. Low DVOL = complacency = potential sell.',
        tested: 'v4 factor test. +$343 individually. Marginal improvement, failed stepwise addition.'
    },
    dvol_change: {
        what: 'Rate of change in DVOL. Rapidly rising IV = panic, rapidly falling = relief.',
        why: 'IV spikes often coincide with price bottoms. IV crush often signals trend exhaustion.',
        tested: 'v4 factor test. +$297 individually. Similar signal to dvol_level, both failed stepwise.'
    },
    fear_greed: {
        what: 'Crypto Fear & Greed Index (0-100). Composite of volatility, volume, social media, dominance, trends.',
        why: 'Theory: extreme fear = buy, extreme greed = sell. In practice: daily granularity is too coarse for 15m trading.',
        tested: 'Part of v1 original factors. HURTS v3 (-$500). Dropped in mega discovery. Daily sentiment = noise on 15m.'
    },
    taker_ratio: {
        what: 'Ratio of taker buy volume to taker sell volume on Binance Futures.',
        why: 'Similar concept to CVD but as a ratio. High ratio = aggressive buying.',
        tested: 'Part of v1. Dropped in v3 mega discovery (-$200). Redundant with liquidation signal.'
    },
    ls_ratio: {
        what: 'Long/Short ratio of top Binance Futures traders. Measures positioning of large accounts.',
        why: 'Theory: fade the crowd. But Binance L/S ratio has known issues with accuracy.',
        tested: 'Part of v1. Dropped in v3 (-$150). Signal too noisy and redundant with other positioning factors.'
    },
    active_addr: {
        what: 'Number of active Bitcoin addresses (daily). On-chain activity metric.',
        why: 'Theory: more active addresses = more adoption = bullish. But daily data lags price significantly.',
        tested: 'v4 factor test. Negative impact (-$754). Daily on-chain data too slow for 15m decisions.'
    },
    dex_ratio: {
        what: 'DEX-to-CEX volume ratio from DefiLlama. Measures DeFi vs centralized exchange activity.',
        why: 'Theory: rising DEX ratio = retail interest shifting to DeFi. In practice: had lookahead bias.',
        tested: 'v4 factor test. Was positive before anti-lookahead fix, flipped to -$53 after correction. Discarded.'
    },
    basis_momentum: {
        what: 'Momentum (rate of change) of futures-spot basis. Rising basis = increasing premium.',
        why: 'Momentum approach to basis. Failed because contrarian approach works much better for basis.',
        tested: 'test_new_factors.py. -$100. Contrarian version (basis_contrarian) outperforms by +$1,809.'
    },
    news_directional: {
        what: 'Crypto news sentiment score. Bullish news = long signal, bearish news = short signal.',
        why: 'Direct sentiment following. Failed because news is already priced in by the time we trade.',
        tested: 'test_new_factors.py. -$200. News too slow for 15m alpha.'
    },
    news_contrarian: {
        what: 'Contrarian interpretation of news sentiment. Extreme bullish news = sell, extreme bearish = buy.',
        why: 'Contrarian news. Failed because news sentiment lacks granularity needed for 15m signals.',
        tested: 'test_new_factors.py. -$100. Neither direction of news works on 15m.'
    },
    displacement: {
        what: 'ICT displacement concept - large single-candle moves that indicate institutional activity.',
        why: 'Theory: displacement candles show where institutions entered. Follow the displacement direction.',
        tested: 'test_new_factors.py. -$300. Price action concepts don\'t add alpha beyond our quant factors.'
    },
    fvg: {
        what: 'Fair Value Gap - price gaps in candle structure (ICT concept). Gaps tend to get filled.',
        why: 'Theory: FVGs act as magnets. Trade in direction of gap fill.',
        tested: 'test_new_factors.py. -$250. Too noisy on 15m timeframe, better suited for manual trading.'
    },
    sweep: {
        what: 'Liquidity sweep detection - price briefly takes out a recent high/low then reverses (ICT concept).',
        why: 'Theory: sweeps grab stop losses before reversing. Trade the reversal after sweep.',
        tested: 'test_new_factors.py. -$350. Worst performing ICT concept. Too many false signals on 15m.'
    },
    btc_dominance: {
        what: 'Bitcoin dominance (BTC market cap / total crypto market cap). Measures BTC vs altcoin preference.',
        why: 'Theory: rising BTC.D = risk-off within crypto (flight to BTC), falling = altcoin season.',
        tested: 'Phase 1 factor test. -$50. Near zero impact. BTC.D changes too slowly for 15m decisions.'
    },
    skew_25d: {
        what: '25-delta skew from Deribit BTC options. Measures put vs call premium difference.',
        why: 'Negative skew = puts expensive = fear. Positive skew = calls expensive = greed. Classic options indicator.',
        tested: 'Not yet tested. Data collection set up but needs scorer function implementation.'
    },
    put_call_ratio: {
        what: 'BTC options put/call volume ratio from Deribit.',
        why: 'High put/call = hedging/bearish, low = speculative/bullish. Well-established in TradFi.',
        tested: 'Not yet tested. Awaiting scorer function.'
    },
    gamma_exposure: {
        what: 'Market maker gamma exposure estimate from BTC options open interest.',
        why: 'Positive GEX = dealers sell rallies/buy dips (stabilizing). Negative GEX = dealers amplify moves.',
        tested: 'Not yet tested. Complex to calculate correctly, needs research.'
    },
    max_pain: {
        what: 'Options max pain strike price - the price where most options expire worthless.',
        why: 'Theory: price gravitates toward max pain near expiry. Pin risk creates predictable drift.',
        tested: 'Not yet tested. Needs expiry-aware implementation.'
    },
};

function _factorRarity(delta) {
    const d = Math.abs(delta || 0);
    if (d >= 5000) return 'legendary';
    if (d >= 1500) return 'epic';
    if (d >= 500) return 'rare';
    return 'common';
}

// Store all factors for detail panel
let _allFactors = [];

function _buildItemData(f) {
    const icon = FACTOR_ICONS[f.name] || '\u{2753}';
    const delta = f.best_delta_pnl || 0;
    const rarity = _factorRarity(delta);
    return { icon, delta, rarity };
}

function _showFactorDetail(name) {
    const f = _allFactors.find(x => x.name === name);
    if (!f) return;
    const el = document.getElementById('factor-detail');
    const { icon, delta, rarity } = _buildItemData(f);
    const sign = delta >= 0 ? '+' : '';
    const desc = FACTOR_DESC[f.name] || {};
    const statusMap = { production: 'Equipped', tested_positive: 'Bench', tested_negative: 'Rejected', untested: 'Untested' };
    const statusColor = { production: 'var(--green)', tested_positive: 'var(--yellow)', tested_negative: 'var(--red)', untested: 'var(--text-dim)' };

    el.innerHTML = `<div class="detail-card ${rarity}">
        <div class="detail-top">
            <span class="detail-icon">${icon}</span>
            <div class="detail-info">
                <div class="detail-name">${f.name.replace(/_/g, ' ')}</div>
                <div class="detail-meta">
                    <span class="detail-badge" style="color:${statusColor[f.status]}">${statusMap[f.status]}</span>
                    <span class="detail-rarity ${rarity}">${rarity}</span>
                    <span style="font-size:10px;color:var(--text-dim)">${f.category || ''}</span>
                </div>
            </div>
            <div class="detail-stats">
                <div class="detail-stat-item">
                    <div class="detail-stat-val" style="color:${delta >= 0 ? 'var(--green)' : 'var(--red)'}">${sign}$${fmtNum(Math.abs(delta))}</div>
                    <div class="detail-stat-lbl">Delta PnL</div>
                </div>
                <div class="detail-stat-item">
                    <div class="detail-stat-val">${f.production_weight || '--'}</div>
                    <div class="detail-stat-lbl">Weight</div>
                </div>
                <div class="detail-stat-item">
                    <div class="detail-stat-val">${f.last_tested || '--'}</div>
                    <div class="detail-stat-lbl">Tested</div>
                </div>
            </div>
        </div>
        <div class="detail-desc-grid">
            ${desc.what ? `<div class="detail-desc-block">
                <div class="detail-desc-title">What is it?</div>
                <div class="detail-desc-text">${desc.what}</div>
            </div>` : ''}
            ${desc.why ? `<div class="detail-desc-block">
                <div class="detail-desc-title">Why it works</div>
                <div class="detail-desc-text">${desc.why}</div>
            </div>` : ''}
            ${desc.tested ? `<div class="detail-desc-block">
                <div class="detail-desc-title">Test results</div>
                <div class="detail-desc-text">${desc.tested}</div>
            </div>` : ''}
        </div>
        ${f.notes ? `<div class="detail-notes">${f.notes}</div>` : ''}
    </div>`;
    el.style.display = 'block';

    // highlight selected
    el.closest('.section-body').querySelectorAll('.equip-slot, .inv-item').forEach(s => s.classList.remove('selected'));
    const sel = el.closest('.section-body').querySelector(`[data-factor="${f.name}"]`);
    if (sel) sel.classList.add('selected');
}

function renderFactorInventory(data) {
    const factors = data.factors || [];
    _allFactors = factors;
    const summary = data.summary || {};
    const el = document.getElementById('factor-inventory');

    const badge = document.getElementById('factor-count');
    if (badge) badge.textContent = `${summary.production || 0} equipped / ${summary.total || 0} total`;

    const equipped = factors.filter(f => f.status === 'production').sort((a, b) => (b.best_delta_pnl || 0) - (a.best_delta_pnl || 0));
    const bench = factors.filter(f => f.status === 'tested_positive').sort((a, b) => (b.best_delta_pnl || 0) - (a.best_delta_pnl || 0));
    const unknown = factors.filter(f => f.status === 'untested');
    const failed = factors.filter(f => f.status === 'tested_negative').sort((a, b) => (b.best_delta_pnl || 0) - (a.best_delta_pnl || 0));

    let html = '';

    // ── EQUIPPED SLOTS ──
    html += `<div class="equip-section">
        <div class="equip-label">EQUIPPED</div>
        <div class="equip-grid">`;
    // v6 uses only these 2 factors
    const V6_FACTORS = ['liquidation', 'tick_liq'];
    equipped.forEach(f => {
        const { icon, delta, rarity } = _buildItemData(f);
        const inV6 = V6_FACTORS.includes(f.name);
        const tags = `<span class="equip-tag v3">v3</span><span class="equip-tag v5">v5</span>` +
            (inV6 ? `<span class="equip-tag v6">v6</span>` : '');
        html += `<div class="equip-slot ${rarity}" data-factor="${f.name}" onclick="_showFactorDetail('${f.name}')">
            <div class="equip-icon">${icon}</div>
            <div class="equip-info">
                <div class="equip-model-tags">${tags}</div>
                <div class="equip-name">${f.name.replace(/_/g, ' ')}</div>
            </div>
        </div>`;
    });
    html += '</div></div>';

    // ── INVENTORY ──
    html += `<div class="inv-section">
        <div class="inv-label">INVENTORY</div>
        <div class="inv-grid">`;

    bench.forEach(f => {
        const { icon, rarity } = _buildItemData(f);
        html += `<div class="inv-item bench ${rarity}" data-factor="${f.name}" onclick="_showFactorDetail('${f.name}')">
            <div class="inv-icon">${icon}</div>
            <div class="inv-name">${f.name.replace(/_/g, ' ')}</div>
        </div>`;
    });

    unknown.forEach(f => {
        const { icon } = _buildItemData(f);
        html += `<div class="inv-item unknown" data-factor="${f.name}" onclick="_showFactorDetail('${f.name}')">
            <div class="inv-icon">${icon}</div>
            <div class="inv-name">${f.name.replace(/_/g, ' ')}</div>
        </div>`;
    });

    failed.forEach(f => {
        const { icon } = _buildItemData(f);
        html += `<div class="inv-item broken" data-factor="${f.name}" onclick="_showFactorDetail('${f.name}')">
            <div class="inv-icon">${icon}</div>
            <div class="inv-name">${f.name.replace(/_/g, ' ')}</div>
        </div>`;
    });

    html += '</div></div>';

    // ── DETAIL PANEL (hidden by default) ──
    html += '<div id="factor-detail" style="display:none"></div>';

    el.innerHTML = html;
}

// ── MODEL HQ: Static config for each version ──
const MODEL_CONFIGS = {
    v3: {
        status: 'production',
        desc: '8 factors, grid-searched per-coin thresholds. Validated in paper trading since 03-15.',
        sl: 10, tp: 5, threshold: '2.5-3.5', cooldown: '4-8',
        weights: { liq: 2.0, fr: 2.0, ob: 2.0, tick: 2.0, basis: 1.5, whale: 1.5, etf: 1.0, oi: 0.5 },
        backtest: { pnl: 14121, sharpe: '4.97-6.83', wr: 66.6, trades: 946 },
    },
    v5: {
        status: 'production',
        desc: 'Tournament R1 champion: liq 2\u21925, tick 2\u21923, SL 10\u219215, TP 5\u219212. Deployed 03-19.',
        sl: 15, tp: 12, threshold: '3.0', cooldown: '4',
        weights: { liq: 5.0, fr: 2.0, ob: 2.0, tick: 3.0, basis: 1.5, whale: 1.5, etf: 1.0, oi: 0.5 },
        backtest: { pnl: 49052, sharpe: '8.2-10.1', wr: 68, trades: 'avg ~160/coin' },
    },
    v6: {
        status: 'testing',
        desc: 'Liq-only architecture: cascade(1.1x MA) + tick. +35% vs v5 per-coin. Deployed 03-23.',
        sl: 25, tp: 20, threshold: '3.0', cooldown: '4',
        weights: { liq: 8.0, tick: 8.0 },
        backtest: { pnl: 69701, sharpe: '25.63', wr: 69.8, trades: 2038 },
    },
};

let _activeModelTab = 'v3';

function renderModelHQ(champ, models) {
    const el = document.getElementById('model-hq');
    const badge = document.getElementById('model-badge');
    if (badge) badge.textContent = '3 versions';

    let html = '<div class="mhq-tabs">';
    for (const ver of ['v3', 'v5', 'v6']) {
        const cfg = MODEL_CONFIGS[ver];
        const ms = _modelStats[ver] || {};
        const coins = _coinList[ver] || [];
        const isActive = ver === _activeModelTab;
        const pnl = ms.pnl || 0;

        html += `<div class="mhq-tab ${ver} ${isActive ? 'active' : ''}" onclick="_selectModelTab('${ver}')">
            <div class="mhq-tab-top">
                <span class="mhq-tab-ver">${ver}</span>
                <span class="mhq-tab-status ${cfg.status}">${cfg.status === 'production' ? 'PROD' : 'TEST'}</span>
            </div>
            <div class="mhq-tab-pnl ${pnl >= 0 ? 'positive' : 'negative'}">${pnl >= 0 ? '+' : ''}$${fmtNum(Math.abs(pnl))}</div>
            <div class="mhq-tab-meta">${ms.trades || 0}T &bull; ${(ms.win_rate || 0).toFixed(0)}% &bull; ${coins.length}c</div>
        </div>`;
    }
    html += '</div>';
    html += `<div id="model-detail">${_renderModelDetail(_activeModelTab)}</div>`;
    el.innerHTML = html;
}

function _selectModelTab(ver) {
    _activeModelTab = ver;
    document.querySelectorAll('.mhq-tab').forEach(t => t.classList.remove('active'));
    const tab = document.querySelector('.mhq-tab.' + ver);
    if (tab) tab.classList.add('active');
    const detail = document.getElementById('model-detail');
    if (detail) detail.innerHTML = _renderModelDetail(ver);
}

function _renderModelDetail(ver) {
    const cfg = MODEL_CONFIGS[ver];
    const ms = _modelStats[ver] || {};
    const coins = _coinList[ver] || [];
    const coinStats = _perCoin.filter(c => c.model === ver).sort((a, b) => (b.total_pnl || 0) - (a.total_pnl || 0));
    const v3w = MODEL_CONFIGS.v3.weights;

    let html = `<div class="mhq-desc">${cfg.desc}</div>`;

    // ── Config row ──
    html += `<div class="mhq-config">
        <div class="mhq-cfg-item"><span class="mhq-cfg-label">SL</span><span class="mhq-cfg-val">${cfg.sl} ATR</span></div>
        <div class="mhq-cfg-item"><span class="mhq-cfg-label">TP</span><span class="mhq-cfg-val">${cfg.tp} ATR</span></div>
        <div class="mhq-cfg-item"><span class="mhq-cfg-label">Threshold</span><span class="mhq-cfg-val">${cfg.threshold}</span></div>
        <div class="mhq-cfg-item"><span class="mhq-cfg-label">CD</span><span class="mhq-cfg-val">${cfg.cooldown} bars</span></div>
    </div>`;

    // ── Factor weights bars ──
    html += '<div class="mhq-weights"><div class="mhq-section-title">Factor Weights</div>';
    const maxW = 5.5;
    for (const [factor, weight] of Object.entries(cfg.weights)) {
        const pct = Math.min(weight / maxW * 100, 100).toFixed(0);
        const diff = ver !== 'v3' ? weight - (v3w[factor] || 0) : 0;
        const diffStr = diff !== 0 ? `<span class="mhq-w-diff ${diff > 0 ? 'up' : 'down'}">${diff > 0 ? '+' : ''}${diff.toFixed(1)}</span>` : '';
        html += `<div class="mhq-w-row">
            <span class="mhq-w-name">${factor}</span>
            <div class="mhq-w-bar"><div class="mhq-w-fill ${ver}" style="width:${pct}%"></div></div>
            <span class="mhq-w-val">${weight}</span>${diffStr}
        </div>`;
    }
    html += '</div>';

    // ── Backtest reference ──
    if (cfg.backtest) {
        const bt = cfg.backtest;
        html += `<div class="mhq-backtest">
            <span class="mhq-section-title">Backtest (OOS)</span>
            <span class="mhq-bt-stat">$${fmtNum(bt.pnl)}</span>
            <span class="mhq-bt-meta">Sharpe ${bt.sharpe} &bull; WR ${bt.wr}% &bull; ${bt.trades} trades</span>
        </div>`;
    }

    // ── Coin performance (paper) or coin list ──
    if (coinStats.length > 0) {
        html += `<div class="mhq-section-title" style="margin-top:10px">Paper Performance (${coinStats.length} coins)</div>`;
        html += '<div class="mhq-coins">';
        coinStats.forEach(c => {
            const pnl = c.total_pnl || 0;
            const wr = c.win_rate || 0;
            html += `<div class="mhq-coin">
                <span class="mhq-coin-name">${c.coin}</span>
                <span class="mhq-coin-pnl ${pnl >= 0 ? 'positive' : 'negative'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(1)}</span>
                <span class="mhq-coin-meta">${c.trades}T ${wr}%</span>
            </div>`;
        });
        html += '</div>';
    } else {
        html += `<div class="mhq-section-title" style="margin-top:10px">Coins (${coins.length})</div>`;
        html += '<div class="mhq-coin-tags">' + coins.map(c => `<span class="mhq-coin-tag ${ver}">${c}</span>`).join('') + '</div>';
    }

    return html;
}

// ══════════════════════════════════════════════════════
// DAILY MISSIONS
// ══════════════════════════════════════════════════════

const MISSION_ICONS = {
    factor_test: '\u{1F9EA}',      // test tube
    revalidation: '\u{1F50D}',     // magnifying glass
    combo_test: '\u{2697}\uFE0F',  // alembic
    coin_deep_dive: '\u{1F4A0}',   // diamond
    regime_test: '\u{1F30D}',      // globe
    paper_vs_backtest: '\u{1F4CA}', // bar chart
    param_sweep: '\u{1F9F9}',      // broom
    trade_analysis: '\u{1F4C8}',   // chart increasing
    signal_quality: '\u{1F4E1}',   // satellite
    drawdown_analysis: '\u{1F4C9}', // chart decreasing
    web_discovery: '\u{1F310}',    // globe with meridians
};

const ANALYSIS_TYPES = ['trade_analysis', 'signal_quality', 'drawdown_analysis',
                        'paper_vs_backtest', 'coin_deep_dive', 'revalidation'];
const DISCOVERY_TYPES = ['web_discovery', 'factor_test', 'combo_test',
                         'regime_test', 'param_sweep'];

// ── RPG World Map (PNG Background + SVG Overlay) ──
// ViewBox matches world_map.png (21:9 aspect ratio)
const MAP_VB = { w: 630, h: 270 };

// Land safe zone for random pin placement (mainland area, avoids ocean)
const MAP_LAND = { x: 60, y: 25, w: 510, h: 220 };

// Region names for flavor -- cycled for any number of missions
const MAP_REGIONS = [
    'Port Siren', 'Haven Village', "Trader's Rest", 'Crossroads',
    'Sunlit Meadow', 'Timberline', 'Whispering Glade', 'Canopy Watch',
    'Ember Ridge', 'Lakeshore Camp', "River's End", 'Stone Gate',
    'Dragon Spine', 'Frostpeak', 'Last Frontier', 'Crystal Basin',
    'Iron Hollow', 'Coral Bay', 'Thornwood', 'Sky Temple',
];

// Seeded random: same mission index → same position every render
function _mapRandom(seed) {
    let s = seed;
    return function() {
        s = (s * 16807 + 12345) % 2147483647;
        return (s & 0x7fffffff) / 2147483647;
    };
}

function _missionPosition(idx, total) {
    total = Math.max(total || 1, 1);
    // Grid layout: calculate cols/rows to fill the map evenly
    const aspect = MAP_LAND.w / MAP_LAND.h;
    const cols = Math.max(1, Math.ceil(Math.sqrt(total * aspect)));
    const rows = Math.max(1, Math.ceil(total / cols));
    const cellW = MAP_LAND.w / cols;
    const cellH = MAP_LAND.h / rows;
    const col = idx % cols;
    const row = Math.floor(idx / cols);
    // Center in cell + small seeded jitter for organic RPG feel
    const rng = _mapRandom(idx * 7919 + 31);
    const jitterX = (rng() - 0.5) * cellW * 0.4;
    const jitterY = (rng() - 0.5) * cellH * 0.4;
    return {
        x: MAP_LAND.x + (col + 0.5) * cellW + jitterX,
        y: MAP_LAND.y + (row + 0.5) * cellH + jitterY,
        region: MAP_REGIONS[idx % MAP_REGIONS.length],
    };
}

// ── "You Are Here" flag on latest mission ──
function _renderYouAreHereFlag(x, y) {
    const fx = x + 9, fy = y - 9;
    let s = '';
    s += `<circle cx="${x}" cy="${y}" r="13" fill="none" stroke="#c084fc" stroke-width="1.5" opacity="0.7">
        <animate attributeName="r" values="13;20;13" dur="2s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.7;0.1;0.7" dur="2s" repeatCount="indefinite"/>
    </circle>`;
    s += `<line x1="${fx}" y1="${fy}" x2="${fx}" y2="${fy - 15}" stroke="#f0f0f0" stroke-width="1.5"/>`;
    s += `<polygon points="${fx},${fy - 15} ${fx + 10},${fy - 11} ${fx},${fy - 7}" fill="#f0c040" opacity="0.95"/>`;
    return s;
}

// ── SVG Overlay Renderer (pins only -- PNG background is the map) ──
function _renderMap(missions) {
    const W = MAP_VB.w, H = MAP_VB.h;

    let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">`;

    // ── Defs ──
    svg += `<defs>
        <filter id="glow-purple" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur"/>
            <feFlood flood-color="#a855f7" flood-opacity="0.6"/>
            <feComposite in2="blur" operator="in"/>
            <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="glow-red" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur"/>
            <feFlood flood-color="#ef4444" flood-opacity="0.6"/>
            <feComposite in2="blur" operator="in"/>
            <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
    </defs>`;

    // ── Mission pins ──
    missions.forEach((m, i) => {
        const pos = _missionPosition(i, missions.length);
        const verdict = (m.result || {}).verdict || m.status;
        const failed = verdict === 'failed' || verdict === 'negative' || verdict === 'degraded';
        const isCurrent = i === missions.length - 1;
        const icon = MISSION_ICONS[m.type] || '\u{2753}';
        const _mId = m.mission_id || (i + 1);
        const _num = String(_mId).match(/^(\d+)$/) ? _mId
            : (String(_mId).match(/mission_(\d{3})/) || [])[1]
            || String(i + 1).padStart(2, '0');
        const label = `M${_num}`;
        const cls = isCurrent ? 'current' : (failed ? 'failed' : 'completed');
        svg += `<g class="map-node" data-idx="${i}" style="cursor:pointer" onclick="window.open('/mission/${_mId}','_blank');event.stopPropagation()">`;
        svg += `<circle cx="${pos.x}" cy="${pos.y}" r="9" class="map-node-circle ${cls}"/>`;
        svg += `<text x="${pos.x}" y="${pos.y}" class="map-node-icon" dy="1">${icon}</text>`;
        svg += `<text x="${pos.x}" y="${pos.y + 16}" class="map-node-label">${label}</text>`;
        if (isCurrent) {
            svg += _renderYouAreHereFlag(pos.x, pos.y);
        }
        svg += `</g>`;
    });

    svg += '</svg>';
    return svg;
}

// ── Timeline Renderer ──
function _renderTimeline(missions, todayDates) {
    let html = '';
    missions.forEach(m => {
        const icon = MISSION_ICONS[m.type] || '\u{2753}';
        const verdict = (m.result || {}).verdict || m.status;
        const verdictClass = _verdictClass(verdict);
        const isFailed = verdictClass === 'negative' || verdictClass === 'degraded';
        const isToday = todayDates.has(m.date);
        const itemClass = `mission-tl-item${isFailed ? ' failed' : ''}${isToday ? ' today' : ''}`;

        const _mId = m.mission_id || (missions.indexOf(m) + 1);
        const _tlNum = String(_mId).match(/^(\d+)$/) ? _mId
            : (String(_mId).match(/mission_(\d{3})/) || [])[1]
            || String(missions.indexOf(m) + 1).padStart(2, '0');
        html += `<div class="${itemClass}" style="cursor:pointer" onclick="window.open('/mission/${_mId}','_blank')">`;
        html += `<div class="mission-tl-date">${m.date || ''}</div>`;
        html += `<div class="mission-tl-card">`;
        html += `<div class="mission-tl-header">`;
        html += `<span class="mission-tl-icon">${icon}</span>`;
        html += `<span class="mission-tl-title">M${_tlNum}</span>`;
        html += `<span class="mission-tl-verdict mission-h-verdict ${verdictClass}">${verdict}</span>`;
        html += `<span class="mission-tl-xp">+${m.xp_reward || 0} XP</span>`;
        html += `</div>`; // header

        if (m.insight) {
            html += `<div class="mission-tl-insight">${m.insight}</div>`;
        }

        html += `</div>`; // card
        html += `</div>`; // item
    });
    return html;
}

// ── Map Tooltip Init ──
function _initMapTooltips(missions) {
    const tooltip = document.querySelector('.map-tooltip');
    if (!tooltip) return;

    document.querySelectorAll('.map-node').forEach(g => {
        const idx = parseInt(g.dataset.idx, 10);
        const m = missions[idx];
        const pos = _missionPosition(idx);

        g.addEventListener('mouseenter', () => {
            const verdict = (m.result || {}).verdict || m.status;
            tooltip.innerHTML = `<div class="tt-title">${m.title || m.type}</div>
                <div class="tt-region">${pos.region}</div>
                <div class="tt-date">${m.date || ''}</div>
                <div class="tt-verdict">${verdict}</div>
                <div class="tt-xp">+${m.xp_reward || 0} XP</div>`;
            tooltip.style.display = 'block';

            // Position near node with bounds check
            const container = tooltip.parentElement;
            const rect = container.getBoundingClientRect();
            const gRect = g.getBoundingClientRect();
            let left = gRect.left - rect.left + gRect.width / 2;
            let top = gRect.top - rect.top - 60;

            // Bounds check
            const tw = tooltip.offsetWidth || 200;
            if (left + tw > rect.width) left = rect.width - tw - 8;
            if (left < 4) left = 4;
            if (top < 4) top = gRect.top - rect.top + gRect.height + 8;

            tooltip.style.left = left + 'px';
            tooltip.style.top = top + 'px';
        });

        g.addEventListener('mouseleave', () => {
            tooltip.style.display = 'none';
        });
    });
}

// ── Main Render (replaces old renderMissions) ──
function renderMissions(data) {
    const el = document.getElementById('mission-panel');
    const badge = document.getElementById('mission-badge');
    const meta = data.meta || {};
    const today = data.today;
    const missions = data.missions || [];

    // Badge
    const levelName = meta.level_name || 'Apprentice';
    if (badge) badge.textContent = `Lv.${meta.level || 1} ${levelName}`;

    let html = '';

    // ── XP Progress Bar ──
    const xp = meta.total_xp || 0;
    const progress = meta.xp_needed > 0 ? (meta.xp_progress / meta.xp_needed * 100) : 100;
    const streak = meta.current_streak || 0;

    html += `<div class="mission-xp-bar">
        <span class="mission-level-badge">Lv.${meta.level || 1}</span>
        <span class="mission-level-name">${levelName}</span>
        <div class="mission-xp-track">
            <div class="mission-xp-fill" style="width:${Math.min(100, progress)}%"></div>
        </div>
        <span class="mission-xp-text">${xp} XP</span>
        <span class="mission-streak">${streak > 0 ? streak + 'd \u{1F525}' : '0d'}</span>
    </div>`;

    // ── Exploration Map ── (oldest-first for M1=first, M4=latest)
    const mapMissions = [...missions].sort((a, b) => (a.date || '').localeCompare(b.date || '') || (a.started_at || '').localeCompare(b.started_at || ''));
    html += `<div class="mission-section-label">EXPLORATION MAP</div>`;
    html += `<div class="mission-map-container" style="position:relative">`;
    html += _renderMap(mapMissions);
    html += `<div class="map-tooltip"></div>`;
    html += `</div>`;

    // ── Mission Log (Timeline) ──
    html += `<div class="mission-section-label" style="margin-top:14px">MISSION LOG</div>`;
    html += `<div class="mission-timeline">`;
    if (missions.length > 0) {
        // today is now an array of 0-2 missions
        const todayList = Array.isArray(today) ? today : (today ? [today] : []);
        const todayDates = new Set(todayList.map(m => m.date).filter(Boolean));
        html += _renderTimeline(missions, todayDates);
    } else {
        html += `<div class="empty-state">No missions yet. Run your first mission!</div>`;
    }
    html += `</div>`;

    el.innerHTML = html;

    // Init tooltips after DOM is set
    _initMapTooltips(mapMissions);
}

function _verdictClass(verdict) {
    const positive = ['positive', 'stable', 'on_track', 'profitable', 'outperforming', 'strong', 'worth_adding', 'completed', 'significant', 'continuation_pattern'];
    const negative = ['negative', 'degraded', 'underperforming', 'losing', 'weak', 'failed', 'not_worth'];
    if (positive.includes(verdict)) return verdict === 'worth_adding' ? 'worth_adding' : 'positive';
    if (negative.includes(verdict)) return verdict === 'degraded' ? 'degraded' : 'negative';
    return 'neutral';
}

// ══════════════════════════════════════════════════════
// TAB: REAL - DATA STREAMS + EXPERIMENTS
// ══════════════════════════════════════════════════════

async function loadDataHealth() {
    try {
        const res = await fetch(API + '/api/data/health');
        const data = await res.json();
        renderDataStreams(data.health);
    } catch (e) {
        console.error('Data health load failed:', e);
    }
}

function renderDataStreams(health) {
    const el = document.getElementById('data-streams');
    if (!health || health.length === 0) {
        el.innerHTML = '<div class="empty-state" style="width:100%">No data streams</div>';
        document.getElementById('data-badge').textContent = 'No data';
        return;
    }

    const liveCount = health.filter(h => h.status === 'live').length;
    const staleCount = health.filter(h => h.status === 'stale').length;
    const downCount = health.filter(h => h.status === 'down' || h.status === 'error').length;

    document.getElementById('data-badge').textContent =
        `${liveCount} live / ${staleCount} stale / ${downCount} down`;

    el.innerHTML = health.map(h => {
        const status = h.status || 'down';
        const icon = status === 'live' ? '\u25CF' : status === 'stale' ? '\u26A0' : '\u2716';
        const iconColor = status === 'live' ? 'var(--green)' :
                          status === 'stale' ? 'var(--yellow)' : 'var(--red)';
        const staleness = h.staleness_hours != null ?
            (h.staleness_hours < 1 ? Math.round(h.staleness_hours * 60) + 'm' :
             h.staleness_hours.toFixed(1) + 'h') : '?';
        const rows = h.total_rows != null ? fmtNum(h.total_rows) + ' rows' : '';

        return `<div class="stream-item ${status}">
            <span style="color:${iconColor};font-size:14px">${icon}</span>
            <span class="stream-name">${h.name}</span>
            <span class="stream-staleness">${staleness} ago</span>
            <span class="stream-staleness">${rows}</span>
        </div>`;
    }).join('');
}

async function loadExperimentLog() {
    try {
        const res = await fetch(API + '/api/research/experiments?limit=20');
        const data = await res.json();
        renderExperimentLog(data.experiments || []);
    } catch (e) {
        console.error('Experiment log load failed:', e);
    }
}

function renderExperimentLog(experiments) {
    const el = document.getElementById('experiment-log');
    if (!experiments || experiments.length === 0) {
        el.innerHTML = '<div class="empty-state">No experiments recorded</div>';
        return;
    }

    el.innerHTML = `<table class="exp-table">
        <tr><th>ID</th><th>Date</th><th>Description</th></tr>
        ${experiments.slice().reverse().map(e => {
            const id = e.experiment_id || '';
            const shortId = id.length > 25 ? id.substring(0, 25) + '...' : id;
            return `<tr>
                <td style="color:var(--cyan);font-size:11px">${shortId}</td>
                <td style="color:var(--text-dim);font-size:11px;white-space:nowrap">${e.date || ''}</td>
                <td>${e.description || ''}</td>
            </tr>`;
        }).join('')}
    </table>`;
}

// ══════════════════════════════════════════════════════
// SNAPSHOT (WebSocket full update)
// ══════════════════════════════════════════════════════

function applySnapshot(snap) {
    if (snap.latest_equity || snap.exchange_balance) {
        const eq = snap.latest_equity || {};
        const equity = snap.exchange_balance || eq.equity || INIT_EQUITY;
        const profitPct = ((equity - INIT_EQUITY) / INIT_EQUITY * 100);
        const eqEl = document.getElementById('hdr-equity');
        eqEl.innerHTML = '$' + fmtNum(equity) + ` <span class="header-stat-pct" style="color:${profitPct >= 0 ? 'var(--green)' : 'var(--red)'}">${(profitPct >= 0 ? '+' : '') + profitPct.toFixed(1)}%</span>`;
        eqEl.style.color = equity >= INIT_EQUITY ? 'var(--green)' : 'var(--red)';
    }
    // Update equity chart with latest equity from snapshot
    if (snap.latest_equity && snap.latest_equity.ts && snap.latest_equity.equity != null && _lastEquityData.length > 0) {
        const last = _lastEquityData[_lastEquityData.length - 1];
        if (snap.latest_equity.ts !== last.ts) {
            _lastEquityData.push({ts: snap.latest_equity.ts, equity: snap.latest_equity.equity});
        } else {
            last.equity = snap.latest_equity.equity;
        }
        renderEquityChart(_lastEquityData);
    }
    if (snap.coin_list) _coinList = snap.coin_list;
    if (snap.model_stats) {
        _modelStats = snap.model_stats;
        _renderModelStat('v3', snap.model_stats.v3 || {});
        _renderModelStat('v5', snap.model_stats.v5 || {});
        _renderModelStat('old', snap.model_stats.old || {});
    }
    if (snap.positions) renderActiveBattles(snap.positions, snap.coin_list || _coinList);
    if (snap.recent_trades) renderBattleLog(snap.recent_trades);
    if (snap.coin_stats) {
        _perCoin = snap.coin_stats;
        renderCoinWarriors(snap.coin_stats);
    }
}

// ══════════════════════════════════════════════════════
// UTILS
// ══════════════════════════════════════════════════════

function fmtNum(n) {
    if (n == null) return '0';
    n = Number(n);
    if (Math.abs(n) >= 1000000) return (n/1000000).toFixed(1) + 'M';
    if (Math.abs(n) >= 10000) return (n/1000).toFixed(1) + 'K';
    if (Math.abs(n) >= 1000) return n.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return n.toFixed(2);
}
