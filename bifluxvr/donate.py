import time

from . import net, selfupdate

DONATE_URL = "https://revolut.me/intramy?amount={amount}00&currency=EUR&note=BifluxVR%20{item}"

GOAL = 50
RAISED = 0
RAISED_MONTH = "2026-11"

TIERS = [
    ("energy", "⚡", "Energy drink", ("A can for the next late session.", "One can. The next feature runs on this."), 2),
    ("snus", "🧊", "Can of snus", ("A can of snus for the long sessions.", "Ice cool mint. Keeps the bug hunt going."), 5),
    ("cigarette", "🚬", "Pack of cigarettes", ("A pack for the long debugging nights.", "For the 3 a.m. bugs. Don't tell my mom."), 6),
    ("lunch", "🍔", "Lunch", ("A proper meal, away from the desk.", "Real food. Rare around here."), 6),
]

CUSTOM = ("updates", "🚀", "Your own number",
          ("Any amount. It goes toward keeping BifluxVR working as Proton, WiVRn and games change.",
           "Whatever you like. Proton, WiVRn or some game will break something next week anyway."), 1, 100, 5, (5, 15, 25))


def url_for(tier_id, amount):
    if not DONATE_URL:
        return ""
    return DONATE_URL.format(amount=int(amount), item=tier_id)


def funding():
    month, raised, goal = RAISED_MONTH, RAISED, GOAL
    if selfupdate.REPO:
        try:
            d = net.get_json(f"https://raw.githubusercontent.com/{selfupdate.REPO}/main/funding.json")
            month, raised, goal = d["month"], int(d["raised"]), int(d["goal"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return (raised if month == time.strftime("%Y-%m") else 0), goal
