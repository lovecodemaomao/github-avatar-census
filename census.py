"""Reproducible, resumable GitHub avatar cohort census.

Run `python census.py --help` for the pipeline commands.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"
DB_PATH = DATA / "census.sqlite3"
LANGUAGES = ["Python", "JavaScript", "TypeScript", "Go", "Rust", "Java"]
STAR_BANDS = [(50, 499), (500, 4999), (5000, 50000)]
COHORTS = [(2008, 2012), (2013, 2016), (2017, 2020), (2021, 2023), (2024, 2026)]
LABELS = ["human", "anime", "character", "meme", "animal", "logo", "illustration", "default_abstract", "uncertain"]
LABEL_ZH = {"human": "真人", "anime": "二次元", "character": "虚拟角色", "meme": "梗图", "animal": "动物", "logo": "标志", "illustration": "其他插画", "default_abstract": "默认或抽象", "uncertain": "无法判断"}
SEED = 20261017


def db() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    (DATA / "avatars").mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS repositories(
      full_name TEXT PRIMARY KEY, language TEXT, star_band TEXT, stars INTEGER,
      search_query TEXT, search_page INTEGER, status TEXT DEFAULT 'selected',
      error TEXT);
    CREATE TABLE IF NOT EXISTS windows(
      repo TEXT, window_start TEXT, window_end TEXT, status TEXT,
      commit_count INTEGER DEFAULT 0, error TEXT,
      PRIMARY KEY(repo, window_start));
    CREATE TABLE IF NOT EXISTS evidence(
      user_id INTEGER, login TEXT, repo TEXT, sha TEXT, committed_at TEXT,
      PRIMARY KEY(user_id, repo, sha));
    CREATE INDEX IF NOT EXISTS evidence_user ON evidence(user_id);
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, login TEXT, created_at TEXT, cohort TEXT,
      avatar_url TEXT, type TEXT, status TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS avatars(
      user_id INTEGER PRIMARY KEY, sha256 TEXT, path TEXT, status TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS classifications(
      sha256 TEXT PRIMARY KEY, label TEXT, raw_response TEXT, model TEXT, status TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS fast_classifications(
      sha256 TEXT, model TEXT, label TEXT, raw_response TEXT, status TEXT, error TEXT,
      PRIMARY KEY(sha256, model));
    CREATE TABLE IF NOT EXISTS audit(
      user_id INTEGER PRIMARY KEY, human_label TEXT, notes TEXT);
    """)
    con.commit()
    return con


