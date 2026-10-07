# ReferralPilot

Local-first engine for the fresher job hunt: it **discovers entry-level engineering roles** on public
job boards, **tailors a LaTeX resume** to each job description with an ATS match score, **finds referral
contacts** at the company, and **drip-feeds personalised referral requests** with hard anti-spam limits.
A Kanban dashboard tracks every job from *Discovered* to *Referred*.

Everything runs on your machine: SQLite for state, APScheduler for background work, FastAPI + htmx
for the dashboard. Sending is **dry-run by default**, and nothing leaves the queue without your approval.

```
 Greenhouse / Lever / Ashby / YC ──► Harvester ──► filters (titles, 3+/5+ yrs, Senior/Staff/Lead) ──► SQLite (dedupe)
                                                                                                        │
         exports/{company}_{role}_resume.pdf ◄── LaTeX tailor ◄── JD parser + ATS score ◄───────────────┤
                                                                                                        │
             Hunter / Apollo / Brave / Google CSE / DuckDuckGo ──► Prospector (domain + email pattern) ──┤
                                                                                                        │
    you approve ──► rate-limited queue (≤20/24h, 3–7 min gaps) ──► Gmail API / SMTP / dry-run ──► follow-up after 4 days
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate          # Python 3.11+
pip install -e ".[dev]"                                     # add ,gmail,typst,dns as needed
cp .env.example .env                                        # optional - safe defaults without it

referralpilot verify          # step 7: fetch → match → tailor → prospect → queue → dry-run send (24 checks, ~2s)
referralpilot init            # create data/referralpilot.db, seed profile + config/companies.json
referralpilot serve           # dashboard at http://127.0.0.1:8000 (+ background scheduler)
```

Want to click around before configuring anything? `referralpilot serve --demo` runs the dashboard
against bundled mock Greenhouse/Lever/Ashby/YC/Hunter/DuckDuckGo responses (fully offline; run
`referralpilot demo` once first to load the mock jobs).

Then:

1. Replace the sample profile in `config/candidate_profile.json` (or edit it on the **Profile** page).
2. Pick target companies in `config/companies.json` (or on the **Companies** page) and click **Harvest now**.
3. Open a job card → **Compile tailored resume** → **Find contacts** → **Draft email** → review/edit → **Approve & queue**.
4. Keep `EMAIL_BACKEND=dry_run` until the `.eml` files in `exports/outbox/` look right, then switch to `smtp` or `gmail_api`.

## Use it in your browser (GitHub Pages)

**https://jatin-29.github.io/Referral-Pilot/** — no install. The site is the same dashboard, with the
same Python code, running *inside your browser tab* on [Pyodide](https://pyodide.org) (Python
compiled to WebAssembly). The first visit downloads up to about 20 MB; later visits start from cache.

| | Local install | Browser edition |
|---|---|---|
| Your data | `data/referralpilot.db` | the browser's storage (IndexedDB) — never uploaded; **Settings → Download backup** to move it |
| Job discovery | APScheduler crawl every 6 h | a GitHub Action crawls every 6 h and publishes `jobs.json`; the tab also calls board APIs directly when a board allows browser requests |
| Resumes | pdflatex → Typst → fpdf | pure-Python PDF in the browser, plus **Open in Overleaf** for the LaTeX version |
| Contacts | Hunter, Apollo, Brave, Google CSE, DuckDuckGo | address-pattern guesses, Google CSE, MX checks over DNS-over-HTTPS; the other APIs block browsers unless you set a CORS relay in Settings |
| Sending | dry run, SMTP, Gmail API | dry run, Gmail (OAuth sign-in in the page), or **Open in Gmail** + **Mark as sent** |
| Background work | always on | only while the tab is open (keep it pinned on sending days); one tab at a time |

The hard limits are identical: at most 20 emails per rolling 24 hours, 3–7 minutes apart, inside your
send window, nothing sent without approval, opt-outs suppressed forever.

**Publishing your own copy (one time):** in the repository go to *Settings → Pages → Build and
deployment* and set *Source* to **GitHub Actions**, then run *Actions → GitHub Pages → Run workflow*.
The workflow (`.github/workflows/pages.yml`) runs the tests, crawls every board in
`config/companies.json`, builds the site, smoke-tests it in Chrome and deploys it — and repeats the
crawl every 6 hours. (GitHub pauses scheduled workflows after 60 days without commits; re-enable it
from the Actions tab if the job list stops refreshing.)

