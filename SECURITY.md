# Security

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue: use the
hosting platform's private vulnerability reporting for this repository
(e.g. GitHub "Report a vulnerability" under the Security tab) once it is
published. Include the version, how to reproduce it, and its impact. You
should get a reply within two weeks.

## What the package does on a host

Useful for judging the impact of a report:

- **Reads hardware counters, read-only.** NVML (energy, power, device names,
  driver version) and Linux powercap/RAPL files under `/sys`. It never sets
  power limits, clocks or any other device state. On many Linux systems the
  RAPL files are readable only by root; the profiler does not ask for or
  need elevated privileges and reports such domains as unavailable.
- **Writes files only where it is told to:** the spans/events file, the
  sampler's `power.csv` and sidecar, and the output directory of `aep run`,
  `aep attribute`, `aep export` and `aep otel-export`. An existing spans file
  is not overwritten unless `overwrite=True`.
- **No network access, no telemetry,** except `aep otel-export --endpoint`
  (and `otel.export_spans` with an exporter you configure), which sends span
  data to the endpoint you name.
- **`aep run` executes the command you give it**, as you, with a few `AEP_*`
  environment variables added.
- **Recorded data can be sensitive.** Span attributes are whatever your code
  passes (prompts or user ids, if you choose to record them). Schema-v2
  session headers record the process id, platform, Python version and GPU
  names and driver, and, since 0.4.0, the hostname from a hardware report
  passed for `auto` mode; v1 events also record the hostname and the git commit
  of the working directory; `aep validate` reports the hostname. Review
  artifacts before sharing them or exporting them to a tracing backend.

## Dependencies

The core package has no runtime dependencies. Optional extras pull in
`nvidia-ml-py`, `matplotlib`, and the OpenTelemetry API/SDK/OTLP exporter;
their lower bounds are in `pyproject.toml`. Keep them updated in deployed
environments.