class GitHub:
    def __init__(self):
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "User-Agent": "github-avatar-census/1.0"})

    def get(self, path: str, params: dict | None = None) -> dict | list:
        url = "https://api.github.com/" + path.lstrip("/")
        for attempt in range(6):
            try:
                response = self.session.get(url, params=params, timeout=40)
            except requests.RequestException:
                time.sleep(min(60, 2 ** attempt))
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code in (403, 429):
                reset = int(response.headers.get("x-ratelimit-reset", "0"))
                remaining = response.headers.get("x-ratelimit-remaining")
                retry = response.headers.get("retry-after")
                wait = int(retry) if retry and retry.isdigit() else (max(1, reset - int(time.time()) + 2) if remaining == "0" else min(300, 60 * (attempt + 1)))
                print(f"GitHub rate limit; waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            if response.status_code >= 500:
                time.sleep(min(60, 2 ** attempt))
                continue
            raise RuntimeError(f"GitHub {response.status_code} for {path}: {response.text[:300]}")
        raise RuntimeError(f"GitHub request failed after retries: {path}")

    def graphql(self, query: str) -> dict:
        for attempt in range(6):
            try:
                response = self.session.post("https://api.github.com/graphql", json={"query": query}, timeout=60)
            except requests.RequestException:
                time.sleep(min(60, 2 ** attempt))
                continue
            if response.status_code == 200:
                body = response.json()
                if body.get("data") is not None:
                    return body
            if response.status_code in (200, 403, 429):
                reset = int(response.headers.get("x-ratelimit-reset", "0"))
                remaining = response.headers.get("x-ratelimit-remaining")
                retry = response.headers.get("retry-after")
                wait = int(retry) if retry and retry.isdigit() else (max(1, reset - int(time.time()) + 2) if remaining == "0" else min(300, 60 * (attempt + 1)))
                print(f"GitHub GraphQL limit; waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"GitHub GraphQL {response.status_code}: {response.text[:300]}")
        raise RuntimeError("GitHub GraphQL failed after retries")


def cutoff_and_windows() -> tuple[datetime, list[tuple[str, str]]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=365)
    # Four reproducible two-week windows, one per quarter of the prior year.
    windows = []
    for quarter in range(4):
        start = cutoff + timedelta(days=quarter * 91 + 35)
        end = min(start + timedelta(days=14), now)
        windows.append((start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return cutoff, windows


def discover(con: sqlite3.Connection, api: GitHub, per_stratum: int = 4) -> None:
    cutoff, windows = cutoff_and_windows()
    con.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("run_started_utc", datetime.now(timezone.utc).isoformat()))
    con.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("cutoff_utc", cutoff.isoformat()))
    con.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("windows", json.dumps(windows)))
    con.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("seed", str(SEED)))
    con.commit()
    rng = random.Random(SEED)
    for lang in LANGUAGES:
        for low, high in STAR_BANDS:
            band = f"{low}-{high}"
            n = con.execute("SELECT COUNT(*) FROM repositories WHERE language=? AND star_band=?", (lang, band)).fetchone()[0]
            if n >= per_stratum:
                continue
            query = f"language:{lang} stars:{low}..{high} pushed:>{cutoff.date()} fork:false archived:false"
            page = rng.randint(1, 5)
            result = api.get("search/repositories", {"q": query, "sort": "updated", "order": "desc", "per_page": 100, "page": page})
            items = result.get("items", [])
            if not items and page != 1:
                page = 1
                items = api.get("search/repositories", {"q": query, "sort": "updated", "order": "desc", "per_page": 100, "page": 1}).get("items", [])
            rng.shuffle(items)
            existing = {row[0] for row in con.execute("SELECT full_name FROM repositories")}
            choices = [item for item in items if item["full_name"] not in existing][:per_stratum - n]
            for item in choices:
                con.execute("INSERT OR IGNORE INTO repositories(full_name, language, star_band, stars, search_query, search_page) VALUES (?,?,?,?,?,?)", (item["full_name"], lang, band, item["stargazers_count"], query, page))
            con.commit()
            print(f"discovered {lang} {band}: {len(choices)} repositories", flush=True)


def collect_commits(con: sqlite3.Connection, api: GitHub, max_pages: int = 10) -> None:
    windows = json.loads(con.execute("SELECT value FROM metadata WHERE key='windows'").fetchone()[0])
    repos = con.execute("SELECT full_name FROM repositories ORDER BY language, star_band, full_name").fetchall()
    for repo_row in repos:
        repo = repo_row[0]
        for start, end in windows:
            prior = con.execute("SELECT status FROM windows WHERE repo=? AND window_start=?", (repo, start)).fetchone()
            if prior and prior[0] in ("complete", "truncated"):
                continue
            count = 0
            status = "complete"
            error = None
            try:
                for page in range(1, max_pages + 1):
                    commits = api.get(f"repos/{repo}/commits", {"since": start, "until": end, "per_page": 100, "page": page})
                    for c in commits:
                        author = c.get("author")
                        if not author or author.get("type") != "User":
                            continue
                        date = (c.get("commit", {}).get("author") or {}).get("date")
                        con.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?)", (author["id"], author["login"], repo, c["sha"], date))
                    count += len(commits)
                    if len(commits) < 100:
                        break
                else:
                    status = "truncated"
            except Exception as exc:
                status, error = "error", str(exc)
            con.execute("INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?)", (repo, start, end, status, count, error))
            con.commit()
            print(f"{repo} {start[:10]}: {count} commits ({status})", flush=True)


def cohort_for(date: str | None) -> str | None:
    if not date:
        return None
    year = int(date[:4])
    for low, high in COHORTS:
        if low <= year <= high:
            return f"{low}-{high}"
    return None