**Sending real email from the browser (optional):** the page needs your own Google OAuth client.

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and enable the **Gmail API**.
2. *OAuth consent screen*: user type **External**, publishing status **Testing**, add your Gmail address
   as a **test user** and the scopes `gmail.send` and `gmail.readonly` (reply detection).
3. *Credentials → Create credentials → OAuth client ID → Web application*; under **Authorized
   JavaScript origins** add `https://jatin-29.github.io` (your Pages origin, no path).
4. In ReferralPilot open **Settings**: set *Sending mode* to `gmail_web`, paste the client ID, save, then
   click **Connect Gmail**. Google warns that the app is unverified — expected for a personal test app.
   The sign-in lasts about an hour; when it expires, queued emails wait until you click **Connect Gmail**.

Build and serve the site locally: `python scripts/build_web.py --out site --jobs jobs.json` (after
`referralpilot export-jobs --out jobs.json --all-companies`), then `python -m http.server -d site 8000`.

## The modules

### A. Job harvester — `referralpilot/harvester/`
| Source | Endpoint | Notes |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | full JD HTML in one call |
| Lever | `api.lever.co/v0/postings/{company}?mode=json` | set `"region": "eu"` for `api.eu.lever.co` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}` | used by many YC startups |
| YC jobs | `ycombinator.com/jobs/role/{role}` | best-effort HTML parse; honours robots.txt |

* **Title filter** (`config/filters.json`, regexes): keeps Software Engineer, SDE/SDE-1, Backend/Frontend/
  Full Stack Developer/Engineer, Graduate Engineer, Associate/Junior… and drops Senior, Sr., Staff, Lead,
  Principal, Manager, Architect, level III+ (and level II unless `exclude_level_two=false`), internships.
* **Experience filter**: parses "3+ years", "5+ yrs", "3-5 years", "minimum of two years of experience" and
  drops postings above `MAX_REQUIRED_YEARS` (default 2). Ignores "up to 2 years" and non-experience numbers.
* **Location filter**: optional `LOCATION_KEYWORDS` (unknown locations are kept).
* **Dedupe**: unique `(company, external_id)` constraint — the ATS job id — so re-crawls only update.
* Retries with backoff on 429/5xx, polite User-Agent, one transaction per company, errors logged per board.

### B. Resume matcher & LaTeX tailor — `referralpilot/tailor/`
* **JD parser** splits the description into *required* / *responsibilities* / *preferred* sections and
  detects 90+ skills (languages, frameworks, databases, cloud, concepts) with context-aware patterns —
  "Go above and beyond" is not Go, "react to incidents" is not React, "the rest of the team" is not REST.
  Alternatives like "Java, Go, Ruby **or** Python" become one requirement satisfied by any of them.
* **ATS score (0–100)** = 60% weighted skill coverage + 15% evidence (matched skills backed by projects or
  experience, not just listed) + 15% role fit (backend/frontend/full-stack vs. your `target_roles`)
  + 10% experience fit. Matched skills and gaps are shown on the card.
* **Tailoring**: projects are re-ranked by JD relevance (top `MAX_PROJECTS_ON_RESUME`), bullets are
  reordered, matched skills move first within each skills category, and JD keywords are **bolded**.
* **Template**: `templates/base_resume.tex` is Jinja2 with LaTeX-safe delimiters (`\VAR{...}`, `%%` line
  statements). Each section is a macro; `doc.sections` controls order. All values are escaped.
* **Compile chain** (`LATEX_ENGINE=auto`): `pdflatex → xelatex → lualatex → tectonic → typst → fpdf`.
  pdflatex runs with `-no-shell-escape`; Typst uses `templates/base_resume.typ` (`pip install typst`);
  `fpdf` renders the same content with fpdf2 (pure Python) and always works. The `.tex` and an ATS-friendly `.md`
  are always written next to `exports/{company}_{role}_resume.pdf`.

### C. Target discovery — `referralpilot/prospector/`
* **Domain**: `companies.json` domain → employer domain from the posting URL (ATS hosts ignored) →
  DNS-checked guess (`.com/.io/.ai/...`). Guessed domains are flagged and their email confidence is cut.
* **Email patterns**: `{first}.{last}`, `{first}`, `{f}{last}`, … ranked by prevalence; a pattern from
  Hunter (or inferred from verified addresses) overrides the priors. Alternates are one click away.
* **Providers** (`CONTACT_PROVIDERS`, in order): `hunter` (domain search + pattern), `apollo` (people
  search), `brave` (Brave Search API), `google_cse` (Custom Search JSON API), `duckduckgo` (HTML endpoint;
  respects robots.txt, may be rate-limited). Search providers run prioritised queries:
  `site:linkedin.com/in "{company}" ("Software Engineer" OR "SDE")`, then alumni
  (`"{company}" "{your college}"`), then recruiters. People who now work elsewhere are dropped.
* **Ranking**: alumni > engineers (SDE-1 peers get a bonus) > recruiters > managers > executives, plus
  email confidence. With no API keys, the drawer shows ready-made search links and a manual-add form
  that guesses the address from the domain pattern.

### D. Composer & sender — `referralpilot/outreach/`
* **Drafts** are 3–4 sentences: who you are (alumni variant when applicable), the role and its top
  required skills from the JD, 1–2 of your most relevant projects, and a respectful ask to refer you or
  forward your resume (tailored PDF attached as `First_Last_Resume.pdf`). Phrasing varies per recipient.
* **Backends**: `dry_run` (writes `.eml` files), `smtp` (STARTTLS/SSL, refuses to send credentials in
  clear text), `gmail_api` (OAuth2 installed-app flow, token cached in `secrets/token.json`).
* **Safety rails** (enforced in code, config can only tighten them):

  | Rule | Default |
  |---|---|
  | Max emails per rolling 24h (initial + follow-ups) | **20** (hard cap) |
  | Random gap between sends | **3–7 min** (minimum 180 s is hard) |
  | Send window / weekdays only | 09:00–19:00 `APP_TIMEZONE` / off |
  | Max emails per company per day | 3 |
  | Same address cold-emailed again | not within 30 days |
  | Human approval before queueing | always (drafts are never auto-sent) |
  | Opt-out | "reply unsubscribe" P.S. + `List-Unsubscribe` header; opt-outs and bounces go to a suppression list |
  | Failures | retried with backoff; bounces suppressed; auth errors pause the queue |
* **Follow-ups**: if an initial email has no reply after `FOLLOWUP_AFTER_DAYS` (4), a one-sentence nudge
  is queued **in the same thread** (`Re:` subject, `In-Reply-To`/`References`, Gmail `threadId`) after a
  final reply check. One follow-up per contact; never to people who replied, opted out or bounced.
* **Reply detection**: Gmail API thread scan or IMAP (`IMAP_*`, defaults to the SMTP account) every 30
  minutes; detects replies, "unsubscribe"-style opt-outs and mailer-daemon bounces. In dry-run, use the
  **Replied / Referred / Opt-out** buttons.

### E. Dashboard — `referralpilot/ui/`
FastAPI + Jinja2 + htmx + Tailwind (precompiled to `static/tailwind.css`; `UI_TAILWIND_CDN=true` adds the
Play CDN while you customise templates). htmx and SortableJS are vendored, so it works offline.

* **Board**: Discovered → Matched / Tailored → Outreach Queued → Contacted → Follow-Up Sent → Replied /
  Referred. Drag cards between stages; search / company / score filters; statuses advance automatically.
* **Job drawer**: ATS breakdown, matched skills and gaps, one-click resume compile with PDF/TeX links,
  contact discovery, alternate emails, OSINT links, email history, notes and the full JD.
* **Draft editor**: edit recipient/subject/body with a sentence counter, then **Approve & queue**.
* **Outbox**: 24h usage, next-send countdown, queue with ETAs, drafts, sent log, failures, pause/resume.
* **Live activity**: scheduler, scraper, tailor, prospector and outreach events streamed over SSE.
* State-changing requests must carry htmx's `HX-Request` header from the same origin, so other websites
  you visit cannot trigger sends on `localhost`.

## Configuration

All settings live in `.env` (see `.env.example` for every option). The important ones:

| Variable | Purpose |
|---|---|
| `EMAIL_BACKEND` | `dry_run` (default), `smtp`, `gmail_api` |
| `SENDER_NAME`, `SENDER_EMAIL` | From header (defaults to the profile) |
| `SMTP_HOST/PORT/USERNAME/PASSWORD` | For Gmail use an [App Password](https://support.google.com/accounts/answer/185833) |
| `GMAIL_CREDENTIALS_FILE` | OAuth *Desktop app* client JSON; then run `referralpilot gmail-auth` once |
| `CONTACT_PROVIDERS` + API keys | `hunter,apollo,brave,google_cse,duckduckgo` |
| `APP_TIMEZONE`, `SEND_WINDOW_START/END` | When emails may go out |
| `MAX_REQUIRED_YEARS`, `LOCATION_KEYWORDS` | Harvest filters |
| `AUTO_TAILOR_NEW_JOBS` | Compile a PDF for every new job (default: score only) |

Gmail API setup: Google Cloud Console → enable the Gmail API → OAuth consent screen (add yourself as a
test user) → Credentials → *OAuth client ID* → *Desktop app* → download to `secrets/credentials.json` →
`pip install -e ".[gmail]"` → `referralpilot gmail-auth`.

## CLI

```
referralpilot init | demo | harvest [--company X] [--offline] | jobs [--status S]
referralpilot tailor JOB_ID [--engine pdflatex|typst|fpdf] | prospect JOB_ID [--offline]
referralpilot draft CONTACT_ID | approve OUTREACH_ID | queue | send-tick | followups
referralpilot gmail-auth | engines | serve [--demo] [--no-scheduler] | verify [--live] [--keep]
referralpilot export-jobs [--out jobs.json] [--all-companies]     # crawl → public postings snapshot
```

## Verification & tests

```bash
python scripts/verify_pipeline.py      # same as `referralpilot verify`; add --live to try the real APIs
pytest                                 # 122 tests: parsers, filters, scoring, LaTeX/Typst/fpdf builds,
                                       # SMTP round-trip (aiosmtpd), queue limits, follow-ups, dashboard,
                                       # the browser runtime (web.py), snapshots, Gmail REST, XHR transport
