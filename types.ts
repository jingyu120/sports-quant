export interface OddsApiGame {
    id: string;
    sport_key: string;
    sport_title: string;
    commence_time: string;
    home_team: string;
    away_team: string;
    bookmakers: Bookmaker[];
}

export interface Bookmaker {
    key: string;
    title: string;
    last_update: string;
    markets: Market[];
}

export interface Market {
    key: string; // 'h2h', 'spreads', 'totals'
    last_update: string;
    outcomes: Outcome[];
}

export interface Outcome {
    name: string;
    price: number;
    point?: number;
}

export interface SabermetricProfile {
    starter: { name: string; SIERA: number; FIP: number; BB9: number };
    offense: { wRC_vs_LHP: number; wRC_vs_RHP: number; last_3_days_bullpen_pitches: number };
    park_factor: { run_modifier: number; hr_modifier: number };
}