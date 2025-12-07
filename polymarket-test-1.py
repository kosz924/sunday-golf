import requests
import json

def fetch_game_winners_only():
    NFL_TAG_ID = 450
    
    params = {
        "tag_id": NFL_TAG_ID,
        "active": "true",
        "closed": "false",
        "sort": "volume", 
        "limit": 100 
    }

    print(f"Fetching ONLY Game Winner Markets...\n")

    try:
        response = requests.get("https://gamma-api.polymarket.com/events", params=params)
        response.raise_for_status()
        events = response.json()

        count = 0
        for event in events:
            markets = event.get('markets', [])
            
            for market in markets:
                question = market.get('question', '')

                # --- STRICT FILTER ---
                # 1. Must contain " vs. " (The hallmark of a game matchup)
                # 2. Must NOT contain ":" (Removes "Spread:", "Total:", "Player:")
                if " vs. " not in question or ":" in question:
                    continue
                # ---------------------

                # Handle Prices
                raw_prices = market.get('outcomePrices', '["0", "0"]')
                if isinstance(raw_prices, str):
                    try: prices = json.loads(raw_prices)
                    except: prices = ["0", "0"]
                else: prices = raw_prices

                # Handle Team Names
                raw_names = market.get('outcomes', '["Team A", "Team B"]')
                if isinstance(raw_names, str):
                    try: names = json.loads(raw_names)
                    except: names = ["Team A", "Team B"]
                else: names = raw_names

                count += 1
                print(f"GAME: {question}")
                for name, price in zip(names, prices):
                    pct = float(price) * 100
                    print(f"  {name:<20}: {pct:.1f}%")
                print("-" * 30)
        
        if count == 0:
            print("No active games found. (Note: Markets often close shortly after kickoff)")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    fetch_game_winners_only()
