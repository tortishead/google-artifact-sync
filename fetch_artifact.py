#!/usr/bin/env python3
"""Download signed gms_car_prod builds from ci.android.com.

Downloads need an ID token from your corporate Google login. The first run
(or --login) opens a Chrome window where you sign in on ci.android.com. The
browser session is kept in a local profile directory, so later runs pick up
a fresh token without showing a window.
"""

import argparse
import base64
import json
import re
import time
import sys
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

DEFAULT_PROFILE = Path.home() / ".cache" / "ci-fetch" / "profile"
CHUNK_SIZE = 1 << 20
ARCHES = ("x86_64", "arm64")

BUILD_PAGE = "https://ci.android.com/builds/submitted/{build}/{target}/latest/"
TARGET = "gms_car_prod_{arch}-user"
ARTIFACT = "signed/signed-gms_car_prod_{arch}-{build}.zip"

ID_TOKEN_RE = re.compile(r"idToken=(eyJ[\w-]+\.[\w-]+\.[\w-]+)")
# ci.android.com signs in through gapi.auth2; ask it for a freshly issued ID token
# (the cached one may already be expired).
GAPI_ID_TOKEN_JS = """async () => {
  try {
    const auth = gapi.auth2.getAuthInstance();
    if (!auth || !auth.isSignedIn.get()) return null;
    const resp = await Promise.race([
      auth.currentUser.get().reloadAuthResponse(),
      new Promise((_, reject) => setTimeout(() => reject("timeout"), 15000)),
    ]);
    return resp.id_token || null;
  } catch (e) { return null; }
}"""
# On a rejected token the server serves its viewer page, which embeds these variables.
JS_VARIABLES_RE = re.compile(r"var JSVariables = (\{.*?\});")


class AuthRequired(Exception):
    pass


def download_url(build, arch, token):
    path = BUILD_PAGE.format(build=build, target=TARGET.format(arch=arch))
    artifact = quote(ARTIFACT.format(arch=arch, build=build), safe="")
    return f"{path}{artifact}?idToken={token}"


def launch(pw, profile, headless):
    profile.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(profile), headless=headless)
    try:
        # Google sign-in accepts real Chrome more readily than bundled Chromium.
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except PlaywrightError:
        return pw.chromium.launch_persistent_context(**kwargs)


