"use client";

import { useEffect, useState, useCallback } from "react";

const API = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

async function login(password: string): Promise<{ success: boolean }> {
    const res = await fetch(`${API}/api/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ password }),
    });
    return res.json();
}

async function checkAuth(): Promise<boolean> {
    try {
        const res = await fetch(`${API}/api/check-auth`, { credentials: "include" });
        const data = await res.json();
        return data.authenticated === true;
    } catch (e) {
        console.warn("checkAuth failed:", e);
        return false;
    }
}

interface Stats {
    total_trades: number;
    live_trades: number;
    dry_run_trades: number;
    error_trades: number;
    pending_trades: number;
    stopped_out_trades: number;
    manual_close_trades: number;
    total_cost_cents: number;
    total_potential_profit_cents: number;
    realized_pnl_cents: number;
    wins: number;
    losses: number;
    win_rate: number;
    total_scans: number;
    total_opportunities: number;
    balance_cents: number;
    portfolio_value_cents: number;
    open_positions: number;
    open_cost_cents: number;
    open_potential_profit_cents: number;
    total_deposited_cents: number;
}

interface Trade {
    id: number;
    placed_at: string;
    ticker: string;
    event_ticker: string;
    title: string;
    side: string;
    count: number;
    yes_price: number;
    cost_cents: number;
    potential_profit_cents: number;
    status: string;
    pnl_cents: number | null;
    dry_run: boolean;
    error: string | null;
    order_id: string | null;
}

interface PnlPoint {
    placed_at: string;
    pnl_cents: number;
    status: string;
    ticker: string;
    title: string | null;
    cost_cents: number;
    yes_price: number;
    count: number;
}

interface KalshiMarket {
    ticker: string;
    team: string;
    yes_sub_title: string;
    yes_bid: number;
    yes_ask: number;
    volume: number;
}

interface LiveGame {
    espn_id: string;
    sport: string;
    series: string;
    home_team: string;
    away_team: string;
    home_score: number;
    away_score: number;
    period: number;
    display_clock: string;
    clock_seconds: number;
    state: string;
    is_final_minutes: boolean;
    is_target: boolean;
    is_watching: boolean;
    has_bet: boolean;
    score_diff: number;
    min_score_lead: number;
    final_period: number;
    kalshi_markets: KalshiMarket[];
}

interface SportConfig {
    sport_path: string;
    name: string;
    kalshi_series: string;
    final_period: number;
    min_score_lead: number;
    stretch_score_lead: number;
    clock_direction: "down" | "up" | "none";
    final_minutes_desc: string;
    final_minutes_seconds: number | null;
}

interface AppConfig {
    trading: {
        min_yes_price: number;
        max_bet_cents: number;
        max_bet_pct: number;
        max_positions: number;
        min_volume: number;
        dry_run: boolean;
        stop_loss_price: number;
    };
    stretch: { price_min: number };
    polling: {
        espn_interval_s: number;
        kalshi_scan_interval_s: number;
        kalshi_ws: boolean;
        db_backup_interval_s: number;
    };
    sports: SportConfig[];
}

// ─── Utilities ───────────────────────────────────────────────────

function cents(c: number): string {
    return `$${(c / 100).toFixed(2)}`;
}

function formatGameTime(g: LiveGame): string {
    const sport = g.sport;
    const p = g.period;
    const clock = g.display_clock;
    const clockNum = parseFloat(clock);
    let timeStr: string;
    if (clock.includes(":")) {
        timeStr = clockNum === 0 ? "End" : clock;
    } else {
        const secs = Math.floor(clockNum);
        timeStr = secs === 0 ? "End" : `0:${secs.toString().padStart(2, "0")}`;
    }
    if (sport.startsWith("basketball/")) {
        if (g.final_period === 2) {
            const label = p > 2 ? "OT" : p === 1 ? "1st Half" : "2nd Half";
            return `${label} ${timeStr}`;
        }
        const label = p > g.final_period ? "OT" : `Q${p}`;
        return `${label} ${timeStr}`;
    }
    if (sport.startsWith("hockey/")) {
        const ord = p === 1 ? "1st" : p === 2 ? "2nd" : p === 3 ? "3rd" : "OT";
        return `${ord} ${timeStr}`;
    }
    if (sport.startsWith("football/")) {
        const label = p > g.final_period ? "OT" : `Q${p}`;
        return `${label} ${timeStr}`;
    }
    if (sport.startsWith("baseball/")) {
        const ord = p === 1 ? "1st" : p === 2 ? "2nd" : p === 3 ? "3rd" : `${p}th`;
        return `${ord} inning`;
    }
    if (sport.startsWith("soccer/")) {
        const minute = Math.floor(clockNum);
        return minute > 0 ? `${minute}'` : p === 1 ? "1st Half" : "2nd Half";
    }
    if (sport.startsWith("mma/")) return `R${p} ${timeStr}`;
    return `P${p} ${timeStr}`;
}

