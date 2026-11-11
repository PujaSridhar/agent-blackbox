# Agent Black Box

**Drop in one agent trace file. Get structured analytics out. No SDK, no vendor, no model calls.**

Everyone is shipping agents. Almost nobody can say what their agent actually
did, what it cost, or where it went wrong — not after the fact, not cheaply,
and not in a form anyone else can check.

The usual answer is an observability vendor: a new SDK in your hot path, a
contract, and a dashboard you have to trust. But your agent already writes a
trace file. That file is enough.

```bash
blackbox                 # the latest run in this directory
```

```
AGENT BLACK BOX   1919 events from session.jsonl
  adapter claude_code · 384 tool calls · 14 failed · 6.7h active over 31.2h
  46min waiting on you · 2 calls you declined

COST  $319.28      rates as of 2026-11-10
  model                    msgs     output   cache read   1h writes        usd
  claude-fable-5            203    651,144   27,159,636   1,428,422       88.96
  claude-opus-4-8           568    748,144  196,595,856  11,385,828      230.33
  44.4% of spend was 1-hour cache writes  <- rebuilding a long cached prefix
                                             is where this run's money went
  at least $24.88 (7.8%) went to work that did not advance the task
    thrash $23.75, retry loop $1.13, error cluster $0.35

FINDINGS  50 total (7 high, 18 medium, 25 low)
  [HIGH] thrash  backend/physics.py written 9x in 70 tool calls   $5.06
         backend/physics.py was written 9 times inside a span of 70 tool
         calls. Edits that tightly packed usually mean each change was
         fixing the last one.
         · step 1204 line 1533  Edit: /repo/backend/physics.py
         · step 1211 line 1540  Edit: /repo/backend/physics.py
  ...

  payload digest 4b2f61f38c729c  ·  detectors ran with no model calls
```

Then prove it:

```bash
blackbox --verify
```

```
verify OK   50 findings replayed byte-identical from the event store,
            with no model calls
```

---

## The part that makes the numbers mean something

A trace file is a flat list of rows. Counting tool calls in file order is easy
and mostly useless, because a flat list cannot answer two questions:

**Which result belongs to which call?** When an assistant message issues
several tool calls at once, file order cannot say which result answers which
call. Every per-call duration computed from it is wrong, and "the agent
stalled here" becomes a coin flip. This tool re-parents each result onto its
own `tool_use` block, so the edge is causal rather than chronological.

**Whose cost is this?** A subagent's tokens are billed to the run but are not
the main thread's work. Smear them together and the expensive step looks cheap
while the main thread looks bloated. Sidechain events are resolved into their
own branches and costed separately.

Everything downstream — cost, detectors, reports — reads that graph, never the
raw file. Build the graph wrong and every number is confidently wrong.

### The cost detail nobody surfaces

Token totals hide where money goes. This splits the cache tiers, because they
are billed at different multiples of the input rate:

| tier | rate | why it matters |
|---|---|---|
| cache read | 0.1× input (lower where published) | nearly free; a high read share is healthy |
| cache write, 5 min | 1.25× input | costs *more* than fresh input |
| cache write, 1 hour | 2.0× input | the most expensive tokens in the run |

A run can look cheap per token and still be expensive. In the session above,
**44.4% of $319.28 went to 1-hour cache writes** — a long cached prefix being
rebuilt over and over. That is invisible in any flat token count.

### Findings cost money, and the report says how much

Every finding carries the cost of the model turns behind it, and the run
carries a total. On that session, **$24.88 — 7.8% of the bill — went to work
that did not advance the task**, nearly all of it rewriting the same files.

That figure is a floor, deliberately. It counts the turns that issued the
implicated calls; it does not try to price the downstream cost of carrying
their results in context, which cannot be attributed exactly. A number you can
defend beats a larger one you cannot.

---

## Findings

Six detectors, all pure functions of the graph. No model decides anything.

