#!/usr/bin/env python3
"""
export_status.py — SLIP SELECT / King's Court live-status exporter.

Reads the programme's authoritative records under ~/workspace/backtest/
(READ-ONLY) and writes status.json next to this script for the dashboard
website. Stdlib only. Defensive: any missing/unparseable source yields
nulls for that field, never a crash.

Run:  python3 export_status.py [--out PATH]
"""

import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    REGINA = ZoneInfo("America/Regina")
except Exception:  # pragma: no cover — Regina is a fixed UTC-6 zone
    REGINA = timezone(timedelta(hours=-6))

BACKTEST = os.path.expanduser("~/workspace/backtest")
OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.json")
NEXT_VAULT_OPENING = "2026-12-01"

COURTS = ["Legacy", "Regime", "Quantum", "Selector", "Vanguard"]

LOG_FILES = {
    "Legacy": "mutation_loop_log_legacy.md",
    "Regime": "mutation_loop_log_regime.md",
    "Quantum": "mutation_loop_log_quantum.md",
    "Selector": "mutation_loop_log_selector.md",
    "Vanguard": "mutation_loop_log_vanguard.md",
    "Arena": "mutation_loop_log_arena.md",
    "Discovery": "discovery_loop_log.md",
    "Edge Finder": "edge_finder_log.md",
    "Loss Autopsy": "loss_autopsy_log.md",
    "Stakehunter": "stakehunter_log.md",
}

AGENT_ROLES = {
    "Legacy": "King — market-first picking court",
    "Regime": "Grand king — its pick is the card pick on every slip",
    "Quantum": "King — model-weighted picking court",
    "Selector": "King — chooses which king to follow, game by game",
    "Vanguard": "King — fragility-discount picking court",
    "Arena": "Proving court — exotic ideas fight here; never touches the kings",
    "Discovery": "Evolution search — breeds candidate kings (DEAP + Optuna)",
    "Edge Finder": "Hunts brand-new edges and files collector specs",
    "Loss Autopsy": "Studies the kings' losses to invent new rules",
    "Stakehunter": "Goblin banker — hunts stake-sizing rules (proposals only; flat 25m stays the scoreboard)",
    "Research Director": "Daily manager — assigns hunting grounds, issues stop orders",
}

SLIP_TRACKERS = {
    33: "data_badminton/tracking_slip33_nov17.md",
    34: "data_badminton/tracking_slip34_nov17.md",
    35: "data_badminton/tracking_slip35_nov18.md",
    36: "data_badminton/tracking_slip36_nov18.md",
    37: "data_badminton/tracking_slip37_nov19.md",
    38: "data_badminton/tracking_slip38_nov19.md",
}

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
BANKROLL_CONFIG = os.path.join(SITE_DIR, "bankroll_config.json")
DEFAULT_BANKROLL_CONFIG = {"start_m": None, "reset_date": "2026-11-18"}

