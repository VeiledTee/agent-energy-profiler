"""0.3.0: release metadata stays consistent (source tree only).

Skipped when testing an installed wheel from a copy of tests/, where
pyproject.toml and the documentation are not present.
"""

import os
import re
import unittest

import agent_energy_profiler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
	with open(os.path.join(ROOT, name), encoding="utf-8") as f:
		return f.read()


@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "pyproject.toml"))
                     and os.path.exists(os.path.join(ROOT, "CHANGELOG.md")),
                     "needs the source tree")
class TestReleaseMetadata(unittest.TestCase):

	def setUp(self):
		self.pyproject = read("pyproject.toml")

	def test_one_version_everywhere(self):
		version = re.search(r'^version = "([^"]+)"', self.pyproject, re.M).group(1)
		self.assertEqual(version, agent_energy_profiler.__version__)
		self.assertRegex(read("CHANGELOG.md"), rf"(?m)^## {re.escape(version)}\b")
		self.assertIn(f"# Public API ({version})", read("docs/API.md"))

	def test_every_extra_is_documented_in_the_readme(self):
		block = self.pyproject.split("[project.optional-dependencies]")[1].split("\n[")[0]
		extras = re.findall(r"(?m)^([a-z]+) = \[", block)
		self.assertIn("otel", extras)
		readme = read("README.md")
		for extra in extras:
			if extra in ("dev", "test"):
				continue
			self.assertIn(f"[{extra}]", readme, extra)

	def test_python_classifiers_match_ci(self):
		classified = set(re.findall(r"Python :: (3\.\d+)\"", self.pyproject))
		ci = read(".github/workflows/ci.yml")
		matrix = re.search(r"python: \[([^\]]+)\]", ci).group(1)
		tested = set(re.findall(r"3\.\d+", matrix))
		self.assertEqual(classified, tested)
		minimum = re.search(r'requires-python = ">=(3\.\d+)"', self.pyproject).group(1)
		self.assertEqual(minimum, min(tested, key=lambda v: int(v.split(".")[1])))

	def test_core_declares_no_dependencies(self):
		self.assertRegex(self.pyproject, r"(?m)^dependencies = \[\]$")

	# 0.4.0 (M9): public name and publication metadata

	def test_command_is_named_after_the_distribution(self):
		block = self.pyproject.split("[project.scripts]")[1].split("\n[")[0]
		scripts = dict(re.findall(r'(?m)^([\w-]+) = "([^"]+)"', block))
		self.assertEqual(scripts.get("agent-energy-profiler"), "agent_energy_profiler.cli:main")
		for name in scripts:
			self.assertTrue(name.startswith("agent-energy-profiler"), name)

	def test_project_urls_point_to_the_repository(self):
		block = self.pyproject.split("[project.urls]")[1].split("\n[")[0]
		urls = dict(re.findall(r'(?m)^(\w+) = "([^"]+)"', block))
		for key in ("Homepage", "Source", "Issues", "Changelog"):
			self.assertTrue(urls[key].startswith(
				"https://github.com/VeiledTee/agent-energy-profiler"), key)

	def test_current_documentation_does_not_use_the_old_command(self):
		old = re.compile(r"(?<![\w.-])aep(?:-(?:sample|attribute|validate|aggregate|visualize)\b"
		                 r"|\s+(?:validate|run|attribute|report|export|otel-export|sample)\b"
		                 r"|\s+--)")
		paths = ["README.md", "SECURITY.md", "CONTRIBUTING.md"]
		for folder in ("docs", "examples", "src"):
			for root, dirs, files in os.walk(os.path.join(ROOT, folder)):
				dirs[:] = [d for d in dirs if d != "__pycache__"]
				paths += [os.path.relpath(os.path.join(root, f), ROOT) for f in files
				          if f.endswith((".md", ".py"))]
		# the provenance record describes milestones under the name they had
		paths = [p for p in paths if p != os.path.join("docs", "PROVENANCE.md")]
		for path in paths:
			for n, line in enumerate(read(path).splitlines(), 1):
				self.assertIsNone(old.search(line), f"{path}:{n}: {line.strip()}")

	def test_security_policy_uses_private_vulnerability_reporting(self):
		self.assertIn("https://github.com/VeiledTee/agent-energy-profiler/security/advisories/new",
		              read("SECURITY.md"))

	def test_readme_states_the_limits_of_the_measurement(self):
		status = read("README.md").split("## What it measures")[0]
		for phrase in ("measured at device scope", "may be estimated",
		               "has not been established against an independent meter",
		               "has not been validated on real hardware",
		               "demonstrated on one configuration"):
			self.assertIn(phrase, " ".join(status.replace(">", " ").split()), phrase)


if __name__ == "__main__":
	unittest.main()
