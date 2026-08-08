"""
============================================================================
 KITCHENSENSE — END-TO-END USER PROCESS FLOW
 Version 2
============================================================================

WHY THIS EXISTS
---------------
Kavitha's instruction: map what the user does OUTSIDE the app as well as
inside it, starting from the trigger, so the real end-to-end process is
visible before any features are decided.

The purpose is not to draw the app. It is to draw the user's LIFE, then mark
which parts the app touches. Everything it does not touch is either a
deliberate scope decision or a gap worth knowing about.

THE FIVE FIGURES
----------------
    A   The process today, with no app        — where waste actually happens
    D   First-run journey                     — the trigger to start, and the
                                                riskiest period in the product
    B   The same weekly process, with the app — who does what
    E   When things go wrong                  — user-side unhappy paths
    C   Traceability                          — every feature traced to a
                                                real problem, plus the gaps

Figures A and D come first in any presentation. They establish that the
problem is real and that the hardest moment is the first week, before a
single feature is proposed.

COLOUR MEANING (consistent across all five)
-------------------------------------------
    Grey    outside the app — the real world, which we do not control
    Blue    the user is actively in the app
    Green   the system acting on its own, with nobody watching
    Amber   a decision point, or a partial answer to a gap
    Red     food is wasted, or the user leaves — the outcomes to prevent

SETUP
-----
    pip install graphviz
    winget install --id Graphviz.Graphviz -e     (Windows)
    brew install graphviz                         (macOS)
    sudo apt install graphviz                     (Linux)

    Close and reopen the terminal, then:
        python user_process_flow_v2.py

Outputs land in ./diagrams/ as PNG, PDF and SVG.
============================================================================
"""

