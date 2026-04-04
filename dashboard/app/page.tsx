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
        const res = await fetch(`${API}/api/check-auth`, {
            credentials: "include",
        });
        const data = await res.json();
        return data.authenticated;
    } catch {
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
        const ord =
            p === 1
                ? "1st"
                : p === 2
                    ? "2nd"
                    : p === 3
                        ? "3rd"
                        : `${p}th`;
        return `${ord} inning`;
    }
    if (sport.startsWith("soccer/")) {
        const minute = Math.floor(clockNum);
        return minute > 0 ? `${minute}'` : p === 1 ? "1st Half" : "2nd Half";
    }
    if (sport.startsWith("mma/")) {
        return `R${p} ${timeStr}`;
    }
    return `P${p} ${timeStr}`;
}

function timeAgo(iso: string): string {
    // Server sends UTC timestamps without Z suffix — append it
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

// ─── Controls Panel ──────────────────────────────────────────────

function ControlsPanel({
    config,
    onUpdate,
}: {
    config: AppConfig;
    onUpdate: () => void;
}) {
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

    return (
        <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 mb-6">
            <h2 className="text-xs uppercase tracking-wider text-zinc-500 mb-4">
                Controls
            </h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {/* Mode toggle */}
                <div>
                    <div className="text-xs text-zinc-500 mb-2">Mode</div>
                    <button
                        onClick={() =>
                            updateConfig(
                                "dry_run",
                                config.trading.dry_run ? "false" : "true",
                            )
                        }
                        disabled={saving === "dry_run"}
                        className={`w-full py-2.5 px-4 rounded-lg text-sm font-bold transition-all border ${
                            config.trading.dry_run
                                ? "bg-yellow-900/30 text-yellow-400 border-yellow-700/50 hover:bg-yellow-900/50"
                                : "bg-green-900/30 text-green-400 border-green-700/50 hover:bg-green-900/50"
                        }`}
                    >
                        {saving === "dry_run"
                            ? "..."
                            : config.trading.dry_run
                                ? "DRY RUN"
                                : "LIVE"}
                    </button>
                </div>

                {/* Min YES Price */}
                <ConfigStepper
                    label="Min YES Price"
                    value={config.trading.min_yes_price}
                    suffix="c"
                    step={1}
                    min={80}
                    max={99}
                    configKey="min_yes_price"
                    saving={saving}
                    onUpdate={updateConfig}
                />

                {/* Max Bet % */}
                <ConfigStepper
                    label="Max Bet"
                    value={config.trading.max_bet_pct || Math.round(config.trading.max_bet_cents / 100)}
                    format={(v) => `${v}%`}
                    step={1}
                    min={1}
                    max={50}
                    configKey="max_bet_pct"
                    saving={saving}
                    onUpdate={updateConfig}
                />

                {/* Max Positions */}
                <ConfigStepper
                    label="Max Positions"
                    value={config.trading.max_positions}
                    step={1}
                    min={1}
                    max={50}
                    configKey="max_positions"
                    saving={saving}
                    onUpdate={updateConfig}
                />

                {/* Stop-Loss Price */}
                <ConfigStepper
                    label="Stop-Loss"
                    value={config.trading.stop_loss_price ?? 50}
                    suffix="c"
                    step={5}
                    min={0}
                    max={80}
                    configKey="stop_loss_price"
                    saving={saving}
                    onUpdate={updateConfig}
                />
            </div>
        </div>
    );
}

function ConfigStepper({
    label,
    value,
    suffix,
    format,
    step,
    min,
    max,
    configKey,
    saving,
    onUpdate,
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
            <div className="text-xs text-zinc-500 mb-2">{label}</div>
            <div className="flex items-center gap-1">
                <button
                    onClick={() =>
                        onUpdate(
                            configKey,
                            String(Math.max(min, value - step)),
                        )
                    }
                    disabled={saving === configKey || value <= min}
                    className="w-8 h-10 rounded-l-lg bg-zinc-800 border border-zinc-700 text-zinc-400 hover:bg-zinc-700 hover:text-white transition-colors disabled:opacity-30"
                >
                    -
                </button>
                <div className="flex-1 h-10 flex items-center justify-center bg-zinc-800/50 border-y border-zinc-700 text-sm font-mono text-zinc-200">
                    {saving === configKey ? "..." : display}
                </div>
                <button
                    onClick={() =>
                        onUpdate(
                            configKey,
                            String(Math.min(max, value + step)),
                        )
                    }
                    disabled={saving === configKey || value >= max}
                    className="w-8 h-10 rounded-r-lg bg-zinc-800 border border-zinc-700 text-zinc-400 hover:bg-zinc-700 hover:text-white transition-colors disabled:opacity-30"
                >
                    +
                </button>
            </div>
        </div>
    );
}

// ─── Sports Config Panel ──────────────────────────────────────

function SportsConfigPanel({
    config,
    onUpdate,
}: {
    config: AppConfig;
    onUpdate: () => void;
}) {
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

    // Group sports by category, dedup by sport_path
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
            const min = Math.floor(s.final_minutes_seconds / 60);
            return `${min}' min`;
        }
        if (s.final_minutes_seconds) {
            const min = Math.floor(s.final_minutes_seconds / 60);
            const sec = s.final_minutes_seconds % 60;
            return sec > 0 ? `${min}:${sec.toString().padStart(2, "0")} left` : `${min}:00 left`;
        }
        return s.final_minutes_desc;
    }

    function timingSeconds(s: SportConfig): number {
        return s.final_minutes_seconds || 0;
    }

    function timingStep(s: SportConfig): number {
        if (s.clock_direction === "up") return 300; // 5 min for soccer
        return 60; // 1 min for countdown
    }

    function timingMax(s: SportConfig): number {
        if (s.clock_direction === "up") return 5400; // 90 min
        return 900; // 15 min
    }

    function timingConfigKey(s: SportConfig): string {
        return `final_seconds:${s.sport_path}`;
    }

    function leadConfigKey(s: SportConfig): string {
        return `lead:${s.sport_path}`;
    }

    return (
        <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl mb-6 overflow-hidden">
            <button
                onClick={() => setOpen(!open)}
                className="w-full px-5 py-3 flex items-center justify-between hover:bg-zinc-800/30 transition-colors"
            >
                <h2 className="text-xs uppercase tracking-wider text-zinc-500">
                    Sport Settings ({uniqueSports.length} sports)
                </h2>
                <span className="text-zinc-600 text-xs">{open ? "▲" : "▼"}</span>
            </button>
            {open && (
                <div className="px-5 pb-4">
                    {catOrder
                        .filter((cat) => categories[cat])
                        .map((cat) => (
                            <div key={cat} className="mb-4 last:mb-0">
                                <div className="text-[10px] uppercase tracking-wider text-zinc-600 mb-2 border-b border-zinc-800/50 pb-1">
                                    {cat}
                                </div>
                                <div className="space-y-2">
                                    {categories[cat].map((s) => (
                                        <div
                                            key={s.sport_path}
                                            className="flex items-center gap-3 py-1.5"
                                        >
                                            {/* Sport name */}
                                            <span className="text-xs text-zinc-400 w-28 shrink-0 truncate">
                                                {s.name === s.sport_path ? s.sport_path.split("/")[1] : s.name}
                                            </span>

                                            {/* Min Lead stepper */}
                                            <div className="flex items-center gap-1">
                                                <span className="text-[10px] text-zinc-600 w-8">Lead</span>
                                                <button
                                                    onClick={() => updateConfig(leadConfigKey(s), String(Math.max(0, s.min_score_lead - 1)))}
                                                    disabled={saving === leadConfigKey(s) || s.min_score_lead <= 0}
                                                    className="w-6 h-6 rounded-l bg-zinc-800 border border-zinc-700 text-zinc-400 hover:bg-zinc-700 text-xs disabled:opacity-30"
                                                >
                                                    -
                                                </button>
                                                <div className="w-8 h-6 flex items-center justify-center bg-zinc-800/50 border-y border-zinc-700 text-[11px] font-mono text-zinc-200">
                                                    {saving === leadConfigKey(s) ? ".." : s.min_score_lead}
                                                </div>
                                                <button
                                                    onClick={() => updateConfig(leadConfigKey(s), String(s.min_score_lead + 1))}
                                                    disabled={saving === leadConfigKey(s)}
                                                    className="w-6 h-6 rounded-r bg-zinc-800 border border-zinc-700 text-zinc-400 hover:bg-zinc-700 text-xs disabled:opacity-30"
                                                >
                                                    +
                                                </button>
                                            </div>

                                            {/* Timing stepper */}
                                            {s.clock_direction !== "none" && (
                                                <div className="flex items-center gap-1">
                                                    <span className="text-[10px] text-zinc-600 w-8">Time</span>
                                                    <button
                                                        onClick={() =>
                                                            updateConfig(
                                                                timingConfigKey(s),
                                                                String(Math.max(60, timingSeconds(s) - timingStep(s))),
                                                            )
                                                        }
                                                        disabled={saving === timingConfigKey(s) || timingSeconds(s) <= 60}
                                                        className="w-6 h-6 rounded-l bg-zinc-800 border border-zinc-700 text-zinc-400 hover:bg-zinc-700 text-xs disabled:opacity-30"
                                                    >
                                                        -
                                                    </button>
                                                    <div className="w-16 h-6 flex items-center justify-center bg-zinc-800/50 border-y border-zinc-700 text-[10px] font-mono text-zinc-200">
                                                        {saving === timingConfigKey(s) ? ".." : timingLabel(s)}
                                                    </div>
                                                    <button
                                                        onClick={() =>
                                                            updateConfig(
                                                                timingConfigKey(s),
                                                                String(Math.min(timingMax(s), timingSeconds(s) + timingStep(s))),
                                                            )
                                                        }
                                                        disabled={saving === timingConfigKey(s) || timingSeconds(s) >= timingMax(s)}
                                                        className="w-6 h-6 rounded-r bg-zinc-800 border border-zinc-700 text-zinc-400 hover:bg-zinc-700 text-xs disabled:opacity-30"
                                                    >
                                                        +
                                                    </button>
                                                </div>
                                            )}
                                            {s.clock_direction === "none" && (
                                                <span className="text-[10px] text-zinc-600 italic">
                                                    Final period only
                                                </span>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            </div>
                        ))}
                </div>
            )}
        </div>
    );
}

// ─── Active Bets Panel ──────────────────────────────────────

function ActiveBetsPanel({ trades, games, onRefresh }: { trades: Trade[]; games: LiveGame[]; onRefresh: () => void }) {
    const [closing, setClosing] = useState<number | null>(null);
    const [limitMode, setLimitMode] = useState<number | null>(null);
    const [limitPrice, setLimitPrice] = useState("");
    const [closeError, setCloseError] = useState<string | null>(null);

    const activeTrades = trades.filter(
        (t) =>
            !t.dry_run &&
            (t.status === "placed" || t.status === "filled" || t.status === "resting"),
    );

    if (activeTrades.length === 0) return null;

    const totalCost = activeTrades.reduce((s, t) => s + t.cost_cents, 0);
    const totalProfit = activeTrades.reduce(
        (s, t) => s + t.potential_profit_cents,
        0,
    );

    function findGame(trade: Trade): LiveGame | null {
        return games.find((g) =>
            g.kalshi_markets.some((m) => m.ticker === trade.ticker)
        ) || null;
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
        <div className="bg-zinc-900/80 border border-emerald-800/50 rounded-xl overflow-hidden mb-6">
            <div className="px-5 py-3 border-b border-emerald-800/30 flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                    <h2 className="text-xs uppercase tracking-wider text-emerald-400">
                        Active Bets ({activeTrades.length})
                    </h2>
                </div>
                <div className="flex items-center gap-4 text-xs text-zinc-400">
                    <span>
                        Cost: <span className="text-zinc-200 font-mono">{cents(totalCost)}</span>
                    </span>
                    <span>
                        Potential:{" "}
                        <span className="text-emerald-400 font-mono">
                            +{cents(totalProfit)}
                        </span>
                    </span>
                </div>
            </div>
            {closeError && (
                <div className="px-5 py-2 bg-red-900/20 text-red-400 text-xs border-b border-red-800/30">
                    {closeError}
                </div>
            )}
            <div className="divide-y divide-zinc-800/50">
                {activeTrades.map((t) => {
                    const game = findGame(t);
                    const bid = currentBid(t, game);
                    const pnlIfSell = bid !== null ? (bid - t.yes_price) * t.count : null;
                    const isClosing = closing === t.id;
                    const isLimitMode = limitMode === t.id;
                    return (
                        <div
                            key={t.id}
                            className="px-5 py-3 hover:bg-zinc-800/20 transition-colors"
                        >
                            <div className="flex items-center justify-between">
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2">
                                        <span className="text-sm text-zinc-200 truncate">
                                            {t.title}
                                        </span>
                                        {game ? (
                                            <span className="flex items-center gap-1.5 text-xs font-mono">
                                                <span className="text-yellow-400 font-bold">
                                                    {game.home_score} - {game.away_score}
                                                </span>
                                                <span className="text-zinc-500">
                                                    {formatGameTime(game)}
                                                </span>
                                            </span>
                                        ) : (
                                            <span className="text-xs text-zinc-600 italic">
                                                Awaiting settlement
                                            </span>
                                        )}
                                    </div>
                                    <div className="text-xs text-zinc-500 mt-0.5">
                                        {t.count}x YES @ {t.yes_price}c
                                        {bid !== null && (
                                            <span className="ml-1.5">
                                                &middot; bid:{" "}
                                                <span className={bid >= t.yes_price ? "text-emerald-400" : "text-red-400"}>
                                                    {bid}c
                                                </span>
                                            </span>
                                        )}
                                        {pnlIfSell !== null && (
                                            <span className={`ml-1.5 ${pnlIfSell >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                                &middot; {pnlIfSell >= 0 ? "+" : ""}{cents(pnlIfSell)}
                                            </span>
                                        )}
                                        <span className="ml-1.5">&middot; {t.placed_at ? timeAgo(t.placed_at) : ""}</span>
                                    </div>
                                </div>
                                <div className="flex items-center gap-2 ml-4">
                                    <div className="text-right mr-2">
                                        <div className="text-sm font-mono text-zinc-300">
                                            {cents(t.cost_cents)}
                                        </div>
                                        <div className="text-xs font-mono text-emerald-400">
                                            +{cents(t.potential_profit_cents)}
                                        </div>
                                    </div>
                                    {isLimitMode ? (
                                        <div className="flex items-center gap-1">
                                            <input
                                                type="number"
                                                placeholder="price"
                                                value={limitPrice}
                                                onChange={(e) => setLimitPrice(e.target.value)}
                                                className="w-14 h-7 bg-zinc-800 border border-zinc-600 rounded text-xs text-zinc-200 px-1.5 text-center font-mono"
                                            />
                                            <button
                                                onClick={() => {
                                                    const p = parseInt(limitPrice);
                                                    if (p > 0) closeTrade(t.id, p);
                                                }}
                                                disabled={isClosing}
                                                className="h-7 px-2 text-xs rounded bg-yellow-700/40 text-yellow-300 border border-yellow-600/50 hover:bg-yellow-700/60"
                                            >
                                                {isClosing ? "..." : "Go"}
                                            </button>
                                            <button
                                                onClick={() => { setLimitMode(null); setLimitPrice(""); }}
                                                className="h-7 px-1.5 text-xs text-zinc-500 hover:text-zinc-300"
                                            >
                                                X
                                            </button>
                                        </div>
                                    ) : (
                                        <div className="flex items-center gap-1">
                                            <button
                                                onClick={() => closeTrade(t.id)}
                                                disabled={isClosing}
                                                className="h-7 px-2.5 text-xs rounded bg-red-900/40 text-red-300 border border-red-700/50 hover:bg-red-900/60 transition-colors"
                                            >
                                                {isClosing ? "Closing..." : "Close"}
                                            </button>
                                            <button
                                                onClick={() => { setLimitMode(t.id); setLimitPrice(bid ? String(bid) : ""); }}
                                                className="h-7 px-2 text-xs rounded bg-zinc-800 text-zinc-400 border border-zinc-700 hover:bg-zinc-700 transition-colors"
                                            >
                                                Limit
                                            </button>
                                        </div>
                                    )}
                                </div>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

// ─── P&L Chart ──────────────────────────────────────────────

function PnlChart({
    trades,
    balanceCents,
    portfolioCents,
    depositedCents,
}: {
    trades: Trade[];
    balanceCents: number;
    portfolioCents: number;
    depositedCents: number;
}) {
    const [hoverIdx, setHoverIdx] = useState<number | null>(null);
    const totalNow = balanceCents + portfolioCents;

    const settledTrades = trades
        .filter((t) => !t.dry_run && t.pnl_cents !== null && t.placed_at)
        .sort(
            (a, b) =>
                new Date(a.placed_at).getTime() -
                new Date(b.placed_at).getTime(),
        );

    // Use real deposited amount as baseline
    const startingBalance = depositedCents || totalNow;
    const totalPnl = totalNow - startingBalance;

    const steps: { value: number; label: string; date: Date | null }[] = [
        {
            value: startingBalance,
            label: "Start",
            date:
                settledTrades.length > 0
                    ? new Date(settledTrades[0].placed_at)
                    : null,
        },
    ];
    let runningValue = startingBalance;
    for (const t of settledTrades) {
        runningValue += t.pnl_cents!;
        const result = t.pnl_cents! >= 0 ? "WIN" : "LOSS";
        steps.push({
            value: runningValue,
            label: `${result} ${t.pnl_cents! >= 0 ? "+" : ""}${(t.pnl_cents! / 100).toFixed(2)}`,
            date: new Date(t.placed_at),
        });
    }
    steps.push({ value: totalNow, label: "Now", date: new Date() });

    const values = steps.map((d) => d.value);
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const dataRange = rawMax - rawMin || 1;
    const padding = dataRange * 0.35;
    const yMin = rawMin - padding;
    const yMax = rawMax + padding;
    const range = yMax - yMin;

    const w = 800,
        h = 200;
    const padLeft = 60,
        padRight = 12,
        padTop = 14,
        padBottom = 24;
    const chartW = w - padLeft - padRight;
    const chartH = h - padTop - padBottom;

    const toY = (val: number) =>
        padTop + chartH - ((val - yMin) / range) * chartH;

    const stepCount = steps.length;
    const points: {
        x: number;
        y: number;
        value: number;
        label: string;
        date: Date | null;
    }[] = [];
    for (let i = 0; i < stepCount; i++) {
        const x = padLeft + (i / (stepCount - 1)) * chartW;
        const y = toY(steps[i].value);
        if (i > 0) {
            points.push({
                x,
                y: toY(steps[i - 1].value),
                value: steps[i - 1].value,
                label: "",
                date: null,
            });
        }
        points.push({
            x,
            y,
            value: steps[i].value,
            label: steps[i].label,
            date: steps[i].date,
        });
    }

    const line = points
        .map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`)
        .join(" ");
    const baseY = toY(startingBalance);
    const area = `${line} L${points[points.length - 1].x},${baseY} L${points[0].x},${baseY} Z`;

    const findClosest = (mouseX: number) => {
        let closest = 0;
        let closestDist = Infinity;
        for (let i = 0; i < points.length; i++) {
            const dist = Math.abs(points[i].x - mouseX);
            if (dist < closestDist) {
                closestDist = dist;
                closest = i;
            }
        }
        return closest;
    };

    const hp = hoverIdx !== null ? points[hoverIdx] : null;
    const color = totalPnl >= 0 ? "#4ade80" : "#f87171";

    return (
        <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 mb-6">
            <div className="flex justify-between items-center mb-3">
                <h2 className="text-xs uppercase tracking-wider text-zinc-500">
                    Account Value
                </h2>
                <div className="flex items-center gap-4 text-sm">
                    <span className="text-zinc-500 font-mono">
                        {cents(startingBalance)}
                    </span>
                    <span className="text-zinc-200 font-bold font-mono">
                        {cents(totalNow)}
                    </span>
                    {totalPnl !== 0 && (
                        <span
                            className={`font-bold font-mono px-2 py-0.5 rounded ${totalPnl > 0 ? "text-green-400 bg-green-900/30" : "text-red-400 bg-red-900/30"}`}
                        >
                            {totalPnl > 0 ? "+" : ""}
                            {cents(totalPnl)}
                        </span>
                    )}
                </div>
            </div>
            <svg
                viewBox={`0 0 ${w} ${h}`}
                className="w-full h-48"
                onMouseMove={(e) => {
                    const rect = e.currentTarget.getBoundingClientRect();
                    setHoverIdx(
                        findClosest(
                            ((e.clientX - rect.left) / rect.width) * w,
                        ),
                    );
                }}
                onMouseLeave={() => setHoverIdx(null)}
            >
                <defs>
                    <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={color} stopOpacity="0.3" />
                        <stop
                            offset="100%"
                            stopColor={color}
                            stopOpacity="0.02"
                        />
                    </linearGradient>
                </defs>
                <line
                    x1={padLeft}
                    y1={baseY}
                    x2={w - padRight}
                    y2={baseY}
                    stroke="#3f3f46"
                    strokeWidth="1"
                    strokeDasharray="4,4"
                />
                <path d={area} fill="url(#pnlGrad)" />
                <path
                    d={line}
                    fill="none"
                    stroke={color}
                    strokeWidth="2.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                />
                {points
                    .filter(
                        (p) =>
                            p.label && !["Start", "Now", ""].includes(p.label),
                    )
                    .map((p, i) => (
                        <circle
                            key={i}
                            cx={p.x}
                            cy={p.y}
                            r="4"
                            fill={
                                p.value >= startingBalance
                                    ? "#4ade80"
                                    : "#f87171"
                            }
                            stroke="#09090b"
                            strokeWidth="1"
                        />
                    ))}
                {hoverIdx === null && points.length > 0 && (
                    <circle
                        cx={points[points.length - 1].x}
                        cy={points[points.length - 1].y}
                        r="4"
                        fill={color}
                        className="animate-pulse"
                    />
                )}
                {hp && (
                    <>
                        <line
                            x1={hp.x}
                            y1={padTop}
                            x2={hp.x}
                            y2={padTop + chartH}
                            stroke="#52525b"
                            strokeWidth="1"
                            strokeDasharray="3,3"
                        />
                        <circle
                            cx={hp.x}
                            cy={hp.y}
                            r="5"
                            fill="#e4e4e7"
                            stroke="#09090b"
                            strokeWidth="1.5"
                        />
                        <rect
                            x={hp.x < w / 2 ? hp.x + 10 : hp.x - 130}
                            y={Math.max(hp.y - 30, padTop)}
                            width="120"
                            height="36"
                            rx="5"
                            fill="#18181b"
                            stroke="#3f3f46"
                            strokeWidth="0.5"
                            opacity="0.95"
                        />
                        <text
                            x={hp.x < w / 2 ? hp.x + 18 : hp.x - 122}
                            y={Math.max(hp.y - 14, padTop + 16)}
                            fill="#e4e4e7"
                            fontSize="12"
                            fontWeight="bold"
                            fontFamily="monospace"
                        >
                            {cents(hp.value)}
                        </text>
                        <text
                            x={hp.x < w / 2 ? hp.x + 18 : hp.x - 122}
                            y={Math.max(hp.y + 2, padTop + 32)}
                            fill={
                                hp.value >= startingBalance
                                    ? "#4ade80"
                                    : "#f87171"
                            }
                            fontSize="10"
                            fontFamily="monospace"
                        >
                            {hp.label ||
                                `${hp.value >= startingBalance ? "+" : ""}${cents(hp.value - startingBalance)}`}
                        </text>
                    </>
                )}
            </svg>
        </div>
    );
}

// ─── Trade Health Panel ──────────────────────────────────────

function TradeHealthPanel({
    stats,
    onFilterTrades,
}: {
    stats: Stats;
    onFilterTrades: (filter: string | null) => void;
}) {
    const warnings: { label: string; count: number; color: string; status: string; desc: string }[] = [];

    if (stats.error_trades > 0) {
        warnings.push({
            label: "Error",
            count: stats.error_trades,
            color: "red",
            status: "error",
            desc: "Trades hidden from view — may include FOK kills, reconciliation failures, or phantom trades that have real Kalshi fills",
        });
    }
    if (stats.pending_trades > 0) {
        warnings.push({
            label: "Pending",
            count: stats.pending_trades,
            color: "yellow",
            status: "pending",
            desc: "Trades written to DB before Kalshi confirmation — may indicate a crash during placement",
        });
    }
    if (stats.stopped_out_trades > 0) {
        warnings.push({
            label: "Stopped Out",
            count: stats.stopped_out_trades,
            color: "orange",
            status: "stopped_out",
            desc: "Trades closed by stop-loss",
        });
    }
    if (stats.manual_close_trades > 0) {
        warnings.push({
            label: "Manual Close",
            count: stats.manual_close_trades,
            color: "zinc",
            status: "manual_close",
            desc: "Trades closed outside the scanner — P&L may not be tracked",
        });
    }

    if (warnings.length === 0) return null;

    const colorMap: Record<string, string> = {
        red: "bg-red-900/20 border-red-800/50 text-red-400",
        yellow: "bg-yellow-900/20 border-yellow-800/50 text-yellow-400",
        orange: "bg-orange-900/20 border-orange-800/50 text-orange-400",
        zinc: "bg-zinc-800/50 border-zinc-700 text-zinc-400",
    };
    const badgeColorMap: Record<string, string> = {
        red: "bg-red-600/20 text-red-400 border-red-600/30",
        yellow: "bg-yellow-600/20 text-yellow-400 border-yellow-600/30",
        orange: "bg-orange-600/20 text-orange-400 border-orange-600/30",
        zinc: "bg-zinc-700/40 text-zinc-400 border-zinc-600/30",
    };

    return (
        <div className="bg-zinc-900/80 border border-amber-800/40 rounded-xl p-4 mb-6">
            <div className="flex items-center gap-2 mb-3">
                <span className="text-amber-500 text-sm">⚠</span>
                <h2 className="text-xs uppercase tracking-wider text-amber-500/80">
                    Trade Health
                </h2>
                <span className="text-[10px] text-zinc-600">
                    {warnings.reduce((s, w) => s + w.count, 0)} trades need attention
                </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {warnings.map((w) => (
                    <button
                        key={w.status}
                        onClick={() => onFilterTrades(w.status)}
                        className={`flex items-center gap-3 p-3 rounded-lg border transition-all hover:brightness-110 text-left ${colorMap[w.color]}`}
                    >
                        <span className={`text-xs font-bold px-2 py-0.5 rounded border ${badgeColorMap[w.color]}`}>
                            {w.count}
                        </span>
                        <div className="flex-1 min-w-0">
                            <div className="text-xs font-medium">{w.label}</div>
                            <div className="text-[10px] text-zinc-500 truncate">{w.desc}</div>
                        </div>
                        <span className="text-zinc-600 text-xs">→</span>
                    </button>
                ))}
            </div>
        </div>
    );
}

// ─── Live Games ──────────────────────────────────────────────

function gameReadiness(g: LiveGame): { score: number; label: string; color: string } {
    if (g.has_bet) return { score: 100, label: "BET PLACED", color: "green" };
    if (g.is_target) return { score: 95, label: "TARGET", color: "blue" };

    // Calculate how close this game is to our thresholds
    const periodPct = Math.min(100, (g.period / g.final_period) * 100);
    const leadPct = g.min_score_lead > 0
        ? Math.min(100, (g.score_diff / g.min_score_lead) * 100)
        : (g.score_diff > 0 ? 100 : 0);

    const avg = (periodPct + leadPct) / 2;

    if (g.is_watching) return { score: avg, label: "WATCHING", color: "yellow" };
    if (periodPct >= 50) return { score: avg, label: "APPROACHING", color: "zinc" };
    return { score: avg, label: "EARLY", color: "zinc" };
}

function ThresholdBar({ label, current, target, unit, isMet }: {
    label: string; current: number; target: number; unit: string; isMet: boolean;
}) {
    const pct = target > 0 ? Math.min(100, (current / target) * 100) : 100;
    return (
        <div className="flex items-center gap-2">
            <span className="text-[10px] text-zinc-600 w-10 shrink-0">{label}</span>
            <div className="flex-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                <div
                    className={`h-full rounded-full transition-all ${
                        isMet ? "bg-green-500" : pct >= 75 ? "bg-yellow-500" : "bg-zinc-600"
                    }`}
                    style={{ width: `${pct}%` }}
                />
            </div>
            <span className={`text-[10px] font-mono w-16 text-right shrink-0 ${
                isMet ? "text-green-400" : "text-zinc-500"
            }`}>
                {current}{unit}/{target}{unit}
            </span>
        </div>
    );
}

function LiveGamesPanel({ games }: { games: LiveGame[] }) {
    if (games.length === 0) {
        return (
            <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 mb-6">
                <h2 className="text-xs uppercase tracking-wider text-zinc-500 mb-3">
                    Live Games
                </h2>
                <p className="text-zinc-600 text-sm">
                    No live games right now.
                </p>
            </div>
        );
    }

    const targets = games.filter(g => g.is_target || g.has_bet);
    const watching = games.filter(g => g.is_watching && !g.is_target && !g.has_bet);
    const early = games.filter(g => !g.is_watching && !g.is_target && !g.has_bet);

    return (
        <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 mb-6">
            <div className="flex items-center gap-3 mb-4">
                <h2 className="text-xs uppercase tracking-wider text-zinc-500">
                    Live Games
                </h2>
                <div className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
                <span className="text-xs text-zinc-600">
                    {games.length} active
                </span>
                {targets.length > 0 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-600/20 text-blue-400 border border-blue-600/30">
                        {targets.length} target{targets.length > 1 ? "s" : ""}
                    </span>
                )}
                {watching.length > 0 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-600/20 text-yellow-400 border border-yellow-600/30">
                        {watching.length} close
                    </span>
                )}
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {[...games]
                    .sort((a, b) => {
                        const ra = gameReadiness(a);
                        const rb = gameReadiness(b);
                        return rb.score - ra.score;
                    })
                    .map((g) => {
                        const readiness = gameReadiness(g);
                        const leadingTeam =
                            g.home_score >= g.away_score
                                ? g.home_team
                                : g.away_team;
                        const trailingTeam =
                            g.home_score >= g.away_score
                                ? g.away_team
                                : g.home_team;
                        const leadingMarket = g.kalshi_markets?.find(
                            (m) => m.team === leadingTeam,
                        );
                        const trailingMarket = g.kalshi_markets?.find(
                            (m) => m.team === trailingTeam,
                        );

                        const periodMet = g.period >= g.final_period;
                        const leadMet = g.score_diff >= g.min_score_lead;
                        const isSoccer = g.sport.startsWith("soccer/");

                        return (
                            <div
                                key={g.espn_id}
                                className={`border rounded-lg p-3 transition-all ${
                                    g.has_bet
                                        ? "border-green-500/50 bg-green-950/20"
                                        : g.is_target
                                            ? "border-blue-500/50 bg-blue-950/20"
                                            : g.is_watching
                                                ? "border-yellow-500/30 bg-yellow-950/10"
                                                : "border-zinc-800 bg-zinc-900/50"
                                }`}
                            >
                                <div className="flex justify-between items-start mb-2">
                                    <span className="text-xs font-medium text-zinc-500">
                                        {sportLabel(g.sport)}
                                    </span>
                                    <div className="flex items-center gap-1.5">
                                        {g.has_bet && (
                                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-600/20 text-green-400 border border-green-600/30 font-bold animate-pulse">
                                                BET
                                            </span>
                                        )}
                                        {g.is_target && !g.has_bet && (
                                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-600/20 text-blue-400 border border-blue-600/30 font-bold">
                                                TARGET
                                            </span>
                                        )}
                                        {g.is_watching &&
                                            !g.is_target &&
                                            !g.has_bet && (
                                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-600/20 text-yellow-400 border border-yellow-600/30">
                                                    CLOSE
                                                </span>
                                            )}
                                        {g.state === "in" && (
                                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/30 text-red-400 border border-red-700/30">
                                                LIVE
                                            </span>
                                        )}
                                    </div>
                                </div>
                                <div className="flex justify-between items-center">
                                    <div className="space-y-1">
                                        <div
                                            className={`text-sm font-medium ${g.away_score > g.home_score ? "text-zinc-100" : "text-zinc-500"}`}
                                        >
                                            {g.away_team}
                                        </div>
                                        <div
                                            className={`text-sm font-medium ${g.home_score > g.away_score ? "text-zinc-100" : "text-zinc-500"}`}
                                        >
                                            {g.home_team}
                                        </div>
                                    </div>
                                    <div className="text-right space-y-1">
                                        <div className="flex items-center gap-2 justify-end">
                                            {g.away_team === leadingTeam &&
                                                leadingMarket && (
                                                    <span className="text-[10px] text-green-400 font-mono">
                                                        {leadingMarket.yes_ask}c
                                                    </span>
                                                )}
                                            {g.away_team === trailingTeam &&
                                                trailingMarket && (
                                                    <span className="text-[10px] text-zinc-600 font-mono">
                                                        {trailingMarket.yes_ask}c
                                                    </span>
                                                )}
                                            <span
                                                className={`text-sm font-bold ${g.away_score > g.home_score ? "text-zinc-100" : "text-zinc-500"}`}
                                            >
                                                {g.away_score}
                                            </span>
                                        </div>
                                        <div className="flex items-center gap-2 justify-end">
                                            {g.home_team === leadingTeam &&
                                                leadingMarket && (
                                                    <span className="text-[10px] text-green-400 font-mono">
                                                        {leadingMarket.yes_ask}c
                                                    </span>
                                                )}
                                            {g.home_team === trailingTeam &&
                                                trailingMarket && (
                                                    <span className="text-[10px] text-zinc-600 font-mono">
                                                        {trailingMarket.yes_ask}c
                                                    </span>
                                                )}
                                            <span
                                                className={`text-sm font-bold ${g.home_score > g.away_score ? "text-zinc-100" : "text-zinc-500"}`}
                                            >
                                                {g.home_score}
                                            </span>
                                        </div>
                                    </div>
                                </div>

                                {/* Game time */}
                                {g.state === "in" && (
                                    <div className="mt-2 text-xs text-zinc-500 font-medium">
                                        {formatGameTime(g)}
                                    </div>
                                )}

                                {/* Threshold progress bars */}
                                {g.state === "in" && (
                                    <div className="mt-2 space-y-1">
                                        <ThresholdBar
                                            label="Period"
                                            current={g.period}
                                            target={g.final_period}
                                            unit=""
                                            isMet={periodMet}
                                        />
                                        {g.min_score_lead > 0 && (
                                            <ThresholdBar
                                                label="Lead"
                                                current={g.score_diff}
                                                target={g.min_score_lead}
                                                unit={isSoccer ? "g" : "pt"}
                                                isMet={leadMet}
                                            />
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}
            </div>
        </div>
    );
}

// ─── Login ──────────────────────────────────────────────

function LoginForm({ onLogin }: { onLogin: () => void }) {
    const [password, setPassword] = useState("");
    const [error, setError] = useState(false);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        const result = await login(password);
        if (result.success) {
            onLogin();
        } else {
            setError(true);
            setPassword("");
        }
    };

    return (
        <div className="min-h-screen bg-zinc-950 flex items-center justify-center p-6">
            <div className="w-full max-w-sm">
                <h1 className="text-2xl font-bold text-zinc-100 mb-1 text-center">
                    Sweep
                </h1>
                <p className="text-zinc-600 text-sm text-center mb-8">
                    Kalshi Sports Scanner
                </p>
                <form onSubmit={handleSubmit}>
                    <input
                        type="password"
                        value={password}
                        onChange={(e) => {
                            setPassword(e.target.value);
                            setError(false);
                        }}
                        placeholder="Password"
                        className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-3 text-white placeholder-zinc-600 mb-3 focus:outline-none focus:border-zinc-600 transition-colors"
                        autoFocus
                    />
                    {error && (
                        <p className="text-red-400 text-sm mb-3">
                            Wrong password
                        </p>
                    )}
                    <button
                        type="submit"
                        className="w-full bg-zinc-100 text-zinc-900 font-semibold py-3 rounded-lg hover:bg-white transition-colors"
                    >
                        Enter
                    </button>
                </form>
            </div>
        </div>
    );
}

// ─── Main Dashboard ──────────────────────────────────────────────

function useLiveGames(authed: boolean | null) {
    const [games, setGames] = useState<LiveGame[]>([]);

    useEffect(() => {
        if (!authed) return;
        const fetchGames = async () => {
            try {
                const res = await fetch(`${API}/api/live-games`, {
                    credentials: "include",
                    cache: "no-store",
                });
                if (!res.ok) return;
                const data = await res.json();
                setGames(data.games);
            } catch {
                /* ignore */
            }
        };
        fetchGames();
        const interval = setInterval(fetchGames, 7000);
        return () => clearInterval(interval);
    }, [authed]);

    return games;
}

export default function Dashboard() {
    const [authed, setAuthed] = useState<boolean | null>(null);
    const [stats, setStats] = useState<Stats | null>(null);
    const [trades, setTrades] = useState<Trade[]>([]);
    const [config, setConfig] = useState<AppConfig | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [lastFetch, setLastFetch] = useState<number>(Date.now());
    const [stale, setStale] = useState(false);
    const [tradeFilter, setTradeFilter] = useState<string | null>(null);
    const games = useLiveGames(authed);

    useEffect(() => {
        checkAuth().then(setAuthed);
    }, []);

    // Staleness checker: warn if data is >30s old
    useEffect(() => {
        const timer = setInterval(() => {
            setStale(Date.now() - lastFetch > 30000);
        }, 5000);
        return () => clearInterval(timer);
    }, [lastFetch]);

    const fetchData = useCallback(async () => {
        try {
            const tradesUrl = tradeFilter
                ? `${API}/api/trades?limit=50&status=${tradeFilter}`
                : `${API}/api/trades?limit=50`;
            const [statsRes, tradesRes, configRes] = await Promise.all([
                fetch(`${API}/api/stats`, { credentials: "include", cache: "no-store" }),
                fetch(tradesUrl, { credentials: "include", cache: "no-store" }),
                fetch(`${API}/api/config`, { credentials: "include", cache: "no-store" }),
            ]);
            if (statsRes.status === 401 || tradesRes.status === 401) {
                setAuthed(false);
                return;
            }
            if (!statsRes.ok || !tradesRes.ok) {
                setError("API error — retrying...");
                return;
            }
            setStats(await statsRes.json());
            setTrades((await tradesRes.json()).trades);
            if (configRes.ok) setConfig(await configRes.json());
            setError(null);
            setLastFetch(Date.now());
        } catch {
            setError("Cannot connect to API — retrying...");
        }
    }, [tradeFilter]);

    useEffect(() => {
        if (!authed) return;
        fetchData();
        const interval = setInterval(fetchData, 7000);
        return () => clearInterval(interval);
    }, [authed, fetchData]);

    if (authed === null) return <div className="min-h-screen bg-zinc-950" />;
    if (!authed) return <LoginForm onLogin={() => setAuthed(true)} />;

    if (error && !stats) {
        return (
            <div className="min-h-screen bg-zinc-950 text-white flex items-center justify-center">
                <div className="text-zinc-500 text-sm">Loading...</div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-zinc-950 text-white p-4 md:p-6">
            <div className="max-w-6xl mx-auto">
                {error && (
                    <div className="mb-4 px-4 py-2 bg-red-900/40 border border-red-700/50 rounded-lg text-red-300 text-sm">
                        {error}
                    </div>
                )}
                {/* Header */}
                <div className="flex items-center justify-between mb-6">
                    <div>
                        <h1 className="text-2xl font-bold text-zinc-100">
                            Sweep
                        </h1>
                        <p className="text-zinc-600 text-sm">
                            Kalshi Sports Scanner
                        </p>
                    </div>
                    <div className="flex items-center gap-3">
                        {config && (
                            <span
                                className={`text-xs font-bold px-2.5 py-1 rounded-full ${
                                    config.trading.dry_run
                                        ? "bg-yellow-900/30 text-yellow-400 border border-yellow-700/50"
                                        : "bg-green-900/30 text-green-400 border border-green-700/50"
                                }`}
                            >
                                {config.trading.dry_run ? "DRY RUN" : "LIVE"}
                            </span>
                        )}
                        <div className="flex items-center gap-1.5">
                            <div className={`w-2 h-2 rounded-full ${stale ? "bg-red-500" : "bg-green-500 animate-pulse"}`} />
                            <span className={`text-xs ${stale ? "text-red-400" : "text-zinc-600"}`}>
                                {stale ? "STALE" : "5s"}
                            </span>
                        </div>
                    </div>
                </div>

                {/* Stats */}
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
                    {[
                        {
                            label: "Balance",
                            value: cents(stats.balance_cents),
                        },
                        {
                            label: "Open",
                            value: cents(stats.open_cost_cents || 0),
                            sub: `${stats.open_positions || 0} positions`,
                        },
                        {
                            label: "Win Rate",
                            value: `${stats.win_rate}%`,
                            sub: `${stats.wins}W ${stats.losses}L`,
                        },
                        {
                            label: "P&L",
                            value: cents(stats.realized_pnl_cents),
                        },
                        {
                            label: "Trades",
                            value: String(stats.live_trades),
                            sub: `${stats.dry_run_trades} dry`,
                        },
                    ].map((card) => (
                        <div
                            key={card.label}
                            className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-4"
                        >
                            <div className="text-zinc-500 text-xs mb-1">
                                {card.label}
                            </div>
                            <div className="text-xl font-bold text-zinc-100 font-mono">
                                {card.value}
                            </div>
                            {card.sub && (
                                <div className="text-zinc-600 text-xs mt-0.5">
                                    {card.sub}
                                </div>
                            )}
                        </div>
                    ))}
                </div>

                {/* Trade Health Warnings */}
                {stats && (stats.error_trades > 0 || stats.pending_trades > 0 || stats.stopped_out_trades > 0 || stats.manual_close_trades > 0) && (
                    <TradeHealthPanel stats={stats} onFilterTrades={setTradeFilter} />
                )}

                {/* P&L Chart */}
                <PnlChart
                    trades={trades}
                    balanceCents={stats.balance_cents}
                    portfolioCents={stats.portfolio_value_cents}
                    depositedCents={29600}
                />

                {/* Controls */}
                {config && (
                    <ControlsPanel config={config} onUpdate={fetchData} />
                )}

                {/* Sport Settings */}
                {config && (
                    <SportsConfigPanel config={config} onUpdate={fetchData} />
                )}

                {/* Active Bets */}
                <ActiveBetsPanel trades={trades} games={games} onRefresh={fetchData} />

                {/* Live Games */}
                <LiveGamesPanel games={games} />

                {/* Recent Trades */}
                <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl overflow-hidden">
                    <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <h2 className="text-xs uppercase tracking-wider text-zinc-500">
                                {tradeFilter ? `${tradeFilter.replace("_", " ").toUpperCase()} Trades` : "Recent Trades"}
                            </h2>
                            {tradeFilter && (
                                <button
                                    onClick={() => setTradeFilter(null)}
                                    className="text-[10px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-400 border border-zinc-700 hover:bg-zinc-700 transition-colors"
                                >
                                    ✕ Clear filter
                                </button>
                            )}
                        </div>
                        <div className="flex items-center gap-2">
                            {!tradeFilter && (
                                <button
                                    onClick={() => setTradeFilter("error")}
                                    className="text-[10px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-500 border border-zinc-700 hover:bg-zinc-700 hover:text-zinc-300 transition-colors"
                                >
                                    Show errors
                                </button>
                            )}
                        </div>
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-zinc-800 text-zinc-500 text-xs">
                                    <th className="text-left p-3">Time</th>
                                    <th className="text-left p-3">Market</th>
                                    <th className="text-right p-3">Qty</th>
                                    <th className="text-right p-3">Price</th>
                                    <th className="text-right p-3">Cost</th>
                                    <th className="text-right p-3">P&L</th>
                                    <th className="text-right p-3">Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {trades.length === 0 && (
                                    <tr>
                                        <td
                                            colSpan={7}
                                            className="p-8 text-center text-zinc-700"
                                        >
                                            No trades yet. Scanner is watching.
                                        </td>
                                    </tr>
                                )}
                                {trades.map((t) => (
                                    <tr
                                        key={t.id}
                                        className="border-b border-zinc-800/50 hover:bg-zinc-800/30 transition-colors"
                                    >
                                        <td className="p-3 text-zinc-500 text-xs">
                                            {t.placed_at
                                                ? timeAgo(t.placed_at)
                                                : "-"}
                                        </td>
                                        <td className="p-3">
                                            <div className="text-zinc-200 truncate max-w-xs text-xs">
                                                {t.title}
                                            </div>
                                            <div className="text-zinc-600 text-xs">
                                                {t.ticker}
                                            </div>
                                        </td>
                                        <td className="p-3 text-right text-zinc-300 font-mono">
                                            {t.count}
                                        </td>
                                        <td className="p-3 text-right text-zinc-300 font-mono">
                                            {t.yes_price}c
                                        </td>
                                        <td className="p-3 text-right text-zinc-300 font-mono">
                                            {cents(t.cost_cents)}
                                        </td>
                                        <td className="p-3 text-right font-mono">
                                            {t.pnl_cents !== null ? (
                                                <span
                                                    className={
                                                        t.pnl_cents >= 0
                                                            ? "text-green-400"
                                                            : "text-red-400"
                                                    }
                                                >
                                                    {t.pnl_cents >= 0
                                                        ? "+"
                                                        : ""}
                                                    {cents(t.pnl_cents)}
                                                </span>
                                            ) : (
                                                <span className="text-zinc-700">
                                                    -
                                                </span>
                                            )}
                                        </td>
                                        <td className="p-3 text-right">
                                            <span
                                                className={`px-2 py-0.5 text-xs rounded-full border ${
                                                    t.dry_run
                                                        ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                                                        : t.status === "settled_win"
                                                            ? "bg-green-900/30 text-green-400 border-green-700/50"
                                                            : t.status === "settled_loss"
                                                                ? "bg-red-900/30 text-red-400 border-red-700/50"
                                                                : t.status === "error"
                                                                    ? "bg-red-900/30 text-red-400 border-red-700/50"
                                                                    : t.status === "stopped_out"
                                                                        ? "bg-orange-900/30 text-orange-400 border-orange-700/50"
                                                                        : t.status === "stop_failed"
                                                                            ? "bg-red-900/40 text-red-300 border-red-600/50"
                                                                            : t.status === "manual_close"
                                                                                ? "bg-zinc-800 text-zinc-300 border-zinc-600"
                                                                                : t.status === "pending"
                                                                                    ? "bg-yellow-900/30 text-yellow-400 border-yellow-700/50"
                                                                                    : "bg-zinc-800 text-zinc-300 border-zinc-700"
                                                }`}
                                            >
                                                {t.dry_run
                                                    ? "DRY"
                                                    : t.status
                                                        .replace("_", " ")
                                                        .toUpperCase()}
                                            </span>
                                            {t.error && (
                                                <div className="text-[10px] text-red-400/70 mt-1 max-w-[200px] truncate" title={t.error}>
                                                    {t.error}
                                                </div>
                                            )}
                                            {t.order_id && t.status === "error" && (
                                                <div className="text-[10px] text-yellow-500/70 mt-0.5" title={`Order: ${t.order_id}`}>
                                                    has order_id ⚠
                                                </div>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    );
}
