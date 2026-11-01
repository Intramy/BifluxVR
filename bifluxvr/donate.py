# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Donation tiers shown on the Support page.

Set DONATE_URL to your payment link to switch the buttons on. `{amount}`
(whole euros) and `{item}` are filled in, e.g.
    "https://paypal.me/yourname/{amount}EUR"
    "https://ko-fi.com/yourname"
Revolut.me takes the amount in cents, hence the 00.

The goal bar is typed by hand, since Revolut has no public total. With a GitHub
REPO set, funding.json at the top of the repo overrides these without a release:
    {"month": "2026-11", "raised": 12, "goal": 50}
A month that isn't the current one counts as nothing raised yet.
"""

import time

from . import net, selfupdate

DONATE_URL = "https://revolut.me/intramy?amount={amount}00&currency=EUR&note=BifluxVR%20{item}"

GOAL = 50
RAISED = 0
RAISED_MONTH = "2026-11"

TIERS = [
    # (id, emoji, title, (serious blurb, funny blurb), min €, max €, default €)
    ("cigarette", "🚬", "Buy me a cigarette",
     ("A small tip for the developer.",
      "For the 3 a.m. debugging sessions. Don't tell my mom."), 2, 5, 3),
    ("energy", "⚡", "Buy me an energy drink",
     ("Helps fund the next feature.",
      "Liquid motivation. The next feature literally runs on this."), 1, 3, 2),
    ("support", "💜", "Offer some support",
     ("Supports ongoing development and maintenance.",
      "Keeps BifluxVR free, alive and slightly less buggy."), 1, 10, 5),
]

# The open-ended one under the tiers: any whole amount, picked by the giver.
# (id, emoji, title, (serious blurb, funny blurb), min €, max €, default €, quick picks)
CUSTOM = ("updates", "🚀", "Keep the updates coming",
          ("Pick any amount you like. It goes toward keeping BifluxVR updated as Proton, WiVRn and "
           "games change.",
           "Name your price. It keeps the updates rolling every time Proton, WiVRn or some game "
           "decides to break everything."), 1, 500, 10, (5, 10, 20, 50))


def url_for(tier_id, amount):
    if not DONATE_URL:
        return ""
    return DONATE_URL.format(amount=int(amount), item=tier_id)


def funding():
    """(raised, goal) in whole euros for this month. May hit the network."""
    month, raised, goal = RAISED_MONTH, RAISED, GOAL
    if selfupdate.REPO:
        try:
            d = net.get_json(f"https://raw.githubusercontent.com/{selfupdate.REPO}/main/funding.json")
            month, raised, goal = d["month"], int(d["raised"]), int(d["goal"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return (raised if month == time.strftime("%Y-%m") else 0), goal