def collect_users(con: sqlite3.Connection, api: GitHub, max_requests: int = 5000) -> None:
    rows = con.execute("SELECT user_id, MIN(login) AS login FROM evidence GROUP BY user_id ORDER BY user_id").fetchall()
    ids = [tuple(row) for row in rows]
    random.Random(SEED).shuffle(ids)
    requests_made = 0
    for user_id, login in ids:
        if requests_made >= max_requests:
            break
        prior = con.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
        if prior and prior[0] in ("eligible", "selected", "excluded"):
            continue
        try:
            info = api.get(f"user/{user_id}")
            requests_made += 1
            cohort = cohort_for(info.get("created_at"))
            kind = info.get("type")
            eligible = kind == "User" and not re.search(r"\[bot\]|-bot$", info.get("login", ""), re.I) and cohort
            status = "eligible" if eligible else "excluded"
            con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?)", (user_id, info.get("login"), info.get("created_at"), cohort, info.get("avatar_url"), kind, status, None))
        except Exception as exc:
            con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?)", (user_id, login, None, None, None, None, "error", str(exc)))
        con.commit()
        if requests_made % 100 == 0:
            print(f"profiles {requests_made}; eligible {con.execute('SELECT COUNT(*) FROM users WHERE status=\"eligible\"').fetchone()[0]}", flush=True)
    print(f"profile collection stopped after {requests_made} requests", flush=True)


def collect_users_batch(con: sqlite3.Connection, api: GitHub, batch_size: int = 40) -> None:
    rows = con.execute("""SELECT e.user_id,MIN(e.login) FROM evidence e
      LEFT JOIN users u ON u.id=e.user_id
      WHERE u.id IS NULL OR u.status='error'
      GROUP BY e.user_id ORDER BY e.user_id""").fetchall()
    rng = random.Random(SEED + 3)
    candidates = [tuple(row) for row in rows]
    rng.shuffle(candidates)
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start+batch_size]
        fields = " ".join(f"u{i}:user(login:{json.dumps(login)}){{databaseId login createdAt avatarUrl}}" for i, (_, login) in enumerate(batch))
        result = api.graphql("query { " + fields + " }")
        data = result.get("data", {})
        for i, (user_id, login) in enumerate(batch):
            info = data.get(f"u{i}")
            try:
                if info and info.get("databaseId") == user_id:
                    cohort = cohort_for(info["createdAt"])
                    eligible = cohort and not re.search(r"\[bot\]|-bot$", info["login"], re.I)
                    con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?)", (user_id, info["login"], info["createdAt"], cohort, info["avatarUrl"], "User", "eligible" if eligible else "excluded", None))
                else:
                    # A renamed or deleted login can resolve to another account. The ID is authoritative.
                    user = api.get(f"user/{user_id}")
                    cohort = cohort_for(user.get("created_at"))
                    eligible = user.get("type") == "User" and cohort and not re.search(r"\[bot\]|-bot$", user.get("login", ""), re.I)
                    con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?)", (user_id, user.get("login"), user.get("created_at"), cohort, user.get("avatar_url"), user.get("type"), "eligible" if eligible else "excluded", None))
            except Exception as exc:
                con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?)", (user_id, login, None, None, None, None, "error", str(exc)))
        con.commit()
        print(f"profiles batch {min(start+batch_size,len(candidates))}/{len(candidates)}", flush=True)


def select_users(con: sqlite3.Connection, target: int = 300) -> None:
    rng = random.Random(SEED + 2)
    con.execute("UPDATE users SET status='eligible' WHERE status='selected'")
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("target_per_cohort", str(target)))
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("selection_seed", str(SEED + 2)))
    for low, high in COHORTS:
        cohort = f"{low}-{high}"
        ids = [row[0] for row in con.execute("SELECT id FROM users WHERE status='eligible' AND cohort=? ORDER BY id", (cohort,))]
        rng.shuffle(ids)
        for user_id in ids[:target]:
            con.execute("UPDATE users SET status='selected' WHERE id=?", (user_id,))
        print(f"{cohort}: {min(target, len(ids))}/{len(ids)} selected", flush=True)
    con.commit()


