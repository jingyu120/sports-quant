import json
import os
import datetime
import requests

# =============================================================================
# QuantSlate live data core
# -----------------------------------------------------------------------------
# Builds real_sabermetrics.json keyed by team name. For every team that plays on
# the target date we resolve:
#   - the ACTUAL probable starting pitcher and that pitcher's individual,
#     league-calibrated, regressed FIP (used as the run-prevention skill input)
#   - the team's REAL platoon hitting splits (wRC+ proxy vs LHP / vs RHP)
#   - the team's REAL trailing-3-day bullpen workload (pitch counts from boxscores)
#   - the home park's run/HR factors (static, legitimately stable data)
#
# Everything hits the public MLB Stats API (statsapi.mlb.com). Every network
# call has a fallback to a league-average value so a single missing data point
# never aborts the slate.
# =============================================================================

BASE = "https://statsapi.mlb.com/api/v1"
SESSION = requests.Session()
TIMEOUT = 15

# Target date: override with QUANTSLATE_DATE=YYYY-MM-DD, else today (local).
DATE_STR = os.environ.get("QUANTSLATE_DATE") or datetime.date.today().isoformat()
TARGET_DATE = datetime.date.fromisoformat(DATE_STR)
SEASON = TARGET_DATE.year

# League-average priors used when live data is missing or the sample is tiny.
LG_ERA_FALLBACK = 4.10
PITCHER_REGRESSION_IP = 40.0   # innings of league-average prior added to each starter
BULLPEN_BASELINE = 120          # league-average trailing-3-day bullpen pitch count

# Home-park run / HR factors. These are stable year over year and are not exposed
# by the Stats API, so they stay as a maintained static table.
PARK_FACTORS = {
    "Baltimore Orioles": {"run": 0.98, "hr": 0.95},
    "Seattle Mariners": {"run": 0.92, "hr": 0.94},
    "Boston Red Sox": {"run": 1.07, "hr": 1.05},
    "New York Yankees": {"run": 1.00, "hr": 1.08},
    "Tampa Bay Rays": {"run": 0.95, "hr": 0.90},
    "Toronto Blue Jays": {"run": 1.02, "hr": 1.03},
    "Chicago White Sox": {"run": 0.99, "hr": 1.04},
    "Cleveland Guardians": {"run": 0.97, "hr": 0.93},
    "Detroit Tigers": {"run": 0.96, "hr": 0.92},
    "Kansas City Royals": {"run": 0.98, "hr": 0.94},
    "Minnesota Twins": {"run": 1.01, "hr": 1.02},
    "Houston Astros": {"run": 1.02, "hr": 1.05},
    "Los Angeles Angels": {"run": 0.98, "hr": 0.99},
    "Athletics": {"run": 0.95, "hr": 0.91},
    "Oakland Athletics": {"run": 0.95, "hr": 0.91},
    "Texas Rangers": {"run": 1.02, "hr": 1.04},
    "Atlanta Braves": {"run": 1.03, "hr": 1.06},
    "Miami Marlins": {"run": 0.97, "hr": 0.92},
    "New York Mets": {"run": 0.99, "hr": 0.97},
    "Philadelphia Phillies": {"run": 1.01, "hr": 1.03},
    "Washington Nationals": {"run": 1.00, "hr": 0.98},
    "Chicago Cubs": {"run": 0.98, "hr": 0.96},
    "Cincinnati Reds": {"run": 1.05, "hr": 1.12},
    "Milwaukee Brewers": {"run": 1.01, "hr": 1.04},
    "Pittsburgh Pirates": {"run": 0.98, "hr": 0.95},
    "St. Louis Cardinals": {"run": 0.99, "hr": 0.96},
    "Arizona Diamondbacks": {"run": 1.02, "hr": 1.01},
    "Colorado Rockies": {"run": 1.32, "hr": 1.28},
    "Los Angeles Dodgers": {"run": 0.98, "hr": 1.02},
    "San Diego Padres": {"run": 0.96, "hr": 0.94},
    "San Francisco Giants": {"run": 0.94, "hr": 0.88},
}


def get_json(path, params=None):
    """GET a Stats API endpoint, returning parsed JSON or None on any failure."""
    try:
        url = path if path.startswith("http") else f"{BASE}/{path}"
        return SESSION.get(url, params=params, timeout=TIMEOUT).json()
    except Exception as e:
        print(f"  [warn] request failed ({path}): {e}")
        return None


def ip_to_float(ip):
    """Convert MLB innings-pitched notation ('88.2' = 88 + 2/3) to a real float."""
    try:
        whole = int(float(ip))
        frac = round((float(ip) - whole) * 10)  # .1 -> 1 out, .2 -> 2 outs
        return whole + frac / 3.0
    except (TypeError, ValueError):
        return 0.0


