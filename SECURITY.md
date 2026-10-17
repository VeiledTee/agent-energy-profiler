# Security

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue, pull
request or discussion. Use GitHub's private vulnerability reporting:

1. Open <https://github.com/VeiledTee/agent-energy-profiler/security/advisories/new>
   (or the repository's **Security** tab, then **Report a vulnerability**).
2. Include the profiler version (`agent-energy-profiler --version`), the
   operating system, how to reproduce the problem, and its impact.

Only the maintainer and you can see the report. Expect an acknowledgement
within two weeks. Fixes are coordinated in the private advisory and
disclosed with the release that contains them.

Measurement-accuracy questions (for example, a value that disagrees with a
power meter) are not security issues. Please open a normal issue for those.

## What the package does on a host

Useful for judging the impact of a report:

- **Reads hardware counters, read-only.** NVML (energy, power, device names,
  driver version) and Linux powercap/RAPL files under `/sys`. It never sets
  power limits, clocks or any other device state. On many Linux systems the
  RAPL files are readable only by root; the profiler does not ask for or
  need elevated privileges and reports such domains as unavailable.
- **Writes files only where it is told to:** the spans/events file, the
  sampler's `power.csv` and sidecar, and the output directory of
  `agent-energy-profiler run`, `attribute`, `export` and `otel-export`
  (`run` defaults to `agent-energy-profiler-runs/<timestamp>` in the current
  directory). An existing spans file is not overwritten unless
  `overwrite=True`.
- **No network access, no telemetry,** except
  `agent-energy-profiler otel-export --endpoint` (and `otel.export_spans`
  with an exporter you configure), which sends span data to the endpoint you
  name.
- **`agent-energy-profiler run` executes the command you give it**, as you,
  with a few `AEP_*` environment variables added.
- **Recorded data can be sensitive.** Span attributes are whatever your code
  passes (prompts or user ids, if you choose to record them). Schema-v2
  session headers record the process id, platform, Python version, GPU
  names and driver and, since 0.4.0, the hostname from a hardware report
  passed for `auto` mode. v1 events also record the hostname and the git
  commit of the working directory, and `agent-energy-profiler validate`
  reports the hostname. Review artifacts before sharing them or exporting
  them to a tracing backend.

## Dependencies

The core package has no runtime dependencies. Optional extras pull in
`nvidia-ml-py`, `matplotlib`, and the OpenTelemetry API/SDK/OTLP exporter;
their lower bounds are in `pyproject.toml`. Keep them updated in deployed
environments.
