"""Review-first LinkedIn text publisher. Python stdlib only; no unattended approval.

Credentials are imported in a real interactive console and encrypted with Windows
DPAPI. Network calls never follow redirects or automatically retry. See README.md.
"""
import argparse
import ctypes
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA_HOME = Path(os.environ.get("LINKEDIN_AGENT_HOME", str(Path.home() / ".linkedin-agent"))).expanduser().resolve()
OUTPUTS = DATA_HOME / "outputs"
WORK = DATA_HOME / "work"
API_VERSION = "202609"  # Working pinned version; review LinkedIn support before changing.
EXPECTED_NAME = "LinkedIn account owner"
POSTS = "https://api.linkedin.com/rest/posts"
USERINFO = "https://api.linkedin.com/v2/userinfo"
SCOPES = {"openid", "profile", "w_member_social"}


class GuardError(Exception):
    pass


class HttpError(GuardError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"LinkedIn returned HTTP {status}; response body withheld.")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)



def settings():
    path = WORK / "settings.json"
    defaults = {"timezone": "UTC", "post_days": [0, 2, 4]}
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        days = data["post_days"]
        if not isinstance(data.get("timezone"), str) or not data["timezone"].strip():
            raise ValueError()
        if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days) or len(set(days)) != len(days):
            raise ValueError()
        return {"timezone": data["timezone"], "post_days": days}
    except (OSError, ValueError, KeyError, TypeError):
        raise GuardError("Invalid local settings; use a timezone name and unique post-day numbers 0-6.") from None