def download_avatars(con: sqlite3.Connection, workers: int = 4) -> None:
    rows = con.execute("SELECT id, avatar_url FROM users WHERE status='selected' AND id NOT IN (SELECT user_id FROM avatars WHERE status='ok')").fetchall()
    def fetch(user_id: int, url: str) -> tuple[int, str | None, str | None, str, str | None]:
        try:
            if not url or not url.startswith("https://avatars.githubusercontent.com/"):
                raise ValueError("unexpected avatar URL")
            for attempt in range(4):
                try:
                    response = requests.get(url, params={"s": 384}, headers={"User-Agent": "github-avatar-census/1.0"}, timeout=30)
                    response.raise_for_status()
                    break
                except requests.RequestException:
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            if len(response.content) > 5_000_000:
                raise ValueError("image exceeds 5 MB")
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            image.thumbnail((384, 384))
            out = io.BytesIO()
            image.save(out, format="PNG")
            content = out.getvalue()
            digest = hashlib.sha256(content).hexdigest()
            path = DATA / "avatars" / f"{digest}.png"
            if not path.exists():
                path.write_bytes(content)
            return user_id, digest, str(path.relative_to(ROOT)), "ok", None
        except Exception as exc:
            return user_id, None, None, "error", str(exc)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, user_id, url) for user_id, url in rows]
        for idx, future in enumerate(as_completed(futures), 1):
            con.execute("INSERT OR REPLACE INTO avatars VALUES (?,?,?,?,?)", future.result())
            con.commit()
            if idx % 100 == 0:
                print(f"avatars {idx}/{len(rows)}", flush=True)


PROMPT = """Classify this GitHub profile avatar by its visible image only. Return JSON with exactly one key, label, whose value is EXACTLY one of these strings: human, anime, character, meme, animal, logo, illustration, default_abstract, uncertain. Meanings: human=real human photograph; anime=anime or manga styled person; character=other game, film, or fictional character; meme=reaction image or internet joke; animal=real or drawn animal; logo=brand or text mark; illustration=other drawn art; default_abstract=GitHub identicon, abstract pattern, scenery, or object; uncertain=too ambiguous. If anime style and character overlap, choose anime. Do not infer the owner's identity. No explanation."""


def normalize_label(parsed: dict) -> str:
    label = parsed.get("label")
    if label is None and len(parsed) == 1:
        key, value = next(iter(parsed.items()))
        label = value if isinstance(value, str) else key
    if isinstance(label, str):
        label = label.split("(", 1)[0].strip().lower().replace(" ", "_")
    return label if label in LABELS else "uncertain"


def ollama_predict(image64: str, model: str, think: bool = True) -> tuple[str, str]:
    for attempt in range(3):
        response = requests.post("http://localhost:11434/api/chat", json={
            "model": model, "stream": False, "think": think, "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT, "images": [image64]}],
        }, timeout=180)
        response.raise_for_status()
        body = response.json()
        message = body.get("message", {})
        raw = message.get("content") or (message.get("thinking", "") if not think else "")
        if body.get("done") and raw.strip():
            return normalize_label(json.loads(raw)), raw
        time.sleep(1 + attempt)
    raise RuntimeError("empty/incomplete model response")


def classify(con: sqlite3.Connection, model: str = "qwen3-vl:4b", limit: int = 0) -> None:
    rows = con.execute("SELECT DISTINCT sha256, path FROM avatars WHERE status='ok' AND sha256 NOT IN (SELECT sha256 FROM classifications WHERE status='ok' AND model=?) ORDER BY sha256", (model,)).fetchall()
    if limit:
        rows = rows[:limit]
    for idx, (digest, path) in enumerate(rows, 1):
        try:
            image64 = base64.b64encode((ROOT / path).read_bytes()).decode("ascii")
            label, raw = ollama_predict(image64, model)
            con.execute("INSERT OR REPLACE INTO classifications VALUES (?,?,?,?,?,?)", (digest, label, raw, model, "ok", None))
        except Exception as exc:
            con.execute("INSERT OR REPLACE INTO classifications VALUES (?,?,?,?,?,?)", (digest, "uncertain", None, model, "error", str(exc)))
        con.commit()
        if idx % 25 == 0:
            print(f"classified {idx}/{len(rows)}", flush=True)


