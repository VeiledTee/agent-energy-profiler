# HTML report

```bash
agent-energy-profiler report RUN --html report.html                # attributes left out
agent-energy-profiler report RUN --html report.html --attributes question,model
agent-energy-profiler report RUN --html report.html --all-attributes   # everything: check before sharing
agent-energy-profiler report RUN --html report.html --redact-labels    # labels become "operation N"
```

One self-contained file. It opens offline in any current browser, needs no
server, and loads nothing from the network. It has three views:

- **Workflow explorer** (default). Your spans as a tree of hierarchy
  paths (`agent:answer › llm:generate`). Repeated operations are grouped
  (`×3`) and expand to their individual spans. Each row shows the count,
  total time, p50 and p95, and energy on the run's one ranking basis, with
  its share of the ranked rows. Notes such as `parent`, `shared`, `partial`
  and `quantised` say why a row is not ranked. Click a row for its details;
  click a span for its timing, status, tokens, accounting and every energy
  value: inclusive, exclusive and the window estimate, each with its kind
  and reason.

  ![Span details: llm:generate span #1 with its timing, tokens and energy
  values; inclusive and exclusive n/a with their reasons, window estimate
  ~11.325 J](images/span-details.png)

  *The quick start's span #1 (RTX 4090, WSL2, default mode; a CPU-only
  workload, so the GPU estimate mainly reflects idle draw). In this mode no
  per-span counter is read: the inclusive and exclusive values are `n/a`,
  each with its reason, and the window estimate is marked `~`.*
- **Operation breakdown**: the same rows grouped by hierarchy path or by
  label, with a filter.
- **Provenance**:
  - what each kind of number is;
  - the measurement mode and why it was chosen, the sampler, counter
    disturbance, resolution and GPU;
  - the whole-run measurements;
  - per-span coverage, every warning, the limitations;
  - what this file leaves out.

`#tab=operations`, `#tab=provenance`, `#path=N` and `#span=N` at the end
of the file's URL open that view.

## Where the numbers come from

Every number is computed in Python by `view_model.build()`. It uses the same
code as the text reports:

- label rows are the brief report's rows;
- path rows are the same rows grouped by hierarchy path;
- per-span values are copied from the attributed spans.

The page only displays, sorts and filters. It never adds energy, never
combines measured and estimated values, and never decides what is ranked.

| Shown as | Meaning |
|---|---|
| `● measured` | a hardware counter difference |
| `~ estimated` | sampled power integrated over a window; never added to measured values |
| `— n/a (reason)` | no defensible value; never shown as 0 |
| `✕ invalid` | readings that could not be used |
| `⧉ not separable` | the span overlapped other recorded work, so device energy cannot be split |
| `≈ below resolution` | a measured value from a span shorter than the counter's update interval |

Ranked rows use one basis per run, as in the brief report: measured
exclusive GPU, else estimated GPU (leaf spans that nothing overlapped),
else measured exclusive CPU package. Each span belongs to exactly one
hierarchy path, so ranked path values never count a span twice.

## Privacy

The file is meant to be shareable, so by default it leaves out:

- every span and session attribute (`--attributes NAME,...` keeps the
  named ones, `--all-attributes` keeps all);
- host names, and file paths (a hardware report's path is reduced to its
  file name);
- span, trace and session ids (spans are numbered in file order).

The run's directory path, your home directory and host names are removed
from text copied from the recording. The report is refused if any of them
would remain.

**Operation labels are included as you wrote them**, because they are what
makes the report useful. If a label contains user data
(`f"query:{text}"`), so does the file. Two safeguards:

- `report --html` warns (on stderr) when labels look like free text: over
  64 characters, a line break or tab, four or more words, or more than 50
  distinct labels that cover fewer than two spans each on average. It
  counts them; it does not print them or change them.
- `--redact-labels` replaces every label with `operation N`, numbered in
  the order the labels first appear in the recording. The hierarchy, counts
  and energy are unchanged; the file then says that labels were replaced.

The better fix is in the code: keep labels to operation names
(`"retrieval:search"`) and put the input, if you need it, in an attribute,
which is left out by default.

Text from the recording is shown as text, never interpreted as markup. The
page's Content-Security-Policy allows only its own script and stylesheet
(by SHA-256 hash), and no network access.

## Large runs

Every aggregate, count, coverage figure and warning covers the whole run.
Only the list of individual spans is limited, by `--max-instances`
(default 10,000). Beyond it, each path keeps an equal share: spans not ok
first, then shared ones, then the slowest. The page says how many are
listed.

On the development machine, a synthetic run of 150,000 spans took 7.7 s to
build (two accounting passes over the spans). The file was 10.2 MB, and
headless Chrome opened and rendered it in about 1.3 s.

## View model (`view_version` 1)

`view_model.build(path, attributes=(), all_attributes=False,
max_instances=10000, redact_labels=False)` returns the dict that the page
embeds. Its contract:

- **Energy only appears inside a cell:** `{domain, scope, kind, value_j,
  reason, qualifiers, method}`.
  - An absent key means null.
  - `value_j` is present only when `kind` is `measured` or `estimated`.
  - `scope` is one of `inclusive`, `exclusive`, `window` or `whole_run`.
- **`operations` and `paths`** are rows with `energy_j`, `ranked`, `share`
  and `notes` on `basis`. A ranked row is never `shared`, `parent`,
  `partial` or `none`, and the shares add up to 1.
- **`instances`** are individual spans with their cells; `instances_info`
  says how many are listed.
- **`provenance`, `warnings`, `whole_run` and `coverage`** describe the
  whole run.

`view_model.validate(view)` checks this; `build()` and the HTML renderer
call it.