def initialize(timezone, days):
    names = {name: i for i, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
    if not timezone.strip() or not days or any(day not in names for day in days) or len(set(days)) != len(days):
        raise GuardError("Use a timezone name and unique post days such as mon wed fri.")
    path = WORK / "settings.json"
    if path.exists():
        raise GuardError("Settings already exist; edit them locally rather than silently overwriting.")
    WORK.mkdir(parents=True, exist_ok=True)
    data = {"timezone": timezone.strip(), "post_days": [names[day] for day in days]}
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    return {"status": "initialized", "data_home": str(DATA_HOME), **data}


def stage(draft_id, file, kind, topic):
    if not re.fullmatch(r"LI-[0-9]{8}-[A-Z0-9-]+-v[0-9]+", draft_id):
        raise GuardError("Invalid draft ID; use LI-YYYYMMDD-TOPIC-v1.")
    try:
        date = dt.datetime.strptime(draft_id[3:11], "%Y%m%d").date().isoformat()
        text = Path(file).read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    except (OSError, UnicodeError, ValueError):
        raise GuardError("Read a UTF-8 text file and use a valid date in the draft ID.") from None
    if not text.strip() or len(text) > 3000:
        raise GuardError("A post must contain 1-3,000 characters.")
    base = OUTPUTS / "drafts" / draft_id
    if base.with_suffix(".txt").exists() or base.with_suffix(".json").exists():
        raise GuardError("This version already exists; stage a new version ID.")
    if kind == "scheduled":
        claim_slot(date, draft_id)
    raw = text.encode("utf-8")
    meta = {"draft_id": draft_id, "kind": kind, "sha256": digest(raw),
            "characters": len(text), "local_date": date, "timezone": settings()["timezone"],
            "topic": topic, "created_at": now(), "status": "pending_review"}
    base.parent.mkdir(parents=True, exist_ok=True)
    with base.with_suffix(".txt").open("xb") as handle:
        handle.write(raw)
    with base.with_suffix(".json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {**meta, "text_path": str(base.with_suffix(".txt")), "text": text}


def member_urn(value):
    if not re.fullmatch(r"urn:li:person:[A-Za-z0-9_-]+", value or ""):
        raise GuardError("A personal member URN is required.")
    return value


def post_urn(value):
    if not re.fullmatch(r"urn:li:(share|ugcPost):[0-9]+", value or ""):
        raise GuardError("A returned share/ugcPost URN is required.")
    return value


def load_draft(draft_id):
    if not re.fullmatch(r"LI-[0-9]{8}-[A-Z0-9-]+-v[0-9]+", draft_id):
        raise GuardError("Invalid draft ID.")
    manifest = OUTPUTS / "drafts" / (draft_id + ".json")
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        raw = manifest.with_suffix(".txt").read_bytes()
    except (OSError, ValueError):
        raise GuardError("Saved draft or manifest is missing/invalid.") from None
    if meta.get("draft_id") != draft_id or digest(raw) != meta.get("sha256"):
        raise GuardError("Saved text hash does not match the reviewed version.")
    text = raw.decode("utf-8")
    if not text.strip() or len(text) > 3000 or "\r" in text:
        raise GuardError("Draft must be UTF-8, LF, nonempty and at most 3,000 characters.")
    if meta.get("kind") not in ("sample", "scheduled", "requested"):
        raise GuardError("Invalid draft kind.")
    return meta, text


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def database():
    WORK.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(WORK / "state.sqlite3", timeout=10, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS approvals (
          draft_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, author TEXT,
          approved_at TEXT NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS submissions (
          draft_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, author TEXT NOT NULL,
          status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
          post_urn TEXT, url TEXT, http_status INTEGER, UNIQUE(sha256, author));
        CREATE TABLE IF NOT EXISTS scheduled_slots (
          local_date TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS withdrawals (
          draft_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
          withdrawn_at TEXT NOT NULL, evidence TEXT NOT NULL);
    """)
    return db


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, url, token, payload=None):
    if url not in (USERINFO, POSTS):
        raise GuardError("Only the two configured official LinkedIn endpoints are allowed.")
    if (url == USERINFO and method != "GET") or (url == POSTS and method != "POST"):
        raise GuardError("Unsupported endpoint operation.")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.update({"Content-Type": "application/json",
                        "LinkedIn-Version": API_VERSION,
                        "X-Restli-Protocol-Version": "2.0.0"})
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            data = response.read(1024 * 1024)
            return response.status, dict(response.headers), data
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise GuardError("LinkedIn request failed; sensitive error details withheld.") from None


def identity(token):
    status, _, raw = request("GET", USERINFO, token)
    if status != 200:
        raise GuardError("UserInfo did not return HTTP 200.")
    try:
        info = json.loads(raw)
        author = member_urn("urn:li:person:" + info["sub"])
        name = info.get("name") or " ".join(
            [info.get("given_name", ""), info.get("family_name", "")]).strip()
    except (ValueError, KeyError, TypeError):
        raise GuardError("LinkedIn identity response is invalid.") from None
    return {"author": author, "name": name}


def dpapi(data, decrypt=False):
    if os.name != "nt":
        raise GuardError("Credential storage requires Windows DPAPI.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(src), "LinkedIn agent token", None, None, None, 1,
                ctypes.byref(out))
    fn.restype = wintypes.BOOL
    if not ok:
        raise GuardError("Windows credential encryption/decryption failed.")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel.LocalFree(ctypes.cast(out.data, wintypes.HLOCAL))


def load_credentials():
    path = WORK / "private" / "token.dpapi"
    if not path.exists():
        raise GuardError("Account authorization is pending. Run connect in your own console.")
    try:
        creds = json.loads(dpapi(path.read_bytes(), decrypt=True))
        expires = dt.datetime.fromisoformat(creds["expires_at"])
        if expires.tzinfo is None or expires <= dt.datetime.now(dt.timezone.utc):
            raise GuardError("Token expiry reached; reauthorize. Approved drafts are retained.")
        if not SCOPES.issubset(set(creds["scopes"])):
            raise GuardError("The configured granted scopes are incomplete.")
        member_urn(creds["author"])
        if creds["intended_name"] != EXPECTED_NAME or not creds["access_token"]:
            raise GuardError("Credential identity binding is invalid.")
        return creds
    except (ValueError, OSError, KeyError, TypeError):
        raise GuardError("Private credentials are invalid; reconnect.") from None


def parse_expiry(value, reference_time=None):
    """Accept the portal's Unix expiry or an explicit timezone-aware ISO date."""
    raw = value.strip()
    if raw.upper().endswith(" UTC"):
        raw = raw[:-4].strip()
        if not re.fullmatch(r"[0-9]+", raw):
            raw += "+00:00"
    try:
        if re.fullmatch(r"[0-9]+", raw):
            expiry = dt.datetime.fromtimestamp(int(raw), dt.timezone.utc)
        else:
            expiry = dt.datetime.fromisoformat(raw)
        current = reference_time if reference_time is not None else dt.datetime.now(dt.timezone.utc)
        if expiry.tzinfo is None or expiry <= current:
            raise ValueError()
        return expiry.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise GuardError(
            "Enter a future Unix timestamp (seconds, optionally followed by UTC) "
            "or an ISO 8601 timestamp with a timezone."
        ) from None


def connect():
    if not sys.stdin.isatty():
        raise GuardError("Run connect yourself in a real interactive console; no chat secrets.")
    token = getpass.getpass("LinkedIn access token (hidden): ").strip()
    expires_at = parse_expiry(
        input("Actual token expiry, Unix seconds or ISO 8601 with timezone: ")
    ).isoformat()
    scopes = input("Actual granted scopes from the portal (space separated): ").split()
    if not SCOPES.issubset(set(scopes)):
        raise GuardError("Grant openid, profile, and w_member_social before connecting.")
    member = identity(token)  # Only GET; never a test post.
    print(json.dumps(member, ensure_ascii=False, indent=2))
    if input("Verify this is your intended profile; type the exact author URN: ").strip() != member["author"]:
        raise GuardError("Member confirmation was not supplied; no credentials saved.")
    existing = WORK / "identity.json"
    if existing.exists():
        old = json.loads(existing.read_text(encoding="utf-8"))
        if old["author"] != member["author"]:
            raise GuardError("Connected member changed; do not overwrite this account binding.")
    creds = {"access_token": token, "expires_at": expires_at, "scopes": scopes,
             "author": member["author"], "intended_name": EXPECTED_NAME}
    private = WORK / "private"
    private.mkdir(parents=True, exist_ok=True)
    sealed = dpapi(json.dumps(creds).encode("utf-8"))
    path = private / "token.dpapi"
    tmp = private / "token.tmp"
    with tmp.open("wb") as f:
        f.write(sealed)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    atomic_json(existing, {**member, "intended_name": EXPECTED_NAME,
                           "confirmed_at": now(), "expires_at": expires_at})
    print("Encrypted token saved. Posting scope declared; live posting has not been tested.")


def auth_check():
    creds = load_credentials()
    live = identity(creds["access_token"])
    if live["author"] != creds["author"]:
        raise GuardError("Live token identity differs from the confirmed publishing account.")
    return {**live, "expires_at": creds["expires_at"], "identity_check": "passed",
            "posting_scope": "declared in setup; endpoint access untested"}


def little_text(text):
    # Escape once for LinkedIn's text grammar; JSON serialization escapes the wire
    # backslashes separately. This preserves the reviewed visible plain text.
    reserved = set("|{}@[]()<>#\\*_~")
    return "".join("\\" + char if char in reserved else char for char in text)


def payload(text, author):
    return {"author": member_urn(author), "commentary": little_text(text), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}


def approve(draft_id, sha256, evidence, author=None):
    # Evidence is recorded by the agent ONLY after a direct human approval in chat.
    meta, _ = load_draft(draft_id)
    if meta["kind"] == "sample":
        raise GuardError("Samples cannot be approved for publication. Create a reviewable real version.")
    if sha256 != meta["sha256"] or not evidence.strip():
        raise GuardError("Approval requires the exact reviewed SHA-256 and direct-human evidence.")
    if author:
        member_urn(author)
    with database() as db:
        if db.execute("SELECT 1 FROM withdrawals WHERE draft_id=?", (draft_id,)).fetchone():
            raise GuardError("This version was withdrawn; review a new version before approving.")
        if db.execute("SELECT 1 FROM submissions WHERE draft_id=?", (draft_id,)).fetchone():
            raise GuardError("A submission record already exists; approval cannot reset it.")
        previous = db.execute("SELECT * FROM approvals WHERE draft_id=?", (draft_id,)).fetchone()
        if previous and (previous["sha256"] != sha256 or
                         (previous["author"] and previous["author"] != author)):
            raise GuardError("Approval binding cannot be changed.")
        db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?)",
                   (draft_id, sha256, author, now(), evidence))
    return {"draft_id": draft_id, "status": "approved" if author else "approved_pending_identity",
            "sha256": sha256, "author": author, "intended_name": EXPECTED_NAME}


def publish(draft_id):
    meta, text = load_draft(draft_id)
    if meta["kind"] == "sample":
        raise GuardError("Samples are blocked from live publication.")
    # Fail before any request if approval or credential binding is absent.
    with database() as db:
        approval = db.execute("SELECT * FROM approvals WHERE draft_id=?", (draft_id,)).fetchone()
        withdrawn = db.execute("SELECT 1 FROM withdrawals WHERE draft_id=?", (draft_id,)).fetchone()
        previous = db.execute("SELECT * FROM submissions WHERE draft_id=? OR sha256=?",
                              (draft_id, meta["sha256"])).fetchone()
    if withdrawn:
        raise GuardError("This reviewed version was withdrawn; publication is blocked.")
    if previous:
        raise GuardError(f"Submission already recorded: {previous['status']}; no automatic retry.")
    if not approval or approval["sha256"] != meta["sha256"]:
        raise GuardError("No direct-human approval exists for this exact text version.")
    if not approval["author"]:
        raise GuardError("Approval retained; confirm and bind the intended member before publishing.")
    creds = load_credentials()
    if approval["author"] != creds["author"]:
        raise GuardError("Approved member differs from the confirmed token identity.")
    live = identity(creds["access_token"])
    if live["author"] != approval["author"]:
        raise GuardError("Live member identity differs from the approved account.")
    data = payload(text, live["author"])
    # Durable transaction before the single POST. SQLite enforces cross-process uniqueness.
    with database() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM withdrawals WHERE draft_id=?", (draft_id,)).fetchone():
                raise GuardError("Approval was withdrawn before submission.")
            db.execute("INSERT INTO submissions(draft_id,sha256,author,status,started_at) VALUES (?,?,?,?,?)",
                       (draft_id, meta["sha256"], live["author"], "in_flight", now()))
        except sqlite3.IntegrityError:
            raise GuardError("Duplicate submission blocked.") from None
    try:
        status, headers, _ = request("POST", POSTS, creds["access_token"], data)
        urn = next((v for k, v in headers.items() if k.lower() == "x-restli-id"), None)
        if status != 201:
            raise GuardError("Unexpected create response; outcome must be reconciled.")
        post_urn(urn)
        url = "https://www.linkedin.com/feed/update/" + urn + "/"
        with database() as db:
            db.execute("UPDATE submissions SET status='published',finished_at=?,post_urn=?,url=?,http_status=? WHERE draft_id=?",
                       (now(), urn, url, status, draft_id))
        return {"draft_id": draft_id, "status": "published", "post_urn": urn, "url": url,
                "sha256": meta["sha256"]}
    except BaseException as exc:
        status = exc.status if isinstance(exc, HttpError) else None
        # 4xx/3xx rejection also stays locked until investigated; 5xx/timeouts are uncertain.
        outcome = "rejected" if status and 300 <= status < 500 else "unknown"
        with database() as db:
            db.execute("UPDATE submissions SET status=?,finished_at=?,http_status=? WHERE draft_id=? AND status='in_flight'",
                       (outcome, now(), status, draft_id))
        raise GuardError(f"Submission {outcome}; do not retry. Verify the profile and reconcile.") from None


def withdraw(draft_id, evidence):
    meta, _ = load_draft(draft_id)
    if not evidence.strip():
        raise GuardError("Withdrawal requires a direct-human skip/revision reference.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM submissions WHERE draft_id=?", (draft_id,)).fetchone():
            raise GuardError("A submission exists; withdrawal cannot retract or cancel a sent request.")
        db.execute("INSERT OR IGNORE INTO withdrawals VALUES (?,?,?,?)",
                   (draft_id, meta["sha256"], now(), evidence))
    return {"draft_id": draft_id, "status": "withdrawn"}


def reconcile(draft_id, urn, evidence):
    post_urn(urn)
    if not evidence.strip():
        raise GuardError("Manual verification evidence is required.")
    with database() as db:
        row = db.execute("SELECT * FROM submissions WHERE draft_id=?", (draft_id,)).fetchone()
        if not row or row["status"] not in ("unknown", "in_flight"):
            raise GuardError("Only uncertain/in-flight submissions can be reconciled.")
        url = "https://www.linkedin.com/feed/update/" + urn + "/"
        db.execute("UPDATE submissions SET status='published_reconciled',finished_at=?,post_urn=?,url=? WHERE draft_id=?",
                   (now(), urn, url, draft_id))
    # This does not assert API read access or allow a resubmission.
    atomic_json(WORK / (draft_id + "-reconciliation.json"),
                {"draft_id": draft_id, "evidence": evidence, "post_urn": urn, "url": url})
    return {"status": "published_reconciled", "url": url}


def claim_slot(date, draft_id):
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        raise GuardError("Use an ISO local date in the configured scheduler timezone.") from None
    if day.weekday() not in settings()["post_days"]:
        raise GuardError("Scheduled original drafts are limited to the configured post days.")
    with database() as db:
        previous = db.execute("SELECT draft_id FROM scheduled_slots WHERE local_date=?", (date,)).fetchone()
        if previous:
            if previous["draft_id"] == draft_id:
                return {"status": "already_claimed", "draft_id": draft_id}
            raise GuardError("A scheduled draft already exists or is reserved for this local date.")
        db.execute("INSERT INTO scheduled_slots VALUES (?,?)", (date, draft_id))
    return {"status": "claimed", "local_date": date, "draft_id": draft_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("connect")
    sub.add_parser("auth-check")
    sub.add_parser("status")
    init = sub.add_parser("init")
    init.add_argument("--timezone", default="UTC")
    init.add_argument("--post-days", nargs="+", default=["mon", "wed", "fri"])
    draft = sub.add_parser("stage")
    draft.add_argument("draft_id")
    draft.add_argument("--file", required=True)
    draft.add_argument("--kind", choices=["sample", "scheduled", "requested"], default="requested")
    draft.add_argument("--topic", default="")
    for action in ("dry-run", "approve", "publish", "reconcile", "withdraw"):
        p = sub.add_parser(action)
        p.add_argument("draft_id")
        if action == "approve":
            p.add_argument("--sha256", required=True)
            p.add_argument("--evidence", required=True)
            p.add_argument("--author")
        if action == "reconcile":
            p.add_argument("--post-urn", required=True)
            p.add_argument("--evidence", required=True)
        if action == "withdraw":
            p.add_argument("--evidence", required=True)
    p = sub.add_parser("claim-slot")
    p.add_argument("local_date")
    p.add_argument("draft_id")
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(json.dumps(initialize(args.timezone, args.post_days), indent=2))
            return
        if args.command == "stage":
            print(json.dumps(stage(args.draft_id, args.file, args.kind, args.topic), ensure_ascii=False, indent=2))
            return
        if args.command == "connect":
            connect()
            return
        if args.command == "auth-check":
            result = auth_check()
        elif args.command == "dry-run":
            meta, text = load_draft(args.draft_id)
            result = {"draft_id": args.draft_id, "sha256": meta["sha256"],
                      "characters": len(text), "network_calls": 0,
                      "api_version": API_VERSION, "sample_blocked": meta["kind"] == "sample",
                      "payload": payload(text, "urn:li:person:OFFLINE_PLACEHOLDER")}
        elif args.command == "approve":
            result = approve(args.draft_id, args.sha256, args.evidence, args.author)
        elif args.command == "publish":
            result = publish(args.draft_id)
        elif args.command == "reconcile":
            result = reconcile(args.draft_id, args.post_urn, args.evidence)
        elif args.command == "withdraw":
            result = withdraw(args.draft_id, args.evidence)
        elif args.command == "claim-slot":
            result = claim_slot(args.local_date, args.draft_id)
        else:
            with database() as db:
                result = {"account_configured": (WORK / "private" / "token.dpapi").exists(),
                          "approvals": [dict(r) for r in db.execute("SELECT * FROM approvals")],
                          "submissions": [dict(r) for r in db.execute("SELECT * FROM submissions")],
                          "withdrawals": [dict(r) for r in db.execute("SELECT * FROM withdrawals")],
                          "scheduled_slots": [dict(r) for r in db.execute("SELECT * FROM scheduled_slots")]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except GuardError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except Exception:
        print("Local operation failed; sensitive details withheld. Inspect saved state before retrying.", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