def classify_pipeline(con: sqlite3.Connection, model: str = "qwen3-vl:4b") -> None:
    final_model = model + "/two-stage"
    rows = con.execute("""SELECT DISTINCT a.sha256,a.path FROM avatars a
      WHERE a.status='ok' AND a.sha256 NOT IN
      (SELECT sha256 FROM fast_classifications WHERE model=? AND status='ok')
      ORDER BY a.sha256""", (model,)).fetchall()
    for idx, (digest, path) in enumerate(rows, 1):
        try:
            image64 = base64.b64encode((ROOT / path).read_bytes()).decode("ascii")
            label, raw = ollama_predict(image64, model, think=False)
            con.execute("INSERT OR REPLACE INTO fast_classifications VALUES (?,?,?,?,?,?)", (digest, model, label, raw, "ok", None))
        except Exception as exc:
            con.execute("INSERT OR REPLACE INTO fast_classifications VALUES (?,?,?,?,?,?)", (digest, model, "uncertain", None, "error", str(exc)))
        con.commit()
        if idx % 100 == 0:
            print(f"fast classified {idx}/{len(rows)}", flush=True)
    rows = con.execute("""SELECT f.sha256,a.path,f.label,f.raw_response FROM fast_classifications f
      JOIN avatars a ON a.sha256=f.sha256 AND a.status='ok'
      LEFT JOIN classifications c ON c.sha256=f.sha256 AND c.model=? AND c.status='ok'
      WHERE f.model=? AND f.status='ok' AND c.sha256 IS NULL
      ORDER BY f.sha256""", (final_model, model)).fetchall()
    for idx, (digest, path, fast_label, fast_raw) in enumerate(rows, 1):
        try:
            if fast_label == "uncertain":
                image64 = base64.b64encode((ROOT / path).read_bytes()).decode("ascii")
                label, raw = ollama_predict(image64, model, think=True)
            else:
                label, raw = fast_label, fast_raw
            con.execute("INSERT OR REPLACE INTO classifications VALUES (?,?,?,?,?,?)", (digest, label, raw, final_model, "ok", None))
        except Exception as exc:
            con.execute("INSERT OR REPLACE INTO classifications VALUES (?,?,?,?,?,?)", (digest, "uncertain", None, final_model, "error", str(exc)))
        con.commit()
        if idx % 100 == 0:
            print(f"final classified {idx}/{len(rows)}", flush=True)


def audit_export(con: sqlite3.Connection, per_cohort: int = 30) -> None:
    rng = random.Random(SEED + 1)
    rows = con.execute("""SELECT u.id,u.login,u.cohort,c.label,a.path FROM users u JOIN avatars a ON a.user_id=u.id LEFT JOIN classifications c ON c.sha256=a.sha256 AND c.status='ok' WHERE u.status='selected' AND a.status='ok' ORDER BY u.id""").fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[2]].append(row)
    sampled = []
    for cohort in [f"{a}-{b}" for a, b in COHORTS]:
        group = grouped[cohort]
        sampled.extend(rng.sample(group, min(per_cohort, len(group))))
    audit_dir = DATA / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / "audit_sample.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["audit_index", "user_id", "login", "cohort", "model_label", "avatar_path", "human_label", "notes"])
        for index, row in enumerate(sampled, 1):
            prior = con.execute("SELECT human_label,notes FROM audit WHERE user_id=?", (row[0],)).fetchone()
            writer.writerow([index, *row, prior[0] if prior else "", prior[1] if prior else ""])
    for start in range(0, len(sampled), 30):
        sheet = Image.new("RGB", (5 * 152, 6 * 154), "white")
        draw = ImageDraw.Draw(sheet)
        for j, row in enumerate(sampled[start:start+30]):
            index = start + j + 1
            avatar = Image.open(ROOT / row[4]).convert("RGB")
            avatar.thumbnail((128, 128))
            x, y = (j % 5) * 152 + 12, (j // 5) * 154 + 2
            sheet.paste(avatar, (x, y))
            draw.text((x, y + 130), str(index), fill="black")
        sheet.save(audit_dir / f"audit_{start//30+1:02d}.png")
    print(f"audit sample: {len(sampled)} rows at {path}")


def audit_import(con: sqlite3.Connection, path: Path) -> None:
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            label = row["human_label"].strip()
            if label:
                if label not in LABELS:
                    raise ValueError(f"Invalid label {label!r} for {row['user_id']}")
                con.execute("INSERT OR REPLACE INTO audit VALUES (?,?,?)", (int(row["user_id"]), label, row.get("notes", "")))
    con.commit()


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    z = 1.96
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, center - margin), min(1.0, center + margin)


