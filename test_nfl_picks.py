import unittest
import datetime as dt
from bs4 import BeautifulSoup
from nfl_picks import (
    parse_existing_picks_html,
    summarize_existing_comparison,
    generate_team_aliases,
    aliases_from_label,
    canonicalize_label,
    ExistingSubmission,
    ExistingGamePick,
    MondaySummary,
    Pick,
    Game,
    GameOdds,
)

class TestNewWebsiteLayout(unittest.TestCase):
    def setUp(self):
        with open("data/sample_week1_new_layout.html", "r", encoding="utf-8") as f:
            self.sample_html = f.read()

    def test_parse_clean_form(self):
        sub = parse_existing_picks_html(self.sample_html)
        self.assertEqual(len(sub.picks), 0)
        self.assertIsNone(sub.tie_breaker)
        self.assertIsNone(sub.suicide)

    def test_parse_form_with_picks(self):
        # Simulate user having selected Carolina, 16 pts, 48 tiebreaker, and Buffalo suicide
        html = self.sample_html.replace('value="G3570PV"', 'value="G3570PV" checked')
        html = html.replace('id="pts1" name="pts1" type="number" inputmode="numeric"\n                 min="1" max="16" step="1" value=""', 'id="pts1" name="pts1" value="16"')
        html = html.replace('name="tiebreaker" type="number" inputmode="numeric" min="0" step="1"\n               value=""', 'name="tiebreaker" value="48"')
        html = html.replace('<option value="Buffalo">Buffalo</option>', '<option value="Buffalo" selected>Buffalo</option>')

        sub = parse_existing_picks_html(html)
        self.assertEqual(len(sub.picks), 1)
        pick = sub.picks[0]
        self.assertEqual(pick.visitor, "Carolina")
        self.assertEqual(pick.home, "Chicago")
        self.assertEqual(pick.selected, "Carolina")
        self.assertEqual(pick.points, 16)
        self.assertEqual(sub.tie_breaker, 48)
        self.assertEqual(sub.suicide, "Buffalo")

    def test_suicide_dropdown_matching(self):
        soup = BeautifulSoup(self.sample_html, "html.parser")
        suicide_select = soup.find("select", attrs={"name": "suicide"})
        self.assertIsNotNone(suicide_select)
        options = [opt.get_text(strip=True) for opt in suicide_select.find_all("option") if opt.get_text(strip=True) != "Select One"]

        test_teams = [
            ({"displayName": "Carolina Panthers", "location": "Carolina", "name": "Panthers", "abbreviation": "CAR"}, "Carolina"),
            ({"displayName": "Chicago Bears", "location": "Chicago", "name": "Bears", "abbreviation": "CHI"}, "Chicago"),
            ({"displayName": "Kansas City Chiefs", "location": "Kansas City", "name": "Chiefs", "abbreviation": "KC"}, "Kansas City"),
            ({"displayName": "Los Angeles Chargers", "location": "Los Angeles", "name": "Chargers", "abbreviation": "LAC"}, "LA Chargers"),
            ({"displayName": "New York Giants", "location": "New York", "name": "Giants", "abbreviation": "NYG"}, "NY Giants"),
            ({"displayName": "New York Jets", "location": "New York", "name": "Jets", "abbreviation": "NYJ"}, "NY Jets"),
            ({"displayName": "Washington Commanders", "location": "Washington", "name": "Commanders", "abbreviation": "WSH"}, "Washington"),
        ]

        for team_info, expected_option in test_teams:
            team_aliases = set(generate_team_aliases(team_info))
            matched = None
            for opt in options:
                opt_aliases = aliases_from_label(opt) | {canonicalize_label(opt)}
                if team_aliases & opt_aliases:
                    matched = opt
                    break
            self.assertEqual(matched, expected_option, f"Failed matching {team_info['displayName']} to {expected_option}")

    def test_legacy_table_parsing(self):
        legacy_html = """
        <html><body><table>
        <tr>
          <td><input type="radio" name="gm1" value="v1" checked></td>
          <td><img src="/logos/panthers.gif"></td>
          <td class="lineitem">Carolina Panthers</td>
          <td><img src="/logos/bears.gif"></td>
          <td><input type="radio" name="gm1" value="h1"></td>
          <td class="lineitem">Chicago Bears</td>
          <td><input type="text" name="pts1" value="15"></td>
        </tr>
        <tr>
          <td>Monday Night Total</td>
          <td><input type="text" name="tiebreaker" value="44"></td>
        </tr>
        </table></body></html>
        """
        sub = parse_existing_picks_html(legacy_html)
        self.assertEqual(len(sub.picks), 1)
        self.assertEqual(sub.picks[0].selected, "Carolina Panthers")
        self.assertEqual(sub.picks[0].points, 15)
        self.assertEqual(sub.tie_breaker, 44)

    def test_summarize_existing_comparison_with_suicide(self):
        odds = GameOdds(spread=-3.5, over_under=41.5, provider="test", favorite_side="away")
        now = dt.datetime.now()
        game = Game(
            event_id="1",
            competition_id="1",
            start_utc=now,
            start_et=now,
            home={"team": {"displayName": "Chicago Bears", "location": "Chicago", "name": "Bears"}},
            away={"team": {"displayName": "Carolina Panthers", "location": "Carolina", "name": "Panthers"}},
            odds=odds,
            status={},
        )
        pick = Pick(game=game, points=16, selection="favorite")

        existing = ExistingSubmission(
            picks=[ExistingGamePick(visitor="Carolina", home="Chicago", selected="Carolina", points=16)],
            tie_breaker=45,
            suicide="Buffalo",
        )
        summary = MondaySummary(label="Monday", games=[], missing_totals=False, combined_total=45, computed_pick=45)

        # Same suicide pick
        comp_match = summarize_existing_comparison([pick], existing, monday_summary=summary, suicide_team="Buffalo")
        self.assertIn("site picks match the computed selections", comp_match)

        # Different suicide pick
        comp_diff = summarize_existing_comparison([pick], existing, monday_summary=summary, suicide_team="Miami")
        self.assertIn("Suicide pick:", comp_diff)
        self.assertIn("Site has Buffalo, script selects Miami", comp_diff)

if __name__ == "__main__":
    unittest.main()
