"""A run's diff applied to the file on the default branch: exactly, or not at all.

Pure, so tested on its own like the failure classes: a file and a diff in, the
patched file or the reason out. The diffs are the shapes a model writes —
headers or none, line numbers off, a blank context line that lost its space.
"""

from __future__ import annotations

import pytest

from lares_agent_trigger.patches import PatchError, apply_diff

PATH = "faults.yaml"

FILE = """\
faults:
  - name: appliance_runtime
    devices:
      Trockner:
        max_run_hours: 4
      Waschmaschine:
        max_run_hours: 16

  - name: appliance_standby
    parameters:
      rise_ma: 40
"""

DRYER = """\
@@ -4,3 +4,3 @@
       Trockner:
-        max_run_hours: 4
+        max_run_hours: 5
       Waschmaschine:
"""


def test_a_hunk_applies_at_the_line_its_header_names() -> None:
    assert apply_diff(FILE, DRYER, PATH) == FILE.replace("hours: 4\n", "hours: 5\n")


def test_headers_naming_the_path_are_taken() -> None:
    diff = f"diff --git a/{PATH} b/{PATH}\nindex 1a2b..3c4d 100644\n--- a/{PATH}\n+++ b/{PATH}\n"
    assert apply_diff(FILE, diff + DRYER, PATH) == FILE.replace("hours: 4\n", "hours: 5\n")


def test_a_hunk_off_by_some_lines_applies_where_its_lines_stand_once() -> None:
    assert apply_diff(FILE, DRYER.replace("@@ -4,3 +4,3 @@", "@@ -9,3 +9,3 @@"), PATH) == (
        FILE.replace("hours: 4\n", "hours: 5\n")
    )


def test_two_hunks_apply_in_order() -> None:
    diff = (
        DRYER
        + """\
@@ -11,1 +11,1 @@
-      rise_ma: 40
+      rise_ma: 50
"""
    )
    assert apply_diff(FILE, diff, PATH) == FILE.replace("hours: 4\n", "hours: 5\n").replace(
        "rise_ma: 40", "rise_ma: 50"
    )


def test_a_blank_context_line_that_lost_its_space_is_still_context() -> None:
    diff = """\
@@ -7,3 +7,3 @@
         max_run_hours: 16

-  - name: appliance_standby
+  - name: appliance_idle
"""
    assert apply_diff(FILE, diff, PATH) == FILE.replace("appliance_standby", "appliance_idle")


def test_an_addition_stands_where_its_context_does() -> None:
    diff = """\
@@ -6,2 +6,4 @@
       Waschmaschine:
         max_run_hours: 16
+      Trockner-Neu:
+        max_run_hours: 3
"""
    assert apply_diff(FILE, diff, PATH) == FILE.replace(
        "max_run_hours: 16\n", "max_run_hours: 16\n      Trockner-Neu:\n        max_run_hours: 3\n"
    )


def test_a_new_file_comes_from_dev_null() -> None:
    diff = f"--- /dev/null\n+++ b/{PATH}\n@@ -0,0 +1,2 @@\n+faults:\n+  - name: new\n"
    assert apply_diff(None, diff, PATH) == "faults:\n  - name: new\n"


def test_an_untouched_end_without_newline_stays_so() -> None:
    bare = FILE.rstrip("\n")
    assert apply_diff(bare, DRYER, PATH) == bare.replace("hours: 4", "hours: 5")


def test_the_no_newline_marker_after_an_addition_ends_the_file_bare() -> None:
    diff = """\
@@ -11,1 +11,1 @@
-      rise_ma: 40
+      rise_ma: 50
\\ No newline at end of file
"""
    assert apply_diff(FILE, diff, PATH) == FILE.replace("rise_ma: 40\n", "rise_ma: 50")


@pytest.mark.parametrize(
    ("original", "diff", "reason"),
    [
        (
            FILE,
            DRYER.replace("-        max_run_hours: 4", "-        max_run_hours: 3"),
            "stand nowhere",
        ),
        (
            FILE.replace(
                "Waschmaschine:\n        max_run_hours: 16", "Trockner:\n        max_run_hours: 4"
            ),
            DRYER.replace("@@ -4,3", "@@ -1,3").replace("       Waschmaschine:\n", ""),
            "fit 2 other places (lines 4, 6)",
        ),
        (FILE, f"--- a/other.yaml\n+++ b/other.yaml\n{DRYER}", "names other.yaml"),
        (
            FILE,
            f"--- a/{PATH}\n+++ b/{PATH}\n{DRYER}--- a/b.yaml\n+++ b/b.yaml\n",
            "touches a second file",
        ),
        (FILE, f"--- a/{PATH}\n+++ b/{PATH}\n", "holds no @@ hunk"),
        (FILE, "Raise the dryer to five hours.\n" + DRYER, "comes before the first @@"),
        (FILE, DRYER.replace("+        max_run_hours: 5", "*        max_run_hours: 5"), "neither"),
        (
            FILE,
            "@@ -5,1 +5,1 @@\n-        max_run_hours: 4\n+        max_run_hours: 4\n",
            "changes nothing",
        ),
        (FILE, f"--- /dev/null\n+++ b/{PATH}\n@@ -0,0 +1 @@\n+x\n", "may not delete or create"),
        (None, DRYER.replace("@@", f"--- a/{PATH}\n+++ b/{PATH}\n@@", 1), "starts from /dev/null"),
        (FILE, "@@ -7,0 +8,2 @@\n+      Trockner-Neu:\n+        max_run_hours: 3\n", "no context"),
    ],
    ids=[
        "context-differs",
        "ambiguous",
        "other-path",
        "two-files",
        "no-hunk",
        "prose-first",
        "bad-marker",
        "no-change",
        "create-existing",
        "edit-missing",
        "addition-without-context",
    ],
)
def test_a_diff_that_does_not_fit_is_refused_with_where(
    original: str | None, diff: str, reason: str
) -> None:
    with pytest.raises(PatchError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        apply_diff(original, diff, PATH)