def report(con: sqlite3.Connection) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = con.execute("""SELECT u.id,u.cohort,c.label,r.language,r.star_band FROM users u
      LEFT JOIN avatars a ON a.user_id=u.id AND a.status='ok'
      LEFT JOIN classifications c ON c.sha256=a.sha256 AND c.status='ok'
      LEFT JOIN evidence e ON e.user_id=u.id
      LEFT JOIN repositories r ON r.full_name=e.repo
      WHERE u.status='selected' GROUP BY u.id""").fetchall()
    cohort_names = [f"{a}-{b}" for a, b in COHORTS]
    stats = {}
    for cohort in cohort_names:
        group = [r for r in rows if r[1] == cohort]
        labeled = [r for r in group if r[2] is not None]
        known = [r for r in labeled if r[2] != "uncertain"]
        anime = sum(r[2] == "anime" for r in known)
        broad = sum(r[2] in ("anime", "character") for r in known)
        lo, hi = wilson(anime, len(known))
        stats[cohort] = {"selected": len(group), "classified": len(labeled), "known": len(known), "uncertain": len(labeled)-len(known), "anime": anime, "anime_pct": 100*anime/len(known) if known else None, "broad_anime_character": broad, "broad_pct": 100*broad/len(known) if known else None, "ci_low": 100*lo, "ci_high": 100*hi, "labels": dict(Counter(r[2] for r in labeled))}
    with (RESULTS / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    with (DATA / "sample.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "login", "created_at", "cohort", "avatar_sha256", "model_label", "evidence_repositories"])
        for row in con.execute("""SELECT u.id,u.login,u.created_at,u.cohort,a.sha256,c.label,
          (SELECT GROUP_CONCAT(repo,';') FROM (SELECT DISTINCT repo FROM evidence WHERE user_id=u.id ORDER BY repo))
          FROM users u LEFT JOIN avatars a ON a.user_id=u.id AND a.status='ok'
          LEFT JOIN classifications c ON c.sha256=a.sha256 AND c.status='ok'
          WHERE u.status='selected' ORDER BY u.cohort,u.id"""):
            writer.writerow(tuple(row))
    fig, ax = plt.subplots(figsize=(9, 5))
    vals = [stats[c]["anime_pct"] or 0 for c in cohort_names]
    errs = [[max(0, vals[i]-stats[c]["ci_low"]) for i,c in enumerate(cohort_names)], [max(0, stats[c]["ci_high"]-vals[i]) for i,c in enumerate(cohort_names)]]
    ax.bar(cohort_names, vals, color="#7452ad")
    ax.errorbar(cohort_names, vals, yerr=errs, fmt="none", ecolor="#222", capsize=4)
    ax.set_ylabel("Anime avatars (%)")
    ax.set_xlabel("GitHub signup cohort")
    ax.set_title("Current anime avatars among sampled active OSS contributors")
    fig.tight_layout()
    fig.savefig(RESULTS / "anime_by_cohort.png", dpi=170)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 5))
    bottom = [0] * 5
    for label in LABELS:
        heights = [stats[c]["labels"].get(label, 0) / stats[c]["classified"] * 100 if stats[c]["classified"] else 0 for c in cohort_names]
        ax.bar(cohort_names, heights, bottom=bottom, label=label)
        bottom = [a+b for a,b in zip(bottom, heights)]
    ax.set_ylabel("Share of classified avatars (%)")
    ax.set_title("Current avatar categories by signup cohort")
    ax.legend(bbox_to_anchor=(1.01, 1), loc="upper left")
    fig.tight_layout()
    fig.savefig(RESULTS / "categories_by_cohort.png", dpi=170)
    plt.close(fig)
    windows = con.execute("SELECT status,COUNT(*) FROM windows GROUP BY status").fetchall()
    truncated_windows = [f"{row[0]} ({row[1][:10]})" for row in con.execute("SELECT repo,window_start FROM windows WHERE status='truncated' ORDER BY repo,window_start")]
    zero_windows = con.execute("SELECT COUNT(*) FROM windows WHERE commit_count=0 AND status='complete'").fetchone()[0]
    audit_rows = con.execute("""SELECT a.user_id,a.human_label,c.label FROM audit a JOIN avatars av ON av.user_id=a.user_id JOIN classifications c ON c.sha256=av.sha256 WHERE c.status='ok'""").fetchall()
    audit_csv = DATA / "audit" / "audit_sample.csv"
    holdout_ids = set()
    if audit_csv.exists():
        with audit_csv.open(encoding="utf-8-sig", newline="") as file:
            holdout_ids = {int(row["user_id"]) for row in csv.DictReader(file) if int(row["audit_index"]) % 2 == 0}
    audit = [(row[1], row[2]) for row in audit_rows if not holdout_ids or row[0] in holdout_ids]
    tp = sum(a=="anime" and b=="anime" for a,b in audit)
    predicted = sum(b=="anime" for a,b in audit)
    actual = sum(a=="anime" for a,b in audit)
    subrows = con.execute("""SELECT DISTINCT u.id,u.cohort,c.label,r.language,r.star_band
      FROM users u JOIN avatars a ON a.user_id=u.id JOIN classifications c ON c.sha256=a.sha256
      JOIN evidence e ON e.user_id=u.id JOIN repositories r ON r.full_name=e.repo
      WHERE u.status='selected' AND a.status='ok' AND c.status='ok' AND c.label!='uncertain'""").fetchall()
    subgroup_lines = []
    for field, title, order in [(3, "仓库主语言", LANGUAGES), (4, "仓库星数区间", [f"{a}-{b}" for a,b in STAR_BANDS])]:
        subgroup_lines += [f"### {title}", "", "同一用户可能出现在多个组；这里只作偏差检查。", "", "| 组别 | 老组可判断 | 老组二次元 | 新组可判断 | 新组二次元 | 新−老 |", "|---|---:|---:|---:|---:|---:|"]
        for value in order:
            old = {r[0]: r[2] for r in subrows if r[field] == value and r[1] == cohort_names[0]}
            new = {r[0]: r[2] for r in subrows if r[field] == value and r[1] == cohort_names[-1]}
            old_pct = 100*sum(label == "anime" for label in old.values())/len(old) if old else None
            new_pct = 100*sum(label == "anime" for label in new.values())/len(new) if new else None
            fmt = lambda x: f"{x:.1f}%" if x is not None else "—"
            diff = f"{new_pct-old_pct:+.1f} pp" if old_pct is not None and new_pct is not None else "—"
            subgroup_lines.append(f"| {value} | {len(old)} | {fmt(old_pct)} | {len(new)} | {fmt(new_pct)} | {diff} |")
        subgroup_lines.append("")
    lines = ["# GitHub 头像世代统计（第一版）", "", f"生成时间：{datetime.now(timezone.utc).isoformat()}。研究对象是**所抽样的活跃开源贡献者**，不代表所有 GitHub 用户或程序员。", "", "## 结果", "", "主口径仅把 anime 计为二次元；另列 anime＋character 的宽口径，便于观察定义变化对结果的影响。", "", "| 注册年份 | 入样人数 | 已分类 | 无法判断 | 二次元 / 可判断 | 比例 | 95% Wilson 区间 | 宽口径比例 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for c in cohort_names:
        s = stats[c]
        pct = f"{s['anime_pct']:.1f}%" if s['anime_pct'] is not None else "—"
        ci = f"{s['ci_low']:.1f}–{s['ci_high']:.1f}%" if s["known"] else "—"
        broad_pct = f"{s['broad_pct']:.1f}%" if s["broad_pct"] is not None else "—"
        lines.append(f"| {c} | {s['selected']} | {s['classified']} | {s['uncertain']} | {s['anime']} / {s['known']} | {pct} | {ci} | {broad_pct} |")
    old, new = stats[cohort_names[0]], stats[cohort_names[-1]]
    if old["anime_pct"] is not None and new["anime_pct"] is not None:
        delta = new["anime_pct"] - old["anime_pct"]
        delta_lo = new["ci_low"] - old["ci_high"]
        delta_hi = new["ci_high"] - old["ci_low"]
        lines += ["", f"新组（2024–2026）与老组（2008–2012）的模型标注样本比例差为 **{delta:+.1f} 个百分点**；两组 Wilson 区间端点相减为 {delta_lo:+.1f} 至 {delta_hi:+.1f} 个百分点，仅作描述参考，并非差值的正式置信区间。"]
    lines += ["", "## 人工标注子样本", "", "每组从已入样账号中随机抽 30 张头像，由单人看未显示模型标签的拼图标注。小样本区间较宽，仅用于交叉核对模型趋势。", "", "| 注册年份 | 人工二次元 / 抽查数 | 比例 | 95% Wilson 区间 | 宽口径 / 抽查数 |", "|---|---:|---:|---:|---:|"]
    for cohort in cohort_names:
        human_labels = [row[0] for row in con.execute("SELECT a.human_label FROM audit a JOIN users u ON u.id=a.user_id WHERE u.cohort=?", (cohort,))]
        n = len(human_labels)
        k = sum(label == "anime" for label in human_labels)
        broad_k = sum(label in ("anime", "character") for label in human_labels)
        low, high = wilson(k, n)
        lines.append(f"| {cohort} | {k} / {n} | {100*k/n:.1f}% | {100*low:.1f}–{100*high:.1f}% | {broad_k} / {n} |" if n else f"| {cohort} | 0 / 0 | — | — | 0 / 0 |")
    lines += ["", "人工抽查的严格二次元比例未呈现模型结果中的逐组上升：最新组仅 1/30，最早组为 0/30。每组只有 30 张，不能据此否定或确认总体趋势；预留评估集的二次元正例也很少，无法充分检验各世代的分类误差是否相同。因此，模型比例应视为探索性估计。"]
    profile_status = dict(con.execute("SELECT status,COUNT(*) FROM users GROUP BY status").fetchall())
    avatar_status = dict(con.execute("SELECT status,COUNT(*) FROM avatars GROUP BY status").fetchall())
    class_status = dict(con.execute("SELECT status,COUNT(*) FROM classifications GROUP BY status").fetchall())
    model_counts = dict(con.execute("SELECT model,COUNT(*) FROM classifications WHERE status='ok' GROUP BY model").fetchall())
    audit_text = f"单人盲审标注：{len(audit_rows)} 张（每组 30 张，拼图未显示模型标签）；预留的偶数编号 {len(audit)} 张中，二次元真阳性 {tp}、误判为二次元 {predicted-tp}、漏判 {actual-tp}（精确率 {tp}/{predicted}，召回率 {tp}/{actual}）。" if audit else "尚未完成人工复核；模型标签只应作为探索性结果。"
    lines += ["", "![二次元比例](anime_by_cohort.png)", "", "![头像类别](categories_by_cohort.png)", "", "## 项目组成检查", ""] + subgroup_lines + ["## 数据质量与局限", "", f"- 仓库数：{con.execute('SELECT COUNT(*) FROM repositories').fetchone()[0]}；采样窗口状态：{dict(windows)}；其中 {zero_windows} 个窗口没有默认分支提交。每窗口最多抓取 1,000 条；截断窗口：{', '.join(truncated_windows) if truncated_windows else '无'}。", f"- 用户资料状态：{profile_status}；头像下载状态：{avatar_status}；分类状态：{class_status}；有效模型标签：{model_counts}。", f"- {audit_text}", "- 仓库按语言、星数分层，搜索结果和被抽中的项目仍有选择偏差；提交窗口也只覆盖过去一年的部分日期。", "- 置信区间按独立用户计算，未消除同一仓库贡献者之间的相关性，仅用于描述抽样误差。", "- 真人类别按照片外观判断，未核实是否为本人或 AI 生成。", "- 注册年份是 GitHub 账号创建时间，头像是采集时的当前头像；这不是个人头像变化轨迹，也不能推断生理年龄。", ""]
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(RESULTS / "report.md")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    dp = sub.add_parser("discover")
    dp.add_argument("--per-stratum", type=int, default=4)
    sub.add_parser("commits")
    users = sub.add_parser("users")
    users.add_argument("--max-requests", type=int, default=5000)
    sub.add_parser("users-batch")
    select = sub.add_parser("select-users")
    select.add_argument("--target", type=int, default=300)
    av = sub.add_parser("avatars")
    av.add_argument("--workers", type=int, default=4)
    cp = sub.add_parser("classify")
    cp.add_argument("--limit", type=int, default=0)
    cp.add_argument("--model", default="qwen3-vl:4b")
    sub.add_parser("classify-pipeline")
    sub.add_parser("audit-export")
    imp = sub.add_parser("audit-import")
    imp.add_argument("csv_path", type=Path)
    sub.add_parser("report")
    args = p.parse_args()
    con = db()
    if args.command == "discover": discover(con, GitHub(), args.per_stratum)
    elif args.command == "commits": collect_commits(con, GitHub())
    elif args.command == "users": collect_users(con, GitHub(), args.max_requests)
    elif args.command == "users-batch": collect_users_batch(con, GitHub())
    elif args.command == "select-users": select_users(con, args.target)
    elif args.command == "avatars": download_avatars(con, args.workers)
    elif args.command == "classify": classify(con, args.model, args.limit)
    elif args.command == "classify-pipeline": classify_pipeline(con)
    elif args.command == "audit-export": audit_export(con)
    elif args.command == "audit-import": audit_import(con, args.csv_path)
    elif args.command == "report": report(con)


if __name__ == "__main__":
    main()
