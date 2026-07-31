#!/usr/bin/env python3
"""
NAIL Digest — Subscriber Email Sender
======================================
Sends the freshly published issue to every confirmed subscriber. This is the
final step of ni-biweekly.yml, run AFTER the issue has been committed and
pushed — the email links straight to the live page, so the page needs to
already be reachable when subscribers click it.

Usage (run from inside the ni-biweekly/ folder, same as ni_biweekly.py):
    python3 ../send_digest_email.py --issue 3

Requirements:
    python3 -m pip install resend requests
    export RESEND_API_KEY="re_..."
    export SUBSCRIBER_API_KEY="..."   # shared secret with the subscribe Worker
"""

import argparse
import glob
import json
import os
import re
import sys
import time

import requests
import resend

# ← set this to the same Worker URL used in ni_biweekly.py's SUBSCRIBE_WORKER_URL
WORKER_BASE = "https://nail-subscribe.martin-michalowski.workers.dev"
SUBSCRIBERS_URL = f"{WORKER_BASE}/subscribers"
UNSUBSCRIBE_BASE = f"{WORKER_BASE}/unsubscribe"
SITE_BASE = "https://www.nailcollab.org/ni-biweekly"

BRAND = {
    "slate_deep": "#111C26",
    "amber":      "#E8C46A",
    "mut":        "#5E6B76",
    "paper":      "#FAFAF7",
    "line":       "#E4E0D8",
}


def get_confirmed_subscribers() -> list[dict]:
    resp = requests.get(
        SUBSCRIBERS_URL,
        headers={"Authorization": f"Bearer {os.environ['SUBSCRIBER_API_KEY']}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def load_newest_issue_json() -> dict:
    """ni_biweekly.py just wrote today's file into the current directory —
    filenames sort correctly by date (YYYY-MM-DD), so the newest one is last."""
    files = sorted(glob.glob("ni-biweekly-*.json"))
    if not files:
        print("ERROR: no ni-biweekly-*.json found in the current directory. "
              "Run this from inside ni-biweekly/, after ni_biweekly.py.")
        sys.exit(1)
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def build_email_html(issue: dict, unsub_token: str) -> str:
    issue_num = issue.get("issue")
    date_range = issue.get("date_range", {})
    issue_url = f"{SITE_BASE}/ni-biweekly-{issue.get('generated_at', '')[:10]}.html"
    unsub_url = f"{UNSUBSCRIBE_BASE}?token={unsub_token}"

    synth = issue.get("synthesis") or {}
    teaser = ""
    if synth.get("overview"):
        teaser = re.sub(r"</?strong>", "", synth["overview"][0])

    paper_rows = ""
    for p in issue.get("papers", [])[:6]:
        paper_rows += f"""
        <tr><td style="padding:14px 0;border-top:1px solid {BRAND['line']};">
          <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:{BRAND['amber']};">{p.get('topic','')}</div>
          <div style="font-family:Georgia,serif;font-size:16px;color:{BRAND['slate_deep']};margin:4px 0;">
            <a href="{p.get('pubmed_url','')}" style="color:{BRAND['slate_deep']};text-decoration:none;">{p.get('title','')}</a>
          </div>
          <div style="font-size:13.5px;color:{BRAND['mut']};line-height:1.6;">{p.get('summary','')}</div>
        </td></tr>"""

    teaser_html = (
        f"<p style='color:{BRAND['mut']};line-height:1.7;font-size:14.5px;margin:0 0 20px;'>{teaser}</p>"
        if teaser else ""
    )

    return f"""<!DOCTYPE html>
<html><body style="margin:0;background:{BRAND['paper']};font-family:Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:32px 24px;">
  <div style="text-align:center;margin-bottom:24px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:{BRAND['amber']};">NAIL Collaborative</div>
    <h1 style="font-family:Georgia,serif;font-weight:500;color:{BRAND['slate_deep']};font-size:24px;margin:8px 0;">NAIL Digest — Issue #{issue_num}</h1>
    <div style="font-size:13px;color:{BRAND['mut']};">Week of {date_range.get('start','')} – {date_range.get('end','')}</div>
  </div>
  {teaser_html}
  <table width="100%" cellpadding="0" cellspacing="0">{paper_rows}</table>
  <div style="text-align:center;margin:32px 0;">
    <a href="{issue_url}" style="display:inline-block;background:{BRAND['amber']};color:{BRAND['slate_deep']};
       font-weight:700;text-decoration:none;padding:12px 28px;border-radius:99px;">Read the full issue</a>
  </div>
  <div style="text-align:center;font-size:11.5px;color:{BRAND['mut']};padding-top:20px;border-top:1px solid {BRAND['line']};">
    NAIL Digest · NAIL Collaborative ·
    <a href="{unsub_url}" style="color:{BRAND['mut']};">Unsubscribe</a>
  </div>
</div>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Send the NAIL Digest issue to confirmed subscribers")
    parser.add_argument("--issue", type=int, required=True, help="Issue number just published")
    args = parser.parse_args()

    resend.api_key = os.environ["RESEND_API_KEY"]

    issue = load_newest_issue_json()
    if issue.get("issue") != args.issue:
        print(f"  WARNING: newest JSON is issue #{issue.get('issue')}, expected #{args.issue}. Using it anyway.")

    print(f"► Fetching confirmed subscriber list...")
    subscribers = get_confirmed_subscribers()
    print(f"  {len(subscribers)} confirmed subscriber(s)")

    if not subscribers:
        print("  No subscribers yet — nothing to send.")
        return

    print(f"► Sending Issue #{args.issue} to {len(subscribers)} subscriber(s)...")
    sent = failed = 0
    for sub in subscribers:
        html = build_email_html(issue, sub["unsub_token"])
        for attempt in range(2):
            try:
                resend.Emails.send({
                    "from": "NAIL Digest <hello@digest.nailcollab.org>",
                    "reply_to": "ainurse@nailcollab.org",
                    "to": [sub["email"]],
                    "subject": f"NAIL Digest Issue #{args.issue} is out",
                    "html": html,
                })
                sent += 1
                break
            except Exception as e:
                print(f"  SEND ERROR for {sub['email']} (attempt {attempt + 1}): {type(e).__name__}: {e}")
                if attempt == 1:
                    failed += 1
                time.sleep(1)
        time.sleep(0.15)  # stay comfortably under Resend's rate limit

    print(f"\n✓ Done. {sent} sent · {failed} failed.\n")
    if failed:
        sys.exit(1)  # surface failures as a red X in the Actions log


if __name__ == "__main__":
    main()
