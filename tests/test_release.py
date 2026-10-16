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


if __name__ == "__main__":
	unittest.main()