| detector | fires on | deliberately does not fire on |
|---|---|---|
| `retry_loop` | the same acting call repeated with identical input inside a sliding window | observation and progress tools — eight identical screenshots is polling, and advancing a task list ten times is bookkeeping |
| `stall` | wall-clock gap between a call and its result | interrupted calls; tools that exist to wait for a human; pauses long enough to be a person away, reported separately as idle gaps |
| `error_cluster` | failed calls grouped by normalised error shape, keyed by the program that ran | bare `Exit code 1` headers; calls a human declined |
| `contradiction` | the same *region* of a file re-read with different bytes and no write between; a file edited in place the agent has never seen | paging through a file with `offset`/`limit`; editing a file the agent created itself; editing a file it read through the shell |
| `thrash` | several writes to one file inside a sliding window | the same number of writes spread across a long session |
| `context_bloat` | a large tool result, priced by the turns that carried it afterwards | runs with no model price available — no rate, no claim |

**Every exclusion in the right-hand column is a false positive found by running
against real traces, not a hypothetical.** On the first real corpus the naive
versions produced 249 findings; the current ones produce 198, and the 51 that
disappeared were all wrong:

* 17 of 17 "blind edits" were files the agent had created itself with `Write`.
  Having authored a file is knowing its contents.
* The one "stale read" — the finding this README used to lead with — was an
  agent paging through `game.py` at `offset=95` then `offset=164`. Two regions
  of an unchanged file, reported as tampering.
* Seven unrelated shell failures collapsed into one `Exit code <n>` cluster,
  because the first line of a failed `Bash` result is a header, not an error.
* Four "stalls" were `AskUserQuestion` waiting for a human to type.
* Two "tool failures" were a human declining a call.
* Files were reported as thrashed for three edits across six hours.

Adding the second adapter found one more of exactly the same kind:

* 84 of 84 "blind edits" on the Codex corpus had a `sed -n '1,200p' <file>`
  before them. That agent inspects files through the shell rather than a file
  tool, and a shell read is a read. Counting it dropped contradictions across
  both corpora from 92 to 14.

Each of those has a regression test with the real-world case written into it.

---

## Why replay is the point

An analysis of an agent run that cannot itself be re-verified is just another
opinion. This one reproduces exactly:

* the parsed run is written to a **DuckDB** event store;
* the detectors re-derive every finding from that store — no trace file, no
  network, no model;
* thresholds are read back out of the store, so a moved default cannot let a
  replay quietly disagree with the original;
* the payload contains no wall-clock reads, so two runs serialise to identical
  bytes.

`tests/test_determinism.py` enforces all of it, including a test that
monkeypatches `socket` to prove the analysis path cannot open a connection.
Across 37 real transcripts from two different agents, all 37 replay
byte-identical.

Because the store is just DuckDB, it is also the query interface:

```sql
-- where did the time go?
select tool_name, count(*) calls, sum(length(tool_output)) output_bytes
from events where kind = 'tool_call' group by 1 order by 2 desc;

-- what did each branch cost?
select branch, sum(output_tokens) out, sum(cache_write_1h_tokens) writes_1h
from events group by 1;
```

---

## Install and use

```bash
pip install -e ".[dev]"
```

Nobody remembers a session UUID, so you never have to type one:

```bash
blackbox                         # latest run for the current directory
blackbox ls                      # recent sessions, newest first
blackbox scan                    # every local run, aggregated
blackbox ~/.claude/projects/app/ # every run for one project
blackbox run.jsonl               # one specific trace
```

Discovery covers both agents: Claude Code encodes the project into the
directory name, Codex records it inside the file, and `ls` and `scan` read
whichever applies. Symlinked paths are resolved both ways, because on macOS
`/tmp` and `/private/tmp` encode differently.

`scan` is the one to try first — it reads every transcript on the machine,
offline, and tells you what your agent gets wrong *habitually*:

```
AGENT BLACK BOX   every run on this machine
  37 run(s) · 3,811 tool calls · 300 findings · 34.7h active

SPEND  $1,029.85 across 37 run(s)
  at least $88.64 (8.6%) on work that did not advance the task
  cache hit rate 95.2% of billed input · 37,212,551 tokens of 1-hour cache writes
  tool failure rate 5.0% (190 failed)

MOST REWRITTEN FILES
   39x  …/scratchpad/Puja Sridhar SDE/main.tex
   16x  /Users/me/marimo/marimo/_data/get_datasets.py
```

Outputs:

