#!/usr/bin/env python3
"""Rotate the Meta system-user token in carousel-app/.env, then prove it works.

The token is read from a hidden prompt and never printed, logged, or echoed.

    python rotate_meta_token.py

Checks after writing:
  1. token is valid and belongs to the carousel-app system user
  2. read_insights is present  (this is the whole point of the rotation)
  3. every scope the publishing pipeline needs survived the rotation
  4. Facebook page insights actually return data now
  5. Instagram insights still work (regression check)

Rolls back automatically if the new token fails checks 1-3.
"""
import getpass
import json
import pathlib
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ENV = HERE / ".env"
KEY = "META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH"
G = "https://graph.facebook.com/v21.0"

# scopes the pipeline depends on; losing any of these silently breaks publishing
REQUIRED = [
    "instagram_basic", "instagram_content_publish", "instagram_manage_comments",
    "instagram_manage_insights", "pages_manage_posts", "pages_read_engagement",
    "pages_show_list", "read_insights",
]


def read_env():
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get(path, tok, **params):
    params["access_token"] = tok
    url = "{}/{}?{}".format(G, path, urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode())["error"]}
        except Exception:
            return {"error": {"message": "HTTP " + str(e.code)}}
    except Exception as e:
        return {"error": {"message": str(e)[:200]}}


def main():
    if not ENV.exists():
        print("ERROR: {} not found".format(ENV))
        return 1

    env = read_env()
    old = env.get(KEY, "")
    page = env.get("FACEBOOK_PAGE_ID")
    ig = env.get("INSTAGRAM_USER_ID")

    print("Paste the new system-user token from Meta Business Suite.")
    print("(input is hidden - nothing is echoed to the screen)\n")
    new = getpass.getpass("New token: ").strip()
    if not new:
        print("No token entered. Nothing changed.")
        return 1
    if new == old:
        print("That is the same token already in .env. Nothing changed.")
        return 1

    print("\nValidating before writing anything...\n")

    d = get("debug_token", new, input_token=new).get("data", {})
    if not d.get("is_valid"):
        print("FAIL  token is not valid. .env untouched.")
        return 1
    scopes = set(d.get("scopes") or [])
    print("PASS  token valid | app: {} | type: {}".format(d.get("application"), d.get("type")))
    print("      expires_at: {}".format(d.get("expires_at") or "0 (never)"))

    missing = [s for s in REQUIRED if s not in scopes]
    if missing:
        print("\nFAIL  the new token is missing required scopes: {}".format(", ".join(missing)))
        print("      Regenerate it with those ticked. .env untouched.")
        return 1
    print("PASS  all {} required scopes present, including read_insights".format(len(REQUIRED)))

    # write it
    backup = ENV.with_suffix(".env.bak")
    shutil.copy2(ENV, backup)
    lines = ENV.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == KEY:
            lines[i] = "{}={}".format(KEY, new)
            break
    else:
        lines.append("{}={}".format(KEY, new))
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("PASS  written to .env  (previous value backed up to {})".format(backup.name))

    # does Facebook insights actually work now?
    print("\nTesting what this was all for...\n")
    acct = get("me/accounts", new)
    ptok = next((a["access_token"] for a in acct.get("data", []) if a["id"] == page), None)
    if not ptok:
        print("WARN  could not obtain a page token for page {}".format(page))
    else:
        r = get("{}/insights".format(page), ptok, metric="page_impressions_unique",
                period="day", since="2026-08-25", until="2026-09-07")
        if "error" in r:
            print("FAIL  FB page insights still refused: {}".format(r["error"].get("message", "")[:120]))
            print("      Permission may take a few minutes to propagate - re-run this check shortly.")
        else:
            vals = (r.get("data") or [{}])[0].get("values", [])
            nz = [v for v in vals if v.get("value")]
            print("PASS  FB page insights: {} days returned, {} with data".format(len(vals), len(nz)))
            if nz:
                print("      sample: {} = {}".format(nz[-1].get("end_time", "")[:10], nz[-1].get("value")))

    r = get("{}/insights".format(ig), new, metric="reach", period="day",
            since="2026-09-01", until="2026-09-07")
    if "error" in r:
        print("FAIL  Instagram insights regressed: {}".format(r["error"].get("message", "")[:120]))
    else:
        print("PASS  Instagram insights still working ({} days)".format(
            len((r.get("data") or [{}])[0].get("values", []))))

    print("\nDone. Nothing above printed the token itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