# -----------------------------------------------------------------------------
# 1. League calibration: derive the FIP constant from real league-wide totals so
#    each pitcher's FIP is on the same scale as ERA for this exact season.
# -----------------------------------------------------------------------------
def compute_league_fip_constant():
    data = get_json(
        "teams/stats",
        {"sportId": 1, "season": SEASON, "group": "pitching", "stats": "season"},
    )
    tot = {"hr": 0, "bb": 0, "hbp": 0, "k": 0, "ip": 0.0, "er": 0}
    if data:
        for split in data.get("stats", [{}])[0].get("splits", []):
            s = split.get("stat", {})
            tot["hr"] += int(s.get("homeRuns", 0) or 0)
            tot["bb"] += int(s.get("baseOnBalls", 0) or 0)
            tot["hbp"] += int(s.get("hitBatsmen", 0) or 0)
            tot["k"] += int(s.get("strikeOuts", 0) or 0)
            tot["ip"] += ip_to_float(s.get("inningsPitched", 0))
            tot["er"] += int(s.get("earnedRuns", 0) or 0)

    if tot["ip"] <= 0:
        print("  [warn] no league pitching totals; using fallback ERA/FIP constant")
        return LG_ERA_FALLBACK, 3.15  # league ERA, FIP constant

    lg_era = 9.0 * tot["er"] / tot["ip"]
    lg_fip_core = (13 * tot["hr"] + 3 * (tot["bb"] + tot["hbp"]) - 2 * tot["k"]) / tot["ip"]
    fip_constant = lg_era - lg_fip_core
    return round(lg_era, 3), round(fip_constant, 3)


# -----------------------------------------------------------------------------
# 2. Per-pitcher real metrics: individual FIP (league-calibrated + regressed to
#    league mean by sample size), plus handedness and rate stats.
# -----------------------------------------------------------------------------
def fetch_pitcher_profile(pitcher, lg_era, fip_constant):
    pid = pitcher["id"]
    name = pitcher["fullName"]
    data = get_json(
        f"people/{pid}",
        {"hydrate": f"stats(group=[pitching],type=[season],season={SEASON})"},
    )

    hand = "RHP"
    stat = None
    if data and data.get("people"):
        person = data["people"][0]
        code = person.get("pitchHand", {}).get("code", "R")
        hand = "LHP" if code == "L" else "RHP"
        splits = person.get("stats", [{}])[0].get("splits", []) if person.get("stats") else []
        if splits:
            stat = splits[0].get("stat")

    # No season sample yet (call-up / season just started): pure league-average pitcher.
    if not stat:
        print(f"  [info] no {SEASON} stats for {name}; using league-average pitcher profile")
        return {"name": name, "handedness": hand, "SIERA": round(lg_era, 2),
                "FIP": round(lg_era, 2), "BB9": 3.0, "K9": 8.5, "HR9": 1.2}

    ip = ip_to_float(stat.get("inningsPitched", 0))
    hr = int(stat.get("homeRuns", 0) or 0)
    bb = int(stat.get("baseOnBalls", 0) or 0)
    hbp = int(stat.get("hitBatsmen", 0) or 0)
    k = int(stat.get("strikeOuts", 0) or 0)

    if ip <= 0:
        fip_raw = lg_era
    else:
        fip_raw = (13 * hr + 3 * (bb + hbp) - 2 * k) / ip + fip_constant

    # Regress toward league mean by innings: low-sample starters get pulled to lg_era.
    fip_reg = (fip_raw * ip + lg_era * PITCHER_REGRESSION_IP) / (ip + PITCHER_REGRESSION_IP)
    fip_reg = max(2.20, min(round(fip_reg, 2), 5.80))

    k9 = round(9 * k / ip, 2) if ip > 0 else 8.5
    bb9 = round(9 * bb / ip, 2) if ip > 0 else 3.0
    hr9 = round(9 * hr / ip, 2) if ip > 0 else 1.2

    # Without batted-ball data a true SIERA cannot be computed, so we use the
    # league-calibrated regressed FIP as the fielding-independent skill input.
    return {"name": name, "handedness": hand, "SIERA": fip_reg, "FIP": fip_reg,
            "BB9": bb9, "K9": k9, "HR9": hr9}


# -----------------------------------------------------------------------------
# 3. Real team platoon splits -> wRC+ proxy (OPS relative to the league split avg).
# -----------------------------------------------------------------------------
def fetch_platoon_splits():
    data = get_json(
        "teams/stats",
        {"sportId": 1, "season": SEASON, "group": "hitting",
         "stats": "statSplits", "sitCodes": "vl,vr"},
    )
    per_team = {}  # name -> {"vl": ops, "vr": ops}
    lg = {"vl": [], "vr": []}
    if data:
        for st in data.get("stats", []):
            for sp in st.get("splits", []):
                team = sp.get("team", {}).get("name")
                code = sp.get("split", {}).get("code")  # 'vl' or 'vr'
                ops = sp.get("stat", {}).get("ops")
                if not (team and code in ("vl", "vr") and ops):
                    continue
                try:
                    ops_f = float(ops)
                except ValueError:
                    continue
                per_team.setdefault(team, {})[code] = ops_f
                lg[code].append(ops_f)

    lg_avg = {
        "vl": sum(lg["vl"]) / len(lg["vl"]) if lg["vl"] else 0.715,
        "vr": sum(lg["vr"]) / len(lg["vr"]) if lg["vr"] else 0.715,
    }
    return per_team, lg_avg


