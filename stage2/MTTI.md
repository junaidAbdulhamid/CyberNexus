# Mean Time To Identify: method, results, and what they are worth

The brief asks for a ~40% improvement in the time it takes a SOC analyst to
identify a compromised host, measured by a reproducible harness.

This document reports **50.7%** (three runs of the same harness gave 46.7%,
50.7% and 53.6%; §5.1 explains the spread), and then spends most of its length
explaining
precisely what that number is, because the honest answer to "did you measure a
40% improvement in analyst performance?" is **no** — nobody was timed. What was
measured is how the two interfaces behave; what was modelled is how long a
person would take to use them.

---

## 1. The problem with automated MTTI

MTTI is a property of a human doing a task. An automated agent driving a browser
reads the DOM in microseconds and has no visual system, so any "search time" it
exhibits is a property of the test script, not of the interface. A harness that
reports an MTTI improvement without saying this is reporting how its own code
was written.

So the harness is split, and only the halves are honest:

| | what it is | trustworthiness |
|---|---|---|
| **Measured** | System latency, how many items each interface presents, where the target sits among them, how many rows belong to the target host, whether the identity was shown without interaction | Direct observation of the running system. Reproducible. |
| **Modelled** | Converting those observations into seconds of human time | A published model with stated parameters. Defensible, not measured. |
| **Not done** | Timing actual people | — |

Everything below is labelled with which of the three it is.

---

## 2. What the harness does

`ui-tests/mtti/run-mtti.js` runs N randomised trials. Each trial:

1. clears prior state and injects a fixed volume of background noise (60
   low-severity alerts by default) so both interfaces have a realistic haystack;
2. picks a random end host and a random attack scenario from a seeded PRNG;
3. injects the incident through the **real** alert path — the same
   correlate-and-store code a Stage 1 alert travels through;
4. observes each interface until the information is on screen, and records what
   it sees;
5. alternates which condition goes first, so drift in the running system cannot
   systematically favour one.

Both conditions are shown the same incident on the same host with the same noise.

### The two conditions

**3D map** — the live map, with the incident banner, ranked alert list and
severity-coded scene.

**Alert log** (`frontend/src/baseline/LogView.jsx`) — a competent SOC log
console: reverse-chronological, auto-refreshing every second, sortable columns,
severity and score as text, a working filter box, and the host one click away.

The baseline is deliberately *not* a strawman. It has every piece of information
the map has. Making it bad would make the comparison worthless. The only thing
it cannot do is show the shape of the network, and that is the variable under
test.

---

## 3. Measured results

30 trials, 60 noise alerts per trial, seed 20260918, on the machine in §7.
**These are direct observations, not model output.**

| observation | 3D map | alert log |
|---|---:|---:|
| alert injection → on screen, median | **101 ms** | 94 ms |
| same, p95 | **186 ms** | 1,100 ms |
| items presented to the operator, median | 1 competing marked node | 500 rows |
| rows/nodes belonging to the target host, median | 1 node | 10 rows |
| position of the target among presented items, median | rank 1 (banner) | mid-list |
| identity shown with no interaction | 87% of trials | 0% |
| frame rate during the incident, median | 60 fps | n/a |

Both interfaces put the data on screen in well under a second, so **raw system
latency is not where the difference lives**. The difference is in what the
operator then has to do with what is on screen:

- the log presents **500 rows**, of which the target's **10** are scattered
  among noise;
- the map presents **one** marked node and names it in a banner.

That is the entire mechanism, and it is measured, not assumed.

---

## 4. The model

Two standard HCI techniques, implemented in `ui-tests/mtti/model.js`:

**KLM-GOMS** (Card, Moran & Newell, 1980) decomposes a task into primitive
operators with published average durations — mental preparation M = 1.35 s,
pointing P = 1.10 s, button press B = 0.20 s, keystroke K = 0.28 s. It models an
expert performing an error-free routine task, so it under-predicts absolute
times. It does so *in both conditions*, which is why the ratio is more
trustworthy than either absolute figure.

