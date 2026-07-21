"""Build the site's KBO hitter dataset from the public KBO record tables.

The calculated metrics are estimates, not official KBO statistics.  Only the
first (qualified-hitter) table is used, so league context is also estimated
from that population.  Keep the labels and methodology notice on the website.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.parse
import http.cookiejar
from html import unescape
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.koreabaseball.com/Record/Player/HitterBasic/"
PAGES = {"basic": BASE + "Basic1.aspx", "rate": BASE + "Basic2.aspx"}
KST = timezone(timedelta(hours=9))


class RecordTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_record = False
        self.in_table = False
        self.in_cell = False
        self.is_header = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self.player_id: str | None = None
        self.row_player_id: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and "record_result" in (values.get("class") or ""):
            self.in_record = True
        elif self.in_record and tag == "table" and not self.in_table:
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row, self.row_player_id = [], None
        elif self.in_table and tag in {"th", "td"}:
            self.in_cell, self.is_header, self.cell_parts = True, tag == "th", []
        elif self.in_cell and tag == "a":
            match = re.search(r"playerId=(\d+)", values.get("href") or "")
            if match:
                self.row_player_id = match.group(1)

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"th", "td"} and self.in_cell:
            value = " ".join("".join(self.cell_parts).split())
            self.row.append(value)
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.row:
            if self.is_header and not self.headers:
                self.headers = self.row
            elif len(self.row) == len(self.headers):
                if self.row_player_id:
                    self.row.append(self.row_player_id)
                self.rows.append(self.row)
        elif self.in_table and tag == "table":
            self.in_table = False
            self.in_record = False


USER_AGENT = "DugoutData/1.0 (daily statistics update)"
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def download(url: str, payload: dict[str, str] | None = None) -> str:
    data = urllib.parse.urlencode(payload).encode() if payload else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Referer": url})
    with OPENER.open(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_table(html: str, url: str) -> list[dict[str, str]]:
    parser = RecordTableParser()
    parser.feed(html)
    if not parser.headers or not parser.rows:
        raise RuntimeError(f"KBO record table was not found: {url}")
    records = []
    for row in parser.rows:
        values, player_id = row[: len(parser.headers)], row[-1] if len(row) > len(parser.headers) else ""
        item = dict(zip(parser.headers, values))
        item["player_id"] = player_id
        records.append(item)
    return records


def fetch_table(url: str) -> list[dict[str, str]]:
    """Fetch every ASP.NET pager page, not only the first 30 rows."""
    first_html = download(url)
    records = parse_table(first_html, url)
    page_targets = {
        int(number): unescape(target)
        for target, number in re.findall(
            r"__doPostBack\(&#39;([^']*ucPager\$btnNo(\d+))&#39;", first_html
        )
    }
    print(f"Found pager pages {sorted(page_targets)} for {url}")
    hidden = {
        unescape(name): unescape(value)
        for name, value in re.findall(
            r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', first_html
        )
    }
    selected = {
        unescape(name): unescape(value)
        for name, options in re.findall(r'<select[^>]+name="([^"]+)"[^>]*>(.*?)</select>', first_html, re.S)
        for value in re.findall(r'<option[^>]+selected="selected"[^>]+value="([^"]*)"', options)
    }
    manager_match = re.search(r"PageRequestManager\._initialize\('([^']+)'.*?\['t([^']+udpContent)'", first_html, re.S)
    for page_number in sorted(page for page in page_targets if page > 1):
        payload = dict(hidden)
        payload.update(selected)
        payload["__EVENTTARGET"] = page_targets[page_number]
        payload["__EVENTARGUMENT"] = ""
        for field_name in list(payload):
            if field_name.endswith("$hfPage"):
                payload[field_name] = str(page_number)
        if manager_match:
            payload[manager_match.group(1)] = f"{manager_match.group(2)}|{page_targets[page_number]}"
        page_html = download(url, payload)
        page_records = parse_table(page_html, url)
        print(f"Fetched page {page_number}: {len(page_records)} rows, first={page_records[0]['player_id'] if page_records else '-'}")
        existing_ids = {row["player_id"] for row in records}
        records.extend(row for row in page_records if row["player_id"] not in existing_ids)
    return records


def number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "0").replace(",", ""))
    except ValueError:
        return 0.0


def main() -> None:
    basic = fetch_table(PAGES["basic"])
    rates = fetch_table(PAGES["rate"])
    rate_by_id = {row["player_id"]: row for row in rates}
    merged = [(row, rate_by_id[row["player_id"]]) for row in basic if row["player_id"] in rate_by_id]
    if len(merged) < 10:
        raise RuntimeError("Too few matching KBO hitter rows; page structure may have changed")

    contexts = []
    for base, rate in merged:
        hits, doubles, triples, homers = (number(base, k) for k in ("H", "2B", "3B", "HR"))
        walks = max(0, number(rate, "BB") - number(rate, "IBB"))
        hbp, ab, sf, pa = (number(rate if k in rate else base, k) for k in ("HBP", "AB", "SF", "PA"))
        singles = max(0, hits - doubles - triples - homers)
        denominator = ab + walks + hbp + sf
        woba = (0.69 * walks + 0.72 * hbp + 0.89 * singles + 1.27 * doubles + 1.62 * triples + 2.10 * homers) / denominator if denominator else 0
        contexts.append((base, rate, woba, pa))

    total_pa = sum(item[3] for item in contexts)
    league_woba = sum(item[2] * item[3] for item in contexts) / total_pa
    league_r_per_pa = sum(number(item[0], "R") for item in contexts) / total_pa
    woba_scale, runs_per_win = 1.20, 10.0

    players = []
    for base, rate, woba, pa in contexts:
        wraa = (woba - league_woba) / woba_scale * pa
        wrc_plus = 100 * ((wraa / pa + league_r_per_pa) / league_r_per_pa) if pa and league_r_per_pa else 100
        offensive_war = (wraa + 20 * pa / 600) / runs_per_win
        estimated_runs = wraa + league_r_per_pa * pa
        players.append({
            "id": base["player_id"], "name": base["선수명"], "team": base["팀명"], "league": "KBO",
            "g": int(number(base, "G")), "pa": int(pa), "avg": number(base, "AVG"),
            "obp": number(rate, "OBP"), "hr": int(number(base, "HR")), "rbi": int(number(base, "RBI")),
            "ops": number(rate, "OPS"), "wrc": round(wrc_plus), "owar": round(offensive_war, 1),
            "xruns": round(estimated_runs, 1),
            "raw": {key: int(number(rate if key in rate else base, key)) for key in ("PA", "AB", "R", "H", "2B", "3B", "HR", "BB", "IBB", "HBP", "SF")},
        })
    players.sort(key=lambda player: player["owar"], reverse=True)
    payload = {
        "updated_at": datetime.now(KST).isoformat(timespec="minutes"),
        "season": datetime.now(KST).year,
        "source": "KBO 공식 기록실",
        "source_url": PAGES["basic"],
        "scope": "KBO 공식 타격 순위표에 노출된 규정타석 대상 선수",
        "method": "사이트 자체 추정치; 구장·수비·주루 보정 미포함",
        "players": players,
    }
    output = Path(__file__).resolve().parents[1] / "data" / "kbo_stats.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(players)} players to {output}")


if __name__ == "__main__":
    main()
