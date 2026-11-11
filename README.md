# Agent Black Box

**Drop in one agent trace file. Get structured analytics out. No SDK, no vendor, no model calls.**

Everyone is shipping agents. Almost nobody can say what their agent actually
did, what it cost, or where it went wrong — not after the fact, not cheaply,
and not in a form anyone else can check.

The usual answer is an observability vendor: a new SDK in your hot path, a
contract, and a dashboard you have to trust. But your agent already writes a
trace file. That file is enough.

```bash
blackbox ~/.claude/projects/my-project/session.jsonl
```

```
AGENT BLACK BOX   1919 events from session.jsonl
  adapter claude_code · 384 tool calls · 16 failed · 6.7h active over 31.2h

COST  $319.28      rates as of 2026-11-10
  model                    msgs     output   cache read   1h writes        usd
  claude-fable-5            203    651,144   27,159,636   1,428,422       88.96
  claude-opus-4-8           568    748,144  196,595,856  11,385,828      230.33
  44.4% of spend was 1-hour cache writes  <- rebuilding a long cached prefix
                                             is where this run's money went

FINDINGS  75 total (12 high, 41 medium, 22 low)
  [HIGH] contradiction  backend/game.py changed between two reads with no write
  [HIGH] retry_loop     Edit called 6x with identical input
  [MED ] stall          Bash took 11.7min to return
  ...

  payload digest 78d9ea65649652  ·  detectors ran with no model calls
```

Then prove it:

```bash
blackbox session.jsonl --verify
```

```
verify OK   75 findings replayed byte-identical from the event store,
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

---

## Findings

Five detectors, all pure functions of the graph. No model decides anything.

| detector | fires on | deliberately does not fire on |
|---|---|---|
| `retry_loop` | the same acting call repeated with identical input inside a sliding window | repeats of *observing* tools — eight identical screenshots is polling, reported at low severity |
| `stall` | wall-clock gap between a call and its result | interrupted calls; pauses long enough to be a human away, reported separately as idle gaps |
| `error_cluster` | failed calls grouped by normalised error shape | one-off failures below the cluster threshold |
| `contradiction` | a file read twice with different contents and no write between; a file edited in place that was never read | `Write` creating a new file |
| `thrash` | a file written three or more times | — |

Each exclusion above is a false positive found by running against real traces,
not a hypothetical. The polling rule alone reclassified 15 of 22 apparent
"retry loops" across the development corpus.

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

## Install

```bash
pip install -e ".[dev]"
```

```bash
blackbox TRACE.jsonl                    # report to stdout
blackbox TRACE.jsonl --html out.html    # self-contained page, no JS, no network
blackbox TRACE.jsonl --json out.json    # canonical payload
blackbox TRACE.jsonl --db run.duckdb    # queryable event store
blackbox TRACE.jsonl --verify           # store, replay, compare digests
blackbox --replay run.duckdb            # re-derive from a stored run
```

Thresholds are flags, not magic numbers: `--stall-seconds`, `--idle-seconds`,
`--retry-min-repeats`, `--retry-window`, `--thrash-min-edits`,
`--error-cluster-min`. Whatever you pass is recorded in the store and reused
on replay.

---

## Adding a format

`blackbox/adapters/` is the only place that knows a vendor's log format.
An adapter exposes `NAME`, `sniff(path)` and `parse(path) -> Trace`, and emits
canonical `Event` objects. Nothing downstream changes.

This matters because **the Claude Code transcript format is undocumented and
will drift.** That is not a reason to avoid reading it; it is a reason to put
the churn behind one boundary. Unrecognised row types degrade to `meta` events
rather than crashing, so a new row type upstream costs fidelity, never a run.

---

## Limitations

Stated plainly, because a tool that overclaims about trustworthiness has
defeated itself:

* **One adapter so far.** Claude Code only. The canonical model was designed
  against one format, and a second adapter is what will prove it general.
* **Stall timings are upper bounds.** The gap between a call and its result
  includes any wait for human approval. The report says so where it reports it.
* **Prices are a table, not an API.** Rates in `blackbox/pricing.py` were
  verified on 2026-11-10. Tokens on a model missing from the table are counted
  and reported as unpriced, and the total is marked a floor — a wrong number
  would be worse than a missing one.
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

The fixture is hand-written rather than copied from a real session: every
detector has a case planted for it, plus the false positives that matter.
A fixture nobody can read is a fixture nobody trusts.

## License

MIT