import os
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# The Windows Graphviz installer frequently skips the "add to PATH" option,
# which produces an unhelpful stack trace. Find dot ourselves instead.
# ---------------------------------------------------------------------------
def ensure_graphviz() -> None:
    if shutil.which("dot"):
        return
    for c in (
        r"C:\Program Files\Graphviz\bin",
        r"C:\Program Files (x86)\Graphviz\bin",
        str(Path.home() / r"AppData\Local\Programs\Graphviz\bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ):
        if (Path(c) / "dot").exists() or (Path(c) / "dot.exe").exists():
            os.environ["PATH"] = f"{c}{os.pathsep}{os.environ['PATH']}"
            return
    sys.exit(
        "Graphviz engine not found.\n"
        "  Windows:  winget install --id Graphviz.Graphviz -e\n"
        "  macOS:    brew install graphviz\n"
        "  Linux:    sudo apt install graphviz\n"
        "Then close and reopen the terminal."
    )


ensure_graphviz()
from graphviz import Digraph  # noqa: E402

OUT = Path("diagrams")
OUT.mkdir(exist_ok=True)

PALETTE = {
    "outside": {"fill": "#E9ECEF", "line": "#5C636A", "band": "#F5F6F7"},
    "app":     {"fill": "#D6E9F8", "line": "#0F6CBD", "band": "#EFF6FC"},
    "system":  {"fill": "#D9F0DE", "line": "#2E7D3A", "band": "#EFF9F1"},
    "decide":  {"fill": "#FCE6CC", "line": "#C4700A", "band": "#FEF5E9"},
    "waste":   {"fill": "#F8D9DA", "line": "#A82A2F", "band": "#FDEFEF"},
}
FONT = "Helvetica"


def node(g, name, label, layer, shape="box", **kw):
    """Style a node by who owns the step. Keeps the flow definitions readable."""
    p = PALETTE[layer]
    g.node(
        name, label, shape=shape,
        style=kw.pop("style", "filled,rounded" if shape == "box" else "filled"),
        fillcolor=p["fill"], color=p["line"], penwidth="1.8",
        fontname=FONT, fontsize=kw.pop("fontsize", "10"),
        fontcolor="#1A1A1A", margin=kw.pop("margin", "0.20,0.11"), **kw,
    )


def edge(g, a, b, label="", layer="outside", style="solid", **kw):
    g.edge(
        a, b, label=label, color=PALETTE[layer]["line"], penwidth="1.5",
        fontname=FONT, fontsize="9", fontcolor=PALETTE[layer]["line"],
        style=style, **kw,
    )


def legend(g, entries, title="Legend"):
    rows = "".join(
        f'<TR><TD BGCOLOR="{PALETTE[k]["fill"]}" WIDTH="18"></TD>'
        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="10">{v}</FONT></TD></TR>'
        for k, v in entries
    )
    g.node(
        "legend",
        f'<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4">'
        f'<TR><TD COLSPAN="2" ALIGN="LEFT"><B>{title}</B></TD></TR>{rows}</TABLE>>',
        shape="plaintext", fontname=FONT,
    )


def render(g, name):
    for fmt in ("png", "pdf", "svg"):
        g.format = fmt
        g.render(str(OUT / name), cleanup=True)
    print(f"  {name}.png / .pdf / .svg")


# ===========================================================================
# FIGURE A — THE PROCESS TODAY, WITH NO APP
# ---------------------------------------------------------------------------
# Present this first. It proves the problem is real before anything is
# proposed, and it is where every feature in the product comes from.
#
# Two additions in version 2:
#   * The storage decision is now its own box. It happens during unpacking,
#     the app never sees it, and it changes shelf life by weeks. Making it
#     visible forces a decision about whether to guess or ask.
#   * The second person is now visible. In most households one person shops
#     and another cooks, and they do not share the same information.
# ===========================================================================
def figure_a_current_state():
    g = Digraph("current")
    g.attr(
        rankdir="TB", splines="polyline", nodesep="0.45", ranksep="0.42",
        bgcolor="white", fontname=FONT,
        label="\\nFigure A — The weekly process today, with no app\\n"
              "where household food waste actually happens",
        labelloc="t", fontsize="17",
    )

    node(g, "trigger", "TRIGGER\\nFridge looks empty,\\n"
                       "or it is the usual shopping day", "outside", shape="oval")
    node(g, "plan", "Thinks about meals\\noften vaguely, or not at all", "outside")
    node(g, "list", "Writes a list,\\nor relies on memory", "outside")
    node(g, "shop", "Shops\\nsome planned, some on offer,\\nsome on impulse", "outside")
    node(g, "topup", "Top-up shops midweek\\noften cash, often no receipt", "outside")
    node(g, "unpack", "Unpacks at home", "outside")

    # The storage decision — invisible to any app, decisive for shelf life
    node(g, "store", "Decides where each item goes\\n"
                     "fridge, freezer or cupboard\\n"
                     "→ changes shelf life by WEEKS", "outside")

    node(g, "f1", "FAILURE POINT 1\\nNo record of what was bought.\\n"
                  "Memory is the only inventory", "waste")

    # The second person — different information, same kitchen
    node(g, "other", "A different person cooks\\n"
                     "did not shop, does not know\\nwhat was bought", "outside")

    node(g, "cook", "Cooks during the week\\n"
                    "decides in the moment, usually tired", "outside")
    node(g, "grab", "Reaches for whatever is\\nvisible and convenient", "outside")

    node(g, "f2", "FAILURE POINT 2\\nItems at the back are forgotten.\\n"
                  "Partly used packs fastest of all", "waste")

    node(g, "away", "Eats out, or is away\\nfood at home goes untouched", "outside")

    node(g, "find", "Eventually finds the item", "outside")
    node(g, "check", "Still good?", "decide", shape="diamond", style="filled")
    node(g, "eat", "Eats it", "outside")
    node(g, "bin", "Throws it away", "waste")

    node(g, "f3", "FAILURE POINT 3\\nThe waste is noticed only\\n"
                  "once it is too late to act", "waste")

    node(g, "repeat", "Next shop\\noften buys the same item again", "outside",
         shape="oval")

    edge(g, "trigger", "plan")
    edge(g, "plan", "list")
    edge(g, "list", "shop")
    edge(g, "shop", "unpack")
    edge(g, "shop", "topup", "midweek", style="dashed")
    edge(g, "topup", "unpack", style="dashed")
    edge(g, "unpack", "store")
    edge(g, "store", "f1", layer="waste")
    edge(g, "f1", "cook", layer="waste")
    edge(g, "other", "cook", "or", style="dashed")
    edge(g, "cook", "grab")
    edge(g, "cook", "away", "some evenings", style="dashed")
    edge(g, "away", "f2", layer="waste", style="dashed")
    edge(g, "grab", "f2", layer="waste")
    edge(g, "f2", "find", layer="waste")
    edge(g, "find", "check", layer="decide")
    edge(g, "check", "eat", "yes", layer="decide")
    edge(g, "check", "bin", "no", layer="waste")
    edge(g, "bin", "f3", layer="waste")
    edge(g, "f3", "repeat", layer="waste")

    legend(g, [("outside", "Something the user does"),
               ("decide", "A decision the user makes"),
               ("waste", "Where the process fails")],
           title="Current process")
    render(g, "figA_current_process")


# ===========================================================================
# FIGURE D — FIRST-RUN JOURNEY
# ---------------------------------------------------------------------------
# Kavitha asked what triggers the user to start. Figure A shows the RECURRING
# trigger. This shows the FIRST one, which is a different journey entirely.
#
# The critical detail is the amber box in the middle: after the first receipt
# there is a two-to-four day wait before the first message arrives. Nothing
# happens, and the user has no evidence the app does anything at all.
#
# That wait is the single riskiest period in the product. Everything on the
# right of the diagram is designed around surviving it.
# ===========================================================================
def figure_d_first_run():
    g = Digraph("firstrun")
    g.attr(
        rankdir="TB", splines="polyline", nodesep="0.42", ranksep="0.45",
        bgcolor="white", fontname=FONT,
        label="\\nFigure D — First-run journey\\n"
              "from hearing about it to the first useful message",
        labelloc="t", fontsize="17",
    )

    node(g, "hear", "TRIGGER TO START\\nThrew food away and felt bad,\\n"
                    "saw the cost of a shop,\\nor a friend mentioned it",
         "outside", shape="oval")
    node(g, "install", "Installs and opens it", "app")
    node(g, "signup", "Creates an account\\n≈ 30 seconds", "app")

    node(g, "setup", "Answers four questions\\n"
                     "household size · allergies\\n"
                     "dietary needs · cooking time\\n"
                     "≈ 60 seconds", "app")
    node(g, "allergy", "Allergies are the ONLY\\nmandatory answer", "system")

    node(g, "empty", "Sees an empty kitchen list\\nwith one clear instruction:\\n"
                     "“photograph your next receipt”", "app")

    node(g, "wait1", "WAITS\\nuntil the next shop\\n— up to 7 days —", "decide")
    node(g, "drop1", "DROPS OUT\\nforgets before shopping again", "waste")

    node(g, "first", "Photographs the FIRST receipt", "app")
    node(g, "build", "Kitchen list appears\\n≈ 3 seconds", "system")
    node(g, "cold", "COLD START\\nNo history yet, so it uses\\ntypical figures for a\\n"
                    "household this size", "system")

    node(g, "gap", "THE RISKY GAP\\n2–4 days of silence\\n"
                   "before anything is at risk.\\n"
                   "The user has no evidence\\nthe app does anything", "waste")

    node(g, "bridge", "BRIDGING THE GAP\\n"
                      "· Show days remaining immediately\\n"
                      "· Flag the 2–3 riskiest items on day one\\n"
                      "· Say plainly when the first\\n"
                      "  message is expected", "decide")

    node(g, "msg1", "FIRST REAL MESSAGE ARRIVES\\nnaming actual items", "system")
    node(g, "judge", "Was that useful?", "decide", shape="diamond", style="filled")

    node(g, "keep", "KEEPS USING IT\\nphotographs the next receipt\\n"
                    "without being reminded", "app")
    node(g, "drop2", "DROPS OUT\\nsuggestion felt irrelevant,\\n"
                     "or arrived too late", "waste")

    node(g, "habit", "HABIT FORMED\\nby roughly the third receipt\\n"
                     "the app knows this household", "system", shape="oval")

    edge(g, "hear", "install")
    edge(g, "install", "signup", layer="app")
    edge(g, "signup", "setup", layer="app")
    edge(g, "setup", "allergy", layer="system", style="dashed")
    edge(g, "setup", "empty", layer="app")
    edge(g, "empty", "wait1", layer="decide")
    edge(g, "wait1", "drop1", "forgets", layer="waste")
    edge(g, "wait1", "first", "shops", layer="app")
    edge(g, "first", "build", layer="system")
    edge(g, "build", "cold", layer="system")
    edge(g, "cold", "gap", layer="waste")
    edge(g, "gap", "bridge", "mitigated by", layer="decide")
    edge(g, "bridge", "msg1", layer="system")
    edge(g, "msg1", "judge", layer="decide")
    edge(g, "judge", "keep", "yes", layer="app")
    edge(g, "judge", "drop2", "no", layer="waste")
    edge(g, "keep", "habit", layer="system")

    legend(g, [("outside", "Outside the app"),
               ("app", "User is in the app"),
               ("system", "System acting alone"),
               ("decide", "Decision point or mitigation"),
               ("waste", "User is lost here")],
           title="First-run journey")
    render(g, "figD_first_run")


# ===========================================================================
# FIGURE B — THE SAME WEEKLY PROCESS, WITH THE APP
# ---------------------------------------------------------------------------
# Same journey as Figure A. Colour shows who does each step.
#
# Deliberately drawn as one vertical column rather than side-by-side lanes.
# Lanes look tidy on a whiteboard and become unreadable in Graphviz once
# arrows cross between them.
#
# Count the blue boxes. Six, and five of them are optional. Photographing the
# receipt is the only required action. That is the design target made visible,
# and if the blue count ever grows the design has failed.
# ===========================================================================
def figure_b_with_app():
    g = Digraph("withapp")
    g.attr(
        rankdir="TB", splines="polyline", nodesep="0.40", ranksep="0.42",
        bgcolor="white", fontname=FONT,
        label="\\nFigure B — The same weekly process, with KitchenSense\\n"
              "colour shows who does each step",
        labelloc="t", fontsize="17",
    )

    node(g, "trigger", "TRIGGER\\nUsual shopping day", "outside", shape="oval")
    node(g, "shop", "Shops as normal\\nno change in behaviour", "outside")

    node(g, "photo", "Photographs the receipt\\n≈ 20 seconds\\n"
                     "THE ONLY REQUIRED ACTION", "app")
    node(g, "noreceipt", "No receipt?\\nbarcode, voice, or one-tap\\n"
                         "from frequent items", "app")

    node(g, "read_rcpt", "Reads the receipt\\nchecks the totals add up", "system")
    node(g, "match", "Matches names to real products", "system")
    node(g, "confirm", "Confirms 1–2 uncertain items\\n"
                       "≈ 10 seconds · only when unsure", "app")
    node(g, "build", "Builds the kitchen list", "system")

    node(g, "storeq", "Storage assumed by category\\n"
                      "corrected once, then remembered", "system")
    node(g, "storefix", "Corrects storage\\nif the guess was wrong\\n"
                        "· one tap, once per product", "app")

    node(g, "estimate", "Estimates days remaining and\\n"
                        "how fast this household consumes", "system")
    node(g, "date", "Answers ONE date question\\n"
                    "milk, meat · ≈ 5 seconds\\n"
                    "only for short-life items", "app")

    node(g, "unpack", "Unpacks and puts things away", "outside")
    node(g, "cooks", "Cooks during the week", "outside")
    node(g, "used", "Taps “used half”\\nonly when prompted", "app")
    node(g, "awaymode", "Taps “away” or “eating out”\\n"
                        "pauses messages", "app")

    node(g, "daily", "EVERY MORNING 06:00\\nchecks the risk scores", "system")
    node(g, "worth", "anything worth\\ninterrupting for?", "decide",
         shape="diamond", style="filled")
    node(g, "quiet", "STAYS SILENT\\nmost mornings", "system")

    node(g, "recipe", "Finds a recipe using\\nthe at-risk items", "system")
    node(g, "safety", "Checks allergies and use-by dates\\n"
                      "plain rules, not the AI", "system")
    node(g, "send", "Sends ONE message\\nwith a “freeze it” option too", "system")

    node(g, "read", "Reads the message", "app")
    node(g, "act", "Cooks it, freezes it,\\nor does neither", "outside")
    node(g, "saved", "FOOD IS EATEN\\nnot wasted", "system", shape="oval")
    node(g, "learn", "Learns from the response\\n"
                     "and adjusts how often it speaks", "system")

    edge(g, "trigger", "shop")
    edge(g, "shop", "photo", "at home, once", layer="app")
    edge(g, "shop", "noreceipt", "cash / market", layer="app", style="dashed")
    edge(g, "noreceipt", "build", layer="system", style="dashed")
    edge(g, "photo", "read_rcpt", layer="system")
    edge(g, "read_rcpt", "match", layer="system")
    edge(g, "match", "confirm", "if unsure", layer="app", style="dashed")
    edge(g, "confirm", "build", layer="system")
    edge(g, "match", "build", layer="system")
    edge(g, "build", "storeq", layer="system")
    edge(g, "storeq", "storefix", "if wrong", layer="app", style="dashed")
    edge(g, "storefix", "estimate", layer="system", style="dashed")
    edge(g, "storeq", "estimate", layer="system")
    edge(g, "estimate", "date", "short-life only", layer="app", style="dashed")
    edge(g, "date", "unpack", layer="outside", style="dashed")
    edge(g, "estimate", "unpack", layer="outside")
    edge(g, "unpack", "cooks")
    edge(g, "cooks", "used", "when prompted", layer="app", style="dashed")
    edge(g, "cooks", "awaymode", "if away", layer="app", style="dashed")
    edge(g, "awaymode", "daily", "pauses", layer="system", style="dashed")
    edge(g, "used", "daily", layer="system", style="dashed")
    edge(g, "cooks", "daily", layer="system")
    edge(g, "daily", "worth", layer="system")
    edge(g, "worth", "quiet", "no — most days", layer="decide")
    edge(g, "quiet", "daily", "check again tomorrow", layer="system",
         style="dotted", constraint="false")
    edge(g, "worth", "recipe", "yes", layer="decide")
    edge(g, "recipe", "safety", layer="system")
    edge(g, "safety", "recipe", "blocked — try another", layer="system",
         style="dashed", constraint="false")
    edge(g, "safety", "send", "passes", layer="system")
    edge(g, "send", "read", layer="app")
    edge(g, "read", "act")
    edge(g, "act", "saved", layer="system")
    edge(g, "act", "learn", layer="system", style="dashed")
    edge(g, "learn", "daily", "next run", layer="system", style="dotted",
         constraint="false")

    legend(g, [("outside", "Outside the app — real world"),
               ("app", "User is in the app (6 boxes, 5 optional)"),
               ("system", "System acting alone"),
               ("decide", "Decision point")],
           title="Who is doing what")
    render(g, "figB_process_with_app")


# ===========================================================================
# FIGURE E — WHEN THINGS GO WRONG
# ---------------------------------------------------------------------------
# What the user does when the app is WRONG matters more than what it does
# when it is right. Every one of these paths ends either in recovery or in
# the user leaving, and the difference between the two is design, not luck.
#
# The bottom row is the abandonment path. Documenting how users leave shows
# that engagement, not accuracy, is understood to be the real failure mode.
# ===========================================================================
def figure_e_unhappy_paths():
    g = Digraph("unhappy")
    g.attr(
        rankdir="LR", splines="polyline", nodesep="0.30", ranksep="0.95",
        bgcolor="white", fontname=FONT,
        label="\\nFigure E — When things go wrong\\n"
              "user-side failures and how each one recovers",
        labelloc="t", fontsize="17",
    )

    node(g, "u1", "Photo is blurred\\nor cut off", "waste")
    node(g, "u2", "An item was read\\nwrongly", "waste")
    node(g, "u3", "Message about food\\nalready eaten", "waste")
    node(g, "u4", "Suggested recipe is\\nimpractical tonight", "waste")
    node(g, "u5", "Too many messages", "waste")
    node(g, "u6", "Stops photographing\\nreceipts", "waste")

    node(g, "r1", "Totals check fails → asks for a retake\\n"
                  "BEFORE anything is saved.\\nManual entry always available", "system")
    node(g, "r2", "Every item is editable in one tap.\\n"
                  "The correction is remembered\\nfor that product permanently", "system")
    node(g, "r3", "“Already used it” in one tap.\\n"
                  "Consumption estimate updates,\\nso it does not repeat", "system")
    node(g, "r4", "“Something else” → another recipe.\\n"
                  "“Freeze it” → a 30-second rescue.\\n"
                  "Refusal reason is learned", "system")
    node(g, "r5", "Ignored messages raise the\\nhousehold threshold automatically.\\n"
                  "The app speaks less, not more", "system")
    node(g, "r6", "Inventory decays rather than\\nnagging. One gentle reminder,\\n"
                  "then silence. No guilt messaging", "system")

    for a, b in (("u1", "r1"), ("u2", "r2"), ("u3", "r3"),
                 ("u4", "r4"), ("u5", "r5"), ("u6", "r6")):
        edge(g, a, b, "recovers by", layer="system")

    node(g, "exit", "ACCEPTED EXIT PATH\\n"
                    "If the user stops entirely, the app stays quiet,\\n"
                    "deletes receipt images after 30 days, and offers\\n"
                    "full account deletion. It does not chase them.", "decide")

    legend(g, [("waste", "What goes wrong"),
               ("system", "How the design recovers"),
               ("decide", "Accepted outcome")],
           title="Unhappy paths")
    render(g, "figE_unhappy_paths")


# ===========================================================================
# FIGURE C — TRACEABILITY
# ---------------------------------------------------------------------------
# The figure that answers "why did you build that?"
#
# Left   : problems observed in the current process (Figure A)
# Middle : the feature that addresses each one
# Right  : gaps that cannot be fully closed, with the partial mitigation
#
# The third column is the honest part. A gap with a stated partial answer
# reads as considered scoping. A gap left blank reads as something nobody
# thought about.
# ===========================================================================
def figure_c_traceability():
    g = Digraph("trace")
    g.attr(
        rankdir="LR", splines="polyline", nodesep="0.28", ranksep="1.1",
        bgcolor="white", fontname=FONT,
        label="\\nFigure C — Every feature traced back to a real problem\\n"
              "and how the remaining gaps are partially covered",
        labelloc="t", fontsize="17",
    )

    node(g, "p1", "No record of\\nwhat was bought", "waste")
    node(g, "p2", "Items at the back\\nare forgotten", "waste")
    node(g, "p3", "Waste noticed\\ntoo late to act", "waste")
    node(g, "p4", "Nothing planned\\nfor tonight", "waste")
    node(g, "p5", "Buys duplicates of\\nwhat is already home", "waste")
    node(g, "p6", "The cook did not\\ndo the shopping", "waste")

    node(g, "f1", "Photograph the receipt\\n→ automatic inventory", "app")
    node(g, "f2", "Risk score per item,\\nbased on this household", "app")
    node(g, "f3", "Daily check — message\\nBEFORE it spoils", "app")
    node(g, "f4", "Recipe built around\\nthe at-risk items", "app")
    node(g, "f5", "Check the kitchen list\\nwhile shopping", "app")
    node(g, "f6", "One shared household account\\neveryone sees the same list", "app")

    for a, b in (("p1", "f1"), ("p2", "f2"), ("p3", "f3"),
                 ("p4", "f4"), ("p5", "f5"), ("p6", "f6")):
        edge(g, a, b, "solved by", layer="app")

    node(g, "g1", "GAP\\nNo receipt\\nmarket stall, cash", "outside")
    node(g, "g2", "GAP\\nMeals eaten\\noutside the home", "outside")
    node(g, "g3", "GAP\\nUser may not\\nactually cook", "outside")
    node(g, "g4", "GAP\\nStorage location\\nis never observed", "outside")

    node(g, "m1", "PARTIAL COVER\\n· Barcode scan\\n"
                  "· Say it out loud: “bought spinach and chicken”\\n"
                  "· One-tap chips of the 20 most\\n"
                  "  frequently bought items", "decide")
    node(g, "m2", "PARTIAL COVER\\n· Away mode — pauses messages and\\n"
                  "  freezes consumption assumptions\\n"
                  "· “Eating out tonight” single tap\\n"
                  "· Unanswered items assumed eaten,\\n"
                  "  not assumed rotting", "decide")
    node(g, "m3", "PARTIAL COVER\\n· “Freeze it” — a 30-second rescue\\n"
                  "  instead of cooking a whole meal\\n"
                  "· Effort filter: 20 minutes or less\\n"
                  "· Asks why a suggestion was refused\\n"
                  "  and stops repeating that kind", "decide")
    node(g, "m4", "PARTIAL COVER\\n· Sensible default per category\\n"
                  "· Corrected once, then remembered\\n"
                  "  for that product permanently", "decide")

    for a, b in (("g1", "m1"), ("g2", "m2"), ("g3", "m3"), ("g4", "m4")):
        edge(g, a, b, "covered by", layer="decide")

    node(g, "x1", "STILL OUT OF SCOPE\\n"
                  "· Food grown, gifted or foraged\\n"
                  "· Anything eaten away from home\\n"
                  "· Compelling anyone to cook\\n"
                  "· Nutrition and diet advice\\n"
                  "· Multiple separate accounts per household", "outside")

    legend(g, [("waste", "Problem seen in the current process"),
               ("app", "Feature that addresses it"),
               ("decide", "Partial cover for a gap"),
               ("outside", "Gap, or deliberately excluded")],
           title="Traceability")
    render(g, "figC_traceability")


if __name__ == "__main__":
    print("Generating process flow diagrams…")
    figure_a_current_state()
    figure_d_first_run()
    figure_b_with_app()
    figure_e_unhappy_paths()
    figure_c_traceability()
    print(f"\nDone — see ./{OUT}/")
    print("\nPresent in this order:  A → D → B → E → C")