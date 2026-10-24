"""The self-contained HTML report: offline, inert data, and the CLI."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from agent_energy_profiler import html_report, view_model
from agent_energy_profiler.measurement import CallableReader
from agent_energy_profiler.session import Session

from test_report import Counter_, V2Case

EVIL = "</script><img src=x onerror=\"document.title='PWNED'\"><!--"


def data_block(page):
	m = re.search(r'<script type="application/json" id="aep-data">(.*?)</script>', page, re.S)
	return json.loads(m.group(1))


class HtmlCase(V2Case):

	def record(self, **kw):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read), **kw) as s:
			with s.span(EVIL, attributes={"prompt": "<svg onload=alert(1)> "}):
				with s.span("tool:x"):
					pass
		self.attribute(power=True)


class TestPage(HtmlCase):

	def test_untrusted_text_cannot_end_the_data_block_or_add_markup(self):
		self.record(attributes={"note": "</script><script>alert(1)</script>"})
		view = view_model.build(self.d, all_attributes=True)
		page = html_report.render(view)
		# the page's own two scripts and nothing else
		self.assertEqual(page.count("</script>"), 2)
		self.assertEqual(page.count("<script"), 2)
		self.assertNotIn("<img", page)
		self.assertNotIn("<svg", page)
		self.assertNotIn("<!--", page)
		self.assertNotIn(" ", page)
		self.assertEqual(data_block(page), json.loads(json.dumps(view)))

	def test_csp_allows_only_this_files_script_and_style(self):
		self.record()
		page = html_report.render(view_model.build(self.d))
		csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
		self.assertIn("default-src 'none'", csp)
		self.assertIn("connect-src 'none'", csp)
		self.assertNotIn("unsafe-inline", csp)
		self.assertNotIn("unsafe-eval", csp)
		script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
		style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
		for text in (script, style):
			digest = base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
			self.assertIn(f"'sha256-{digest}'", csp)

	def test_nothing_is_loaded_from_anywhere(self):
		self.record()
		page = html_report.render(view_model.build(self.d))
		self.assertIsNone(re.search(r"https?://", page))
		self.assertIsNone(re.search(r"\b(src|href)\s*=", page.split('id="aep-data"')[0]))
		script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
		for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
		            "new Function", "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon",
		            "import("):
			self.assertNotIn(api, script)

	def test_the_script_does_no_energy_arithmetic(self):
		# it may compare (sort, scale bars) but never add energy values up
		script = re.search(r"<script>(.*?)</script>",
		                   html_report.render(self._small_view()), re.S).group(1)
		self.assertIsNone(re.search(r"energy_j\s*\+|\+\s*\w+\.energy_j|energy_j\s*\+=", script))
		self.assertIsNone(re.search(r"value_j\s*\+|\+=\s*\w*\.value_j", script))

	def _small_view(self):
		self.record()
		return view_model.build(self.d)

	@unittest.skipUnless(shutil.which("node"), "node is not installed")
	def test_script_parses(self):
		script = re.search(r"<script>(.*?)</script>",
		                   html_report.render(self._small_view()), re.S).group(1)
		path = os.path.join(self.d, "report.js")
		with open(path, "w", encoding="utf-8") as f:
			f.write(script)
		res = subprocess.run(["node", "--check", path], capture_output=True, text=True)
		self.assertEqual(res.returncode, 0, res.stderr)


class TestCli(HtmlCase):

	def cli(self, *args):
		return subprocess.run([sys.executable, "-m", "agent_energy_profiler", *args],
		                      capture_output=True, text=True, timeout=120)

	def test_report_html(self):
		self.record(attributes={"customer": "acme-secret"})
		out = os.path.join(self.d, "report.html")
		res = self.cli("report", self.d, "--html", out)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("attributes omitted", res.stdout)
		with open(out, encoding="utf-8") as f:
			page = f.read()
		view = data_block(page)
		self.assertEqual(view["view_version"], view_model.VIEW_VERSION)
		self.assertNotIn("acme-secret", page)
		self.assertNotIn("<svg", page)
		res = self.cli("report", self.d, "--html", out, "--attributes", "prompt")
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(out, encoding="utf-8") as f:
			self.assertEqual(data_block(f.read())["privacy"]["allowlist"], ["prompt"])

	def test_free_text_labels_warn_and_can_be_redacted(self):
		prompt = "llm:summarise the contract for jane doe"
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			with s.span(prompt):
				pass
		self.attribute(power=False)
		out = os.path.join(self.d, "report.html")
		res = self.cli("report", self.d, "--html", out)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("1 of 1 operation labels look like free text", res.stderr)
		self.assertIn("--redact-labels", res.stderr)
		res = self.cli("report", self.d, "--html", out, "--redact-labels")
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertNotIn("warning", res.stderr)
		self.assertIn("replaced by 'operation N'", res.stdout)
		with open(out, encoding="utf-8") as f:
			page = f.read()
		self.assertNotIn("jane doe", page)
		self.assertEqual([r["name"] for r in data_block(page)["operations"]], ["operation 1"])

	def test_html_options_that_conflict(self):
		self.record()
		out = os.path.join(self.d, "r.html")
		for extra in (["--brief"], ["--format", "json"],
		              ["--attributes", "a", "--all-attributes"]):
			with self.subTest(extra=extra):
				res = self.cli("report", self.d, "--html", out, *extra)
				self.assertEqual(res.returncode, 2)
				self.assertFalse(os.path.exists(out))

	def test_full_reports_are_unchanged_by_the_new_options(self):
		self.record()
		res = self.cli("report", self.d)
		self.assertIn("ENERGY REPORT", res.stdout)
		self.assertNotIn("<html", res.stdout)


if __name__ == "__main__":
	unittest.main()
