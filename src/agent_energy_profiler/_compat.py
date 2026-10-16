"""Python-version compatibility.

`sum`: Python 3.12 made the built-in sum() of floats compensated (Neumaier
summation), which changes the last bits of some totals. The reference
artifacts (tests/golden, tests/golden_v2 and the frozen research runs) were
produced on 3.12. On 3.12 and 3.13 `sum` here IS the built-in; on other
versions it is a pure-Python replica of 3.12's algorithm (3.14 changed sum()
again: it also compensates ints mixed with floats), so every supported
Python writes byte-identical results. Modules that sum floats import it as

    from ._compat import sum  # 3.12 float summation on every version
"""

import builtins
import math
import sys

__all__ = ["sum"]


_LONG_MIN, _LONG_MAX = -2**63, 2**63 - 1


def neumaier_sum(iterable, start=0):
	"""sum() as CPython 3.12 computes it (Python/bltinmodule.c, builtin_sum_impl).

	While the running total is an int that fits a C long, ints and bools
	accumulate exactly. From a float on, floats are added with Neumaier
	compensation and ints that fit a C long without it. Anything else (an
	int outside a C long included) is added with `+`, after folding in the
	compensation, and so is everything after it. Checked against the
	built-in sum of 3.12 and 3.13 on random inputs (tests/test_compat.py).
	"""
	it = iter(iterable)
	result = start
	if type(result) is int and _LONG_MIN <= result <= _LONG_MAX:
		for item in it:
			if ((type(item) is int or type(item) is bool) and _LONG_MIN <= item <= _LONG_MAX
			        and _LONG_MIN <= result + item <= _LONG_MAX):
				result += item
				continue
			result = result + item
			break
		else:
			return result
	if type(result) is float:
		f_result, c = result, 0.0
		for item in it:
			if type(item) is float:
				t = f_result + item
				if abs(f_result) >= abs(item):
					c += (f_result - t) + item
				else:
					c += (item - t) + f_result
				f_result = t
				continue
			if isinstance(item, int) and _LONG_MIN <= item <= _LONG_MAX:
				f_result += float(item)
				continue
			if c and math.isfinite(c):
				f_result += c
			result = f_result + item
			break
		else:
			if c and math.isfinite(c):
				f_result += c
			return f_result
	for item in it:
		result = result + item
	return result


sum = builtins.sum if (3, 12) <= sys.version_info < (3, 14) else neumaier_sum
