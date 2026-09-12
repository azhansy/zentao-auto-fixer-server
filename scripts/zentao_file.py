#!/usr/bin/env python3
"""Download a ZenTao attachment URL (e.g. a screenshot referenced in bug steps) to a local path.

Usage: python3 scripts/zentao_file.py <file-url> <output-path>

The web session login is the same one the service uses for comments; the URL must be
a file-read-* URL taken from the bug detail, not an arbitrary path.
"""
import json
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    for raw_line in (PROJECT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        if key.strip() and key.strip() not in os.environ:
            try:
                os.environ[key.strip()] = " ".join(shlex.split(raw_value.strip(), posix=True))
            except ValueError:
                pass


def _request(method: str, url: str, form=None, cookie: str = "", accept_json: bool = True) -> bytes:
    headers = {"Accept": "application/json" if accept_json else "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if form is not None:
        body = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=30) as response:
        return response.read()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: zentao_file.py <file-url> <output-path>")
    _load_env()
    base = os.environ["ZENTAO_BASE_URL"].rstrip("/")
    file_url, out_path = sys.argv[1], sys.argv[2]

    session_data = json.loads(_request("GET", f"{base}/api-getSessionID.json"))
    session = session_data.get("data") if isinstance(session_data, dict) else None
    if isinstance(session, str):
        session = json.loads(session)
    if not isinstance(session, dict) or not session.get("sessionName") or not session.get("sessionID"):
        raise SystemExit("Could not obtain ZenTao web session.")
    cookie = f"{session['sessionName']}={session['sessionID']}"

    login = json.loads(_request(
        "POST",
        f"{base}/user-login.json",
        form={"account": os.environ["ZENTAO_ACCOUNT"], "password": os.environ["ZENTAO_PASSWORD"]},
        cookie=cookie,
    ))
    if not isinstance(login, dict) or login.get("status") != "success":
        raise SystemExit(f"ZenTao web login failed: {login}")

    data = _request("GET", file_url, cookie=cookie, accept_json=False)
    if not data:
        raise SystemExit(f"Downloaded 0 bytes from {file_url}")
    Path(out_path).write_bytes(data)
    print(f"Downloaded {len(data)} bytes to {out_path}")


if __name__ == "__main__":
    main()