node scripts/web_smoke.cjs URL         # boots a built site in Chromium and walks the dashboard (CI runs it)
```

The verifier runs in a throwaway workspace with a simulated clock and checks: crawling and filtering,
dedupe, ATS scoring, PDF compilation, domain + pattern discovery, contact ranking, a 3–4 sentence draft
with opt-out and attachment, approval, a 3–7 minute gap, the 20-per-24h cap, a threaded follow-up after
4 days, and that an opted-out recipient is never emailed again.

## Project layout

```
referralpilot/
  config.py  db.py  models.py  activity.py  seed.py  pipeline.py  scheduler.py  cli.py  verify.py
  web.py websettings.py backup.py   browser runtime (Pyodide), browser settings, backup/restore
  harvester/   greenhouse.py lever.py ashby.py yc.py filters.py service.py snapshot.py
  tailor/      skills.py jd_parser.py matcher.py document.py render.py compiler.py service.py
  prospector/  domain.py patterns.py providers.py service.py
  outreach/    composer.py senders.py gmail.py gmail_web.py replies.py queue.py followups.py service.py
  ui/          app.py routes.py views.py templates/ static/
  demo_data/   recorded-style API payloads used by tests, `verify` and `--demo`
web/           GitHub Pages shell: index.html, bridge.js (page side), worker.js (Pyodide worker)
scripts/       build_web.py (static site), web_smoke.cjs (browser smoke test), build_css.sh
templates/     base_resume.tex (Jinja2 + LaTeX), base_resume.typ (Typst fallback)
config/        candidate_profile.json, companies.json, filters.json
exports/       tailored resumes + outbox/*.eml (git-ignored)
```

## Responsible use

Cold outreach only works when it is rare, relevant and personal — the limits above exist to keep it
that way and to protect your sender reputation. Review every draft, verify guessed addresses before
approving them (bounces hurt deliverability), honour every opt-out, and follow the terms of the job
boards and data providers you enable. ReferralPilot never scrapes LinkedIn itself; it only reads public
search-engine results or official APIs that you configure. Email rules such as CAN-SPAM, GDPR and
India's DPDP Act apply to you as the sender.
