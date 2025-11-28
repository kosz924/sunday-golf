# NFL Pick'em Automation

This utility scrapes the ESPN NFL odds scoreboard to auto-create weekly Sunday-only pick'em selections and, if you choose, submits them to FantasyTeamsNetwork using headless Selenium.

## Setup

1. **Python environment** – Python 3.12 (or newer 3.10+) is recommended.
2. **Dependencies** – Install requirements:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   The Selenium workflow relies on a WebDriver. Either:
   - install `chromedriver`/`geckodriver` and pass its path with `--selenium-driver-path`, or
   - let `webdriver-manager` download one automatically (requires outbound network during the first run).
3. **Credentials & Odds Provider**
   - FantasyTeamsNetwork:
     ```bash
     export FTN_USER_ID="your_username"
     export FTN_PASSWORD="your_password"
     export FTN_KEY="g3cyq7dllb"   # optional but handy for comparisons
     ```
   - Odds Data (future weeks):
     - Sign up for a free API key at [The Odds API](https://the-odds-api.com/).
     - Set it locally (or pass via `--odds-api-key`):
       ```bash
       export ODDS_API_KEY="your_the_odds_api_key"
       ```
     - The script defaults to ESPN odds; switch to The Odds API with `--odds-provider the-odds-api`. This unlocks future-week spreads/totals.
      - If an individual matchup is missing from the API, save the SportsbookReview table to `data/sbr_week<week>.html`; the script will auto-detect it (or point `--sbr-fallback-dir` to another folder).
   ```bash
   export FTN_USER_ID="your_username"
   export FTN_PASSWORD="your_password"
   # optional key if your link includes k=
   export FTN_KEY="g3cyq7dllb"
   ```

## Usage

The core script is `nfl_picks.py`. Typical flow every Friday:

1. **Dry run / review**
   ```bash
   python3 nfl_picks.py 4
   ```
   - Week is positional and required (`python3 nfl_picks.py WEEK`).
   - Defaults to the 2025 regular season and ESPN BET lines.
   - The script prints ranked picks, then offers to edit any game (swap favorite/underdog, change points) and adjust the Monday total override.

2. **Automated submission**
   After the final confirmation prompt, answer `y` when asked:
   ```
   Submit picks via headless Selenium now? [y/N]: y
   ```
   or run in one shot with:
   ```bash
   python3 nfl_picks.py 4 --submit
   ```

### Common Flags

- `--season 2025` – override season year (defaults to 2025).
- `--non-interactive` – skip prompts (prints picks only; useful for cron/tests).
- `--submit` – auto-submit without prompting (requires credentials).
- `--compare-existing` – pull the site’s current selections and highlight any differences versus the computed set.
   - `--login-id`, `--login-password`, `--login-key` – supply credentials directly.
   - `--odds-provider espn|the-odds-api` – choose where the spreads come from.
   - `--odds-api-key` – pass The Odds API key inline (falls back to `ODDS_API_KEY`).
   - `--odds-bookmakers fanduel,draftkings,...` – bookmaker preference when using The Odds API.
   - `--sbr-fallback-dir data` – directory containing `sbr_week<week>.html` fallback files (optional).
- `--selenium-browser chrome|firefox` – choose browser backend (`chrome` default).
- `--selenium-driver-path /path/to/driver` – explicit WebDriver binary.
- `--selenium-no-headless` – show the browser window for debugging.
- `--selenium-pause-after` – leave the browser open after submission until you press Enter.
- `--max-points`, `--seed`, `--provider` – customize confidence ladder and odds source.
- `--debug` – enable verbose logging (helpful if ESPN/FTN endpoints change).

### Flag Combos & Workflows

| Command | What Happens |
| --- | --- |
| `python3 nfl_picks.py 4` | Scrape week 4, show picks, prompt for manual edits, then ask whether to submit. |
| `python3 nfl_picks.py 4 --compare-existing` | Same as above, plus prints a diff versus the site’s current picks (requires login info). |
| `python3 nfl_picks.py 4 --non-interactive` | Scrape and print picks only—no editing, no submission prompts. |
| `python3 nfl_picks.py 4 --submit` | Scrape, allow edits, and submit automatically afterwards (no submit prompt). |
| `python3 nfl_picks.py 4 --submit --non-interactive` | Headless batch run: compute and immediately submit using defaults (no prompts). |
| `python3 nfl_picks.py 4 --submit --compare-existing` | Show picks, show site diff, then submit once you exit the editor. |
| `python3 nfl_picks.py 4 --selenium-no-headless` | Visual run: browser window stays visible during Selenium automation. |
| `python3 nfl_picks.py 5 --odds-provider the-odds-api` | Generate future-week picks using The Odds API odds (requires key). |
| `python3 nfl_picks.py 5 --odds-provider the-odds-api --sbr-fallback-dir data` | Same as above, but reads `data/sbr_week5.html` to fill gaps if the API is missing a matchup. |

Mix and match flags depending on whether you need a quick summary, a manual tweak session, or a hands-off cron-friendly submit.

### Notes

- Thursday kickoffs are ignored automatically.
- The Monday tie-breaker uses all Monday games’ totals; halves round up (e.g., 89.5 → 90). You can override before submitting.
- The Selenium submitter attempts to fill confidence fields if present; otherwise it leaves the table’s existing numbers.
- Always verify the FantasyTeamsNetwork entry after submission in case the site layout changes.