def wrc_for(team, code, per_team, lg_avg):
    ops = per_team.get(team, {}).get(code)
    if not ops or lg_avg[code] <= 0:
        return 100
    return int(round(100 * ops / lg_avg[code]))


# -----------------------------------------------------------------------------
# 4. Real trailing-3-day bullpen workload from completed-game boxscores.
# -----------------------------------------------------------------------------
def fetch_bullpen_workload():
    start = (TARGET_DATE - datetime.timedelta(days=3)).isoformat()
    end = (TARGET_DATE - datetime.timedelta(days=1)).isoformat()
    sched = get_json(
        "schedule",
        {"sportId": 1, "startDate": start, "endDate": end},
    )
    workload = {}  # team_id -> total bullpen pitches over the window
    if not sched:
        return workload

    game_pks = []
    for day in sched.get("dates", []):
        for g in day.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                game_pks.append(g["gamePk"])

    for pk in game_pks:
        box = get_json(f"game/{pk}/boxscore")
        if not box:
            continue
        for side in ("home", "away"):
            team = box.get("teams", {}).get(side, {})
            tid = team.get("team", {}).get("id")
            if tid is None:
                continue
            pen = 0
            for pid in team.get("pitchers", []):
                p = team.get("players", {}).get(f"ID{pid}", {})
                pitch = p.get("stats", {}).get("pitching", {})
                if str(pitch.get("gamesStarted", 0)) == "1":
                    continue  # exclude the starter; we only want relievers
                pen += int(pitch.get("numberOfPitches", 0) or 0)
            workload[tid] = workload.get(tid, 0) + pen
    return workload


# -----------------------------------------------------------------------------
# Main: assemble the per-team profiles for the target date's slate.
# -----------------------------------------------------------------------------
def fetch_live_mlb_data():
    print(f"Building QuantSlate data core for {DATE_STR} (season {SEASON})...")

    sched = get_json(
        "schedule",
        {"sportId": 1, "date": DATE_STR, "hydrate": "probablePitcher"},
    )
    games = sched.get("dates", [{}])[0].get("games", []) if sched and sched.get("dates") else []
    if not games:
        print("No games scheduled for the target date. Nothing to write.")
        return

    print(f"  {len(games)} games on slate. Calibrating league + fetching splits/bullpen...")
    lg_era, fip_constant = compute_league_fip_constant()
    per_team_ops, lg_split_avg = fetch_platoon_splits()
    bullpen = fetch_bullpen_workload()
    print(f"  league ERA={lg_era}, FIP constant={fip_constant}")

    payload_db = {}
    for g in games:
        for side in ("home", "away"):
            t = g["teams"][side]
            name = t["team"]["name"]
            tid = t["team"]["id"]
            if name in payload_db:
                continue  # team already resolved (e.g. doubleheader)

            probable = t.get("probablePitcher")
            if probable:
                starter = fetch_pitcher_profile(probable, lg_era, fip_constant)
            else:
                print(f"  [info] no probable pitcher listed for {name}; league-average starter")
                starter = {"name": "TBD", "handedness": "RHP", "SIERA": round(lg_era, 2),
                           "FIP": round(lg_era, 2), "BB9": 3.0, "K9": 8.5, "HR9": 1.2}

            park = PARK_FACTORS.get(name, {"run": 1.0, "hr": 1.0})
            payload_db[name] = {
                "starter": starter,
                "offense": {
                    "wRC_vs_LHP": wrc_for(name, "vl", per_team_ops, lg_split_avg),
                    "wRC_vs_RHP": wrc_for(name, "vr", per_team_ops, lg_split_avg),
                    "last_3_days_bullpen_pitches": bullpen.get(tid, BULLPEN_BASELINE),
                },
                "park_factor": {
                    "run_modifier": park["run"],
                    "hr_modifier": park["hr"],
                },
            }
            print(f"  - {name}: {starter['name']} ({starter['handedness']}) "
                  f"FIP {starter['SIERA']}, wRC L/R "
                  f"{payload_db[name]['offense']['wRC_vs_LHP']}/"
                  f"{payload_db[name]['offense']['wRC_vs_RHP']}, "
                  f"pen {payload_db[name]['offense']['last_3_days_bullpen_pitches']}")

    output_path = os.path.join(os.path.dirname(__file__), "real_sabermetrics.json")
    with open(output_path, "w") as f:
        json.dump(payload_db, f, indent=4)

    print(f"\nSuccess. Wrote {len(payload_db)} team profiles to: {output_path}")


if __name__ == "__main__":
    fetch_live_mlb_data()