function timeAgo(iso: string): string {
    const utcIso = iso.endsWith("Z") ? iso : iso + "Z";
    const diff = Date.now() - new Date(utcIso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
}

function sportLabel(sport: string): string {
    const map: Record<string, string> = {
        "basketball/nba": "NBA",
        "hockey/nhl": "NHL",
        "football/nfl": "NFL",
        "baseball/mlb": "MLB",
        "basketball/mens-college-basketball": "NCAAM",
        "football/college-football": "NCAAF",
        "mma/ufc": "UFC",
        "soccer/eng.1": "EPL",
        "soccer/esp.1": "La Liga",
        "soccer/ger.1": "Bundesliga",
        "soccer/fra.1": "Ligue 1",
        "soccer/ita.1": "Serie A",
        "soccer/usa.1": "MLS",
        "soccer/uefa.champions": "UCL",
        "soccer/uefa.europa": "UEL",
    };
    return map[sport] || sport.split("/")[1]?.toUpperCase() || sport;
}

// ─── Status Badge ────────────────────────────────────────────────

function StatusBadge({ trade: t }: { trade: Trade }) {
    if (t.dry_run) {
        return (
            <span className="text-xs font-semibold px-2.5 py-1 rounded-full bg-zinc-800 text-zinc-500 border border-zinc-700">
                DRY
            </span>
        );
    }
    const variants: Record<string, string> = {
        settled_win: "bg-emerald-500/20 text-emerald-300 border-emerald-500/40",
        settled_loss: "bg-red-500/20 text-red-300 border-red-500/40",
        error: "bg-red-500/20 text-red-300 border-red-500/40",
        stopped_out: "bg-orange-500/20 text-orange-300 border-orange-500/40",
        stop_failed: "bg-red-500/25 text-red-200 border-red-500/50",
        manual_close: "bg-zinc-800 text-zinc-400 border-zinc-700",
        pending: "bg-amber-500/20 text-amber-300 border-amber-500/40",
        placed: "bg-indigo-500/20 text-indigo-300 border-indigo-500/40",
        filled: "bg-indigo-500/20 text-indigo-300 border-indigo-500/40",
        resting: "bg-zinc-800 text-zinc-400 border-zinc-700",
    };
    return (
        <span className={`text-xs font-semibold px-2.5 py-1 rounded-full border whitespace-nowrap ${variants[t.status] ?? "bg-zinc-800 text-zinc-400 border-zinc-700"}`}>
            {t.status.replace(/_/g, " ").toUpperCase()}
        </span>
    );
}

// ─── Config Panel ────────────────────────────────────────────────

function ConfigPanel({ config, onUpdate }: { config: AppConfig; onUpdate: () => void }) {
    const [open, setOpen] = useState(false);
    const [saving, setSaving] = useState<string | null>(null);

    const updateConfig = async (key: string, value: string) => {
        setSaving(key);
        try {
            await fetch(`${API}/api/config`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                credentials: "include",
                body: JSON.stringify({ key, value }),
            });
            onUpdate();
        } finally {
            setTimeout(() => setSaving(null), 300);
        }
    };

    const seen = new Set<string>();
    const uniqueSports = config.sports.filter((s) => {
        if (seen.has(s.sport_path)) return false;
        seen.add(s.sport_path);
        return true;
    });

    const categories: Record<string, SportConfig[]> = {};
    for (const s of uniqueSports) {
        const cat = s.sport_path.split("/")[0];
        if (!categories[cat]) categories[cat] = [];
        categories[cat].push(s);
    }

    const catOrder = ["basketball", "football", "hockey", "baseball", "soccer", "lacrosse", "mma", "cricket", "australian-football"];

    function timingLabel(s: SportConfig): string {
        if (s.clock_direction === "none") return "Final period";
        if (s.clock_direction === "up" && s.final_minutes_seconds) {
            return `${Math.floor(s.final_minutes_seconds / 60)}'`;
        }
        if (s.final_minutes_seconds) {
            const min = Math.floor(s.final_minutes_seconds / 60);
            const sec = s.final_minutes_seconds % 60;
            return sec > 0 ? `${min}:${sec.toString().padStart(2, "0")}` : `${min}:00`;
        }
        return s.final_minutes_desc;
    }

    function timingSeconds(s: SportConfig): number { return s.final_minutes_seconds || 0; }
    function timingStep(s: SportConfig): number { return s.clock_direction === "up" ? 300 : 60; }
    function timingMax(s: SportConfig): number { return s.clock_direction === "up" ? 5400 : 900; }
    function timingKey(s: SportConfig): string { return `final_seconds:${s.sport_path}`; }
    function leadKey(s: SportConfig): string { return `lead:${s.sport_path}`; }

    const isLive = !config.trading.dry_run;

    return (
        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl overflow-hidden">
            <button
                onClick={() => setOpen(!open)}
                className="w-full px-6 py-4 flex items-center justify-between hover:bg-white/[0.02] transition-colors"
            >
                <div className="flex items-center gap-3">
                    <svg className="w-4 h-4 text-zinc-600" viewBox="0 0 16 16" fill="none">
                        <circle cx="8" cy="8" r="2.5" stroke="currentColor" strokeWidth="1.5"/>
                        <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                    </svg>
                    <span className="text-sm font-medium text-zinc-400">Configuration</span>
                    <span className="text-xs text-zinc-600">{uniqueSports.length} sports</span>
                </div>
                <div className="flex items-center gap-3">
                    <span className={`text-xs font-bold px-2.5 py-1 rounded-full border ${isLive ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40" : "bg-amber-500/20 text-amber-300 border-amber-500/40"}`}>
                        {isLive ? "LIVE" : "DRY RUN"}
                    </span>
                    <svg className={`w-4 h-4 text-zinc-600 transition-transform duration-200 ${open ? "rotate-180" : ""}`} viewBox="0 0 16 16" fill="none">
                        <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </div>
            </button>

            {open && (
                <div className="border-t border-white/[0.06] divide-y divide-white/[0.06]">
                    {/* Trading params */}
                    <div className="p-6">
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-5">Trading Parameters</div>
                        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
                            <div className="sm:col-span-2 lg:col-span-3">
                                <label className="text-xs text-zinc-500 mb-2 block">Mode</label>
                                <button
                                    onClick={() => updateConfig("dry_run", config.trading.dry_run ? "false" : "true")}
                                    disabled={saving === "dry_run"}
                                    className={`w-full py-3 px-5 rounded-xl text-sm font-bold transition-all border ${config.trading.dry_run ? "bg-amber-500/10 text-amber-300 border-amber-500/25 hover:bg-amber-500/20" : "bg-emerald-500/10 text-emerald-300 border-emerald-500/25 hover:bg-emerald-500/20"}`}
                                >
                                    {saving === "dry_run" ? "Saving…" : config.trading.dry_run ? "DRY RUN — click to go LIVE" : "LIVE — click to switch to DRY RUN"}
                                </button>
                            </div>
                            <ConfigStepper label="Min YES Price" value={config.trading.min_yes_price} suffix="¢" step={1} min={80} max={99} configKey="min_yes_price" saving={saving} onUpdate={updateConfig} />
                            <ConfigStepper label="Bet Size" value={config.trading.max_bet_pct || Math.round(config.trading.max_bet_cents / 100)} format={(v) => `${v}%`} step={1} min={1} max={50} configKey="max_bet_pct" saving={saving} onUpdate={updateConfig} />
                            <ConfigStepper label="Max Positions" value={config.trading.max_positions} step={1} min={1} max={50} configKey="max_positions" saving={saving} onUpdate={updateConfig} />
                            <ConfigStepper label="Stop-Loss" value={config.trading.stop_loss_price ?? 50} suffix="¢" step={5} min={0} max={80} configKey="stop_loss_price" saving={saving} onUpdate={updateConfig} />
                        </div>
                    </div>

                    {/* Sport settings */}
                    <div className="p-6">
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-5">Sport Settings</div>
                        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-8 gap-y-6">
                            {catOrder.filter((cat) => categories[cat]).map((cat) => (
                                <div key={cat}>
                                    <div className="text-xs font-semibold uppercase tracking-wider text-zinc-700 mb-3 capitalize">{cat}</div>
                                    <div className="space-y-2">
                                        {categories[cat].map((s) => (
                                            <div key={s.sport_path} className="flex items-center gap-3 py-1.5">
                                                <span className="text-sm text-zinc-400 w-28 shrink-0 truncate">
                                                    {s.name === s.sport_path ? s.sport_path.split("/")[1] : s.name}
                                                </span>
                                                <div className="flex items-center gap-1 shrink-0">
                                                    <span className="text-xs text-zinc-600 w-7">Lead</span>
                                                    <div className="flex items-center">
                                                        <button onClick={() => updateConfig(leadKey(s), String(Math.max(0, s.min_score_lead - 1)))} disabled={saving === leadKey(s) || s.min_score_lead <= 0} className="w-7 h-7 rounded-l-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-sm disabled:opacity-30 transition-colors">−</button>
                                                        <div className="w-8 h-7 flex items-center justify-center bg-white/[0.02] border-y border-white/10 text-sm font-mono text-zinc-200 tabular-nums">
                                                            {saving === leadKey(s) ? "·" : s.min_score_lead}
                                                        </div>
                                                        <button onClick={() => updateConfig(leadKey(s), String(s.min_score_lead + 1))} disabled={saving === leadKey(s)} className="w-7 h-7 rounded-r-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-sm disabled:opacity-30 transition-colors">+</button>
                                                    </div>
                                                </div>
                                                {s.clock_direction !== "none" && (
                                                    <div className="flex items-center gap-1 shrink-0">
                                                        <span className="text-xs text-zinc-600 w-7">Time</span>
                                                        <div className="flex items-center">
                                                            <button onClick={() => updateConfig(timingKey(s), String(Math.max(60, timingSeconds(s) - timingStep(s))))} disabled={saving === timingKey(s) || timingSeconds(s) <= 60} className="w-7 h-7 rounded-l-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-sm disabled:opacity-30 transition-colors">−</button>
                                                            <div className="w-14 h-7 flex items-center justify-center bg-white/[0.02] border-y border-white/10 text-xs font-mono text-zinc-200">
                                                                {saving === timingKey(s) ? "·" : timingLabel(s)}
                                                            </div>
                                                            <button onClick={() => updateConfig(timingKey(s), String(Math.min(timingMax(s), timingSeconds(s) + timingStep(s))))} disabled={saving === timingKey(s) || timingSeconds(s) >= timingMax(s)} className="w-7 h-7 rounded-r-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-sm disabled:opacity-30 transition-colors">+</button>
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

function ConfigStepper({
    label, value, suffix, format, step, min, max, configKey, saving, onUpdate,
}: {
    label: string;
    value: number;
    suffix?: string;
    format?: (v: number) => string;
    step: number;
    min: number;
    max: number;
    configKey: string;
    saving: string | null;
    onUpdate: (key: string, value: string) => void;
}) {
    const display = format ? format(value) : `${value}${suffix || ""}`;
    return (
        <div>
            <label className="text-xs text-zinc-500 mb-2 block">{label}</label>
            <div className="flex items-center">
                <button
                    onClick={() => onUpdate(configKey, String(Math.max(min, value - step)))}
                    disabled={saving === configKey || value <= min}
                    className="w-10 h-10 rounded-l-xl bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-200 transition-all disabled:opacity-25 text-base"
                >
                    −
                </button>
                <div className="flex-1 h-10 flex items-center justify-center bg-white/[0.02] border-y border-white/10 text-sm font-mono text-zinc-200 tabular-nums font-medium">
                    {saving === configKey ? "···" : display}
                </div>
                <button
                    onClick={() => onUpdate(configKey, String(Math.min(max, value + step)))}
                    disabled={saving === configKey || value >= max}
                    className="w-10 h-10 rounded-r-xl bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-200 transition-all disabled:opacity-25 text-base"
                >
                    +
                </button>
            </div>
        </div>
    );
}

// ─── Active Bets Panel ───────────────────────────────────────────

function ActiveBetsPanel({ trades, games, onRefresh }: { trades: Trade[]; games: LiveGame[]; onRefresh: () => void }) {
    const [closing, setClosing] = useState<number | null>(null);
    const [limitMode, setLimitMode] = useState<number | null>(null);
    const [limitPrice, setLimitPrice] = useState("");
    const [closeError, setCloseError] = useState<string | null>(null);

    const activeTrades = trades.filter(
        (t) => !t.dry_run && (t.status === "placed" || t.status === "filled" || t.status === "resting"),
    );

    if (activeTrades.length === 0) return null;

    const totalCost = activeTrades.reduce((s, t) => s + t.cost_cents, 0);
    const totalProfit = activeTrades.reduce((s, t) => s + t.potential_profit_cents, 0);

    function findGame(trade: Trade): LiveGame | null {
        return games.find((g) => g.kalshi_markets.some((m) => m.ticker === trade.ticker)) || null;
    }

    function currentBid(trade: Trade, game: LiveGame | null): number | null {
        if (!game) return null;
        const market = game.kalshi_markets.find((m) => m.ticker === trade.ticker);
        return market ? market.yes_bid : null;
    }

    const closeTrade = async (tradeId: number, price?: number) => {
        setClosing(tradeId);
        setCloseError(null);
        try {
            const body: Record<string, number> = {};
            if (price) body.limit_price = price;
            const res = await fetch(`${API}/api/trades/${tradeId}/close`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "include",
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!data.ok) {
                setCloseError(data.error || "Failed to close");
            } else {
                setLimitMode(null);
                setLimitPrice("");
                onRefresh();
            }
        } catch (e) {
            setCloseError(String(e));
        } finally {
            setClosing(null);
        }
    };

    return (
        <div className="bg-[#0f0f11] border border-emerald-500/30 rounded-2xl overflow-hidden">
            <div className="px-6 py-4 border-b border-white/[0.06] flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shrink-0" />
                    <span className="text-sm font-semibold text-emerald-300">Active Positions</span>
                    <span className="text-sm font-mono text-zinc-500">{activeTrades.length}</span>
                </div>
                <div className="flex items-center gap-6">
                    <div className="text-right">
                        <div className="text-xs text-zinc-600 mb-0.5">at risk</div>
                        <div className="text-sm font-mono font-bold text-zinc-200">{cents(totalCost)}</div>
                    </div>
                    <div className="text-right">
                        <div className="text-xs text-zinc-600 mb-0.5">to win</div>
                        <div className="text-sm font-mono font-bold text-emerald-400">+{cents(totalProfit)}</div>
                    </div>
                </div>
            </div>
            {closeError && (
                <div className="px-6 py-3 bg-red-500/10 text-red-400 text-sm border-b border-red-500/20">
                    {closeError}
                </div>
            )}
            <div className="divide-y divide-white/[0.04]">
                {activeTrades.map((t) => {
                    const game = findGame(t);
                    const bid = currentBid(t, game);
                    const pnlIfSell = bid !== null ? (bid - t.yes_price) * t.count : null;
                    const isClosing = closing === t.id;
                    const isLimitMode = limitMode === t.id;
                    return (
                        <div key={t.id} className="px-6 py-4 hover:bg-white/[0.02] transition-colors group">
                            <div className="flex items-start gap-4">
                                <div className="flex-1 min-w-0">
                                    <div className="text-sm font-medium text-zinc-200 truncate mb-2">{t.title}</div>
                                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                                        <span className="font-mono text-sm text-zinc-400">{t.count}× YES @ {t.yes_price}¢</span>
                                        {bid !== null && (
                                            <span className={`font-mono text-sm font-semibold ${bid >= t.yes_price ? "text-emerald-400" : "text-red-400"}`}>
                                                bid {bid}¢
                                            </span>
                                        )}
                                        {pnlIfSell !== null && (
                                            <span className={`font-mono text-sm font-semibold ${pnlIfSell >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                                {pnlIfSell >= 0 ? "+" : ""}{cents(pnlIfSell)}
                                            </span>
                                        )}
                                        {game ? (
                                            <span className="text-sm text-zinc-500">
                                                <span className="font-mono font-bold text-amber-400">{game.home_score}–{game.away_score}</span>
                                                {" "}{formatGameTime(game)}
                                            </span>
                                        ) : (
                                            <span className="text-sm text-zinc-600 italic">settling…</span>
                                        )}
                                        <span className="text-sm text-zinc-600">{t.placed_at ? timeAgo(t.placed_at) : ""}</span>
                                    </div>
                                </div>
                                <div className="flex items-center gap-3 shrink-0">
                                    <div className="text-right hidden sm:block">
                                        <div className="text-sm font-mono font-bold text-zinc-200">{cents(t.cost_cents)}</div>
                                        <div className="text-sm font-mono font-bold text-emerald-400">+{cents(t.potential_profit_cents)}</div>
                                    </div>
                                    <div className={`flex items-center gap-2 transition-opacity ${isLimitMode ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}>
                                        {isLimitMode ? (
                                            <>
                                                <input
                                                    type="number"
                                                    placeholder="¢"
                                                    value={limitPrice}
                                                    onChange={(e) => setLimitPrice(e.target.value)}
                                                    className="w-16 h-8 bg-[#141417] border border-white/10 rounded-lg text-sm text-zinc-200 px-2 text-center font-mono focus:outline-none focus:border-indigo-500/50"
                                                />
                                                <button
                                                    onClick={() => { const p = parseInt(limitPrice); if (!isNaN(p) && p > 0) closeTrade(t.id, p); }}
                                                    disabled={isClosing}
                                                    className="h-8 px-3 text-sm font-medium rounded-lg bg-amber-500/15 text-amber-300 border border-amber-500/25 hover:bg-amber-500/25 transition-all disabled:opacity-50"
                                                >
                                                    {isClosing ? "…" : "Go"}
                                                </button>
                                                <button
                                                    onClick={() => { setLimitMode(null); setLimitPrice(""); }}
                                                    className="h-8 px-2 text-sm text-zinc-500 hover:text-zinc-300 transition-colors"
                                                >✕</button>
                                            </>
                                        ) : (
                                            <>
                                                <button
                                                    onClick={() => closeTrade(t.id)}
                                                    disabled={isClosing}
                                                    className="h-8 px-3 text-sm font-medium rounded-lg bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 transition-all disabled:opacity-50"
                                                >
                                                    {isClosing ? "…" : "Close"}
                                                </button>
                                                <button
                                                    onClick={() => { setLimitMode(t.id); setLimitPrice(bid ? String(bid) : ""); }}
                                                    className="h-8 px-3 text-sm font-medium rounded-lg bg-white/[0.04] text-zinc-400 border border-white/10 hover:bg-white/[0.08] hover:text-zinc-300 transition-all"
                                                >Limit</button>
                                            </>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

// ─── P&L Chart ───────────────────────────────────────────────────

function PnlChart({
    pnlHistory, balanceCents, portfolioCents, depositedCents,
}: {
    pnlHistory: PnlPoint[];
    balanceCents: number;
    portfolioCents: number;
    depositedCents: number;
}) {
    const [hoverIdx, setHoverIdx] = useState<number | null>(null);
    const totalNow = balanceCents + portfolioCents;

    const settledTrades = pnlHistory.filter((t) => t.pnl_cents !== null && t.pnl_cents !== undefined && t.placed_at);
    const startingBalance = depositedCents || totalNow || 1;
    const totalPnl = totalNow - startingBalance;
    const pnlPct = ((totalPnl / startingBalance) * 100).toFixed(2);

    const steps: { value: number; label: string; date: Date | null }[] = [
        { value: startingBalance, label: "Start", date: settledTrades.length > 0 ? new Date(settledTrades[0].placed_at) : null },
    ];
    let runningValue = startingBalance;
    for (const t of settledTrades) {
        runningValue += t.pnl_cents!;
        steps.push({
            value: runningValue,
            label: `${t.pnl_cents! >= 0 ? "WIN" : "LOSS"} ${t.pnl_cents! >= 0 ? "+" : ""}${(t.pnl_cents! / 100).toFixed(2)}`,
            date: new Date(t.placed_at),
        });
    }
    steps.push({ value: totalNow, label: "Now", date: new Date() });

    const values = steps.map((d) => d.value);
    if (values.length === 0) return null;
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const dataRange = rawMax - rawMin || 1;
    const padding = dataRange * 0.35;
    const yMin = rawMin - padding;
    const yMax = rawMax + padding;
    const range = yMax - yMin;

    const w = 800, h = 180;
    const padLeft = 64, padRight = 16, padTop = 12, padBottom = 20;
    const chartW = w - padLeft - padRight;
    const chartH = h - padTop - padBottom;

    const toY = (val: number) => padTop + chartH - ((val - yMin) / range) * chartH;
    const stepCount = steps.length;

    const points: { x: number; y: number; value: number; label: string }[] = [];
    for (let i = 0; i < stepCount; i++) {
        const x = padLeft + (i / (stepCount - 1)) * chartW;
        const y = toY(steps[i].value);
        if (i > 0) points.push({ x, y: toY(steps[i - 1].value), value: steps[i - 1].value, label: "" });
        points.push({ x, y, value: steps[i].value, label: steps[i].label });
    }

    const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
    const baseY = toY(startingBalance);
    const area = `${line} L${points[points.length - 1].x},${baseY} L${points[0].x},${baseY} Z`;

    const findClosest = (mouseX: number) => {
        let closest = 0, closestDist = Infinity;
        for (let i = 0; i < points.length; i++) {
            const dist = Math.abs(points[i].x - mouseX);
            if (dist < closestDist) { closestDist = dist; closest = i; }
        }
        return closest;
    };

    const hp = hoverIdx !== null ? points[hoverIdx] : null;
    const color = totalPnl >= 0 ? "#34d399" : "#f87171";

    return (
        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-6">
            <div className="flex flex-wrap items-center justify-between gap-4 mb-5">
                <div>
                    <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-1">Portfolio Value</div>
                    <div className="flex items-baseline gap-3">
                        <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">{cents(totalNow)}</span>
                        {totalPnl !== 0 && (
                            <span className={`text-sm font-bold font-mono tabular-nums px-2.5 py-1 rounded-lg ${totalPnl > 0 ? "text-emerald-400 bg-emerald-500/10" : "text-red-400 bg-red-500/10"}`}>
                                {totalPnl > 0 ? "+" : ""}{cents(totalPnl)} ({totalPnl > 0 ? "+" : ""}{pnlPct}%)
                            </span>
                        )}
                    </div>
                </div>
                <div className="flex items-center gap-6 text-sm">
                    <div className="text-right">
                        <div className="text-xs text-zinc-600 mb-1">deposited</div>
                        <div className="font-mono text-zinc-400">{cents(startingBalance)}</div>
                    </div>
                    {settledTrades.length > 0 && (
                        <div className="text-right">
                            <div className="text-xs text-zinc-600 mb-1">record</div>
                            <div className="font-mono">
                                <span className="text-emerald-400">{settledTrades.filter(t => (t.pnl_cents ?? 0) >= 0).length}W</span>
                                <span className="text-zinc-600"> / </span>
                                <span className="text-red-400">{settledTrades.filter(t => (t.pnl_cents ?? 0) < 0).length}L</span>
                            </div>
                        </div>
                    )}
                </div>
            </div>
            <svg
                viewBox={`0 0 ${w} ${h}`}
                className="w-full"
                style={{ height: "140px" }}
                onMouseMove={(e) => {
                    const rect = e.currentTarget.getBoundingClientRect();
                    setHoverIdx(findClosest(((e.clientX - rect.left) / rect.width) * w));
                }}
                onMouseLeave={() => setHoverIdx(null)}
            >
                <defs>
                    <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={color} stopOpacity="0.2" />
                        <stop offset="100%" stopColor={color} stopOpacity="0.01" />
                    </linearGradient>
                </defs>
                <text x={padLeft - 8} y={padTop + 5} fill="#3f3f46" fontSize="10" textAnchor="end" fontFamily="monospace">{cents(yMax)}</text>
                <text x={padLeft - 8} y={padTop + chartH} fill="#3f3f46" fontSize="10" textAnchor="end" fontFamily="monospace">{cents(yMin)}</text>
                <line x1={padLeft} y1={baseY} x2={w - padRight} y2={baseY} stroke="#ffffff06" strokeWidth="1" strokeDasharray="4,4" />
                <path d={area} fill="url(#pnlGrad)" />
                <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                {points.filter((p) => p.label && !["Start", "Now", ""].includes(p.label)).map((p, i) => (
                    <circle key={i} cx={p.x} cy={p.y} r="3.5" fill={p.value >= startingBalance ? "#34d399" : "#f87171"} stroke="#0f0f11" strokeWidth="2" />
                ))}
                {hoverIdx === null && points.length > 0 && (
                    <circle cx={points[points.length - 1].x} cy={points[points.length - 1].y} r="4" fill={color} className="animate-pulse" />
                )}
                {hp && (
                    <>
                        <line x1={hp.x} y1={padTop} x2={hp.x} y2={padTop + chartH} stroke="#ffffff0a" strokeWidth="1" />
                        <circle cx={hp.x} cy={hp.y} r="5" fill="#e4e4e7" stroke="#0f0f11" strokeWidth="2" />
                        <rect x={hp.x < w / 2 ? hp.x + 12 : hp.x - 128} y={Math.max(hp.y - 32, padTop)} width="116" height="38" rx="6" fill="#111113" stroke="#ffffff15" strokeWidth="0.5" />
                        <text x={hp.x < w / 2 ? hp.x + 20 : hp.x - 120} y={Math.max(hp.y - 14, padTop + 16)} fill="#e4e4e7" fontSize="13" fontWeight="700" fontFamily="monospace">
                            {cents(hp.value)}
                        </text>
                        <text x={hp.x < w / 2 ? hp.x + 20 : hp.x - 120} y={Math.max(hp.y + 4, padTop + 32)} fill={hp.value >= startingBalance ? "#34d399" : "#f87171"} fontSize="10" fontFamily="monospace">
                            {hp.label || `${hp.value >= startingBalance ? "+" : ""}${cents(hp.value - startingBalance)}`}
                        </text>
                    </>
                )}
            </svg>
        </div>
    );
}

// ─── Trade Health ────────────────────────────────────────────────

function TradeHealthPanel({ stats, onFilterTrades }: { stats: Stats; onFilterTrades: (filter: string | null) => void }) {
    const warnings: { label: string; count: number; style: string; status: string; desc: string }[] = [];

    if (stats.error_trades > 0) warnings.push({ label: "Error", count: stats.error_trades, style: "border-red-500/30 bg-red-500/[0.05] hover:bg-red-500/10 text-red-300", status: "error", desc: "FOK kills, reconciliation failures, phantom trades" });
    if (stats.pending_trades > 0) warnings.push({ label: "Pending", count: stats.pending_trades, style: "border-amber-500/30 bg-amber-500/[0.05] hover:bg-amber-500/10 text-amber-300", status: "pending", desc: "Written before Kalshi confirmation — may indicate crash" });
    if (stats.stopped_out_trades > 0) warnings.push({ label: "Stopped Out", count: stats.stopped_out_trades, style: "border-orange-500/30 bg-orange-500/[0.05] hover:bg-orange-500/10 text-orange-300", status: "stopped_out", desc: "Closed by stop-loss" });
    if (stats.manual_close_trades > 0) warnings.push({ label: "Manual Close", count: stats.manual_close_trades, style: "border-white/10 bg-white/[0.02] hover:bg-white/[0.04] text-zinc-400", status: "manual_close", desc: "Closed outside scanner — P&L untracked" });

    if (warnings.length === 0) return null;

    return (
        <div className="bg-[#0f0f11] border border-amber-500/25 rounded-2xl p-5">
            <div className="flex items-center gap-2.5 mb-4">
                <svg className="w-4 h-4 text-amber-500 shrink-0" viewBox="0 0 16 16" fill="none">
                    <path d="M8 2L14 13H2L8 2Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
                    <path d="M8 6.5v3M8 11.5v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
                <span className="text-sm font-semibold text-amber-400">Trade Health</span>
                <span className="text-sm text-zinc-600">{warnings.reduce((s, w) => s + w.count, 0)} trades need attention</span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {warnings.map((w) => (
                    <button key={w.status} onClick={() => onFilterTrades(w.status)} className={`flex items-center gap-4 p-4 rounded-xl border transition-all text-left ${w.style}`}>
                        <div className="text-2xl font-bold font-mono tabular-nums">{w.count}</div>
                        <div className="flex-1 min-w-0">
                            <div className="text-sm font-semibold mb-0.5">{w.label}</div>
                            <div className="text-xs text-zinc-500 line-clamp-1">{w.desc}</div>
                        </div>
                        <svg className="w-4 h-4 text-zinc-600 shrink-0" viewBox="0 0 16 16" fill="none">
                            <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                        </svg>
                    </button>
                ))}
            </div>
        </div>
    );
}

// ─── Live Games ──────────────────────────────────────────────────

function LiveGamesPanel({ games }: { games: LiveGame[] }) {
    const targets = games.filter((g) => g.is_target || g.has_bet);
    const watching = games.filter((g) => g.is_watching && !g.is_target && !g.has_bet);

    const sortedGames = [...games].sort((a, b) => {
        if (a.has_bet !== b.has_bet) return a.has_bet ? -1 : 1;
        if (a.is_target !== b.is_target) return a.is_target ? -1 : 1;
        if (a.is_watching !== b.is_watching) return a.is_watching ? -1 : 1;
        return b.score_diff - a.score_diff;
    });

    return (
        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl overflow-hidden">
            <div className="px-6 py-4 border-b border-white/[0.06] flex items-center gap-3">
                <span className="text-sm font-semibold text-zinc-300">Live Games</span>
                {games.length > 0 && <div className="w-2 h-2 rounded-full bg-rose-500 animate-pulse shrink-0" />}
                <span className="text-sm font-mono text-zinc-600">{games.length}</span>
                {targets.length > 0 && (
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                        {targets.length} target{targets.length !== 1 ? "s" : ""}
                    </span>
                )}
                {watching.length > 0 && (
                    <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">
                        {watching.length} close
                    </span>
                )}
            </div>

            {games.length === 0 ? (
                <div className="py-16 flex flex-col items-center gap-3 text-zinc-600">
                    <div className="w-2 h-2 rounded-full bg-zinc-700 animate-pulse" />
                    <span className="text-sm">No live games right now</span>
                </div>
            ) : (
                <div className="p-4 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
                    {sortedGames.map((g) => {
                        const homeLeads = g.home_score >= g.away_score;
                        const leadingMarket = g.kalshi_markets?.find((m) => m.team === (homeLeads ? g.home_team : g.away_team));
                        const leadMet = g.score_diff >= g.min_score_lead;
                        const periodMet = g.period >= g.final_period;

                        const borderColor = g.has_bet
                            ? "border-emerald-500/50 shadow-[0_0_20px_rgba(52,211,153,0.08)]"
                            : g.is_target ? "border-indigo-500/50"
                            : g.is_watching ? "border-amber-500/30"
                            : "border-white/[0.08]";

                        const bgColor = g.has_bet
                            ? "bg-emerald-950/10"
                            : g.is_target ? "bg-indigo-950/10"
                            : g.is_watching ? "bg-amber-950/[0.06]"
                            : "bg-white/[0.01]";

                        return (
                            <div key={g.espn_id} className={`rounded-xl p-4 border ${borderColor} ${bgColor} transition-all`}>
                                {/* Header row */}
                                <div className="flex items-center justify-between mb-3">
                                    <span className="text-xs font-bold uppercase tracking-wider text-zinc-500 bg-white/[0.05] px-2 py-0.5 rounded-md">
                                        {sportLabel(g.sport)}
                                    </span>
                                    <div className="flex items-center gap-1.5">
                                        {g.state === "in" && (
                                            <span className="flex items-center gap-1 text-xs font-bold text-rose-400">
                                                <span className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
                                                LIVE
                                            </span>
                                        )}
                                        {g.has_bet && <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">BET</span>}
                                        {g.is_target && !g.has_bet && <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">TARGET</span>}
                                        {g.is_watching && !g.is_target && !g.has_bet && <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/25">CLOSE</span>}
                                    </div>
                                </div>

                                {/* Scoreboard */}
                                <div className="space-y-1.5 mb-3">
                                    {[
                                        { team: g.away_team, score: g.away_score, isLeading: g.away_score > g.home_score },
                                        { team: g.home_team, score: g.home_score, isLeading: g.home_score >= g.away_score },
                                    ].map(({ team, score, isLeading }) => (
                                        <div key={team} className="flex items-center justify-between gap-2">
                                            <span className={`text-sm font-semibold truncate ${isLeading ? "text-zinc-100" : "text-zinc-500"}`}>{team}</span>
                                            <span className={`text-lg font-bold font-mono tabular-nums shrink-0 ${isLeading ? "text-zinc-100" : "text-zinc-600"}`}>{score}</span>
                                        </div>
                                    ))}
                                </div>

                                {/* Clock + market price */}
                                <div className="flex items-center justify-between">
                                    {g.state === "in" ? (
                                        <span className="text-xs font-mono text-zinc-500">{formatGameTime(g)}</span>
                                    ) : (
                                        <span className="text-xs text-zinc-600 italic">final</span>
                                    )}
                                    {leadingMarket && (
                                        <span className={`text-sm font-bold font-mono tabular-nums ${leadingMarket.yes_ask >= 94 ? "text-emerald-400" : "text-zinc-400"}`}>
                                            {leadingMarket.yes_ask}¢
                                        </span>
                                    )}
                                </div>

                                {/* Progress bars */}
                                {g.state === "in" && (
                                    <div className="mt-3 space-y-1.5">
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs text-zinc-600 w-10 shrink-0">Period</span>
                                            <div className="flex-1 h-1 bg-white/[0.06] rounded-full overflow-hidden">
                                                <div
                                                    className={`h-full rounded-full transition-all ${periodMet ? "bg-emerald-500" : "bg-zinc-600"}`}
                                                    style={{ width: `${Math.min(100, (g.period / g.final_period) * 100)}%` }}
                                                />
                                            </div>
                                            <span className={`text-xs font-mono tabular-nums w-8 text-right shrink-0 ${periodMet ? "text-emerald-400" : "text-zinc-600"}`}>
                                                {g.period}/{g.final_period}
                                            </span>
                                        </div>
                                        {g.min_score_lead > 0 && (
                                            <div className="flex items-center gap-2">
                                                <span className="text-xs text-zinc-600 w-10 shrink-0">Lead</span>
                                                <div className="flex-1 h-1 bg-white/[0.06] rounded-full overflow-hidden">
                                                    <div
                                                        className={`h-full rounded-full transition-all ${leadMet ? "bg-emerald-500" : g.score_diff / g.min_score_lead >= 0.75 ? "bg-amber-500" : "bg-zinc-700"}`}
                                                        style={{ width: `${Math.min(100, (g.score_diff / g.min_score_lead) * 100)}%` }}
                                                    />
                                                </div>
                                                <span className={`text-xs font-mono tabular-nums w-8 text-right shrink-0 ${leadMet ? "text-emerald-400" : "text-zinc-600"}`}>
                                                    {g.score_diff}/{g.min_score_lead}
                                                </span>
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// ─── Login ───────────────────────────────────────────────────────

function LoginForm({ onLogin }: { onLogin: () => void }) {
    const [password, setPassword] = useState("");
    const [error, setError] = useState(false);
    const [loading, setLoading] = useState(false);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setLoading(true);
        try {
            const result = await login(password);
            if (result.success) {
                onLogin();
            } else {
                setError(true);
                setPassword("");
            }
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="min-h-screen bg-[#09090b] flex items-center justify-center p-6">
            <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,_rgba(99,102,241,0.06)_0%,_transparent_65%)] pointer-events-none" />
            <div className="relative w-full max-w-sm">
                <div className="text-center mb-10">
                    <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-indigo-500/10 border border-indigo-500/20 mb-5">
                        <svg width="24" height="24" viewBox="0 0 20 20" className="text-indigo-400">
                            <path d="M3 16 Q10 3 17 16" stroke="currentColor" strokeWidth="2.5" fill="none" strokeLinecap="round" />
                        </svg>
                    </div>
                    <h1 className="text-2xl font-bold text-zinc-100 mb-2">Sweep</h1>
                    <p className="text-zinc-500 text-sm">Kalshi sports scanner</p>
                </div>
                <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-7">
                    <form onSubmit={handleSubmit} className="space-y-4">
                        <input
                            type="password"
                            value={password}
                            onChange={(e) => { setPassword(e.target.value); setError(false); }}
                            placeholder="Password"
                            className="w-full bg-[#141417] border border-white/10 rounded-xl px-4 py-3 text-zinc-200 placeholder-zinc-600 text-sm focus:outline-none focus:border-indigo-500/60 focus:ring-1 focus:ring-indigo-500/20 transition-all"
                            autoFocus
                        />
                        {error && (
                            <p className="text-red-400 text-sm flex items-center gap-2">
                                <span className="w-1.5 h-1.5 rounded-full bg-red-500 shrink-0" />
                                Incorrect password
                            </p>
                        )}
                        <button
                            type="submit"
                            disabled={loading}
                            className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-3 rounded-xl text-sm transition-colors disabled:opacity-60"
                        >
                            {loading ? "Signing in…" : "Sign In"}
                        </button>
                    </form>
                </div>
            </div>
        </div>
    );
}

// ─── Live Games Hook ─────────────────────────────────────────────

function useLiveGames(authed: boolean | null) {
    const [games, setGames] = useState<LiveGame[]>([]);
    useEffect(() => {
        if (!authed) return;
        const fetchGames = async () => {
            try {
                const res = await fetch(`${API}/api/live-games`, { credentials: "include", cache: "no-store" });
                if (res.status === 401) return;
                if (!res.ok) return;
                const data = await res.json();
                setGames(Array.isArray(data?.games) ? data.games : []);
            } catch (e) {
                console.warn("fetchGames failed:", e);
            }
        };
        fetchGames();
        const interval = setInterval(fetchGames, 7000);
        return () => clearInterval(interval);
    }, [authed]);
    return games;
}

// ─── Dashboard ───────────────────────────────────────────────────

export default function Dashboard() {
    const [authed, setAuthed] = useState<boolean | null>(null);
    const [stats, setStats] = useState<Stats | null>(null);
    const [trades, setTrades] = useState<Trade[]>([]);
    const [pnlHistory, setPnlHistory] = useState<PnlPoint[]>([]);
    const [config, setConfig] = useState<AppConfig | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [lastFetch, setLastFetch] = useState<number>(Date.now());
    const [stale, setStale] = useState(false);
    const [tradeFilter, setTradeFilter] = useState<string | null>(null);
    const games = useLiveGames(authed);

    useEffect(() => { checkAuth().then(setAuthed); }, []);

    useEffect(() => {
        const timer = setInterval(() => setStale(Date.now() - lastFetch > 30000), 5000);
        return () => clearInterval(timer);
    }, [lastFetch]);

    const fetchData = useCallback(async () => {
        try {
            const tradesUrl = tradeFilter
                ? `${API}/api/trades?limit=50&status=${tradeFilter}`
                : `${API}/api/trades?limit=50&include_errors=true`;
            const [statsRes, tradesRes, configRes, pnlRes] = await Promise.all([
                fetch(`${API}/api/stats`, { credentials: "include", cache: "no-store" }),
                fetch(tradesUrl, { credentials: "include", cache: "no-store" }),
                fetch(`${API}/api/config`, { credentials: "include", cache: "no-store" }),
                fetch(`${API}/api/trades/pnl-history`, { credentials: "include", cache: "no-store" }),
            ]);
            if (statsRes.status === 401 || tradesRes.status === 401 || configRes.status === 401) {
                setAuthed(false);
                return;
            }
            if (!statsRes.ok || !tradesRes.ok) {
                setError(`API error (${statsRes.status}/${tradesRes.status})`);
                return;
            }
            setStats(await statsRes.json() ?? null);
            const tradesData = await tradesRes.json();
            setTrades(Array.isArray(tradesData?.trades) ? tradesData.trades : []);
            if (pnlRes.ok) {
                const pnlData = await pnlRes.json();
                setPnlHistory(Array.isArray(pnlData?.trades) ? pnlData.trades : []);
            }
            if (configRes.ok) {
                setConfig(await configRes.json() ?? null);
            }
            setError(null);
            setLastFetch(Date.now());
        } catch (e) {
            console.error("fetchData error:", e);
            setError("Cannot connect to API");
        }
    }, [tradeFilter]);

    useEffect(() => {
        if (!authed) return;
        fetchData();
        const interval = setInterval(fetchData, 7000);
        return () => clearInterval(interval);
    }, [authed, fetchData]);

    if (authed === null) return <div className="min-h-screen bg-[#09090b]" />;
    if (!authed) return <LoginForm onLogin={() => setAuthed(true)} />;

    if (!stats) {
        return (
            <div className="min-h-screen bg-[#09090b] flex items-center justify-center">
                <div className="flex items-center gap-3 text-zinc-500">
                    <div className="w-5 h-5 rounded-full border-2 border-zinc-700 border-t-zinc-400 animate-spin" />
                    <span className="text-sm">{error || "Connecting…"}</span>
                </div>
            </div>
        );
    }

    const pnl = stats.realized_pnl_cents;
    const totalValue = stats.balance_cents + stats.portfolio_value_cents;
    const deposited = stats.total_deposited_cents || 0;
    const totalReturn = deposited > 0 ? ((totalValue - deposited) / deposited * 100).toFixed(2) : null;

    return (
        <div className="min-h-screen bg-[#09090b] text-zinc-100" style={{ background: "radial-gradient(ellipse 100% 40% at 50% -5%, rgba(99,102,241,0.05) 0%, #09090b 55%)" }}>

            {/* ── Header ── */}
            <header className="sticky top-0 z-50 bg-[#09090b]/90 backdrop-blur-md border-b border-white/[0.06]">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3 shrink-0">
                        <svg width="20" height="20" viewBox="0 0 20 20" className="text-indigo-400 shrink-0">
                            <path d="M3 16 Q10 3 17 16" stroke="currentColor" strokeWidth="2.5" fill="none" strokeLinecap="round" />
                        </svg>
                        <span className="font-bold text-base text-zinc-100 tracking-tight">Sweep</span>
                        {config && (
                            <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${config.trading.dry_run ? "bg-amber-500/15 text-amber-300 border-amber-500/30" : "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"}`}>
                                {config.trading.dry_run ? "DRY RUN" : "LIVE"}
                            </span>
                        )}
                    </div>

                    <div className="flex items-center gap-4 sm:gap-5 overflow-x-auto">
                        {error && (
                            <span className="text-xs text-red-400 font-medium shrink-0 hidden sm:block">{error}</span>
                        )}
                        <div className="flex items-center gap-2 shrink-0">
                            <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${stale ? "bg-rose-500" : "bg-emerald-500 animate-pulse"}`} />
                            <span className={`text-xs font-mono ${stale ? "text-rose-400" : "text-zinc-600"}`}>{stale ? "STALE" : "LIVE"}</span>
                        </div>
                        <div className="w-px h-4 bg-white/10 shrink-0 hidden sm:block" />
                        <div className="hidden sm:flex items-center gap-2 shrink-0">
                            <span className="text-xs text-zinc-600 uppercase tracking-wider">Balance</span>
                            <span className="text-sm font-bold font-mono text-zinc-100 tabular-nums">{cents(stats.balance_cents)}</span>
                            {stats.portfolio_value_cents > 0 && (
                                <span className="text-xs font-mono text-zinc-500 tabular-nums">+{cents(stats.portfolio_value_cents)}</span>
                            )}
                        </div>
                        {pnl !== 0 && (
                            <>
                                <div className="w-px h-4 bg-white/10 shrink-0 hidden sm:block" />
                                <span className={`text-sm font-bold font-mono tabular-nums shrink-0 ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                    {pnl >= 0 ? "+" : ""}{cents(pnl)}
                                </span>
                            </>
                        )}
                    </div>
                </div>
            </header>

            <main className="max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-5">

                {/* ── Stats Row ── */}
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
                    {/* Balance */}
                    <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5 col-span-2 sm:col-span-1">
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-3">Balance</div>
                        <div className="text-2xl font-bold font-mono text-zinc-100 tabular-nums leading-none">{cents(stats.balance_cents)}</div>
                        {deposited > 0 && (
                            <>
                                <div className="mt-3 h-1 bg-white/[0.06] rounded-full overflow-hidden">
                                    <div className="h-full bg-indigo-500/50 rounded-full" style={{ width: `${Math.min(100, (stats.balance_cents / deposited) * 100)}%` }} />
                                </div>
                                <div className="text-xs text-zinc-600 mt-2 font-mono tabular-nums">
                                    {totalReturn !== null ? (totalReturn >= "0" ? "+" : "") + totalReturn + "% total" : `of ${cents(deposited)}`}
                                </div>
                            </>
                        )}
                    </div>

                    {/* Exposure */}
                    <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5">
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-3">Exposure</div>
                        <div className="text-2xl font-bold font-mono text-zinc-100 tabular-nums leading-none">{cents(stats.open_cost_cents || 0)}</div>
                        <div className="text-xs text-zinc-600 mt-2">
                            {stats.open_positions || 0} open · +{cents(stats.open_potential_profit_cents || 0)}
                        </div>
                    </div>

                    {/* P&L */}
                    <div className={`bg-[#0f0f11] rounded-2xl p-5 border ${pnl > 0 ? "border-emerald-500/25" : pnl < 0 ? "border-red-500/25" : "border-white/10"}`}>
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-3">Realized P&L</div>
                        <div className={`text-2xl font-bold font-mono tabular-nums leading-none ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                            {pnl >= 0 ? "+" : ""}{cents(pnl)}
                        </div>
                        <div className={`text-xs mt-2 font-mono tabular-nums ${pnl >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                            portfolio {cents(stats.portfolio_value_cents)}
                        </div>
                    </div>

                    {/* Win Rate */}
                    <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5">
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-3">Win Rate</div>
                        <div className="text-2xl font-bold font-mono text-zinc-100 tabular-nums leading-none">{stats.win_rate}%</div>
                        <div className="text-xs text-zinc-600 mt-2 font-mono tabular-nums">
                            <span className="text-emerald-500">{stats.wins}W</span>
                            <span className="text-zinc-700"> · </span>
                            <span className="text-red-500">{stats.losses}L</span>
                        </div>
                    </div>

                    {/* Trades */}
                    <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5">
                        <div className="text-xs font-semibold uppercase tracking-widest text-zinc-600 mb-3">Live Trades</div>
                        <div className="text-2xl font-bold font-mono text-zinc-100 tabular-nums leading-none">{stats.live_trades}</div>
                        <div className="text-xs text-zinc-600 mt-2">{stats.dry_run_trades} dry run · {stats.total_opportunities} opps</div>
                    </div>
                </div>

                {/* ── P&L Chart ── */}
                <PnlChart
                    pnlHistory={pnlHistory}
                    balanceCents={stats.balance_cents}
                    portfolioCents={stats.portfolio_value_cents}
                    depositedCents={deposited}
                />

                {/* ── Active Bets ── */}
                <ActiveBetsPanel trades={trades} games={games} onRefresh={fetchData} />

                {/* ── Live Games ── */}
                <LiveGamesPanel games={games} />

                {/* ── Trade Health ── */}
                <TradeHealthPanel stats={stats} onFilterTrades={setTradeFilter} />

                {/* ── Config ── */}
                {config && <ConfigPanel config={config} onUpdate={fetchData} />}

                {/* ── Trades Table ── */}
                <div className="bg-[#0f0f11] border border-white/10 rounded-2xl overflow-hidden">
                    <div className="px-6 py-4 border-b border-white/[0.06] flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3 min-w-0">
                            <span className="text-sm font-semibold text-zinc-300 shrink-0">
                                {tradeFilter ? `${tradeFilter.replace(/_/g, " ").toUpperCase()} Trades` : "Recent Trades"}
                            </span>
                            {tradeFilter && (
                                <button
                                    onClick={() => setTradeFilter(null)}
                                    className="text-xs px-2.5 py-1 rounded-lg bg-white/[0.04] text-zinc-400 border border-white/10 hover:bg-white/[0.08] hover:text-zinc-300 transition-colors flex items-center gap-1.5 shrink-0"
                                >
                                    <span>✕</span> clear filter
                                </button>
                            )}
                        </div>
                        {!tradeFilter && (
                            <button
                                onClick={() => setTradeFilter("error")}
                                className="text-xs px-2.5 py-1 rounded-lg bg-white/[0.04] text-zinc-500 border border-white/10 hover:bg-white/[0.08] hover:text-zinc-300 transition-colors shrink-0"
                            >
                                Show errors
                            </button>
                        )}
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full min-w-[600px]">
                            <thead>
                                <tr className="border-b border-white/[0.06]">
                                    <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-zinc-600 whitespace-nowrap">Time</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider text-zinc-600">Market</th>
                                    <th className="text-right px-4 py-3 text-xs font-semibold uppercase tracking-wider text-zinc-600 whitespace-nowrap">Qty · Price</th>
                                    <th className="text-right px-4 py-3 text-xs font-semibold uppercase tracking-wider text-zinc-600 whitespace-nowrap">Cost</th>
                                    <th className="text-right px-4 py-3 text-xs font-semibold uppercase tracking-wider text-zinc-600 whitespace-nowrap">P&L</th>
                                    <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-zinc-600 whitespace-nowrap">Status</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-white/[0.04]">
                                {trades.length === 0 && (
                                    <tr>
                                        <td colSpan={6} className="px-6 py-16 text-center">
                                            <div className="flex flex-col items-center gap-3 text-zinc-600">
                                                <div className="w-2 h-2 rounded-full bg-zinc-700 animate-pulse" />
                                                <span className="text-sm">No trades yet — scanner is watching</span>
                                            </div>
                                        </td>
                                    </tr>
                                )}
                                {trades.map((t) => (
                                    <tr
                                        key={t.id}
                                        className={`hover:bg-white/[0.02] transition-colors ${
                                            t.status === "settled_win" ? "border-l-2 border-l-emerald-500/60" :
                                            t.status === "settled_loss" ? "border-l-2 border-l-red-500/60" : ""
                                        }`}
                                    >
                                        <td className="px-6 py-4 text-sm font-mono text-zinc-500 tabular-nums whitespace-nowrap">
                                            {t.placed_at ? timeAgo(t.placed_at) : "—"}
                                        </td>
                                        <td className="px-4 py-4 max-w-0 w-full">
                                            <div className="text-sm text-zinc-200 font-medium truncate">{t.title}</div>
                                            <div className="text-xs font-mono text-zinc-600 mt-0.5 truncate">{t.ticker}</div>
                                            {t.error && (
                                                <div className="text-xs text-red-400/70 mt-0.5 truncate" title={t.error}>{t.error}</div>
                                            )}
                                        </td>
                                        <td className="px-4 py-4 text-right whitespace-nowrap">
                                            <span className="text-sm font-mono text-zinc-400 tabular-nums">{t.count}× {t.yes_price}¢</span>
                                        </td>
                                        <td className="px-4 py-4 text-right whitespace-nowrap">
                                            <span className="text-sm font-mono font-semibold text-zinc-200 tabular-nums">{cents(t.cost_cents)}</span>
                                        </td>
                                        <td className="px-4 py-4 text-right whitespace-nowrap">
                                            {t.pnl_cents !== null ? (
                                                <span className={`text-sm font-mono font-bold tabular-nums ${t.pnl_cents >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                                    {t.pnl_cents >= 0 ? "+" : ""}{cents(t.pnl_cents)}
                                                </span>
                                            ) : (
                                                <span className="text-zinc-700 text-sm font-mono">—</span>
                                            )}
                                        </td>
                                        <td className="px-6 py-4 text-right whitespace-nowrap">
                                            <StatusBadge trade={t} />
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>

            </main>
        </div>
    );
}