```bash
blackbox run.jsonl --html out.html    # self-contained page, no JS, no network
blackbox run.jsonl --json out.json    # canonical payload
blackbox run.jsonl --db run.duckdb    # queryable event store
blackbox run.jsonl --verify           # store, replay, compare digests
blackbox --replay run.duckdb          # re-derive from a stored run
```

Thresholds are flags, not magic numbers: `--stall-seconds`, `--idle-seconds`,
`--retry-min-repeats`, `--retry-window`, `--thrash-min-edits`,
`--thrash-window`, `--error-cluster-min`, `--bloat-min-tokens`,
`--bloat-min-usd`. Whatever you pass is recorded in the store and reused on
replay.

---

## Two agents, one model

`blackbox/adapters/` is the only place that knows a vendor's log format. An
adapter exposes `NAME`, `sniff(path)` and `parse(path) -> Trace`, and emits
canonical `Event` objects.

That boundary was an assertion until a second format tested it. **Adding
OpenAI Codex CLI support took one adapter file and no change to the graph, the
cost engine, or any detector** — but it did force two genuine generalisations,
which is the point of doing it:

| Codex does this | so the canonical model gained |
|---|---|
| no parent pointers; a flat sequence | the spine is rebuilt from `call_id` and turn order, shared by both adapters |
| cumulative token counts (summing per-request usage over-counts by ~5%) | per-turn usage is the delta between readings |
| `input_tokens` already includes cached ones | fresh input is the difference, and no cache-write tier is invented |
| one `apply_patch` edits several files | file operations are a list of `(path, kind)`, so `thrash` and `contradiction` work on patch tools unchanged |
| records a compaction boundary | `context_bloat` stops counting there instead of over-estimating |

Both formats are undocumented and will drift. That is not a reason to avoid
reading them; it is a reason to put the churn behind one boundary.
Unrecognised row types degrade to `meta` events rather than crashing, so a new
row type upstream costs fidelity, never a run.

### Prices for models this build does not ship

Rates are only included where they could be verified. Rather than guess at
another vendor's pricing, supply it:

```bash
blackbox run.jsonl --prices rates.json
```

```json
{ "gpt-5.4": { "input": 1.25, "output": 10.0, "cache_read": 0.125 } }
```

*(Illustrative values — put your own in.)* Supplied rates are stored with the
run and reapplied on replay, so a stored analysis prices identically forever.
Without them the tokens are still counted and the total is marked a floor.

---

## Limitations

Stated plainly, because a tool that overclaims about trustworthiness has
defeated itself:

* **Two adapters.** Claude Code and OpenAI Codex CLI. A third would test the
  model further; two is enough to show it is not one vendor's shape.
* **Only Anthropic rates ship.** Other vendors' models are reported as
  unpriced unless you supply rates with `--prices`.
* **Shell reads are matched loosely.** A file read with `cat` or `sed` is
  matched to an edit by trailing path components, because one is usually
  relative and the other absolute. The bias is deliberate: over-matching
  suppresses a blind-edit report, under-matching invents one.
* **`context_bloat` is an estimate.** Token counts are derived from character
  length, and context editing or compaction would evict a result early. It is
  an upper bound on a context that is never cleared, and it is labelled as one
  everywhere it appears.
* **Stall timings are upper bounds.** The gap between a call and its result
  includes any wait for approval. The report says so where it reports one.
* **Prices are a table, not an API.** Rates in `blackbox/pricing.py` were
  verified on 2026-11-10. Tokens on a model missing from the table are counted
  and reported as unpriced, and the total is marked a floor.
* **Waste attribution is a floor.** It counts the turns behind a finding, not
  the downstream context those turns created.
* **Blind-edit detection is per-trace.** A file read in an earlier session
  looks unread here.
* **A resumed session may reference rows in another file.** Those parents are
  reported as orphan events rather than silently dropped.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
python tests/fixtures/make_fixture.py   # regenerate the synthetic trace
```

```bash
python tests/fixtures/make_codex_fixture.py   # and the Codex one
```

The fixtures are hand-written rather than copied from real sessions: every
detector has a case planted for it, *and every false positive has a negative
case planted next to it* — a file written then edited, a file read in two
regions, a file inspected with `sed`, a question a human took four minutes to
answer, a token count reported twice. A fixture nobody can read is a fixture
nobody trusts.

## License

MIT
