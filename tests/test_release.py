"""0.3.0: release metadata stays consistent (source tree only).

Skipped when testing an installed wheel from a copy of tests/, where
pyproject.toml and the documentation are not present.
"""

import os
import re
import unittest

import agent_energy_profiler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: a file of this repository on its main branch, as the README links to it
REPO_FILE = (r"^(?:https://github\.com/VeiledTee/agent-energy-profiler/(?:blob|tree)/main/"
             r"|https://raw\.githubusercontent\.com/VeiledTee/agent-energy-profiler/main/)"
             r"([^#]+(?:#.*)?)$")


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
		self.assertRegex(read("CITATION.cff"), rf"(?m)^version: {re.escape(version)}$")

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
		readme = read("README.md")
		# the introduction points to the limits before the quick start
		intro = readme.split("## Quick start")[0]
		self.assertIn("(#measurement-limitations)", intro)
		self.assertIn("not per-process", intro)
		limits = readme.split("## Measurement limitations")[1].split("\n## ")[0]
		for phrase in ("measured at device scope", "may be estimated",
		               "has not been established against an independent meter",
		               "has not been validated on real hardware",
		               "demonstrated on one configuration"):
			self.assertIn(phrase, " ".join(limits.split()), phrase)

	# documentation milestone after 0.4.0: the README is the first-run path

	def test_readme_quick_start_is_the_example_file(self):
		block = read("README.md").split("**2. Save this as `quickstart.py`.**")[1]
		code = block.split("```python\n")[1].split("```")[0]
		self.assertEqual(code, read(os.path.join("examples", "quickstart.py")).expandtabs(4))

	def test_relative_links_in_the_readme_and_docs_resolve(self):
		def anchors(path):
			slugs = set()
			for line in read(path).splitlines():
				if line.startswith("#"):
					title = line.lstrip("#").strip().lower()
					slugs.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title)))
			return slugs
		pages = ["README.md", "CONTRIBUTING.md", os.path.join("examples", "README.md")]
		pages += [os.path.join("docs", f) for f in os.listdir(os.path.join(ROOT, "docs"))
		          if f.endswith(".md")]
		for page in pages:
			text = re.sub(r"(?s)```.*?```", "", read(page))
			for target in re.findall(r"\]\(([^)\s]+)\)", text):
				base = page
				own = re.match(REPO_FILE, target)
				if own:
					# the README's links to this repository: absolute, for PyPI
					target, base = own.group(1), "README.md"
				elif re.match(r"[a-z]+:", target):
					continue
				path, _, anchor = target.partition("#")
				resolved = (os.path.normpath(os.path.join(os.path.dirname(base), path))
				            if path else page)
				self.assertTrue(os.path.exists(os.path.join(ROOT, resolved)),
				                f"{page}: {target}")
				if anchor and resolved.endswith(".md"):
					self.assertIn(anchor, anchors(resolved), f"{page}: {target}")

	# 0.5.0: the README is also the PyPI project page

	def test_readme_links_work_on_pypi(self):
		# PyPI resolves no relative link or image: a link to this repository's
		# files must be absolute (and is checked above); anchors are fine
		text = re.sub(r"(?s)```.*?```", "", read("README.md"))
		targets = re.findall(r"\]\(([^)\s]+)\)", text)
		self.assertTrue(any(re.match(REPO_FILE, t) for t in targets))
		for target in targets:
			self.assertRegex(target, r"^(https://|#)", target)
		self.assertIn("pip install agent-energy-profiler\n", read("README.md"))

	def test_citation_names_the_licence_and_repository_of_pyproject(self):
		citation = read("CITATION.cff")
		self.assertRegex(citation, r"(?m)^cff-version: 1\.2\.0$")
		self.assertRegex(citation, r'(?m)^license: MIT$')
		self.assertIn('license = "MIT"', self.pyproject)
		self.assertIn('repository-code: "https://github.com/VeiledTee/agent-energy-profiler"',
		              citation)
		# no DOI is claimed until one has been assigned (docs/RELEASING.md)
		self.assertNotIn("doi", citation.lower())


if __name__ == "__main__":
	unittest.main()