SLIP_JSONS = {
    33: ["slips/slip33.json", "slips/slip33_soccer.json"],
    34: ["slips/slip34.json"],
    35: ["slips/slip35.json"],
    36: ["slips/slip36.json"],
    37: ["slips/slip37.json"],
    38: ["slips/slip38.json"],
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def read_text(path, tail_bytes=None):
    """Read a text file (optionally only its tail). None if missing."""
    try:
        if tail_bytes is not None and os.path.getsize(path) > tail_bytes:
            with open(path, "rb") as fh:
                fh.seek(-tail_bytes, os.SEEK_END)
                raw = fh.read()
            text = raw.decode("utf-8", errors="replace")
            # drop the partial first line left by the tail cut
            nl = text.find("\n")
            return text[nl + 1:] if nl != -1 else text
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def clean(text):
    """Strip markdown decoration and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links -> label
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^#+\s*", "", text.strip())
    text = text.replace("|", "·")
    return re.sub(r"\s+", " ", text).strip(" ·-—")


def trunc140(text):
    text = clean(text)
    if len(text) <= 140:
        return text
    cut = text[:137].rsplit(" ", 1)[0]
    return (cut or text[:137]) + "…"


def num(s):
    return float(s.replace(",", "").replace("−", "-")) if s is not None else None


def money(s):
    return num(s)


def regina_iso(dt):
    return dt.astimezone(REGINA).isoformat()


def file_mtime_iso(path):
    try:
        return regina_iso(datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc))
    except OSError:
        return None


DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ ,T]+(\d{2}):(\d{2}))?")


def section_datetime(section_text, fallback_iso):
    m = DATE_RE.search(section_text[:400]) or DATE_RE.search(section_text)
    if not m:
        return fallback_iso
    date, hh, mm = m.group(1), m.group(2) or "00", m.group(3) or "00"
    return f"{date}T{hh}:{mm}:00-06:00"


# --------------------------------------------------------------------------
# kings — parse the LATEST crown entry per court from mutation_loop_state.md
# --------------------------------------------------------------------------

RECORD_RE = re.compile(r"(\d+)-(\d+),\s*priced\s*(\d+),\s*\+([\d,]+\.\d+)m,\s*\+([\d.]+)%")
DOG_RE = re.compile(
    r"[Dd]og(?:-pick)? line.{0,40}?(\d+) picks,\s*(\d+)-(\d+),\s*priced\s*(\d+),"
    r"\s*\+([\d,]+\.\d+)m,\s*\+([\d.]+)%")
STAMP_RE = re.compile(r"STAMP[^:]{0,40}:\s*(CLEAR|PROVISIONAL)")
CROWN_RE = re.compile(
    r"NEW (LEGACY|REGIME|QUANTUM|SELECTOR|VANGUARD) KING CROWNED\s*—\s*\**([A-Z0-9][A-Z0-9\-\.]+)")
# Mutation generations crown with the bare phrasing "NEW KING CROWNED — NAME"
# inside a line that begins "- <Court> R4-G### is done"; the court is the one
# named at the line's start. (Discovery lines use the CROWN_RE phrasing.)
CROWN_BARE_RE = re.compile(
    r"NEW KING CROWNED\s*—\s*\**([A-Z0-9][A-Z0-9\-\.]+)")
LINE_COURT_RE = re.compile(
    r"^-\s*(Legacy|Regime|Quantum|Selector|Vanguard)\s+R4-G\d+")


def parse_kings():
    text = read_text(os.path.join(BACKTEST, "mutation_loop_state.md"))
    kings = {c: None for c in COURTS}
    if text is None:
        return [kings[c] for c in COURTS]
    crown_lines = {}  # court -> (line_text, name); last match wins per court
    for line in text.split("\n"):
        found = []
        for m in CROWN_RE.finditer(line):
            if not re.search(r"\bNO\s*$", line[:m.start()]):  # negated mention
                found.append((m.start(), m.group(1).capitalize(), m.group(2)))
        bare = CROWN_BARE_RE.search(line)
        if bare and not re.search(r"\bNO\s*$", line[:bare.start()]):
            lc = LINE_COURT_RE.match(line)
            if lc:
                found.append((bare.start(), lc.group(1), bare.group(1)))
        if found:
            found.sort()
            _, court, name = found[-1]
            crown_lines[court] = (line, name)
    for court in COURTS:
        if court not in crown_lines:
            continue
        line, name = crown_lines[court]
        entry = {
            "court": court,
            "name": name,
            "grand_king": court == "Regime",
            "wins": None, "losses": None, "priced": None,
            "pnl_m": None, "roi_pct": None, "set_size": None,
            "dog_line": None, "stamp": None, "crowned_on": None,
        }
        start = line.find(name)
        rec = RECORD_RE.search(line, start)
        if rec:
            entry["wins"] = int(rec.group(1))
            entry["losses"] = int(rec.group(2))
            entry["priced"] = int(rec.group(3))
            entry["pnl_m"] = num(rec.group(4))
            # The ROI printed in the state line can carry typos (the G415
            # line prints the superseded +63.17% beside the corrected
            # +63.22%); the defensible figure is computed from the parsed
            # P&L over flat-25m stakes: roi = pnl / (priced x 25).
            if entry["pnl_m"] is not None and entry["priced"]:
                entry["roi_pct"] = round(
                    entry["pnl_m"] / (entry["priced"] * 25) * 100, 2)
            else:
                entry["roi_pct"] = num(rec.group(5))
            rest = line[rec.end():]
            sz = re.search(r"on the (\d+)-game set", rest)
            if sz:
                entry["set_size"] = int(sz.group(1))
            else:  # every game on the set receives a pick: W+L is the set size
                entry["set_size"] = entry["wins"] + entry["losses"]
            dog = DOG_RE.search(rest)
            if dog:
                entry["dog_line"] = {
                    "picks": int(dog.group(1)),
                    "wins": int(dog.group(2)),
                    "losses": int(dog.group(3)),
                    "priced": int(dog.group(4)),
                    "pnl_m": num(dog.group(5)),
                    "roi_pct": num(dog.group(6)),
                }
            stamp = STAMP_RE.search(line, start)
            if stamp:
                entry["stamp"] = stamp.group(1)
        # crown date: the engine-backup stamp in the crown line is exact
        # (engine.py.bak-discovery-YYYYMMDD-HHMM); else a "(loop, DATE)" tag
        # near the line start; else leave null — later dates in the line are
        # usually the vault opening, NOT the crown date.
        bak = re.search(r"bak-discovery-(\d{4})(\d{2})(\d{2})", line)
        if bak:
            entry["crowned_on"] = f"{bak.group(1)}-{bak.group(2)}-{bak.group(3)}"
        else:
            d = DATE_RE.search(line[:200])
            if d:
                entry["crowned_on"] = d.group(1)
        kings[court] = entry
    return [kings[c] for c in COURTS]


# --------------------------------------------------------------------------
# loss-autopsy challengers — from loss_autopsy_log.md candidate tables
# --------------------------------------------------------------------------

AUTOPSY_MECHANISMS = {
    "L&S-CONSENSUS": ("When the grand king picks the underdog but Legacy and the "
                      "Selector both pick the favourite, drop the dog pick and follow them."),
    "LONE-DOG": ("When the grand king picks the underdog and at most one of the other "
                 "four kings agrees with it, switch to the favourite."),
}

AUTOPSY_ROW_RE = re.compile(
    r"\|\s*(AUTOPSY-\d+)\s+([A-Z0-9&\-\(\)≤→ ]+?)\s*(?:selector|veto|VETO)?\s*\|"
    r"\s*\d+\s*\|\s*\*\*(\d+)-(\d+),\s*\+([\d,]+\.\d+)m,\s*\+([\d.]+)%\*\*")


def parse_autopsy_challengers():
    text = read_text(os.path.join(BACKTEST, "loss_autopsy_log.md"))
    out = []
    if text is None:
        return out
    seen = {}
    for m in re.finditer(
            r"\|\s*(AUTOPSY-\d+)\s+([^|]{3,80}?)\|\s*\d+\s*\|\s*"
            r"\*\*(\d+)-(\d+),\s*\+([\d,]+\.\d+)m,\s*\+([\d.]+)%\*\*", text):
        cid, raw_name = m.group(1), clean(m.group(2))
        if "GATE-CLEARING" not in text[m.end():m.end() + 400]:
            continue
        mech = None
        for key, words in AUTOPSY_MECHANISMS.items():
            if key in raw_name.upper():
                mech = words
                break
        seen[cid] = {
            "id": cid,
            "name": raw_name,
            "mechanism": mech or raw_name,
            "wins": int(m.group(3)),
            "losses": int(m.group(4)),
            "pnl_m": num(m.group(5)),
            "roi_pct": num(m.group(6)),
            "status": "gate-clearing challenger (not crowned)",
        }
    return [seen[k] for k in sorted(seen)]


# --------------------------------------------------------------------------
# activity feed — most recent dated sections across all loop logs
# --------------------------------------------------------------------------

def split_sections(text):
    """Split markdown on level-2 headers; '## GENERATION VERDICT' blocks are
    merged into the generation section above them."""
    sections, cur = [], None
    for line in text.split("\n"):
        if line.startswith("## "):
            if "GENERATION VERDICT" in line and cur is not None:
                cur[1].append(line)
                continue
            if cur is not None:
                sections.append(cur)
            cur = [line[3:].strip(), []]
        elif cur is not None:
            cur[1].append(line)
    if cur is not None:
        sections.append(cur)
    return [(h, "\n".join(body)) for h, body in sections]


def first_paragraph(body, start=0):
    chunk = body[start:]
    for para in re.split(r"\n\s*\n", chunk):
        para = clean(para)
        if len(para) >= 25:
            return para
    return ""


def summarise(agent, header, body):
    """One short, factual line for a log section."""
    if agent in COURTS or agent == "Vanguard":
        v = re.search(r"GENERATION VERDICT[^\n]*\n+(.{40,}?)(?:\n\s*\n|$)", body, re.S)
        if v:
            return trunc140(v.group(1))
        m = re.search(r"(NO NEW KING[^\n]{20,200}|NEW \w+ KING CROWNED[^\n]{0,160})", body)
        if m:
            return trunc140(m.group(1))
    if agent == "Discovery":
        v = re.search(r"OUTCOME[^\n]*\n+(.{40,}?)(?:\n\s*\n|$)", body, re.S)
        if v:
            para = clean(v.group(1))
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for i, s in enumerate(sentences):
                if "NO CROWN" in s.upper() or "CROWNED" in s.upper():
                    return trunc140(" ".join(sentences[i:i + 2]))
            return trunc140(para)
        m = re.search(r"(NO CROWN[^\n]{0,160}|NEW \w+ KING CROWNED[^\n]{0,160})", body)
        if m:
            return trunc140(m.group(1))
    if agent == "Arena":
        v = re.search(r"VERDICT:\s*(.{40,}?)(?:\n\s*\n|$)", body, re.S)
        if v:
            return trunc140("Verdict: " + v.group(1))
    if agent == "Edge Finder":
        if "FILING" in header.upper():
            return trunc140("Filed " + clean(header).replace("ARENA FILING — ", ""))
        v = re.search(r"###\s*5\.\s*DISPOSITION\s*\n+(.{40,}?)(?:\n\s*\n|$)", body, re.S)
        if v:
            return trunc140(v.group(1))
    if agent == "Loss Autopsy":
        m = re.search(r"(AUTOPSY-\d+ [^|\n]{10,120}GATE-CLEARING CHALLENGER[^\n]{0,60})", body)
        if m:
            return trunc140(m.group(1))
        v = re.search(r"What Cycle \d+ actually found\s*\n+(.{40,}?)(?:\n\s*\n|$)", body, re.S)
        if v:
            return trunc140(v.group(1))
    return trunc140(first_paragraph(body) or header)


def event_kind(header, summary):
    blob = (header + " " + summary).upper()
    if "KING CROWNED" in blob:
        return "crown"
    if "GATE-REFUSED" in blob or "GATE-REFUSAL" in blob:
        return "gate-refusal"
    if "CHALLENGER" in blob:
        return "challenger"
    if "FILING" in blob or "FILED" in blob:
        return "filing"
    if "GRADED" in blob:
        return "grading"
    return "generation"


HEADER_PATTERNS = {
    "Legacy": r"^(Legacy\s+)?R4-G\d+",
    "Regime": r"^(Regime\s+)?R4-G\d+",
    "Quantum": r"^(Quantum\s+)?R4-G\d+",
    "Selector": r"^(SELECTOR\s+)?R4-G\d+",
    "Vanguard": r"^(Vanguard\s+)?R4-G\d+",
    "Arena": r"^Arena G\d+",
    "Discovery": r"^CYCLE \d+",
    "Edge Finder": r"^(CYCLE 2026|ARENA FILING)",
    "Loss Autopsy": r"^CYCLE \d+ —",
    "Stakehunter": r"^CYCLE \d+ —",
}


def generation_dates():
    """Map 'R4-G411' / 'Arena G143' -> date, from the state file's dated
    one-line run entries (court log sections themselves carry no run date)."""
    text = read_text(os.path.join(BACKTEST, "mutation_loop_state.md"),
                     tail_bytes=1_500_000)
    dates = {}
    if text is None:
        return dates
    for m in re.finditer(r"(R4-G\d+|Arena G\d+)[^\n]{0,80}?\(loop, (\d{4}-\d{2}-\d{2})\)", text):
        dates[m.group(1)] = m.group(2)
    for m in re.finditer(r"(R4-G\d+|Arena G\d+)[^\n]{0,80}?\((\d{4}-\d{2}-\d{2}), loop\)", text):
        dates.setdefault(m.group(1), m.group(2))
    return dates


def parse_activity(limit=30, per_log=6):
    gen_dates = generation_dates()
    events, seen = [], set()
    seq = 0
    for agent, fname in LOG_FILES.items():
        path = os.path.join(BACKTEST, fname)
        text = read_text(path, tail_bytes=900_000)
        if text is None:
            continue
        fallback = file_mtime_iso(path)
        # A missing header pattern must degrade to "no events", never raise.
        pat = re.compile(HEADER_PATTERNS.get(agent, r"^$"))
        picked = [(h, b) for h, b in split_sections(text) if pat.search(h)]
        for header, body in picked[-per_log:]:
            summary = summarise(agent, header, body)
            if not summary:
                continue
            title = trunc140(header)
            if (agent, title) in seen:
                continue
            seen.add((agent, title))
            # timestamp: a real clock time in the header wins; then the
            # state file's date for this generation; then a header date;
            # then the log's mtime. Date-only stamps sit at 00:00.
            at = None
            hm = DATE_RE.search(header)
            if hm and hm.group(2):
                at = f"{hm.group(1)}T{hm.group(2)}:{hm.group(3)}:00-06:00"
            if at is None:
                gid = re.search(r"(R4-G\d+|Arena G\d+)", header)
                if gid and gid.group(1) in gen_dates:
                    at = f"{gen_dates[gid.group(1)]}T00:00:00-06:00"
                elif hm:
                    at = f"{hm.group(1)}T00:00:00-06:00"
                else:
                    at = fallback
            seq += 1
            events.append({
                "agent": agent,
                "kind": event_kind(header, summary),
                "title": title,
                "summary": summary,
                "at": at,
                "_seq": seq,
            })
    events.sort(key=lambda e: (e["at"] or "", e["_seq"]), reverse=True)
    for e in events:
        e.pop("_seq", None)
    return events[:limit]


# --------------------------------------------------------------------------
# slips — status + bet layer from the trackers
# --------------------------------------------------------------------------

def _grab(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return m if m else None


def parse_slip(number, relpath):
    text = read_text(os.path.join(BACKTEST, relpath))
    slip = {
        "number": number, "status": None, "games": None,
        "grand_pick": None, "confidence_bet": None,
        "underdog_desk": None, "draw_desk": None,
        "results": None, "user_bets": None,
    }
    if text is None:
        return slip
    first_line = text.split("\n", 1)[0]
    counts = [int(x) for x in re.findall(r"\((\d+)(?:\s*new)?\s*games?\)", first_line)]
    if not counts:
        counts = [int(x) for x in re.findall(r"\((\d+)\)", first_line)]
    if counts:
        slip["games"] = sum(counts)
    m = _grab(r"\*\*STATUS: ([A-Z][A-Z\-]+)", text)
    if m:
        slip["status"] = m.group(1)

    m = _grab(r"GRAND PICK — BET: ([^,]+), small (\d+)m @ ([\d.]+)", text)
    if m:
        slip["grand_pick"] = {"selection": m.group(1).strip(),
                              "stake_m": int(m.group(2)), "price": float(m.group(3))}
    m = _grab(r"CONFIDENCE BET \(v1\.1[^)]*\): ([^,]+), (\d+)m @ ([\d.]+)", text)
    if m:
        slip["confidence_bet"] = {"selection": m.group(1).strip(),
                                  "stake_m": int(m.group(2)), "price": float(m.group(3))}
    m = _grab(r"UNDERDOG DESK #(\d+) \(paper[^)]*\): ([^@]+)@ ([\d.]+)", text)
    if m:
        slip["underdog_desk"] = {"pick_no": int(m.group(1)),
                                 "selection": m.group(2).strip(),
                                 "price": float(m.group(3)), "paper": True}
    m = _grab(r"DRAW DESK #(\d+) \(paper[^)]*\): ([^@]+)@ ([\d.]+)", text)
    if m:
        slip["draw_desk"] = {"pick_no": int(m.group(1)),
                             "selection": m.group(2).strip(),
                             "price": float(m.group(3)), "paper": True}

    # graded results (only from the Results part of the tracker)
    ridx = text.find("## Results")
    graded_part = text[ridx:] if ridx != -1 else ""
    results = {}
    if graded_part:
        m = _grab(r"GRAND PICK / chosen bet: .*?— (WON|LOST)\.?\*\*.*?\*\*([+\-−][\d.]+)m",
                  graded_part, re.S)
        if m:
            results["grand_pick"] = {"result": m.group(1), "pnl_m": money(m.group(2))}
        m = _grab(r"CONFIDENCE BET \(v1\.1[^)]*\): .*?— (WON|LOST)\.?\*\*.*?\*\*([+\-−][\d.]+)m",
                  graded_part, re.S)
        if m:
            results["confidence_bet"] = {"result": m.group(1), "pnl_m": money(m.group(2))}
        m = _grab(r"UNDERDOG DESK #\d+ \(paper\): .*?— (WON|LOST)\.?\*\*.*?\*\*([+\-−][\d.]+)m",
                  graded_part, re.S)
        if m:
            results["underdog_desk"] = {"result": m.group(1), "pnl_m": money(m.group(2))}
        m = _grab(r"DRAW DESK #\d+ \(paper\): .*?— (WON|LOST)\.?\*\*.*?([+\-−][\d.]+)m paper",
                  graded_part, re.S)
        if m:
            results["draw_desk"] = {"result": m.group(1), "pnl_m": money(m.group(2))}
        king_records = {}
        best = None
        for km in re.finditer(
                r"\*\*(Legacy|Regime|Quantum|Selector|Vanguard) \([^)]*\)(?:\*\*)?"
                r"[^\n]*?[—→] (?:\*\*)?(\d+)-(\d+), ([+\-−][\d.]+)m paper\.?\*\*", graded_part):
            king_records[km.group(1)] = {
                "wins": int(km.group(2)), "losses": int(km.group(3)),
                "pnl_m": money(km.group(4))}
            line_end = graded_part.find("\n", km.start())
            if "best line" in graded_part[km.start():line_end + 400]:
                best = km.group(1)
        if king_records:
            results["king_records"] = king_records
        if best:
            results["best_king"] = best
    if slip["status"] == "PART-GRADED":
        ids = set(re.findall(
            r"^\| (S\d+-\d+) \|[^|]*\d+[-–]\d+[^|]*\|(?:[^|]*\|)*?\s*\*\*(?:WON|LOST)\*\*",
            text, re.M))
        results["graded_games"] = len(ids) if ids else None
    if results:
        slip["results"] = results

    # the user's own bets (Slip #35 section)
    uidx = text.find("User's own bets")
    if uidx != -1:
        bets = []
        for line in text[uidx:].split("\n")[1:]:
            if not line.startswith("- "):
                if bets:
                    break
                continue
            m = re.match(r"- ([^—]+) — (?:(\d+)m|amount NOT stated[^@]*) @ ([\d.]+)", line)
            if m:
                bets.append({"selection": m.group(1).strip(),
                             "stake_m": int(m.group(2)) if m.group(2) else None,
                             "price": float(m.group(3))})
        if bets:
            slip["user_bets"] = bets

    # slips locked in the JSON bet-layer form record the same headline
    # calls in the lock file; fill whatever the tracker regexes above
    # did not find (never overwrite a tracker-read value).
    json_layer, _json_ledger, _json_fires = parse_json_bet_layer(number)
    gp = json_layer.get("GRAND PICK")
    if slip["grand_pick"] is None and gp:
        slip["grand_pick"] = {"selection": gp["selection"],
                              "stake_m": gp["stake_m"],
                              "price": gp["price"]}
    ud = json_layer.get("UNDERDOG DESK")
    if slip["underdog_desk"] is None and ud:
        slip["underdog_desk"] = {"pick_no": ud["pick_no"],
                                 "selection": ud["selection"],
                                 "price": ud["price"], "paper": True}
    dd = json_layer.get("DRAW DESK")
    if slip["draw_desk"] is None and dd:
        slip["draw_desk"] = {"pick_no": dd["pick_no"],
                             "selection": dd["selection"],
                             "price": dd["price"], "paper": True}
    return slip


# --------------------------------------------------------------------------
# bets board + bankroll — games tables, results tables, bet layer, user bets
# --------------------------------------------------------------------------

BET_KIND_ORDER = ["GRAND PICK", "CONFIDENCE", "UNDERDOG DESK", "DRAW DESK", "USER BET"]
RESULT_KEY = {"GRAND PICK": "grand_pick", "CONFIDENCE": "confidence_bet",
              "UNDERDOG DESK": "underdog_desk", "DRAW DESK": "draw_desk"}
TIME_CELL_RE = re.compile(r"^\d{1,2}:\d{2}$")
SCORE_RE = re.compile(r"\d+\s*[-–]\s*\d+")


def load_bankroll_config():
    """Reset-bankroll config; created with a null start if absent (the user
    sets the amount later — null is handled everywhere downstream)."""
    try:
        with open(BANKROLL_CONFIG, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict) and cfg.get("reset_date"):
            cfg.setdefault("start_m", None)
            return cfg
    except (OSError, ValueError):
        pass
    cfg = dict(DEFAULT_BANKROLL_CONFIG)
    try:
        with open(BANKROLL_CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass
    return cfg


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_games_table(text):
    """game_id -> {game, time, league, pick, bet_call} from the frozen-prices
    table (rows whose second cell is a HH:MM tip time; results-table rows are
    excluded by that same test). The card pick is the table's bold cell — it
    precedes the bet-call cell, which may also be bold."""
    games = {}
    if not text:
        return games
    for line in text.split("\n"):
        if not line.startswith("| S"):
            continue
        cells = _cells(line)
        if (len(cells) >= 5 and re.match(r"^S\d+-\d+$", cells[0])
                and TIME_CELL_RE.match(cells[1])):
            pick = None
            for c in cells:
                if "**" in c:
                    pick = clean(c)
                    break
            games[cells[0]] = {"game": clean(cells[3]), "time": cells[1],
                               "league": clean(cells[2]), "pick": pick,
                               "bet_call": clean(cells[-1])}
    return games


SPORT_WORDS = ["Basketball", "Soccer", "Tennis", "Handball", "Volleyball",
               "Baseball", "Ice Hockey", "Badminton"]


def parse_slip_json_games(number, tracker_text):
    """game_id -> {league, pick, price, sport} from the locked slip JSONs.

    Sport comes from recorded data only: a dedicated *_soccer JSON marks its
    games Soccer; every other game takes the sport named in the tracker
    title (e.g. '# SLIP #34 — Basketball (16 new games)'). The card pick is
    the grand king's (Regime) frozen pick; its price is the recorded
    moneyline for that side (home / away / draw), exactly as frozen —
    model-only games carry no moneyline and get price None."""
    out = {}
    title_sports = []
    if tracker_text:
        first = tracker_text.split("\n", 1)[0]
        title_sports = [s for s in SPORT_WORDS if s in first]
    non_soccer = [s for s in title_sports if s != "Soccer"]
    default_sport = non_soccer[0] if non_soccer else (
        title_sports[0] if title_sports else None)
    for rel in SLIP_JSONS.get(number, []):
        try:
            with open(os.path.join(BACKTEST, "data_core", rel),
                      encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        sport = "Soccer" if "_soccer" in os.path.basename(rel) else default_sport
        games = data.get("games", []) if isinstance(data, dict) else []
        for g in games:
            gid = g.get("game_id")
            if not gid:
                continue
            pick = ((g.get("picks") or {}).get("regime") or {}).get("pick")
            price = None
            if pick:
                if pick == g.get("home"):
                    price = g.get("moneyline_home")
                elif pick == g.get("away"):
                    price = g.get("moneyline_away")
                elif pick.strip().upper() == "DRAW":
                    price = g.get("draw_price")
            home, away = g.get("home"), g.get("away")
            ko_cst = g.get("kickoff_cst") or ""
            time = ko_cst.split()[0] if ko_cst else None
            # a game that records its own sport (Slip #37's mixed lock
            # file is the first) uses it; every earlier slip's games
            # carry no sport key and keep the file-level sport above.
            gsport = g.get("sport")
            game_sport = gsport.capitalize() \
                if isinstance(gsport, str) and gsport else sport
            out[gid] = {"league": g.get("league"), "pick": pick,
                        "price": price, "sport": game_sport,
                        "home": home, "away": away,
                        "game": (f"{home} v {away}"
                                 if home and away else None),
                        "time": time}
    return out


KING_KEYS = [("legacy", "Legacy"), ("regime", "Regime"), ("quantum", "Quantum")]


def parse_game_detail(number, bet_gids=()):
    """game_id -> frozen detail for the card/detail layer, read ONLY from
    the locked slip JSONs (never invented): both moneyline prices, the
    de-vig split + overround, all five kings' picks with the price each
    took and a short reason, the totals line/projection where frozen,
    and — for games carrying a called bet — the frozen bet_call text
    (the named gap / screen numbers, exactly as locked)."""
    out = {}
    for rel in SLIP_JSONS.get(number, []):
        try:
            with open(os.path.join(BACKTEST, "data_core", rel),
                      encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        games = data.get("games", []) if isinstance(data, dict) else []
        for g in games:
            gid = g.get("game_id")
            if not gid:
                continue
            home, away = g.get("home"), g.get("away")
            prices = {"home": g.get("moneyline_home"),
                      "away": g.get("moneyline_away"),
                      "draw": g.get("draw_price")}
            defig = g.get("_devig") or {}
            devig = {"home": defig.get("home"), "away": defig.get("away")}
            if defig.get("draw") is not None:
                devig["draw"] = defig.get("draw")

            def side_price(pick):
                if not pick:
                    return None
                if pick == home:
                    return prices["home"]
                if pick == away:
                    return prices["away"]
                if pick.strip().upper() == "DRAW":
                    return prices["draw"]
                return None

            kings = []
            picks = g.get("picks") or {}
            for key, court in KING_KEYS:
                p = picks.get(key) or {}
                if p.get("pick"):
                    kings.append({"court": court, "pick": p["pick"],
                                  "price": side_price(p["pick"]),
                                  "reason": trunc140(p.get("reason")) or None})
            for prefix, court in (("selector_", "Selector"),
                                  ("vanguard_", "Vanguard")):
                for k, v in g.items():
                    if (k.startswith(prefix) and isinstance(v, dict)
                            and v.get("pick")):
                        kings.append({"court": court, "pick": v["pick"],
                                      "price": side_price(v["pick"]),
                                      "reason": trunc140(v.get("reason")) or None})
                        break
            totals = None
            if g.get("totals_line") is not None:
                totals = {"line": g.get("totals_line"),
                          "projection": g.get("totals_projection")}
            bet_call = clean(g.get("bet_call") or "")
            keep_call = gid in bet_gids or "BET —" in bet_call \
                or "CONFIDENCE" in bet_call or "DESK" in bet_call
            out[gid] = {
                "home": home, "away": away, "prices": prices,
                "devig_pct": devig,
                "overround_pct": g.get("overround_pct"),
                "kings": kings, "totals": totals,
                "bet_call": (bet_call[:900] if keep_call and bet_call else None),
            }
    return out


# --------------------------------------------------------------------------
# live overlay — ESPN scoreboards for UNGRADED slips (display only)
# --------------------------------------------------------------------------
# Official results come only from two-source grading; this overlay is a
# display layer. A "final" here is marked official: false everywhere it
# lands. FAIL-SOFT by construction: any network/parse error yields no
# overlay for that board, never a broken export.

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SOCCER_SLUGS = [
    ("BRAZIL", "bra.1"), ("CHAMPIONS LEAGUE", "uefa.champions"),
    ("EUROPA LEAGUE", "uefa.europa"), ("PREMIER LEAGUE", "eng.1"),
    ("CHAMPIONSHIP", "eng.2"), ("LA LIGA", "esp.1"),
    ("BUNDESLIGA", "ger.1"), ("SERIE A", "ita.1"), ("LIGUE 1", "fra.1"),
    ("EREDIVISIE", "ned.1"), ("PRIMEIRA", "por.1"), ("LIGA MX", "mex.1"),
    ("SCOTTISH", "sco.1"), ("MLS", "usa.1"),
]

try:
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    EASTERN = timezone(timedelta(hours=-5))


def _norm_team(name):
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _espn_board(sport, league):
    lg = (league or "").strip().upper()
    if sport == "Basketball":
        if lg == "NBA" or lg.startswith("NBA "):
            return "basketball/nba"
        if "NCAAB" in lg or lg == "NCAA":
            return "basketball/mens-college-basketball"
    elif sport == "Soccer":
        for kw, slug in SOCCER_SLUGS:
            if kw in lg:
                return f"soccer/{slug}"
    return None


def _espn_fetch(board, date_str, cache):
    """(board, ET date) -> {frozenset(norm pair): event dict | 'AMBIG'}.
    Cached per run; None on any failure (fail-soft)."""
    key = (board, date_str)
    if key in cache:
        return cache[key]
    url = f"{ESPN_BASE}/{board}/scoreboard?dates={date_str}"
    if "mens-college-basketball" in board:
        url += "&groups=50&limit=500"
    index = None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (slip-select exporter)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.load(resp)
        index = {}
        for ev in data.get("events") or []:
            comps = ev.get("competitions") or []
            if not comps:
                continue
            teams = {}
            for c in comps[0].get("competitors") or []:
                nm = _norm_team((c.get("team") or {}).get("displayName"))
                if nm:
                    teams[nm] = c.get("score")
            if len(teams) != 2:
                continue
            pair = frozenset(teams)
            status = ev.get("status") or {}
            entry = {"teams": teams,
                     "state": (status.get("type") or {}).get("state"),
                     "clock": status.get("displayClock"),
                     "period": status.get("period"),
                     "detail": (status.get("type") or {}).get("shortDetail")}
            index[pair] = "AMBIG" if pair in index else entry
    except Exception:
        index = None
    cache[key] = index
    return index


def build_live_overlays(slips, contexts):
    """{slip_no: {game_id: live}} for slips not yet graded. Matching is by
    the normalised team PAIR; an ambiguous pair attaches NOTHING rather
    than a wrong score."""
    overlays = {}
    cache = {}
    fetched_at = regina_iso(datetime.now(tz=timezone.utc))
    for slip in slips:
        if slip.get("status") not in ("LOCKED", "PART-GRADED"):
            continue
        n = slip["number"]
        ctx = contexts.get(n) or {}
        per_game = {}
        for gid, jg in (ctx.get("json_games") or {}).items():
            board = _espn_board(jg.get("sport"), jg.get("league"))
            tip = (ctx.get("tips") or {}).get(gid)
            if not board or tip is None or not jg.get("home") or not jg.get("away"):
                continue
            date_str = tip.astimezone(EASTERN).strftime("%Y%m%d")
            index = _espn_fetch(board, date_str, cache)
            if not index:
                continue
            entry = index.get(frozenset({_norm_team(jg["home"]),
                                         _norm_team(jg["away"])}))
            if not entry or entry == "AMBIG":
                continue
            state = {"pre": "scheduled", "in": "in",
                     "post": "final"}.get(entry["state"])
            if state is None:
                continue

            def score(team):
                if state == "scheduled":
                    return None
                raw = entry["teams"].get(_norm_team(team))
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None

            per_game[gid] = {
                "state": state,
                "home_score": score(jg["home"]),
                "away_score": score(jg["away"]),
                "clock": entry["clock"] if state == "in" else None,
                "period": entry["period"] if state != "scheduled" else None,
                "detail": entry["detail"],
                "fetched_at": fetched_at,
                "source": "ESPN",
                "official": False,
            }
        if per_game:
            overlays[n] = per_game
    return overlays


def _display_fields(live):
    """Additive DISPLAY-ONLY fields derived from the ESPN live overlay:
    display_state mirrors the overlay state (scheduled|in|final) and
    display_final_score is the overlay's full-time score as 'H-A', set
    only when the overlay says final. These NEVER touch settlement —
    result / pnl_m / final_score belong to the two-source grade alone;
    the page uses the display pair to show a finished game as finished
    (marked unofficial) while the grade is still pending."""
    if not live:
        return {"display_state": None, "display_final_score": None}
    score = None
    if live.get("state") == "final" \
            and live.get("home_score") is not None \
            and live.get("away_score") is not None:
        score = f"{live['home_score']}-{live['away_score']}"
    return {"display_state": live.get("state"),
            "display_final_score": score}


def parse_results_table(text):
    """game_id -> winner text, for games with a graded full-time score.
    PENDING rows are excluded first — one of them quotes a prior meeting's
    score in its explanation text."""
    graded = {}
    if not text:
        return graded
    for line in text.split("\n"):
        if not line.startswith("| S"):
            continue
        cells = _cells(line)
        if len(cells) < 4 or not re.match(r"^S\d+-\d+$", cells[0]):
            continue
        status_cell, winner = cells[1], clean(cells[2])
        if "PENDING" in status_cell.upper() or not SCORE_RE.search(status_cell):
            continue
        if winner and winner != "—":
            graded[cells[0]] = winner
    return graded


def parse_final_scores(text):
    """game_id -> final score string (e.g. '54-83'), taken from the graded
    results table's fixture cell ('TeamA 54-83 TeamB'). Same discipline as
    parse_results_table: PENDING rows are excluded first (one of them quotes
    a prior meeting's score), and a row only counts when it carries both a
    score and a graded winner. Ungraded games get no entry — never invented."""
    scores = {}
    if not text:
        return scores
    for line in text.split("\n"):
        if not line.startswith("| S"):
            continue
        cells = _cells(line)
        if len(cells) < 4 or not re.match(r"^S\d+-\d+$", cells[0]):
            continue
        fixture_cell, winner = cells[1], clean(cells[2])
        if "PENDING" in fixture_cell.upper():
            continue
        m = SCORE_RE.search(fixture_cell)
        if not m or not winner or winner == "—":
            continue
        scores[cells[0]] = re.sub(r"\s+", "", m.group(0)).replace("–", "-")
    return scores


def parse_tip_times(number):
    """game_id -> aware datetime (UTC) from the slip JSONs' kickoff_utc."""
    tips = {}
    for rel in SLIP_JSONS.get(number, []):
        try:
            with open(os.path.join(BACKTEST, "data_core", rel),
                      encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        games = data.get("games", []) if isinstance(data, dict) else []
        for g in games:
            gid, ko = g.get("game_id"), g.get("kickoff_utc")
            if gid and ko:
                for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ",
                            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                    try:
                        tips[gid] = datetime.strptime(
                            ko, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
    return tips


def parse_json_bet_layer(number):
    """(bet layer, ledger bets) recorded in the locked slip JSON's own
    bet_layer block. Slip #37 is the first slip locked in this form:
    its tracker announces the calls in prose the tracker regexes do
    not read, while the lock file records them structured — the grand
    pick as its own called bet, the two desk entries PAPER, and every
    game flagged ledger_bet at its recorded flat stake (the user's
    every-pick rule). Slips whose JSONs carry no bet_layer block —
    every slip before #37 — return empty structures and are
    completely unaffected. Prices are the recorded moneylines for the
    card pick's side, computed exactly as in parse_slip_json_games."""
    layer, ledger_bets, json_fires = {}, [], []
    for rel in SLIP_JSONS.get(number, []):
        try:
            with open(os.path.join(BACKTEST, "data_core", rel),
                      encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        bl = data.get("bet_layer") or {}
        gp = bl.get("grand_pick") or {}
        if gp.get("team") and "GRAND PICK" not in layer:
            layer["GRAND PICK"] = {
                "selection": gp["team"], "stake_m": gp.get("stake_m"),
                "price": gp.get("price"), "game_id": gp.get("game_id")}
        ud = bl.get("underdog_desk") or {}
        if ud.get("team") and "UNDERDOG DESK" not in layer:
            layer["UNDERDOG DESK"] = {
                "selection": ud["team"], "stake_m": 25,
                "price": ud.get("price"), "pick_no": ud.get("entry"),
                "game_id": ud.get("game_id")}
        dd = bl.get("draw_desk") or {}
        if dd.get("pick") and "DRAW DESK" not in layer:
            layer["DRAW DESK"] = {
                "selection": dd["pick"], "stake_m": 25,
                "price": dd.get("price"), "pick_no": dd.get("entry"),
                "game_id": dd.get("game_id")}
        for f in bl.get("confidence_fires") or []:
            if f.get("team"):
                json_fires.append({
                    "selection": f["team"],
                    "stake_m": f.get("stake_m") or 25,
                    "price": f.get("price"),
                    "game_id": f.get("game_id")})
        default_stake = (bl.get("ledger_bets") or {}).get("stake_m_each")
        for g in data.get("games", []):
            if not g.get("ledger_bet"):
                continue
            pick = ((g.get("picks") or {}).get("regime") or {}).get("pick")
            price = None
            if pick:
                if pick == g.get("home"):
                    price = g.get("moneyline_home")
                elif pick == g.get("away"):
                    price = g.get("moneyline_away")
                elif pick.strip().upper() == "DRAW":
                    price = g.get("draw_price")
            ledger_bets.append({
                "selection": pick,
                "stake_m": g.get("stake_m") or default_stake,
                "price": price, "game_id": g.get("game_id")})
    return layer, ledger_bets, json_fires


def parse_bet_layer(text, games_table):
    """Called bets from the tracker bet-layer section:
    kind -> {selection, stake_m, price, game_id}. Desk stakes are the flat
    25m paper stake by the standing desk rule."""
    layer = {}
    if not text:
        return layer

    def gid_after(pattern):
        m = re.search(pattern, text)
        return m.group(1) if m else None

    def gid_fallback(*tokens):
        for token in tokens:
            for gid, g in games_table.items():
                if token in g["bet_call"].upper():
                    return gid
        return None

    m = re.search(r"GRAND PICK — BET: ([^,]+), small (\d+)m @ ([\d.]+)", text)
    if m:
        layer["GRAND PICK"] = {
            "selection": m.group(1).strip(), "stake_m": int(m.group(2)),
            "price": float(m.group(3)),
            "game_id": gid_after(r"GRAND PICK — BET: [^@]+@ [\d.]+\*\* \((S\d+-\d+)\)")
                       or gid_fallback("GRAND PICK")}
    m = re.search(r"CONFIDENCE BET \(v1\.1[^)]*\): ([^,]+), (\d+)m @ ([\d.]+)", text)
    if m:
        layer["CONFIDENCE"] = {
            "selection": m.group(1).strip(), "stake_m": int(m.group(2)),
            "price": float(m.group(3)),
            "game_id": gid_after(r"CONFIDENCE BET \(v1\.1[^)]*\): [^@]+@ [\d.]+\*\* \((S\d+-\d+)\)")
                       or gid_fallback("CONFIDENCE BET")}
    # EVERY confidence fire on the slip, in tracker order — a slip can
    # fire the v1.1 rule several times (Slip #36 fires it five times) and
    # each fire is its own called bet at its own called stake. The
    # headline CONFIDENCE entry above stays the first fire.
    fires, seen_fires = [], set()
    for fm in re.finditer(
            r"CONFIDENCE BET \(v1\.1[^)]*\): ([^,]+), (\d+)m @ ([\d.]+)"
            r"(?:\*\*)? \((S\d+-\d+)\)", text):
        key = (fm.group(4), fm.group(1).strip(), fm.group(3))
        if key in seen_fires:
            continue
        seen_fires.add(key)
        fires.append({"selection": fm.group(1).strip(),
                      "stake_m": int(fm.group(2)),
                      "price": float(fm.group(3)),
                      "game_id": fm.group(4)})
    if not fires and "CONFIDENCE" in layer:
        c = layer["CONFIDENCE"]
        fires = [{"selection": c["selection"], "stake_m": c["stake_m"],
                  "price": c["price"], "game_id": c.get("game_id")}]
    if fires:
        layer["CONFIDENCE_FIRES"] = fires
    m = re.search(r"UNDERDOG DESK #(\d+) \(paper[^)]*\): ([^@]+)@ ([\d.]+)", text)
    if m:
        layer["UNDERDOG DESK"] = {
            "selection": m.group(2).strip(), "stake_m": 25,
            "price": float(m.group(3)), "pick_no": int(m.group(1)),
            "game_id": gid_after(r"UNDERDOG DESK #\d+ \(paper[^)]*\)[^@]+@ [\d.]+\*\* \((S\d+-\d+)\)")
                       or gid_fallback(f"DESK #{m.group(1)}", "UNDERDOG DESK")}
    m = re.search(r"DRAW DESK #(\d+) \(paper[^)]*\): ([^@]+)@ ([\d.]+)", text)
    if m:
        layer["DRAW DESK"] = {
            "selection": "Draw", "stake_m": 25,
            "price": float(m.group(3)), "pick_no": int(m.group(1)),
            "game_id": gid_after(r"DRAW DESK #\d+ \(paper[^)]*\)[^@]+@ [\d.]+\*\* \((S\d+-\d+)\)")
                       or gid_fallback(f"DRAW DESK #{m.group(1)}", "DRAW DESK")}
    return layer


def parse_user_bets(text):
    """The user's own reported bets: [{selection, stake_m, price, game_id}].
    Trackers with 'None reported on this slip' yield an empty list."""
    if not text:
        return []
    idx = -1
    for marker in ("User's own bets", "USER-PLACED BETS"):
        idx = text.find(marker)
        if idx != -1:
            break
    if idx == -1:
        return []
    bets = []
    for line in text[idx:].split("\n")[1:]:
        if line.startswith("## ") or line.startswith("### "):
            break
        m = re.match(r"- (.+?) — (?:(\d+)m|amount NOT stated[^@]*?) @ ([\d.]+) \((S\d+-\d+)\)", line)
        if m:
            bets.append({"selection": m.group(1).strip(),
                         "stake_m": int(m.group(2)) if m.group(2) else None,
                         "price": float(m.group(3)), "game_id": m.group(4)})
    return bets


def slip_context(number, relpath):
    text = read_text(os.path.join(BACKTEST, relpath))
    ctx = {"text": text, "games_table": {}, "graded": {}, "scores": {},
           "tips": {}, "bet_layer": {}, "user_bets": [], "date": None,
           "json_games": {}, "ledger_bets": []}
    if text is None:
        return ctx
    ctx["games_table"] = parse_games_table(text)
    ctx["json_games"] = parse_slip_json_games(number, text)
    ctx["graded"] = parse_results_table(text)
    ctx["scores"] = parse_final_scores(text)
    ctx["bet_layer"] = parse_bet_layer(text, ctx["games_table"])
    # the multi-fire confidence list rides beside the bet layer (the
    # layer itself stays kind -> bet dict for every other consumer)
    ctx["confidence_fires"] = ctx["bet_layer"].pop("CONFIDENCE_FIRES", [])
    # slips locked in the JSON bet-layer form (Slip #37 onward) record
    # their calls structured in the lock file: fill any kind the
    # tracker parse did not find (the tracker always wins where it
    # speaks) and take the every-pick ledger bets. Slips locked in
    # the tracker form return empty structures here — no change.
    json_layer, json_ledger_bets, json_fires = parse_json_bet_layer(number)
    for kind, bet in json_layer.items():
        ctx["bet_layer"].setdefault(kind, bet)
    ctx["ledger_bets"] = json_ledger_bets
    if not ctx["confidence_fires"] and json_fires:
        ctx["confidence_fires"] = json_fires
    ctx["user_bets"] = parse_user_bets(text)
    bet_gids = {b.get("game_id") for b in ctx["bet_layer"].values()}
    bet_gids |= {b.get("game_id") for b in ctx["user_bets"]}
    bet_gids |= {f.get("game_id") for f in ctx["confidence_fires"]}
    ctx["detail"] = parse_game_detail(number, bet_gids)
    ctx["tips"] = parse_tip_times(number)
    m = DATE_RE.search(text.split("\n", 1)[0])
    if m:
        ctx["date"] = m.group(1)
    else:
        # some tracker headers print the date in words ("Wednesday
        # Nov 18, 2026") instead of ISO — parse that form too, from
        # the same first line, so a slip's date is never silently
        # None (Slip #36's header is the case on file)
        months = {name: i + 1 for i, name in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
        mw = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                       r"[a-z]*\s+(\d{1,2}),\s+(\d{4})",
                       text.split("\n", 1)[0])
        if mw:
            ctx["date"] = (f"{int(mw.group(3)):04d}-"
                           f"{months[mw.group(1)]:02d}-"
                           f"{int(mw.group(2)):02d}")
    return ctx


def pick_won(pick, winner_text):
    """True/False if the graded winner text settles the pick, else None."""
    if not winner_text or not pick:
        return None
    w = winner_text.upper()
    if pick.strip().upper() == "DRAW":
        return "DRAW" in w
    return pick.strip().upper() in w


def _bet_row(slip_no, ctx, slip, kind, selection, stake, price, game_id, paper,
             headline=True):
    game_name = ctx["games_table"].get(game_id, {}).get("game") \
        or ctx["json_games"].get(game_id, {}).get("game")
    tip = ctx["tips"].get(game_id)
    result = None
    # the slip-level graded result names ONE bet per kind (the headline);
    # extra confidence fires settle on their own games below, never by
    # inheriting the headline fire's result.
    res = (slip.get("results") or {}).get(RESULT_KEY.get(kind, "")) or {} \
        if headline else {}
    if res.get("result") in ("WON", "LOST"):
        result = res["result"]
    elif game_id in ctx["graded"]:
        won = pick_won(selection, ctx["graded"][game_id])
        if won is not None:
            result = "WON" if won else "LOST"
    pnl = None
    if result is not None:
        status = "ENDED"
        if stake is not None and price is not None:
            pnl = round(stake * (price - 1), 2) if result == "WON" else -float(stake)
    elif tip is not None:
        status = "UPCOMING" if tip > datetime.now(tz=timezone.utc) else "IN PLAY"
    else:
        status = "IN PLAY"
    return {"slip": slip_no, "game_id": game_id, "game": game_name,
            "pick": selection, "stake_m": stake, "price": price, "kind": kind,
            "paper": paper, "status": status, "result": result, "pnl_m": pnl,
            "fire": None,
            "final_score": ctx["scores"].get(game_id) if game_id else None,
            "tip_at": regina_iso(tip) if tip else None}


def build_bets(slips, contexts):
    rows = []
    for slip in slips:
        n = slip["number"]
        ctx = contexts[n]
        for kind in BET_KIND_ORDER[:-1]:
            if kind == "CONFIDENCE":
                # one row per fire — every fire is its own called bet
                fires = ctx["confidence_fires"] or (
                    [ctx["bet_layer"]["CONFIDENCE"]]
                    if ctx["bet_layer"].get("CONFIDENCE") else [])
                for i, f in enumerate(fires):
                    row = _bet_row(n, ctx, slip, kind, f["selection"],
                                   f["stake_m"], f["price"], f.get("game_id"),
                                   paper=False, headline=(i == 0))
                    row["fire"] = i + 1
                    rows.append(row)
                continue
            b = ctx["bet_layer"].get(kind)
            if not b:
                continue
            rows.append(_bet_row(n, ctx, slip, kind, b["selection"], b["stake_m"],
                                 b["price"], b.get("game_id"),
                                 paper=kind in ("UNDERDOG DESK", "DRAW DESK")))
        for ub in ctx["user_bets"]:
            rows.append(_bet_row(n, ctx, slip, "USER BET", ub["selection"],
                                 ub["stake_m"], ub["price"], ub.get("game_id"),
                                 paper=False))
    return rows


def build_bankroll(slips, contexts):
    """The LIVE bankroll — a tracked ledger of the CHOSEN bets only: the
    grand pick and EVERY confidence fire, each at its called stake as
    recorded in the tracker or the lock file's bet_layer block. (User
    ruling 2026-11-19: the per-game flat-25m ledger_bet rows in the
    lock files are the strategies' measurement layer and never enter
    this ledger.)
    Scope (user directive 2026-11-18, v8 —
    supersedes the original Slip-#36-first scope): every slip DATED
    on/after the reset date in bankroll_config.json — the billion is
    the system's ledger from the reset day itself, so the slips of
    2026-11-18 (Slips #33, #34, #35 graded, #36 pending at the time of
    the change) all count, and future slips join automatically by
    date. The rule is keyed to the slip's date, never to a hardcoded
    slip number. The Underdog Desk is paper by design: it rides on
    the cards but is EXCLUDED here, and the exclusion is listed in
    paper_excluded. A slip's bets sit in pending until the slip's
    tracker is GRADED — the two-source grade is the only settlement
    event; the live overlay never settles the bankroll. Nothing is
    invented: a graded bet whose game cannot be settled from the
    graded record is reported under unsettled_m, never guessed."""
    cfg = load_bankroll_config()
    start = cfg.get("start_m")
    reset = cfg.get("reset_date") or DEFAULT_BANKROLL_CONFIG["reset_date"]
    ledger, pending, paper_excluded = [], [], []
    scoped = []
    out_of_scope = []
    balance = start
    settled_pnl = 0.0
    settled_staked = 0.0
    wins = losses = 0
    pending_m = 0
    unsettled_m = 0
    for slip in sorted(slips, key=lambda s: s["number"]):
        n = slip["number"]
        ctx = contexts.get(n, {})
        sdate = ctx.get("date")
        if not sdate or sdate < reset:
            out_of_scope.append({
                "slip": n, "date": sdate,
                "reason": ("no date on the tracker — cannot be placed "
                           "in the date scope" if not sdate else
                           "dated before tracking began")})
            continue
        scoped.append(n)
        layer = ctx.get("bet_layer", {})
        counted = []
        if layer.get("GRAND PICK"):
            counted.append(("GRAND PICK", layer["GRAND PICK"]))
        fires = ctx.get("confidence_fires") or (
            [layer["CONFIDENCE"]] if layer.get("CONFIDENCE") else [])
        counted += [("CONFIDENCE", f) for f in fires]
        # CHOSEN BETS ONLY (user ruling 2026-11-19, superseding the
        # 2026-11-18 every-pick ledger for this site ledger): the
        # per-game ledger_bet rows the lock files record are the
        # strategies' flat-25m measurement layer — they figure ROI
        # in the programme's own books and are NOT counted here.
        # This ledger counts only the bets the user actually rides:
        # the grand pick and every confidence fire, at called stakes.
        for kind in ("UNDERDOG DESK", "DRAW DESK"):
            if layer.get(kind):
                paper_excluded.append({
                    "slip": n, "kind": kind,
                    "pick": layer[kind]["selection"],
                    "price": layer[kind]["price"]})
        graded_slip = slip.get("status") == "GRADED"
        for kind, b in counted:
            stake, price = b.get("stake_m"), b.get("price")
            if not graded_slip:
                pending.append({
                    "slip": n, "kind": kind, "game_id": b.get("game_id"),
                    "pick": b["selection"], "stake_m": stake, "price": price,
                    "status": slip.get("status")})
                if stake:
                    pending_m += stake
                continue
            won = pick_won(b["selection"],
                           ctx["graded"].get(b.get("game_id")))
            if won is None and kind == "GRAND PICK":
                res = (slip.get("results") or {}).get("grand_pick") or {}
                if res.get("result") in ("WON", "LOST"):
                    won = res["result"] == "WON"
            if won is None or stake is None or price is None:
                unsettled_m += stake or 0
                continue
            pnl = round(stake * (price - 1), 2) if won else -float(stake)
            settled_pnl = round(settled_pnl + pnl, 2)
            settled_staked = round(settled_staked + float(stake), 2)
            if balance is not None:
                balance = round(balance + pnl, 2)
            if won:
                wins += 1
            else:
                losses += 1
            ledger.append({
                "slip": n, "kind": kind, "game_id": b.get("game_id"),
                "pick": b["selection"], "stake_m": stake, "price": price,
                "result": "WON" if won else "LOST", "pnl_m": pnl,
                "balance_after_m": balance})
    settled = wins + losses
    return {
        "start_m": start,
        "reset_date": reset,
        "tracking_since": reset,
        "since": reset,
        "first_tracked_slip": min(scoped) if scoped else None,
        "scoped_slips": scoped,
        "out_of_scope": out_of_scope,
        "balance_m": balance,
        "settled_pnl_m": settled_pnl,
        "settled_staked_m": settled_staked,
        "roi_pct": (round(settled_pnl / settled_staked * 100, 2)
                    if settled_staked else None),
        "wins": wins,
        "losses": losses,
        "record": f"{wins}-{losses}",
        "winrate_pct": round(wins / settled * 100, 2) if settled else None,
        "pending_m": pending_m,
        "pending": pending,
        "ledger": ledger,
        "paper_excluded": paper_excluded,
        "unsettled_m": unsettled_m,
        "note": ("Chosen bets only — the grand pick and every confidence "
                 "fire at their called stakes, on every slip dated "
                 "from the start of tracking onward. The strategies' "
                 "flat-25m measurement rows and the paper desks never "
                 "enter this ledger."),
    }


# --------------------------------------------------------------------------
# forward standings
# --------------------------------------------------------------------------

DESK_TOTAL_RE = re.compile(r"Graded:\s*(\d+)\s*·\s*Won:\s*(\d+)\s*·\s*P&L:\s*([+\-−][\d.]+)m")


def parse_desk(relpath):
    text = read_text(os.path.join(BACKTEST, relpath))
    if text is None:
        return None
    m = DESK_TOTAL_RE.search(text)
    if not m:
        return None
    graded, won = int(m.group(1)), int(m.group(2))
    return {"wins": won, "losses": graded - won, "pnl_m": money(m.group(3))}


def parse_forward():
    fwd = {"chosen_bets": None, "confidence": None,
           "underdog_desk": parse_desk("underdog_desk_record.md"),
           "draw_desk": parse_desk("draw_desk_record.md")}
    # chosen-bet + confidence lifetime lines are restated in the newest tracker
    for rel in ("data_badminton/tracking_slip34_nov17.md",
                "data_badminton/tracking_slip33_nov17.md"):
        text = read_text(os.path.join(BACKTEST, rel))
        if text is None:
            continue
        m = re.search(r"chosen-bet record[^*]*\*\*(\d+)-(\d+), \+([\d.]+)m\*\*", text)
        if m and fwd["chosen_bets"] is None:
            fwd["chosen_bets"] = {"wins": int(m.group(1)), "losses": int(m.group(2)),
                                  "pnl_m": num(m.group(3))}
        m = re.search(r"Confidence line[^*]*\*\*(\d+)-(\d+), \+([\d.]+)m\*\*", text)
        if m and fwd["confidence"] is None:
            fwd["confidence"] = {"wins": int(m.group(1)), "losses": int(m.group(2)),
                                 "pnl_m": num(m.group(3))}
        if fwd["chosen_bets"] and fwd["confidence"]:
            break
    return fwd


# --------------------------------------------------------------------------
# stakehunter — sizing-rule findings from stakehunter_log.md verdicts
# --------------------------------------------------------------------------

_SH_RULE_RE = re.compile(
    r"\*\*([A-Z]\d+(?:-[A-Z]+)?)\s+([A-Z0-9 '&×+\-/]+?)\s*—\s*([^*]{0,70}?)\*\*")


def parse_stakehunter():
    """Findings/refutations from the verdicts (Step 4) section of every
    completed Stakehunter cycle. Defensive: returns empty on any failure."""
    empty = {"latest_cycle": None, "findings": []}
    text = read_text(os.path.join(BACKTEST, "stakehunter_log.md"))
    if not text:
        return empty
    findings = []
    latest = None
    try:
        cycles = re.split(r"^## CYCLE (\d+)", text, flags=re.M)
        # cycles = [pre, num, body, num, body, ...]
        for i in range(1, len(cycles) - 1, 2):
            cyc_no = int(cycles[i])
            body = cycles[i + 1]
            v_idx = body.find("Step 4")
            if v_idx == -1:
                continue  # cycle still in progress — no verdicts yet
            verdicts = body[v_idx:]
            end = verdicts.find("\n---")
            if end != -1:
                verdicts = verdicts[:end]
            latest = cyc_no if latest is None else max(latest, cyc_no)
            for m in _SH_RULE_RE.finditer(verdicts):
                rid, rname, tag = m.group(1), clean(m.group(2)), m.group(3).lower()
                if "not a finding" in tag or "no finding" in tag or "refuted" in tag:
                    status = "NOT A FINDING" if "finding" in tag else "REFUTED"
                elif "finding" in tag:
                    status = "FINDING"
                else:
                    continue
                tail = verdicts[m.end():]
                stop = re.search(r"\n\s*(?:\d+\.\s+\*\*|- \*\*|\*\*REFUTED|\*\*STAKEHUNTER)",
                                 tail)
                if stop:
                    tail = tail[:stop.start()]
                verdict = clean(tail).strip(" .")
                if len(verdict) > 230:
                    verdict = verdict[:227].rsplit(" ", 1)[0] + "…"
                findings.append({"id": rid, "name": rname.strip(),
                                 "cycle": cyc_no, "status": status,
                                 "verdict": verdict})
        findings.sort(key=lambda f: (-f["cycle"], f["id"]))
        return {"latest_cycle": latest, "findings": findings}
    except Exception:
        return empty


# --------------------------------------------------------------------------
# vault — COUNT ONLY (line count; contents are never parsed)
# --------------------------------------------------------------------------

def vault_count():
    path = os.path.join(BACKTEST, "vault", "vault_games.csv")
    try:
        n = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                n += chunk.count(b"\n")
        return max(n - 1, 0)  # minus header row
    except OSError:
        return None


# --------------------------------------------------------------------------
# agents — network-visual nodes
# --------------------------------------------------------------------------

def parse_agents(kings, challengers, stakehunter=None):
    by_court = {k["court"]: k for k in kings if k}
    agents = []
    now = datetime.now(tz=timezone.utc)
    for name in ["Legacy", "Regime", "Quantum", "Selector", "Vanguard",
                 "Arena", "Discovery", "Edge Finder", "Loss Autopsy",
                 "Stakehunter"]:
        fname = LOG_FILES[name]
        path = os.path.join(BACKTEST, fname)
        last = file_mtime_iso(path)
        status = "idle"
        try:
            age_min = (now - datetime.fromtimestamp(
                os.path.getmtime(path), tz=timezone.utc)).total_seconds() / 60
            status = "active" if age_min <= 90 else "idle"
        except OSError:
            last = None
        headline = None
        if name in by_court:
            k = by_court[name]
            headline = (f"{k['name']} — {k['wins']}-{k['losses']}, "
                        f"{k['roi_pct']:+.2f}% fitted ROI") if k.get("roi_pct") is not None else k["name"]
        elif name == "Arena":
            text = read_text(path, tail_bytes=300_000) or ""
            m = re.findall(r"champion (ARENA-[A-Z0-9\-]+)", text)
            headline = f"Sitting champion: {m[-1]}" if m else None
        elif name == "Discovery":
            text = read_text(path, tail_bytes=300_000) or ""
            m = re.findall(r"## CYCLE (\d+)", text)
            headline = f"Latest search cycle: {m[-1]}" if m else None
        elif name == "Edge Finder":
            text = read_text(path, tail_bytes=400_000) or ""
            m = re.findall(r"\bP(\d{2,3})\b", text[-200_000:])
            headline = f"Latest filing: P{max(int(x) for x in m)}" if m else None
        elif name == "Loss Autopsy":
            n = len(challengers)
            headline = (f"{n} gate-clearing challenger(s) banked "
                        f"(AUTOPSY-6, AUTOPSY-7)") if n else "No challenger yet"
        elif name == "Stakehunter":
            sh = stakehunter or {}
            n_find = sum(1 for f in sh.get("findings", [])
                         if f.get("status") == "FINDING")
            headline = (f"Cycle {sh.get('latest_cycle')}: {n_find} sizing "
                        f"finding(s) banked (proposals only)") if sh.get("latest_cycle") \
                else "No completed cycle yet"
        agents.append({"name": name, "role": AGENT_ROLES[name],
                       "status": status, "last_activity": last,
                       "headline": headline})
    agents.append({"name": "Research Director", "role": AGENT_ROLES["Research Director"],
                   "status": "daily", "last_activity": None,
                   "headline": "Daily brief at 08:15 CST"})
    return agents


# --------------------------------------------------------------------------

def main():
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else OUT_DEFAULT
    kings = parse_kings()
    challengers = parse_autopsy_challengers()
    slips = [parse_slip(n, rel) for n, rel in sorted(SLIP_TRACKERS.items())]
    contexts = {n: slip_context(n, rel) for n, rel in SLIP_TRACKERS.items()}
    overlays = build_live_overlays(slips, contexts)
    newest = max(SLIP_TRACKERS)
    for slip in slips:
        ctx = contexts[slip["number"]]
        slip["date"] = ctx["date"]
        total = slip.get("games")
        ended = total if (slip.get("status") == "GRADED" and total is not None) \
            else len(ctx["graded"])
        slip["games_status"] = {"games_total": total, "games_ended": ended}
        # per-game list for the slip accordions + the Record Book: id,
        # fixture, tip time, league, sport, card pick + its frozen price,
        # and — only where the tracker grades the game — the winner, the
        # final score and the card pick's outcome. Ungraded games carry
        # outcome None (the site shows AWAITING); nothing is invented.
        glist = []
        all_gids = sorted(set(ctx["games_table"]) | set(ctx["json_games"]))
        for gid in all_gids:
            g = ctx["games_table"].get(gid, {})
            tip = ctx["tips"].get(gid)
            winner = ctx["graded"].get(gid)
            jg = ctx["json_games"].get(gid, {})
            pick = jg.get("pick") or g.get("pick")
            outcome = None
            if winner and pick:
                won = pick_won(pick, winner)
                if won is not None:
                    outcome = "WON" if won else "LOST"
            glist.append({
                "game_id": gid,
                "game": g.get("game") or jg.get("game"),
                "time": g.get("time") or jg.get("time"),
                "tip_at": regina_iso(tip) if tip else None,
                "status": "ENDED" if winner else "PENDING",
                "winner": winner,
                "final_score": ctx["scores"].get(gid),
                "bet_call": g.get("bet_call"),
                "league": jg.get("league") or g.get("league"),
                "sport": jg.get("sport"),
                "pick": pick,
                "price": jg.get("price"),
                "outcome": outcome,
                "live": overlays.get(slip["number"], {}).get(gid),
                **_display_fields(overlays.get(slip["number"], {}).get(gid)),
                # frozen detail rides only on ungraded slips — that is
                # where the ticket detail panels live; graded games keep
                # their official record and stay lean.
                "detail": ctx["detail"].get(gid)
                if slip.get("status") != "GRADED" else None,
            })
        slip["games_list"] = glist
        if total is None and glist:
            total = len(glist)
            slip["games_status"] = {"games_total": total, "games_ended": ended}
        # DISPLAY-ONLY progress from the live overlay: how many games the
        # ESPN feed already shows final / in play. games_ended above stays
        # the graded count — the overlay never settles anything.
        slip["games_status"]["games_live_final"] = sum(
            1 for g in glist if (g.get("live") or {}).get("state") == "final")
        slip["games_status"]["games_live_in"] = sum(
            1 for g in glist if (g.get("live") or {}).get("state") == "in")
        if True:
            # the ranked top-3 calls PER SLIP: grand pick, confidence,
            # underdog desk — parsed from each slip's own tracker bet
            # layer, never hardcoded. Every slip gets its own board.
            top3 = []
            for rank, kind in enumerate(["GRAND PICK", "CONFIDENCE", "UNDERDOG DESK"], 1):
                b = ctx["bet_layer"].get(kind)
                if not b:
                    continue
                gid = b.get("game_id")
                top3.append({"rank": rank, "kind": kind, "game_id": gid,
                             "game": ctx["games_table"].get(gid, {}).get("game")
                             or ctx["json_games"].get(gid, {}).get("game"),
                             "pick": b["selection"], "stake_m": b["stake_m"],
                             "price": b["price"], "paper": kind == "UNDERDOG DESK"})
            if top3:
                slip["top3"] = top3
            # the slip's full CHOICES board: the grand pick, EVERY
            # confidence fire and the desk pick, in call order — the
            # site renders one slim row per choice from this list (the
            # Top 3 stays as the ranked headline trio).
            raw_choices = []
            grand = ctx["bet_layer"].get("GRAND PICK")
            if grand:
                raw_choices.append(("GRAND PICK", None, grand))
            cfires = ctx["confidence_fires"] or (
                [ctx["bet_layer"]["CONFIDENCE"]]
                if ctx["bet_layer"].get("CONFIDENCE") else [])
            for i, f in enumerate(cfires):
                raw_choices.append(("CONFIDENCE", i + 1, f))
            desk = ctx["bet_layer"].get("UNDERDOG DESK")
            if desk:
                raw_choices.append(("UNDERDOG DESK", None, desk))
            built = []
            for rank, (kind, fire, b) in enumerate(raw_choices, 1):
                gid = b.get("game_id")
                built.append({
                    "rank": rank, "kind": kind, "fire": fire,
                    "game_id": gid,
                    "game": ctx["games_table"].get(gid, {}).get("game")
                            or ctx["json_games"].get(gid, {}).get("game"),
                    "pick": b["selection"], "stake_m": b["stake_m"],
                    "price": b["price"], "paper": kind == "UNDERDOG DESK"})
            if built:
                slip["choices"] = built
    bets = build_bets(slips, contexts)
    # Display-only live overlay on the flat bet rows too (the page's flow
    # strip and Vault tab read bets directly): same discipline as
    # everywhere — additive fields only, settlement fields untouched.
    for row in bets:
        lv = overlays.get(row["slip"], {}).get(row["game_id"])
        row["live"] = lv
        row.update(_display_fields(lv))
    # Live state for the Top-3 hero cards (the Top 3 ARE the live board):
    # each top-3 entry joins its called bet row (same slip/game/kind) for
    # status / result / pnl / final score / tip time, and the user's own bet
    # on the same game + pick rides along as a marker. Nothing is invented —
    # a field with no source row stays None.
    called_by = {(b["slip"], b["game_id"], b["kind"]): b for b in bets
                 if b["kind"] != "USER BET"}
    user_by_game = {}
    for b in bets:
        if b["kind"] == "USER BET":
            user_by_game.setdefault((b["slip"], b["game_id"]), []).append(b)

    def join_live(slip, t):
        ctx = contexts[slip["number"]]
        row = called_by.get((slip["number"], t["game_id"], t["kind"]))
        for field in ("status", "result", "pnl_m", "final_score", "tip_at"):
            t[field] = row[field] if row else None
        t["live"] = overlays.get(slip["number"], {}).get(t["game_id"])
        t.update(_display_fields(t["live"]))
        det = ctx["detail"].get(t["game_id"]) or {}
        t["reason"] = det.get("bet_call") \
            if slip.get("status") != "GRADED" else None
        t["user_bet"] = False
        t["user_stake_m"] = None
        t["user_price"] = None
        for ub in user_by_game.get((slip["number"], t["game_id"]), []):
            if ub["pick"] == t["pick"]:
                t["user_bet"] = True
                t["user_stake_m"] = ub["stake_m"]
                t["user_price"] = ub["price"]
                break

    for slip in slips:
        for t in slip.get("top3") or []:
            join_live(slip, t)
        for t in slip.get("choices") or []:
            join_live(slip, t)
    stakehunter = parse_stakehunter()
    status = {
        "generated_at": regina_iso(datetime.now(tz=timezone.utc)),
        "kings": kings,
        "agents": parse_agents(kings, challengers, stakehunter),
        "stakehunter": stakehunter,
        "activity": parse_activity(),
        "slips": slips,
        "bets": bets,
        "bankroll": build_bankroll(slips, contexts),
        "forward": parse_forward(),
        "vault": {"count": vault_count(), "next_opening": NEXT_VAULT_OPENING},
        "autopsy_challengers": challengers,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, ensure_ascii=False)
    live = [k for k in kings if k]
    print(f"status.json written: {len(live)}/5 kings, "
          f"{len(status['activity'])} activity events, "
          f"{len(status['slips'])} slips, vault {status['vault']['count']}, "
          f"{len(challengers)} autopsy challengers, "
          f"{len(bets)} bet rows, bankroll balance "
          f"{status['bankroll']['balance_m']}m (settled "
          f"{status['bankroll']['settled_pnl_m']}m, pending "
          f"{status['bankroll']['pending_m']}m) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
