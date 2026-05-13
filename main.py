import getpass
import requests

BASE_URL = "https://borker.college/api/v1"


class BorkerClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _get(self, path: str, params: dict = None):
        r = self.session.get(f"{BASE_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict):
        r = self.session.post(f"{BASE_URL}{path}", json=body)
        r.raise_for_status()
        return r.json()

    # ── Account ──────────────────────────────────────────────────────────────

    def me(self) -> dict:
        """Return authenticated user info and balance."""
        return self._get("/me")

    # ── Markets ───────────────────────────────────────────────────────────────

    def list_markets(self, status: str = "open") -> list[dict]:
        """List markets. status: open | closed | resolved | all"""
        return self._get("/markets", params={"status": status})["markets"]

    def get_market(self, slug: str) -> dict:
        """Get full details for a single market by slug."""
        return self._get(f"/markets/{slug}")

    # ── Trading ───────────────────────────────────────────────────────────────

    def trade(
        self,
        slug: str,
        outcome_id: str,
        *,
        shares: float = None,
        max_cost: float = None,
        yes_no: str = "yes",
        max_share_price: float = None,
    ) -> dict:
        """
        Execute a trade.

        Provide exactly one of:
          shares    – number of shares to buy (positive) or sell (negative)
          max_cost  – max Barks to spend (buy only)

        yes_no: "yes" (default) or "no"
          binary / categorical_excl  → "no" buys all other outcomes
          categorical_indep / scalar → "no" bets against this specific outcome

        max_share_price: optional price cap per share
        """
        if shares is None and max_cost is None:
            raise ValueError("Provide either shares or max_cost")

        body: dict = {"outcomeId": outcome_id, "yesNo": yes_no}
        if shares is not None:
            body["shares"] = shares
        if max_cost is not None:
            body["maxCost"] = max_cost
        if max_share_price is not None:
            body["maxSharePrice"] = max_share_price

        return self._post(f"/markets/{slug}/trade", body)

    # ── Convenience helpers ───────────────────────────────────────────────────

    def buy(self, slug: str, outcome_id: str, max_cost: float, **kwargs) -> dict:
        """Buy up to max_cost Barks worth of YES shares on an outcome."""
        return self.trade(slug, outcome_id, max_cost=max_cost, yes_no="yes", **kwargs)

    def sell(self, slug: str, outcome_id: str, shares: float, **kwargs) -> dict:
        """Sell a number of YES shares on an outcome (shares should be positive)."""
        return self.trade(slug, outcome_id, shares=-abs(shares), yes_no="yes", **kwargs)

    def bet_no(self, slug: str, outcome_id: str, max_cost: float, **kwargs) -> dict:
        """Bet NO on an outcome up to max_cost Barks."""
        return self.trade(slug, outcome_id, max_cost=max_cost, yes_no="no", **kwargs)


# ── Quick CLI ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, sys

    api_key = getpass.getpass("Enter your API key: ")
    client = BorkerClient(api_key)
    args = sys.argv[1:]

    if not args or args[0] == "me":
        print(json.dumps(client.me(), indent=2))

    elif args[0] == "markets":
        status = args[1] if len(args) > 1 else "open"
        for m in client.list_markets(status):
            price = m["outcomes"][0]["price"] if m["outcomes"] else "?"
            print(f"[{m['status']}] {m['slug']}  |  {m['title']}  |  {price}")

    elif args[0] == "market" and len(args) > 1:
        print(json.dumps(client.get_market(args[1]), indent=2))

    elif args[0] == "buy" and len(args) == 4:
        # buy <slug> <outcome_id> <max_cost>
        result = client.buy(args[1], args[2], float(args[3]))
        print(json.dumps(result, indent=2))

    elif args[0] == "sell" and len(args) == 4:
        # sell <slug> <outcome_id> <shares>
        result = client.sell(args[1], args[2], float(args[3]))
        print(json.dumps(result, indent=2))

    elif args[0] == "no" and len(args) == 4:
        # no <slug> <outcome_id> <max_cost>
        result = client.bet_no(args[1], args[2], float(args[3]))
        print(json.dumps(result, indent=2))

    else:
        print(
            "Usage:\n"
            "  python main.py me\n"
            "  python main.py markets [open|closed|resolved|all]\n"
            "  python main.py market <slug>\n"
            "  python main.py buy  <slug> <outcome_id> <max_cost>\n"
            "  python main.py sell <slug> <outcome_id> <shares>\n"
            "  python main.py no   <slug> <outcome_id> <max_cost>\n"
        )