**Visual search** (Treisman & Gelade, 1980; Wolfe, 1994 onward). A target
differing from its surroundings in one basic feature — colour, size, motion — is
found in roughly constant time however many distractors there are ("pop-out").
A target that must be identified by reading requires serial inspection, and time
grows with the number of items. This is the whole reason a map can beat a list,
and it is why an alerting node is drawn with a unique colour *and* size *and*
motion: three redundant pop-out channels.

### Worked example (trial 1 of the run)

Trial 1: the target produced 23 of the 412 rows on screen, first appearing at
row 76.

| operator | 3D map | alert log |
|---|---:|---:|
| system latency (measured) | 0.18 s | 0.45 s |
| detect / scan | 0.45 s (pop-out) | 3.25 s (serial over 76 rows) |
| decide | 2.70 s | 1.35 s |
| aggregate 23 rows into one host | — | 6.00 s (capped) |
| scroll to bring the row into view | — | 1.30 s |
| point and click to open the host | 0 s (banner already named it) | 1.30 s |
| read the identity | 1.90 s | 1.90 s |
| **total** | **5.23 s** | **15.55 s** |

The map's own cost is dominated by the two mental-preparation operators, which
is a floor no interface can go below in this model.

---

## 5. Modelled results

| | median | mean | sd |
|---|---:|---:|---:|
| alert log MTTI | 10.45 s | 11.06 s | 3.24 s |
| 3D map MTTI | 5.16 s | 5.25 s | 0.30 s |
| **improvement (median)** | **50.7%** | | |
| 95% CI (percentile bootstrap, 2000 resamples) | 41.9% – 57.3% | | |

**Target of 40%: met** at the median, under this model. The confidence
interval's lower bound sits only a couple of points above the target, and in
earlier runs an individual trial has favoured the log view outright. Both facts
belong in the headline, not a footnote.

Note the variances: the map's spread is tiny (sd 0.30 s) because its cost barely
depends on how much else is happening; the log's is ten times larger (sd 3.24 s)
because it depends entirely on where the target row landed and how many rows the
host produced. The map's advantage is not only that it is faster
on average — it is that it is *predictable*, which is what matters at 3am.

### 5.1 The result varies between runs, and that is a finding

Three runs of this harness, same seed, gave **46.7%**, **50.7%** and **53.6%**.
The difference is not noise in the model; it is the state of the alert console.
The log view shows the most recent 500 alerts, so how quickly a target is found
in it depends on how much history is already there — how many rows the target
contributed, and where among the backlog they landed. The median rows-per-target
was 4.5 in one run and 6.5 in the other, and the improvement moved with it.

The 3D map's figures barely moved across all three runs (5.16–5.18 s median),
because a map shows a host once however many times it has alerted.

So the honest summary is: **the map is roughly 45–55% faster under this model,
and the log view's performance degrades as its backlog grows while the map's
does not.** Anyone re-running this should expect a number in that band rather
than a single figure, and should report the backlog depth alongside it.

### Sensitivity

A single point estimate from a model with assumed parameters is false precision,
so the whole comparison is re-run across a grid of 81 plausible parameter
settings (serial search slope 0.020–0.060 s/item, pop-out base 0.30–0.70 s,
aggregation penalty 0.0–0.50 s/row, M 1.20–1.50 s):

| | |
|---|---|
| improvement range across the grid | 19.5% – 60.8% |
| median across the grid | 49.4% |
| settings meeting the 40% target | **67% of 81** |

The conclusion holds across most of the grid but not all of it: at the
pessimistic corner — a fast reader scanning the log, no penalty for mentally
aggregating repeated rows, a slow pop-out — the improvement falls to 20%, half
the target. A third of the parameter grid misses 40%. That is stated rather than
hidden, and it is the single strongest reason to run the human study in §8
before treating 40%+ as established.

---

## 6. The first run failed, and what fixed it

The first execution of this harness reported **32.6%** — below target. The trial
data showed why, and all three causes were real defects rather than modelling
artefacts:

| finding in the data | defect | fix |
|---|---|---|
| up to **245** nodes marked simultaneously | `acknowledge` flagged a node but never cleared its state, so alerts accumulated forever | acknowledging now removes the node from the active set; history is untouched (`cnmap/store.py`) |
| map latency p95 **925 ms** under a burst | every alert in a 20-alert burst triggered a full rebuild of all instance buffers | rebuilds coalesce to at most one per animation frame (`NetworkScene.requestRebuild`) |
| pop-out destroyed by its own success | with dozens of nodes pulsing, none stood out | halo-and-pulse is reserved for the most severe tier present, capped at 6 nodes; lower severities stay coloured but static (`MAX_ANIMATED_NODES`) |
| most of the map's modelled cost was pointing and clicking merely to learn the host's *name* | the map solved the hard part and made the operator work for the easy part | the incident banner names the worst host, its address and its behaviour with no interaction (`IncidentBanner.jsx`) |

A fifth issue surfaced in the unit tests during the same pass: a node scoring
0.96 crossed the "critical" boundary within 200 ms of decay, so the map
downgraded an incident while the operator was still turning to look at it.
Severity now holds its peak for a dwell period (default 30 s) before decaying.

Re-run after the fixes: **53.6%**, and 46.7% / 50.7% on later runs (§5.1). The
improvement is attributable to specific,
reviewable changes, and the harness that found the problems is the same one that
verified the fixes.

---

## 7. Reproducing this

```bash
cd stage2
make setup
make backend          # or: docker compose up --build
make mtti             # 30 trials, writes ui-tests/results/mtti.json
```

Options: `node mtti/run-mtti.js --trials 50 --noise 120 --seed 7 --headed`.

Because the result depends on console backlog depth (§5.1), a comparable re-run
means a comparable starting state: restart the backend before the run, or accept
a figure in the 45–55% band rather than a point estimate.

Environment for the reported run: Apple M5, 10 cores, 16 GiB, macOS 26.5.1,
Chrome (Playwright `channel: "chrome"`), Python 3.12.14, Node 26.7.0,
topology of 428 nodes / 436 links.

The full per-trial record — every measurement, every model breakdown — is in
`ui-tests/results/mtti.json`.

---

## 8. What would actually establish a 40% improvement

A human study. The protocol, ready to run:

**Design.** Within-subjects, counterbalanced. Each participant performs
identification tasks in both interfaces; half start with the map, half with the
log, so learning effects cancel.

**Participants.** 12–16 SOC analysts with at least six months of console
experience. Below ~12 the confidence interval on a 40% effect is too wide to
support the claim.

**Task.** "An alert has fired. Name the affected host and its IP address."
Timed from alert injection to a spoken or typed answer. The harness already
injects deterministic incidents with known targets (`POST /api/simulate/incident`
with `node_id` and `seed`), so the same trial sequence can be replayed for
every participant.

**Trials.** 20 per participant per condition, randomised order, with the same
background noise rate used here. Discard the first 3 per condition as practice.

**Measures.** Time to correct identification; error rate (naming the wrong
host — the log's aggregation problem predicts more of these); NASA-TLX for
subjective workload; think-aloud on a subset.

**Analysis.** Paired comparison of per-participant medians, bootstrap CI on the
improvement ratio, and report the error rate separately: an interface that is
faster but wrong more often is not better.

**Threats to validity to control for.** Novelty effect (a 3D map is more
interesting than a table, and interest is not skill); learning the synthetic
topology across trials; the operator knowing an alert is coming, which no real
shift involves.

Until that study is run, the number in this document is what it says on the
label: a model output, built on measurements that are real, with its parameters
and its failure corner both written down.

---

## 9. References

- Card, S. K., Moran, T. P., & Newell, A. (1980). *The keystroke-level model for
  user performance time with interactive systems.* CACM 23(7).
- Treisman, A., & Gelade, G. (1980). *A feature-integration theory of attention.*
  Cognitive Psychology 12(1).
- Wolfe, J. M. (1994). *Guided Search 2.0: A revised model of visual search.*
  Psychonomic Bulletin & Review 1(2).
- Healey, C. G., & Enns, J. T. (2012). *Attention and visual memory in
  visualization and computer graphics.* IEEE TVCG 18(7).