def open_build_page(ctx, url):
    """Open the build page and record any ID token seen in its requests or links."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    seen = []

    def on_request(req):
        m = ID_TOKEN_RE.search(req.url) or ID_TOKEN_RE.search(req.post_data or "")
        if m:
            seen.append(m.group(1))

    page.on("request", on_request)
    page.goto(url, wait_until="networkidle")
    return page, seen


def read_token(page, seen, wait_ms=5000):
    for _ in range(wait_ms // 500):
        token = page.evaluate(GAPI_ID_TOKEN_JS)
        if token:
            return token
        if seen:
            return seen[-1]
        hrefs = page.eval_on_selector_all("a[href*='idToken=']", "as => as.map(a => a.href)")
        for href in hrefs:
            m = ID_TOKEN_RE.search(href)
            if m:
                return m.group(1)
        page.wait_for_timeout(500)
    return None


def describe_token(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return "unreadable token"
    minutes = (claims.get("exp", 0) - time.time()) / 60
    return f"token for {claims.get('email', '?')}, expires in {minutes:.0f} min"


def get_token(pw, profile, page_url, force_login):
    token = _get_token(pw, profile, page_url, force_login)
    print(f"Got {describe_token(token)}")
    return token


def _get_token(pw, profile, page_url, force_login):
    if not force_login:
        ctx = launch(pw, profile, headless=True)
        try:
            token = read_token(*open_build_page(ctx, page_url))
        finally:
            ctx.close()
        if token:
            return token
        print("Not signed in (no ID token found on the build page).")

    ctx = launch(pw, profile, headless=False)
    try:
        page, seen = open_build_page(ctx, page_url)
        print("\nSign in with your corporate account in the browser window")
        print("(use the page's Sign in button if it shows one).")
        print("When the build page shows you as signed in, press Enter here...", end="", flush=True)
        input()
        token = read_token(page, seen)
    finally:
        ctx.close()
    if not token:
        raise AuthRequired("still no ID token after sign-in")
    return token


def show_progress(name, done, total):
    mb = done / 1e6
    if total:
        print(f"\r{name}: {mb:,.0f} / {total / 1e6:,.0f} MB ({done * 100 // total}%)", end="", flush=True)
    else:
        print(f"\r{name}: {mb:,.0f} MB", end="", flush=True)


def download(url, out_dir, name=None):
    dest = out_dir / (name or unquote(urlparse(url).path).rsplit("/", 1)[-1])
    part = dest.with_name(dest.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})

    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except HTTPError as e:
        if e.code == 416:  # partial file is already complete
            part.rename(dest)
            return dest
        if e.code in (401, 403):
            raise AuthRequired(f"HTTP {e.code}: token rejected") from None
        raise
    with resp:
        if urlparse(resp.geturl()).hostname == "accounts.google.com":
            raise AuthRequired("redirected to Google sign-in")
        if "text/html" in resp.headers.get("Content-Type", ""):
            # The artifact viewer page: says whether the token was accepted and
            # may point at the real file location.
            m = JS_VARIABLES_RE.search(resp.read().decode("utf-8", "replace"))
            info = json.loads(m.group(1)) if m else {}
            if info.get("authed") and info.get("artifactUrl") and info["artifactUrl"] != url:
                return download(info["artifactUrl"], out_dir, dest.name)
            raise AuthRequired(f"server did not accept the token (authed={info.get('authed')})")
        if resp.status != 206:
            offset = 0  # server ignored Range; start over
        total = int(resp.headers.get("Content-Length", 0)) + offset or None
        done = offset
        with open(part, "ab" if offset else "wb") as f:
            while chunk := resp.read(CHUNK_SIZE):
                f.write(chunk)
                done += len(chunk)
                show_progress(dest.name, done, total)
        print()
    part.rename(dest)
    return dest


def fetch_builds(builds, arches, out_dir, profile=DEFAULT_PROFILE, force_login=False):
    """Download each build/arch zip into out_dir.

    Returns ({(build, arch): path}, number of failures). Zips already in out_dir
    are reused without contacting the server.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved, todo = {}, []
    for build in builds:
        for arch in arches:
            dest = out_dir / Path(ARTIFACT.format(arch=arch, build=build)).name
            if dest.exists():
                print(f"Using existing {dest}")
                saved[build, arch] = dest
            else:
                todo.append((build, arch))
    if not todo:
        return saved, 0

    first_page = BUILD_PAGE.format(build=todo[0][0], target=TARGET.format(arch=todo[0][1]))
    failed = 0
    with sync_playwright() as pw:
        token = get_token(pw, profile, first_page, force_login)
        relogged = force_login

        for build, arch in todo:
            while True:
                try:
                    saved[build, arch] = download(download_url(build, arch, token), out_dir)
                    print(f"Saved {saved[build, arch]}")
                except AuthRequired as e:
                    if not relogged:
                        # Saved session gave a stale/rejected token: sign in once more and retry.
                        print(f"Rejected ({e}); signing in again.")
                        relogged = True
                        token = get_token(pw, profile, first_page, force_login=True)
                        continue
                    failed += 1
                    print(f"Failed build {build} ({arch}): {e}", file=sys.stderr)
                except Exception as e:
                    failed += 1
                    print(f"Failed build {build} ({arch}): {e}", file=sys.stderr)
                break
    return saved, failed


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("builds", nargs="+", help="build ID(s), e.g. 12345678")
    p.add_argument("-a", "--arch", choices=ARCHES, action="append",
                   help="architecture; repeat for both (default: x86_64)")
    p.add_argument("-o", "--out", type=Path, default=Path.cwd(), help="output directory (default: cwd)")
    p.add_argument("--login", action="store_true", help="open the browser to sign in again")
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help=f"browser profile dir (default: {DEFAULT_PROFILE})")
    args = p.parse_args()

    try:
        _, failed = fetch_builds(args.builds, args.arch or ["x86_64"], args.out, args.profile, args.login)
    except AuthRequired as e:
        sys.exit(f"Login failed: {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
