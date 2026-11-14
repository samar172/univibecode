# UnvibeCode PR Brief — prototype

A working prototype for the UnvibeCode Engineering Challenge 2026 (Cohort 3), Track 2.

It turns an existing `unvibecode review` result into a short, branded pull-request
comment that answers the reviewer's first question:

> Which business workflows does this change touch, what connected code did the author
> **not** change, and which tests should run?

It needs no new AI calls. It joins a git diff with data UnvibeCode already produces:

| UnvibeCode output | Used for |
|---|---|
| Business Workflow Map (`business-workflows.html`) — workflow titles + implementation references with exact line ranges | Which workflows the diff touches |
| Complete Repository Context (`connections.jsonl`, `chunks.jsonl`) — verified cross-file links | Blast radius (connected code not in the diff) and linked tests |

## Key design choice: match by symbol, not by line

Changed lines are mapped to the enclosing function or class (`Config.from_object`,
`AppContext.push`) with Python's `ast`, then matched to the workflow references by name.
Names survive the edits that shift line numbers, so **one cached review stays usable for
many later PRs**: the PR comment costs a fraction of a second instead of a full re-review.

## Run it

```bash
python -m unvibecode review --repository ./flask --output flask_results
python unvibecode_brief.py --repo ./flask --results flask_results \
    --base a411a243^ --head a411a243
# --json for machine-readable output, --pr-url / --report-url to add links
```

Standard library only (Python 3.10+).

## Results on real Flask history

Review: `pallets/flask` @ `49943b5a` (30 Oct 2026), one `unvibecode review` run (86 s).
Briefs: real upstream commits, each produced from that same cached review in ~0.2 s.

| Commit | Upstream change | Brief says | Distance from reviewed snapshot |
|---|---|---|---|
| `a411a243` | add back opening session on context push | 🔴 **Session** workflow — `AppContext._get_session`, `AppContext.push` | 53 commits |
| `fb541598` | secret key rotation: fix key list ordering | 🔴 **Session** workflow — `SecureCookieSessionInterface.get_signing_serializer`; run `tests/test_reqctx.py` | 160 commits (~17 months) |
| `fbb6f0bc` | all teardown callbacks are called despite errors | 🟡 **WSGI request** + **Session** workflows via `Flask.do_teardown_*`, `AppContext.pop` | 51 commits |
| `eca5fd1d` | redirect defaults to 303 | No mapped workflow; lists `examples/tutorial/flaskr/auth.py` as a caller of `redirect` | — |
| `de8429ff` | Fix IPv6 session transactions | No mapped workflow (test-client code); lists 5 linked test modules | — |

Full outputs are in [`examples/`](examples/).

## Known limits (prototype)

- Symbol-level matching is Python-only; other languages fall back to file-level matching.
- Quality is bounded by the review's graph. For example, TypeScript files named
  `user.service.ts` currently get no import edges (UnvibeCode issues #158 / #282), so the
  blast radius on NestJS/Angular code is empty until that is fixed.
- Workflow references that point at a whole class (e.g. `Flask`, lines 108–1686) give only
  a weaker "inside a referenced class" match, shown as 🟡.

## Next steps

1. Package as a GitHub Action that upserts one PR comment (`uses: FinanceFlash/unvibecode-brief@v1`).
2. Cache the review per default-branch commit; re-review incrementally only when the
   symbols a PR touches are missing from the cache.
3. Measure: weekly active repos, % of PRs where a reviewer clicks through to the full map,
   👍/👎 on the comment, and 4-week retention of the installed Action.
