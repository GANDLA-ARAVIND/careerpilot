# Finding a company's ATS and board token

For each company you add to `companies.yaml`, you need two things: which of the
three ATSs it uses, and that ATS's token/slug for it. This is the manual step in
an otherwise automated pipeline (Scout will eventually do this — see CLAUDE.md —
but for now it's you, forty times).

## Step 1: figure out which ATS a company uses

Go to the company's careers page and click through to an actual job listing. Look
at the URL you land on, or check where "Apply" sends you — nearly every fresher
job posting URL redirects to one of:

| If the URL contains... | The ATS is |
|---|---|
| `boards.greenhouse.io/...` or `job-boards.greenhouse.io/...` | Greenhouse |
| `jobs.lever.co/...` | Lever |
| `jobs.ashbyhq.com/...` | Ashby |

If none of those show up — the careers page is custom-built, or on Workday,
iCIMS, SAP SuccessFactors, or something else — it's not one of the three ATSs
this project supports yet. Leave it out of `companies.yaml` for now (that's the
scraping long tail, out of scope until we build it).

**If clicking through doesn't make it obvious**, open DevTools → Network tab,
reload the careers page, and filter for `fetch/XHR`. Look for a request to one of:

- `boards-api.greenhouse.io/v1/boards/...`
- `api.lever.co/v0/postings/...`
- `api.ashbyhq.com/posting-api/job-board/...`

Whichever one shows up tells you the ATS **and** gives you the token in the URL
itself, so this trick often does both steps at once.

## Step 2: get the token, per ATS

### Greenhouse

The token is the path segment right after the domain:

```
https://boards.greenhouse.io/stripe          -> token: stripe
https://job-boards.greenhouse.io/gitlab      -> token: gitlab
```

Verify it before adding it to `companies.yaml` by hitting the API directly in a
browser or with curl:

```
https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true
```

A real board returns JSON with a `jobs` array. A bad token returns HTTP 404 with
`{"status":404,"error":"Job not found"}`.

### Lever

Same idea — the token is the path segment after `jobs.lever.co`:

```
https://jobs.lever.co/palantir    -> token: palantir
```

Verify with:

```
https://api.lever.co/v0/postings/palantir?mode=json
```

A real board returns a JSON array of postings directly (not wrapped in an
object). A bad token returns HTTP 404 with `{"ok":false,"error":"Document not
found"}`. Note a *valid* token with zero current openings also returns 200 with
an empty array `[]` — that's a different, harmless case, not a config error.

### Ashby

Same pattern again — the token is the path segment after `jobs.ashbyhq.com`:

```
https://jobs.ashbyhq.com/notion    -> token: notion
```

Verify with:

```
https://api.ashbyhq.com/posting-api/job-board/notion
```

A real board returns a JSON object with a `jobs` array and an `apiVersion`
field. (`adapters/ashby.py` doesn't exist yet — this is just for confirming the
token now so it's ready when that adapter is built.)

## Step 3: add it to companies.yaml

```yaml
- name: Notion
  ats: ashby
  token: notion
  notes: optional - whatever you want to remember about why this one's on the list
```

The loader (`config.py`) will crash immediately and loudly if `ats` isn't one of
`greenhouse`/`lever`/`ashby`, if `token` is missing, or if a field name is
misspelled — so if you get the token wrong in a way that produces a *different*
valid-looking token, you won't find out from the loader. You'll only find out
because that company contributes zero jobs. Always do the curl/browser check in
Step 2 before trusting an entry.
