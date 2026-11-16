#!/usr/bin/env python3
"""Export your ScamBusters submission history to a text report."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from check import notice, prompt_for_key
import scambusters_check as client


def report_text(history: client.HistoryResult, stopped: str) -> str:
    retrieved_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    records = history["submissions"]
    lines = [
        "ScamBusters submission history",
        f"Retrieved {retrieved_at}",
        f"Submissions retrieved  {len(records)} of {history['total_submissions']}",
        f"Pages retrieved  {len(history['pages'])}",
    ]
    if stopped:
        lines.append(f"Partial report. {stopped}")
    if history["next_page"] is not None:
        lines.append(f"Next page  {history['next_page']}")
    if not records:
        lines.extend(["", "No submissions returned."])

    for number, record in enumerate(records, start=1):
        lines.extend(["", f"Submission {number}", "-" * 18])
        for name, value in record.items():
            label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").capitalize()
            if isinstance(value, (dict, list)):
                value = json.dumps(value, indent=2, ensure_ascii=False)
            elif value is None:
                value = "—"
            elif isinstance(value, bool):
                value = "Yes" if value else "No"
            display = str(value).replace("\n", "\n  ")
            lines.append(f"  {label}  {display}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=int, default=1, help="Starting page (default 1)")
    parser.add_argument("--all", dest="all_pages", action="store_true", help="Fetch every page from the starting page")
    parser.add_argument("-o", "--output", type=Path, help="Path for the new text report")
    args = parser.parse_args()
    try:
        history = client._new_history(args.page)
    except ValueError as error:
        parser.error(str(error))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    output = args.output or Path(f"submission-history-{stamp}.txt")
    if not output.name or not output.parent.is_dir():
        parser.error("Choose an output filename in an existing directory.")
    partial = output.with_name(f"{output.stem}.partial{output.suffix}")
    for path in (output, partial):
        if path.exists() or path.is_symlink():
            parser.error(f"Report path already exists ({path}). Choose a new output path.")
        if path.resolve().is_relative_to(client.DEFAULT_STATE_DIR.resolve()):
            parser.error("Choose an output path outside .venv.")

    stopped = ""
    exit_code = 0
    try:
        client._run_history(
            history, all_pages=args.all_pages, state_dir=client.DEFAULT_STATE_DIR,
            replace_key=prompt_for_key, notify=notice,
        )
    except client._CredentialError as error:
        stopped = f"{error} Update the key in .venv/.env and run the command again."
        exit_code = 1
    except client._BatchStop as error:
        stopped, exit_code = str(error), 1
    except KeyboardInterrupt:
        stopped, exit_code = "Interrupted by the user.", 130

    if stopped:
        notice(stopped)
        if not history["pages"]:
            return exit_code
        output = partial
    try:
        with output.open("x", encoding="utf-8", errors="backslashreplace") as file:
            file.write(report_text(history, stopped))
    except OSError as error:
        notice(f"Could not save the report to {output} ({error}).")
        return 1
    print(f"History report saved to {output.absolute()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
