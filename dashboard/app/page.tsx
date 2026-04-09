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
        "soccer/usa.1": "MLS",
    };
    return map[sport] || sport;
}

// ─── Status Badge ────────────────────────────────────────────────

function StatusBadge({ trade: t }: { trade: Trade }) {
    if (t.dry_run) {
        return (
            <span className="text-[10px] font-bold px-2 py-0.5 rounded-full border bg-zinc-800/80 text-zinc-500 border-zinc-700/50 tabular-nums">
                DRY
            </span>
        );
    }
    const variants: Record<string, string> = {
        settled_win: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
        settled_loss: "bg-red-500/15 text-red-400 border-red-500/30",
        error: "bg-red-500/15 text-red-400 border-red-500/30",
        stopped_out: "bg-orange-500/15 text-orange-400 border-orange-500/30",
        stop_failed: "bg-red-500/20 text-red-300 border-red-500/40",
        manual_close: "bg-zinc-800/80 text-zinc-400 border-zinc-700/50",
        pending: "bg-amber-500/15 text-amber-400 border-amber-500/30",
        placed: "bg-indigo-500/15 text-indigo-400 border-indigo-500/30",
        filled: "bg-indigo-500/15 text-indigo-400 border-indigo-500/30",
        resting: "bg-zinc-800/80 text-zinc-400 border-zinc-700/50",
    };
    return (
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${variants[t.status] ?? "bg-zinc-800/80 text-zinc-400 border-zinc-700/50"}`}>
            {t.status.replace(/_/g, " ").toUpperCase()}
        </span>
    );
}

// ─── Config Panel (merged Controls + Sports) ─────────────────────

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

    // Deduplicate sports
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

    return (
        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl overflow-hidden">
            <button
                onClick={() => setOpen(!open)}
                className="w-full px-5 py-4 flex items-center justify-between hover:bg-white/[0.02] transition-colors"
            >
                <h2 className="text-[11px] font-medium uppercase tracking-widest text-zinc-500">
                    Configuration
                </h2>
                <div className="flex items-center gap-2.5">
                    <span className={`text-[11px] font-bold px-2 py-0.5 rounded-full border ${config.trading.dry_run ? "bg-amber-500/15 text-amber-400 border-amber-500/30" : "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"}`}>
                        {config.trading.dry_run ? "DRY RUN" : "LIVE"}
                    </span>
                    <span className="text-zinc-600 text-[11px]">{uniqueSports.length} sports</span>
                    <svg className={`w-4 h-4 text-zinc-600 transition-transform duration-200 ${open ? "rotate-180" : ""}`} viewBox="0 0 16 16" fill="none">
                        <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </div>
            </button>

            {open && (
                <div className="border-t border-white/[0.06]">
                    <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-white/[0.06]">
                        {/* Trading Parameters */}
                        <div className={`px-5 py-5 border-l-2 ${config.trading.dry_run ? "border-l-amber-500/30" : "border-l-emerald-500/30"}`}>
                            <div className="text-[11px] font-medium uppercase tracking-widest text-zinc-600 mb-4">
                                Trading Parameters
                            </div>
                            <div className="grid grid-cols-2 gap-3">
                                {/* Mode toggle */}
                                <div className="col-span-2">
                                    <div className="text-[11px] text-zinc-500 mb-1.5">Mode</div>
                                    <button
                                        onClick={() => updateConfig("dry_run", config.trading.dry_run ? "false" : "true")}
                                        disabled={saving === "dry_run"}
                                        className={`w-full py-2 px-4 rounded-xl text-[13px] font-bold transition-all border ${config.trading.dry_run ? "bg-amber-500/10 text-amber-400 border-amber-500/25 hover:bg-amber-500/20" : "bg-emerald-500/10 text-emerald-400 border-emerald-500/25 hover:bg-emerald-500/20"}`}
                                    >
                                        {saving === "dry_run" ? "..." : config.trading.dry_run ? "DRY RUN — click to go LIVE" : "LIVE — click for DRY RUN"}
                                    </button>
                                </div>

                                <ConfigStepper label="Min YES Price" value={config.trading.min_yes_price} suffix="¢" step={1} min={80} max={99} configKey="min_yes_price" saving={saving} onUpdate={updateConfig} />
                                <ConfigStepper label="Max Bet" value={config.trading.max_bet_pct || Math.round(config.trading.max_bet_cents / 100)} format={(v) => `${v}%`} step={1} min={1} max={50} configKey="max_bet_pct" saving={saving} onUpdate={updateConfig} />
                                <ConfigStepper label="Max Positions" value={config.trading.max_positions} step={1} min={1} max={50} configKey="max_positions" saving={saving} onUpdate={updateConfig} />
                                <ConfigStepper label="Stop-Loss" value={config.trading.stop_loss_price ?? 50} suffix="¢" step={5} min={0} max={80} configKey="stop_loss_price" saving={saving} onUpdate={updateConfig} />
                            </div>
                        </div>

                        {/* Sport Settings */}
                        <div className="px-5 py-5">
                            <div className="text-[11px] font-medium uppercase tracking-widest text-zinc-600 mb-4">
                                Sport Settings
                            </div>
                            <div className="space-y-4">
                                {catOrder.filter((cat) => categories[cat]).map((cat) => (
                                    <div key={cat}>
                                        <div className="text-[10px] uppercase tracking-wider text-zinc-700 mb-2 border-b border-white/[0.06] pb-1">
                                            {cat}
                                        </div>
                                        <div className="space-y-1.5">
                                            {categories[cat].map((s) => (
                                                <div key={s.sport_path} className="flex items-center gap-2 py-1 rounded-lg hover:bg-white/[0.02] px-1 transition-colors">
                                                    <span className="text-[12px] text-zinc-400 w-24 shrink-0 truncate">
                                                        {s.name === s.sport_path ? s.sport_path.split("/")[1] : s.name}
                                                    </span>
                                                    {/* Lead stepper */}
                                                    <div className="flex items-center">
                                                        <span className="text-[10px] text-zinc-600 mr-1.5">Lead</span>
                                                        <div className="flex items-center">
                                                            <button onClick={() => updateConfig(leadKey(s), String(Math.max(0, s.min_score_lead - 1)))} disabled={saving === leadKey(s) || s.min_score_lead <= 0} className="w-6 h-6 rounded-l-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-xs disabled:opacity-30 transition-colors">-</button>
                                                            <div className="w-7 h-6 flex items-center justify-center bg-white/[0.02] border-y border-white/10 text-[11px] font-mono text-zinc-200 tabular-nums">
                                                                {saving === leadKey(s) ? "·" : s.min_score_lead}
                                                            </div>
                                                            <button onClick={() => updateConfig(leadKey(s), String(s.min_score_lead + 1))} disabled={saving === leadKey(s)} className="w-6 h-6 rounded-r-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-xs disabled:opacity-30 transition-colors">+</button>
                                                        </div>
                                                    </div>
                                                    {/* Timing stepper */}
                                                    {s.clock_direction !== "none" ? (
                                                        <div className="flex items-center">
                                                            <span className="text-[10px] text-zinc-600 mr-1.5">Time</span>
                                                            <div className="flex items-center">
                                                                <button onClick={() => updateConfig(timingKey(s), String(Math.max(60, timingSeconds(s) - timingStep(s))))} disabled={saving === timingKey(s) || timingSeconds(s) <= 60} className="w-6 h-6 rounded-l-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-xs disabled:opacity-30 transition-colors">-</button>
                                                                <div className="w-14 h-6 flex items-center justify-center bg-white/[0.02] border-y border-white/10 text-[10px] font-mono text-zinc-200">
                                                                    {saving === timingKey(s) ? "·" : timingLabel(s)}
                                                                </div>
                                                                <button onClick={() => updateConfig(timingKey(s), String(Math.min(timingMax(s), timingSeconds(s) + timingStep(s))))} disabled={saving === timingKey(s) || timingSeconds(s) >= timingMax(s)} className="w-6 h-6 rounded-r-lg bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] text-xs disabled:opacity-30 transition-colors">+</button>
                                                            </div>
                                                        </div>
                                                    ) : (
                                                        <span className="text-[10px] text-zinc-700 italic">Final period only</span>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                ))}
                            </div>
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
            <div className="text-[11px] text-zinc-500 mb-1.5">{label}</div>
            <div className="flex items-center">
                <button
                    onClick={() => onUpdate(configKey, String(Math.max(min, value - step)))}
                    disabled={saving === configKey || value <= min}
                    className="w-8 h-9 rounded-l-xl bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-200 transition-all disabled:opacity-25 text-sm"
                >
                    −
                </button>
                <div className="flex-1 h-9 flex items-center justify-center bg-white/[0.02] border-y border-white/10 text-[13px] font-mono text-zinc-200 tabular-nums">
                    {saving === configKey ? "···" : display}
                </div>
                <button
                    onClick={() => onUpdate(configKey, String(Math.min(max, value + step)))}
                    disabled={saving === configKey || value >= max}
                    className="w-8 h-9 rounded-r-xl bg-white/[0.04] border border-white/10 text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-200 transition-all disabled:opacity-25 text-sm"
                >
                    +
                </button>
            </div>
        </div>
    );
}

// ─── Fix Errors Button ───────────────────────────────────────────

function FixErrorsButton({ errorCount, onFixed }: { errorCount: number; onFixed: () => void }) {
    const [state, setState] = useState<"idle" | "loading" | "done">("idle");
    const [result, setResult] = useState<{ fixed: number; skipped: number } | null>(null);

    const handleFix = async () => {
        setState("loading");
        try {
            const res = await fetch(`${API}/api/fix-error-trades`, {
                method: "POST",
                credentials: "include",
            });
            const data = await res.json();
            setResult({ fixed: data.fixed ?? 0, skipped: data.skipped ?? 0 });
            setState("done");
            setTimeout(() => { setState("idle"); setResult(null); onFixed(); }, 3000);
        } catch {
            setState("idle");
        }
    };

    if (state === "done" && result) {
        return (
            <span className="text-[11px] text-emerald-400 font-medium px-2">
                ✓ Fixed {result.fixed}, skipped {result.skipped}
            </span>
        );
    }

    return (
        <button
            onClick={handleFix}
            disabled={state === "loading"}
            className="text-[11px] px-2.5 py-1 rounded-lg border border-amber-500/25 bg-amber-500/8 text-amber-400 hover:bg-amber-500/15 transition-colors disabled:opacity-50"
        >
            {state === "loading" ? "Fixing…" : `Reconcile ${errorCount} errors`}
        </button>
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
        <div className="bg-[#0f0f11] border border-emerald-500/30 rounded-2xl overflow-hidden shadow-[0_0_0_1px_rgba(52,211,153,0.04),inset_0_0_40px_rgba(52,211,153,0.015)]">
            {/* Header */}
            <div className="px-5 py-4 border-b border-white/[0.06] flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                    <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                    <h2 className="text-[11px] font-medium uppercase tracking-widest text-emerald-400/80">
                        Active Positions
                    </h2>
                    <span className="text-[11px] font-mono text-zinc-600 tabular-nums">{activeTrades.length}</span>
                </div>
                <div className="flex items-center gap-4">
                    <div className="text-right">
                        <div className="text-[10px] text-zinc-600 uppercase tracking-wide">at risk</div>
                        <div className="text-[13px] font-mono font-bold text-zinc-300 tabular-nums">{cents(totalCost)}</div>
                    </div>
                    <div className="text-right">
                        <div className="text-[10px] text-zinc-600 uppercase tracking-wide">to win</div>
                        <div className="text-[13px] font-mono font-bold text-emerald-400 tabular-nums">+{cents(totalProfit)}</div>
                    </div>
                </div>
            </div>
            {closeError && (
                <div className="px-5 py-2.5 bg-red-500/10 text-red-400 text-[12px] border-b border-red-500/20 flex items-center gap-2">
                    <span className="text-red-500">·</span> {closeError}
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
                        <div key={t.id} className="px-5 py-3.5 hover:bg-white/[0.02] transition-colors group">
                            <div className="flex items-center gap-4">
                                <div className="flex-1 min-w-0">
                                    <div className="text-[13px] text-zinc-200 truncate mb-1">{t.title}</div>
                                    <div className="flex items-center gap-2 text-[12px]">
                                        <span className="font-mono text-zinc-500 tabular-nums">{t.count}× YES @ {t.yes_price}¢</span>
                                        {bid !== null && (
                                            <>
                                                <span className="text-zinc-700">·</span>
                                                <span className="text-zinc-600">bid</span>
                                                <span className={`font-mono tabular-nums font-medium ${bid >= t.yes_price ? "text-emerald-400" : "text-red-400"}`}>{bid}¢</span>
                                            </>
                                        )}
                                        {pnlIfSell !== null && (
                                            <>
                                                <span className="text-zinc-700">·</span>
                                                <span className={`font-mono tabular-nums font-medium ${pnlIfSell >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                                    {pnlIfSell >= 0 ? "+" : ""}{cents(pnlIfSell)}
                                                </span>
                                            </>
                                        )}
                                        {game ? (
                                            <>
                                                <span className="text-zinc-700">·</span>
                                                <span className="font-mono text-amber-400 font-bold tabular-nums">{game.home_score}–{game.away_score}</span>
                                                <span className="text-zinc-600">{formatGameTime(game)}</span>
                                            </>
                                        ) : (
                                            <>
                                                <span className="text-zinc-700">·</span>
                                                <span className="text-zinc-600 italic">settling…</span>
                                            </>
                                        )}
                                        <span className="text-zinc-700">·</span>
                                        <span className="text-zinc-600">{t.placed_at ? timeAgo(t.placed_at) : ""}</span>
                                    </div>
                                </div>
                                <div className="flex items-center gap-3 shrink-0">
                                    <div className="text-right">
                                        <div className="text-[13px] font-mono font-bold text-zinc-300 tabular-nums">{cents(t.cost_cents)}</div>
                                        <div className="text-[12px] font-bold font-mono text-emerald-400 tabular-nums">+{cents(t.potential_profit_cents)}</div>
                                    </div>
                                    <div className={`flex items-center gap-1.5 transition-opacity ${isLimitMode ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}>
                                        {isLimitMode ? (
                                            <>
                                                <input
                                                    type="number"
                                                    placeholder="¢"
                                                    value={limitPrice}
                                                    onChange={(e) => setLimitPrice(e.target.value)}
                                                    className="w-14 h-7 bg-[#141417] border border-white/10 rounded-lg text-[12px] text-zinc-200 px-2 text-center font-mono focus:outline-none focus:border-indigo-500/50"
                                                />
                                                <button
                                                    onClick={() => { const p = parseInt(limitPrice); if (!isNaN(p) && p > 0) closeTrade(t.id, p); }}
                                                    disabled={isClosing}
                                                    className="h-7 px-2.5 text-[12px] font-medium rounded-lg bg-amber-500/15 text-amber-400 border border-amber-500/25 hover:bg-amber-500/25 transition-all disabled:opacity-50"
                                                >
                                                    {isClosing ? "…" : "Go"}
                                                </button>
                                                <button
                                                    onClick={() => { setLimitMode(null); setLimitPrice(""); }}
                                                    className="h-7 px-2 text-[12px] text-zinc-500 hover:text-zinc-300 transition-colors"
                                                >
                                                    ✕
                                                </button>
                                            </>
                                        ) : (
                                            <>
                                                <button
                                                    onClick={() => closeTrade(t.id)}
                                                    disabled={isClosing}
                                                    className="h-7 px-2.5 text-[12px] font-medium rounded-lg bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 hover:border-red-500/40 transition-all disabled:opacity-50"
                                                >
                                                    {isClosing ? "…" : "Close"}
                                                </button>
                                                <button
                                                    onClick={() => { setLimitMode(t.id); setLimitPrice(bid ? String(bid) : ""); }}
                                                    className="h-7 px-2.5 text-[12px] font-medium rounded-lg bg-white/[0.04] text-zinc-400 border border-white/10 hover:bg-white/[0.08] hover:text-zinc-300 transition-all"
                                                >
                                                    Limit
                                                </button>
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

    // pnlHistory is already sorted ASC by placed_at from the API
    const settledTrades = pnlHistory.filter((t) => t.pnl_cents !== null && t.pnl_cents !== undefined && t.placed_at);

    const startingBalance = depositedCents || totalNow || 1;  // avoid 0 for division
    const totalPnl = totalNow - startingBalance;

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

    const w = 800, h = 200;
    const padLeft = 60, padRight = 12, padTop = 14, padBottom = 24;
    const chartW = w - padLeft - padRight;
    const chartH = h - padTop - padBottom;

    const toY = (val: number) => padTop + chartH - ((val - yMin) / range) * chartH;
    const stepCount = steps.length;

    const points: { x: number; y: number; value: number; label: string; date: Date | null }[] = [];
    for (let i = 0; i < stepCount; i++) {
        const x = padLeft + (i / (stepCount - 1)) * chartW;
        const y = toY(steps[i].value);
        if (i > 0) points.push({ x, y: toY(steps[i - 1].value), value: steps[i - 1].value, label: "", date: null });
        points.push({ x, y, value: steps[i].value, label: steps[i].label, date: steps[i].date });
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
        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5">
            <div className="flex justify-between items-center mb-4">
                <h2 className="text-[11px] font-medium uppercase tracking-widest text-zinc-500">Account Value</h2>
                <div className="flex items-center gap-4">
                    <span className="text-[12px] text-zinc-600 font-mono tabular-nums">from {cents(startingBalance)}</span>
                    <span className="text-[15px] font-bold font-mono text-zinc-100 tabular-nums">{cents(totalNow)}</span>
                    {totalPnl !== 0 && (
                        <span className={`text-[13px] font-bold font-mono tabular-nums px-2.5 py-1 rounded-lg ${totalPnl > 0 ? "text-emerald-400 bg-emerald-500/10" : "text-red-400 bg-red-500/10"}`}>
                            {totalPnl > 0 ? "+" : ""}{cents(totalPnl)}
                        </span>
                    )}
                    {settledTrades.length > 0 && (
                        <span className="text-[11px] font-mono text-zinc-700 tabular-nums">
                            {settledTrades.filter(t => t.pnl_cents !== null && t.pnl_cents >= 0).length}W / {settledTrades.filter(t => t.pnl_cents !== null && t.pnl_cents < 0).length}L
                        </span>
                    )}
                </div>
            </div>
            <svg
                viewBox={`0 0 ${w} ${h}`}
                className="w-full h-52"
                onMouseMove={(e) => {
                    const rect = e.currentTarget.getBoundingClientRect();
                    setHoverIdx(findClosest(((e.clientX - rect.left) / rect.width) * w));
                }}
                onMouseLeave={() => setHoverIdx(null)}
            >
                <defs>
                    <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={color} stopOpacity="0.25" />
                        <stop offset="100%" stopColor={color} stopOpacity="0.02" />
                    </linearGradient>
                </defs>
                {/* Y-axis labels */}
                <text x={padLeft - 6} y={padTop + 4} fill="#52525b" fontSize="9" textAnchor="end" fontFamily="var(--font-geist-mono), monospace">{cents(yMax)}</text>
                <text x={padLeft - 6} y={padTop + chartH} fill="#52525b" fontSize="9" textAnchor="end" fontFamily="var(--font-geist-mono), monospace">{cents(yMin)}</text>
                {/* Baseline */}
                <line x1={padLeft} y1={baseY} x2={w - padRight} y2={baseY} stroke="#ffffff08" strokeWidth="1" strokeDasharray="4,4" />
                {/* Area fill */}
                <path d={area} fill="url(#pnlGrad)" />
                {/* Line */}
                <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                {/* Trade dots */}
                {points.filter((p) => p.label && !["Start", "Now", ""].includes(p.label)).map((p, i) => (
                    <circle key={i} cx={p.x} cy={p.y} r="3.5" fill={p.value >= startingBalance ? "#34d399" : "#f87171"} stroke="#0f0f11" strokeWidth="1.5" />
                ))}
                {/* Current dot (pulsing) */}
                {hoverIdx === null && points.length > 0 && (
                    <circle cx={points[points.length - 1].x} cy={points[points.length - 1].y} r="4" fill={color} className="animate-pulse" />
                )}
                {/* Hover crosshair + tooltip */}
                {hp && (
                    <>
                        <line x1={hp.x} y1={padTop} x2={hp.x} y2={padTop + chartH} stroke="#ffffff12" strokeWidth="1" strokeDasharray="3,3" />
                        <circle cx={hp.x} cy={hp.y} r="4.5" fill="#e4e4e7" stroke="#0f0f11" strokeWidth="1.5" />
                        <rect x={hp.x < w / 2 ? hp.x + 10 : hp.x - 130} y={Math.max(hp.y - 30, padTop)} width="120" height="36" rx="6" fill="#111113" stroke="#ffffff12" strokeWidth="0.5" opacity="0.97" />
                        <text x={hp.x < w / 2 ? hp.x + 18 : hp.x - 122} y={Math.max(hp.y - 14, padTop + 16)} fill="#e4e4e7" fontSize="12" fontWeight="bold" fontFamily="var(--font-geist-mono), monospace">
                            {cents(hp.value)}
                        </text>
                        <text x={hp.x < w / 2 ? hp.x + 18 : hp.x - 122} y={Math.max(hp.y + 2, padTop + 32)} fill={hp.value >= startingBalance ? "#34d399" : "#f87171"} fontSize="10" fontFamily="var(--font-geist-mono), monospace">
                            {hp.label || `${hp.value >= startingBalance ? "+" : ""}${cents(hp.value - startingBalance)}`}
                        </text>
                    </>
                )}
            </svg>
        </div>
    );
}

// ─── Trade Health Panel ──────────────────────────────────────────

function TradeHealthPanel({ stats, onFilterTrades }: { stats: Stats; onFilterTrades: () => void }) {
    const warnings: { label: string; count: number; style: string; badgeStyle: string; status: string; desc: string }[] = [];

    if (stats.error_trades > 0) warnings.push({ label: "Error", count: stats.error_trades, style: "border-red-500/25 bg-red-500/[0.04] text-red-400", badgeStyle: "bg-red-500/15 text-red-400 border-red-500/30", status: "error", desc: "FOK kills, reconciliation failures, or phantom trades with real fills" });
    if (stats.pending_trades > 0) warnings.push({ label: "Pending", count: stats.pending_trades, style: "border-amber-500/25 bg-amber-500/[0.04] text-amber-400", badgeStyle: "bg-amber-500/15 text-amber-400 border-amber-500/30", status: "pending", desc: "Written to DB before Kalshi confirmation — may indicate a crash" });
    if (stats.stopped_out_trades > 0) warnings.push({ label: "Stopped Out", count: stats.stopped_out_trades, style: "border-orange-500/25 bg-orange-500/[0.04] text-orange-400", badgeStyle: "bg-orange-500/15 text-orange-400 border-orange-500/30", status: "stopped_out", desc: "Closed by stop-loss" });
    if (stats.manual_close_trades > 0) warnings.push({ label: "Manual Close", count: stats.manual_close_trades, style: "border-white/10 bg-white/[0.02] text-zinc-400", badgeStyle: "bg-zinc-800/80 text-zinc-400 border-zinc-700/50", status: "manual_close", desc: "Closed outside the scanner — P&L may not be tracked" });

    if (warnings.length === 0) return null;

    return (
        <div className="bg-[#0f0f11] border border-amber-500/25 rounded-2xl p-4">
            <div className="flex items-center gap-2 mb-3">
                <svg className="w-3.5 h-3.5 text-amber-500 shrink-0" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M8 1L15 14H1L8 1Z" stroke="currentColor" strokeWidth="1" fill="none" />
                    <path d="M8 6v4M8 11.5v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
                <h2 className="text-[11px] font-medium uppercase tracking-widest text-amber-500/80">Trade Health</h2>
                <span className="text-[10px] text-zinc-600">{warnings.reduce((s, w) => s + w.count, 0)} trades need attention</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {warnings.map((w) => (
                    <button key={w.status} onClick={onFilterTrades} className={`flex items-center gap-3 p-3 rounded-xl border transition-all hover:brightness-110 text-left ${w.style}`}>
                        <span className={`text-[11px] font-bold px-2 py-0.5 rounded-lg border tabular-nums ${w.badgeStyle}`}>{w.count}</span>
                        <div className="flex-1 min-w-0">
                            <div className="text-[12px] font-medium">{w.label}</div>
                            <div className="text-[10px] text-zinc-500 truncate">{w.desc}</div>
                        </div>
                        <span className="text-zinc-600 text-[11px] shrink-0">→</span>
                    </button>
                ))}
            </div>
        </div>
    );
}

// ─── Live Games ──────────────────────────────────────────────────

function gameReadiness(g: LiveGame): { score: number } {
    if (g.has_bet) return { score: 100 };
    if (g.is_target) return { score: 95 };
    const periodPct = Math.min(100, (g.period / g.final_period) * 100);
    const leadPct = g.min_score_lead > 0 ? Math.min(100, (g.score_diff / g.min_score_lead) * 100) : (g.score_diff > 0 ? 100 : 0);
    return { score: (periodPct + leadPct) / 2 };
}

function ThresholdBar({ label, current, target, unit, isMet }: { label: string; current: number; target: number; unit: string; isMet: boolean }) {
    const pct = target > 0 ? Math.min(100, (current / target) * 100) : 100;
    return (
        <div className="flex items-center gap-2">
            <span className="text-[10px] text-zinc-600 w-9 shrink-0">{label}</span>
            <div className="flex-1 h-1 bg-white/[0.06] rounded-full overflow-hidden">
                <div className={`h-full rounded-full transition-all ${isMet ? "bg-emerald-500" : pct >= 75 ? "bg-amber-500" : "bg-zinc-700"}`} style={{ width: `${pct}%` }} />
            </div>
            <span className={`text-[10px] font-mono w-16 text-right shrink-0 tabular-nums ${isMet ? "text-emerald-400" : "text-zinc-600"}`}>
                {current}{unit}/{target}{unit}
            </span>
        </div>
    );
}

function LiveGamesPanel({ games }: { games: LiveGame[] }) {
    const targets = games.filter((g) => g.is_target || g.has_bet);
    const watching = games.filter((g) => g.is_watching && !g.is_target && !g.has_bet);

    return (
        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl overflow-hidden">
            {/* Header */}
            <div className="px-5 py-4 border-b border-white/[0.06] flex items-center gap-3">
                <h2 className="text-[11px] font-medium uppercase tracking-widest text-zinc-500">Live Games</h2>
                {games.length > 0 && <div className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />}
                <span className="text-[11px] font-mono text-zinc-600">{games.length}</span>
                {targets.length > 0 && (
                    <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-lg bg-indigo-500/15 text-indigo-400 border border-indigo-500/25">
                        {targets.length} target{targets.length !== 1 ? "s" : ""}
                    </span>
                )}
                {watching.length > 0 && (
                    <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-lg bg-amber-500/15 text-amber-400 border border-amber-500/25">
                        {watching.length} close
                    </span>
                )}
            </div>

            {games.length === 0 ? (
                <div className="px-5 py-12 text-center">
                    <div className="flex flex-col items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-zinc-700 animate-pulse" />
                        <span className="text-zinc-600 text-[12px]">No live games right now</span>
                    </div>
                </div>
            ) : (
                <div className="p-4 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
                    {[...games].sort((a, b) => gameReadiness(b).score - gameReadiness(a).score).map((g) => {
                        const leadingTeam = g.home_score >= g.away_score ? g.home_team : g.away_team;
                        const trailingTeam = g.home_score >= g.away_score ? g.away_team : g.home_team;
                        const leadingMarket = g.kalshi_markets?.find((m) => m.team === leadingTeam);
                        const trailingMarket = g.kalshi_markets?.find((m) => m.team === trailingTeam);
                        const periodMet = g.period >= g.final_period;
                        const leadMet = g.score_diff >= g.min_score_lead;
                        const isSoccer = g.sport.startsWith("soccer/");

                        const cardStyle = g.has_bet
                            ? "border-emerald-500/40 bg-emerald-950/[0.12] shadow-[0_0_24px_rgba(52,211,153,0.07)]"
                            : g.is_target
                                ? "border-indigo-500/40 bg-indigo-950/[0.12]"
                                : g.is_watching
                                    ? "border-amber-500/20 bg-amber-950/[0.06]"
                                    : "border-white/[0.07] bg-white/[0.01]";

                        return (
                            <div key={g.espn_id} className={`rounded-xl p-3.5 border transition-all ${cardStyle}`}>
                                {/* Top row: sport + badges */}
                                <div className="flex items-center justify-between mb-2.5">
                                    <span className="text-[10px] font-bold uppercase tracking-wider text-zinc-600 bg-white/[0.05] px-1.5 py-0.5 rounded">
                                        {sportLabel(g.sport)}
                                    </span>
                                    <div className="flex items-center gap-1">
                                        {g.state === "in" && (
                                            <span className="flex items-center gap-1 text-[10px] font-bold text-rose-400">
                                                <span className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
                                                LIVE
                                            </span>
                                        )}
                                        {g.has_bet && <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-lg bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 ml-1">BET</span>}
                                        {g.is_target && !g.has_bet && <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-lg bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 ml-1">TARGET</span>}
                                        {g.is_watching && !g.is_target && !g.has_bet && <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-lg bg-amber-500/15 text-amber-400 border border-amber-500/25 ml-1">CLOSE</span>}
                                    </div>
                                </div>

                                {/* Scoreboard */}
                                <div className="space-y-1 mb-2.5">
                                    {[
                                        { team: g.away_team, score: g.away_score, isLeading: g.away_score > g.home_score },
                                        { team: g.home_team, score: g.home_score, isLeading: g.home_score > g.away_score },
                                    ].map(({ team, score, isLeading }) => {
                                        const isLead = team === leadingTeam;
                                        const market = isLead ? leadingMarket : trailingMarket;
                                        return (
                                            <div key={team} className="flex items-center justify-between">
                                                <div className="flex items-center gap-2 min-w-0">
                                                    <span className={`text-[13px] font-semibold truncate ${isLeading ? "text-zinc-100" : "text-zinc-500"}`}>{team}</span>
                                                    {market && <span className={`text-[10px] font-mono shrink-0 tabular-nums ${isLead ? "text-emerald-400" : "text-zinc-600"}`}>{market.yes_ask}¢</span>}
                                                </div>
                                                <span className={`text-[15px] font-bold font-mono tabular-nums ml-2 shrink-0 ${isLeading ? "text-zinc-100" : "text-zinc-500"}`}>{score}</span>
                                            </div>
                                        );
                                    })}
                                </div>

                                {/* Clock */}
                                {g.state === "in" && (
                                    <div className="text-[11px] font-mono text-zinc-500 mb-2">{formatGameTime(g)}</div>
                                )}

                                {/* Progress bars */}
                                {g.state === "in" && (
                                    <div className="space-y-1.5">
                                        <ThresholdBar label="Period" current={g.period} target={g.final_period} unit="" isMet={periodMet} />
                                        {g.min_score_lead > 0 && <ThresholdBar label="Lead" current={g.score_diff} target={g.min_score_lead} unit={isSoccer ? "g" : "pt"} isMet={leadMet} />}
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
        <div className="min-h-screen bg-[#09090b] flex items-center justify-center p-6 relative overflow-hidden">
            <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,_rgba(99,102,241,0.05)_0%,_transparent_65%)] pointer-events-none" />
            <div className="relative w-full max-w-[320px]">
                <div className="text-center mb-8">
                    <div className="inline-flex items-center justify-center w-12 h-12 rounded-2xl bg-indigo-500/10 border border-indigo-500/20 mb-4">
                        <svg width="22" height="22" viewBox="0 0 20 20" className="text-indigo-400">
                            <path d="M3 16 Q10 3 17 16" stroke="currentColor" strokeWidth="2.5" fill="none" strokeLinecap="round" />
                        </svg>
                    </div>
                    <h1 className="text-xl font-bold text-zinc-100 mb-1">Sweep</h1>
                    <p className="text-zinc-600 text-[13px]">Kalshi sports scanner</p>
                </div>
                <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-6">
                    <form onSubmit={handleSubmit} className="space-y-3">
                        <input
                            type="password"
                            value={password}
                            onChange={(e) => { setPassword(e.target.value); setError(false); }}
                            placeholder="Password"
                            className="w-full bg-[#141417] border border-white/10 rounded-xl px-4 py-2.5 text-zinc-200 placeholder-zinc-600 text-[14px] focus:outline-none focus:border-indigo-500/50 focus:ring-1 focus:ring-indigo-500/20 transition-all"
                            autoFocus
                        />
                        {error && <p className="text-red-400 text-[13px] flex items-center gap-1.5"><span>·</span> Incorrect password</p>}
                        <button
                            type="submit"
                            disabled={loading}
                            className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-2.5 rounded-xl text-[14px] transition-colors disabled:opacity-60"
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
    const [tradeFilter, setTradeFilter] = useState<"all" | "open" | "settled" | "error">("all");
    const games = useLiveGames(authed);

    useEffect(() => { checkAuth().then(setAuthed); }, []);

    useEffect(() => {
        const timer = setInterval(() => setStale(Date.now() - lastFetch > 30000), 5000);
        return () => clearInterval(timer);
    }, [lastFetch]);

    const fetchData = useCallback(async () => {
        try {
            // Always fetch all statuses including errors — filter client-side by tab
            const tradesUrl = `${API}/api/trades?limit=100&include_errors=true`;
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
                setError(`API error (stats=${statsRes.status}, trades=${tradesRes.status}) — retrying...`);
                return;
            }
            const statsData = await statsRes.json();
            setStats(statsData ?? null);
            const tradesData = await tradesRes.json();
            setTrades(Array.isArray(tradesData?.trades) ? tradesData.trades : []);
            if (pnlRes.ok) {
                const pnlData = await pnlRes.json();
                setPnlHistory(Array.isArray(pnlData?.trades) ? pnlData.trades : []);
            }
            if (configRes.ok) {
                const configData = await configRes.json();
                setConfig(configData ?? null);
            }
            setError(null);
            setLastFetch(Date.now());
        } catch (e) {
            console.error("fetchData error:", e);
            setError("Cannot connect to API — retrying...");
        }
    }, []);

    useEffect(() => {
        if (!authed) return;
        fetchData();
        const interval = setInterval(fetchData, 7000);
        return () => clearInterval(interval);
    }, [authed, fetchData]);

    // Loading / auth states
    if (authed === null) return <div className="min-h-screen bg-[#09090b]" />;
    if (!authed) return <LoginForm onLogin={() => setAuthed(true)} />;

    if (!stats) {
        return (
            <div className="min-h-screen bg-[#09090b] flex items-center justify-center">
                <div className="flex items-center gap-3 text-zinc-600">
                    <div className="w-4 h-4 rounded-full border-2 border-zinc-700 border-t-zinc-400 animate-spin" />
                    <span className="text-[13px]">{error || "Connecting…"}</span>
                </div>
            </div>
        );
    }

    const pnl = stats.realized_pnl_cents;

    return (
        <div className="min-h-screen bg-[#09090b] text-zinc-100" style={{ background: "radial-gradient(ellipse 80% 50% at 50% -10%, rgba(99,102,241,0.04) 0%, #09090b 60%)" }}>
            {/* ── Sticky Top Bar ── */}
            <header className="sticky top-0 z-50 bg-[#09090b]/90 backdrop-blur-md border-b border-white/[0.06] h-14">
                <div className="max-w-7xl mx-auto px-4 md:px-6 h-full flex items-center justify-between">
                    {/* Left: brand + mode */}
                    <div className="flex items-center gap-3">
                        <svg width="20" height="20" viewBox="0 0 20 20" className="text-indigo-400 shrink-0">
                            <path d="M3 16 Q10 3 17 16" stroke="currentColor" strokeWidth="2.5" fill="none" strokeLinecap="round" />
                        </svg>
                        <span className="font-bold text-[15px] tracking-tight text-zinc-100">Sweep</span>
                        {config && (
                            <span className={`text-[11px] font-bold px-2 py-0.5 rounded-full border tracking-wide ${config.trading.dry_run ? "bg-amber-500/15 text-amber-400 border-amber-500/30" : "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"}`}>
                                {config.trading.dry_run ? "DRY RUN" : "LIVE"}
                            </span>
                        )}
                    </div>

                    {/* Right: freshness + balance + P&L */}
                    <div className="flex items-center gap-4">
                        {error && (
                            <span className="text-[11px] text-red-400 font-medium hidden sm:block">{error}</span>
                        )}
                        <div className="flex items-center gap-1.5">
                            <div className={`w-1.5 h-1.5 rounded-full ${stale ? "bg-rose-500" : "bg-emerald-500 animate-pulse"}`} />
                            <span className={`text-[11px] font-mono ${stale ? "text-rose-400" : "text-zinc-600"}`}>
                                {stale ? "STALE" : "LIVE"}
                            </span>
                        </div>
                        <div className="hidden sm:block w-px h-4 bg-white/10" />
                        <div className="hidden sm:flex items-center gap-1.5">
                            <span className="text-[11px] text-zinc-600 uppercase tracking-wider">Balance</span>
                            <span className="text-[13px] font-bold font-mono text-zinc-100 tabular-nums">{cents(stats.balance_cents)}</span>
                            {stats.portfolio_value_cents > 0 && (
                                <span className="text-[11px] font-mono text-zinc-600 tabular-nums">+{cents(stats.portfolio_value_cents)}</span>
                            )}
                        </div>
                        {pnl !== 0 && (
                            <>
                                <div className="hidden sm:block w-px h-4 bg-white/10" />
                                <span className={`text-[13px] font-bold font-mono tabular-nums px-2.5 py-1 rounded-lg border ${pnl >= 0 ? "text-emerald-400 bg-emerald-500/10 border-emerald-500/20" : "text-red-400 bg-red-500/10 border-red-500/20"}`}>
                                    {pnl >= 0 ? "+" : ""}{cents(pnl)}
                                </span>
                            </>
                        )}
                    </div>
                </div>
            </header>

            {/* ── Main Content ── */}
            <main>
                <div className="max-w-7xl mx-auto px-4 md:px-6 py-6 space-y-5">

                    {/* ── 1. Stats Row ── */}
                    <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                        {/* Balance */}
                        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5 group transition-colors cursor-default hover:bg-[#141417]">
                            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 mb-3">Balance</div>
                            <div className="text-[22px] font-bold font-mono text-zinc-100 tabular-nums leading-none">{cents(stats.balance_cents)}</div>
                            <div className="mt-3 h-0.5 bg-white/[0.06] rounded-full overflow-hidden">
                                <div className="h-full bg-indigo-500/40 rounded-full" style={{ width: `${Math.min(100, (stats.balance_cents / (stats.total_deposited_cents || 1)) * 100).toFixed(1)}%` }} />
                            </div>
                            <div className="text-[10px] text-zinc-600 mt-1.5 font-mono tabular-nums">of {cents(stats.total_deposited_cents || 0)} deposited</div>
                        </div>

                        {/* Exposure */}
                        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5 group transition-colors cursor-default hover:bg-[#141417]">
                            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 mb-3">Exposure</div>
                            <div className="text-[22px] font-bold font-mono text-zinc-100 tabular-nums leading-none">{cents(stats.open_cost_cents || 0)}</div>
                            <div className="text-[10px] text-zinc-600 mt-1.5">{stats.open_positions || 0} open position{stats.open_positions !== 1 ? "s" : ""}</div>
                        </div>

                        {/* Realized P&L */}
                        <div className={`bg-[#0f0f11] rounded-2xl p-5 border group transition-colors cursor-default hover:bg-[#141417] ${pnl > 0 ? "border-emerald-500/25" : pnl < 0 ? "border-red-500/25" : "border-white/10"}`}>
                            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 mb-3">Realized P&L</div>
                            <div className={`text-[22px] font-bold font-mono tabular-nums leading-none ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                {pnl >= 0 ? "+" : ""}{cents(pnl)}
                            </div>
                            <div className={`text-[10px] mt-1.5 font-mono tabular-nums ${pnl >= 0 ? "text-emerald-500/60" : "text-red-500/60"}`}>
                                portfolio {cents(stats.portfolio_value_cents)}
                            </div>
                        </div>

                        {/* Win Rate */}
                        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5 group transition-colors cursor-default hover:bg-[#141417]">
                            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 mb-3">Win Rate</div>
                            <div className="text-[22px] font-bold font-mono text-zinc-100 tabular-nums leading-none">{stats.win_rate}%</div>
                            <div className="text-[10px] text-zinc-600 mt-1.5 font-mono tabular-nums">{stats.wins}W · {stats.losses}L</div>
                        </div>

                        {/* Trades */}
                        <div className="bg-[#0f0f11] border border-white/10 rounded-2xl p-5 group transition-colors cursor-default hover:bg-[#141417]">
                            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 mb-3">Trades</div>
                            <div className="text-[22px] font-bold font-mono text-zinc-100 tabular-nums leading-none">{stats.live_trades}</div>
                            <div className="text-[10px] text-zinc-600 mt-1.5">{stats.dry_run_trades} dry run</div>
                        </div>
                    </div>

                    {/* ── 2. P&L Chart ── */}
                    <PnlChart
                        pnlHistory={pnlHistory}
                        balanceCents={stats.balance_cents}
                        portfolioCents={stats.portfolio_value_cents}
                        depositedCents={stats.total_deposited_cents || 0}
                    />

                    {/* ── 3. Active Bets + Live Games (two columns on lg+) ── */}
                    <div className="grid grid-cols-1 lg:grid-cols-[5fr_7fr] gap-4 items-start">
                        <ActiveBetsPanel trades={trades} games={games} onRefresh={fetchData} />
                        <LiveGamesPanel games={games} />
                    </div>

                    {/* ── 4. Trade Health ── */}
                    <TradeHealthPanel stats={stats} onFilterTrades={() => setTradeFilter("error")} />

                    {/* ── 5. Config (collapsible) ── */}
                    {config && <ConfigPanel config={config} onUpdate={fetchData} />}

                    {/* ── 6. Recent Trades Table ── */}
                    {(() => {
                        const OPEN_STATUSES = ["placed", "filled", "pending", "resting"];
                        const SETTLED_STATUSES = ["settled_win", "settled_loss", "stopped_out", "manual_close"];
                        const openTrades = trades.filter(t => OPEN_STATUSES.includes(t.status) && !t.dry_run);
                        const settledTrades = trades.filter(t => SETTLED_STATUSES.includes(t.status) && !t.dry_run);
                        const errorTrades = trades.filter(t => t.status === "error" && !t.dry_run);
                        const allTrades = trades.filter(t => !t.dry_run);

                        const tabCounts = { all: allTrades.length, open: openTrades.length, settled: settledTrades.length, error: errorTrades.length };
                        const visibleTrades = tradeFilter === "open" ? openTrades : tradeFilter === "settled" ? settledTrades : tradeFilter === "error" ? errorTrades : allTrades;

                        const tabs: { key: typeof tradeFilter; label: string }[] = [
                            { key: "all", label: "All" },
                            { key: "open", label: "Open" },
                            { key: "settled", label: "Settled" },
                            { key: "error", label: "Errors" },
                        ];

                        return (
                            <div className="bg-[#0f0f11] border border-white/10 rounded-2xl overflow-hidden">
                                <div className="px-5 py-3.5 border-b border-white/[0.06] flex items-center justify-between gap-4">
                                    <h2 className="text-[11px] font-medium uppercase tracking-widest text-zinc-500 shrink-0">Trades</h2>
                                    <div className="flex items-center gap-2">
                                        <div className="flex items-center gap-1">
                                        {tabs.map(({ key, label }) => {
                                            const count = tabCounts[key];
                                            const isActive = tradeFilter === key;
                                            const isError = key === "error" && count > 0;
                                            return (
                                                <button
                                                    key={key}
                                                    onClick={() => setTradeFilter(key)}
                                                    className={`text-[11px] px-2.5 py-1 rounded-lg border transition-colors flex items-center gap-1.5 ${
                                                        isActive
                                                            ? isError ? "bg-red-500/10 border-red-500/25 text-red-400" : "bg-white/[0.07] border-white/15 text-zinc-200"
                                                            : "bg-transparent border-transparent text-zinc-600 hover:text-zinc-400 hover:bg-white/[0.04]"
                                                    }`}
                                                >
                                                    {label}
                                                    {count > 0 && (
                                                        <span className={`text-[10px] font-mono tabular-nums ${
                                                            isActive ? (isError ? "text-red-400/70" : "text-zinc-400") : isError ? "text-red-600" : "text-zinc-700"
                                                        }`}>{count}</span>
                                                    )}
                                                </button>
                                            );
                                        })}
                                        </div>
                                        {errorTrades.length > 0 && (
                                            <FixErrorsButton errorCount={errorTrades.length} onFixed={fetchData} />
                                        )}
                                    </div>
                                </div>
                                <div className="overflow-x-auto">
                                    <table className="w-full">
                                        <thead>
                                            <tr className="border-b border-white/[0.06]">
                                                <th className="text-left px-5 py-3 text-[11px] font-medium uppercase tracking-wider text-zinc-600">Time</th>
                                                <th className="text-left px-5 py-3 text-[11px] font-medium uppercase tracking-wider text-zinc-600">Market</th>
                                                <th className="text-right px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-zinc-600">Qty × Price</th>
                                                <th className="text-right px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-zinc-600">Cost</th>
                                                <th className="text-right px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-zinc-600">P&L</th>
                                                <th className="text-right px-5 py-3 text-[11px] font-medium uppercase tracking-wider text-zinc-600">Status</th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-white/[0.04]">
                                            {visibleTrades.length === 0 && (
                                                <tr>
                                                    <td colSpan={6} className="px-5 py-14 text-center">
                                                        <div className="flex flex-col items-center gap-2">
                                                            <div className="w-2 h-2 rounded-full bg-zinc-700 animate-pulse" />
                                                            <span className="text-zinc-600 text-[12px]">
                                                                {tradeFilter === "error" ? "No errors — all clean" : tradeFilter === "open" ? "No open positions" : tradeFilter === "settled" ? "No settled trades yet" : "No trades yet — scanner is watching"}
                                                            </span>
                                                        </div>
                                                    </td>
                                                </tr>
                                            )}
                                            {visibleTrades.map((t) => {
                                                const isOpen = OPEN_STATUSES.includes(t.status);
                                                const isWin = t.status === "settled_win";
                                                const isLoss = t.status === "settled_loss";
                                                const isError = t.status === "error";
                                                const rowClass = isWin
                                                    ? "border-l-2 border-l-emerald-500/60 hover:bg-emerald-950/10"
                                                    : isLoss
                                                        ? "border-l-2 border-l-red-500/50 hover:bg-red-950/10"
                                                        : isError
                                                            ? "border-l-2 border-l-red-500/20 opacity-50 hover:opacity-70"
                                                            : isOpen
                                                                ? "border-l-2 border-l-indigo-500/40 hover:bg-indigo-950/10"
                                                                : "hover:bg-white/[0.02]";
                                                return (
                                                    <tr key={t.id} className={`transition-all ${rowClass}`}>
                                                        <td className="px-5 py-3.5 text-[11px] font-mono text-zinc-500 tabular-nums whitespace-nowrap">
                                                            {t.placed_at ? timeAgo(t.placed_at) : "—"}
                                                        </td>
                                                        <td className="px-5 py-3.5 min-w-0">
                                                            <div className={`text-[13px] truncate max-w-[280px] font-medium ${isError ? "text-red-300/60" : "text-zinc-200"}`}>{t.title}</div>
                                                            <div className="text-[10px] font-mono text-zinc-700 mt-0.5">{t.ticker}</div>
                                                            {t.error && (
                                                                <div className="text-[10px] text-red-400/50 mt-0.5 truncate max-w-[280px]" title={t.error}>{t.error}</div>
                                                            )}
                                                        </td>
                                                        <td className="px-4 py-3.5 text-right text-[12px] font-mono text-zinc-500 tabular-nums whitespace-nowrap">
                                                            {t.count > 0 ? <>{t.count} × {t.yes_price}¢</> : "—"}
                                                        </td>
                                                        <td className="px-4 py-3.5 text-right text-[13px] font-mono tabular-nums font-medium">
                                                            <span className={isError ? "text-zinc-700" : "text-zinc-300"}>{t.cost_cents > 0 ? cents(t.cost_cents) : "—"}</span>
                                                        </td>
                                                        <td className="px-4 py-3.5 text-right">
                                                            {t.pnl_cents !== null ? (
                                                                <span className={`text-[13px] font-mono font-bold tabular-nums ${t.pnl_cents >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                                                    {t.pnl_cents >= 0 ? "+" : ""}{cents(t.pnl_cents)}
                                                                </span>
                                                            ) : isOpen && t.potential_profit_cents > 0 ? (
                                                                <span className="text-[12px] font-mono text-indigo-400/50 tabular-nums">+{cents(t.potential_profit_cents)}</span>
                                                            ) : (
                                                                <span className="text-zinc-800 text-[12px] font-mono">—</span>
                                                            )}
                                                        </td>
                                                        <td className="px-5 py-3.5 text-right">
                                                            <StatusBadge trade={t} />
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        );
                    })()}

                </div>
            </main>
        </div>
    );
}
