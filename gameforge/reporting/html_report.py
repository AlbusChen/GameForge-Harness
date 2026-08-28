from __future__ import annotations

import html
from pathlib import Path


def write_html_report(
    destination: Path,
    summary: dict[str, object],
    acceptance_results: list[dict[str, object]],
) -> None:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['id']))}</td>"
        f"<td>{html.escape(str(item['status']))}</td>"
        f"<td>{html.escape(str(item['reason']))}</td>"
        "</tr>"
        for item in acceptance_results
    )
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    if summary.get("mode") == "mock":
        notice = (
            "Mock validation only. Unity compile, Play Mode, acceptance, build, "
            "and smoke gates were not run."
        )
    else:
        notice = "This report contains results from real Unity batch and player processes."
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GameForge Harness run report</title>
  <style>
    body {{
      font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto;
      padding: 0 1rem; color: #1d2433;
    }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
    th, td {{ border: 1px solid #ccd2dc; padding: .6rem; text-align: left; }}
    th {{ background: #f2f5f9; }}
    .notice {{ border-left: 4px solid #c47a00; background: #fff5df; padding: 1rem; }}
  </style>
</head>
<body>
  <h1>Run report</h1>
  <p class="notice">
    {html.escape(notice)}
  </p>
  <h2>Summary</h2>
  <table>{summary_rows}</table>
  <h2>Acceptance results</h2>
  <table><thead><tr><th>ID</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table>
</body>
</html>
"""
    destination.write_text(document, encoding="utf-8")
