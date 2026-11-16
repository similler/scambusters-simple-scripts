#!/usr/bin/env python3
"""Check ScamBusters reporting and wallet status for up to 200 websites."""

import argparse
from datetime import datetime, timezone
import getpass
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import warnings

import scambusters_check as client


STATUS_LABELS = {
    "unreported": "Unreported",
    "reported_no_wallets": "Reported with no wallets",
    "reported_with_wallets": "Reported with wallets",
    "failed": "Could not check",
    "not_checked": "Not checked",
}
CLASSIFIED_STATUSES = ("unreported", "reported_no_wallets", "reported_with_wallets")


def notice(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def save_key(key: str) -> None:
    state_dir = client.DEFAULT_STATE_DIR
    state_dir.mkdir(mode=0o700, exist_ok=True)
    lines = [line for line in client._read_key_lines(state_dir) if not client.KEY_LINE.match(line)]
    lines.append(f"{client.KEY_NAME}={key}")
    # The temporary file is private and replacing it keeps the update atomic.
    temporary = None
    try:
        with NamedTemporaryFile(dir=state_dir, mode="w", encoding="utf-8", delete=False) as file:
            temporary = Path(file.name)
            file.write("\n".join(lines) + "\n")
        temporary.replace(state_dir / ".env")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prompt_for_key(error: client._CredentialError, rotate: bool) -> str:
    discord_command = "/rotate-api-key" if rotate else "/get-api-key"
    instructions = f"Use {discord_command} in ScamBusters Discord to get a key."
    manual = f"Set {client.KEY_NAME} in .venv/.env beside check.py and run the command again."
    if not sys.stdin.isatty():
        raise client._BatchStop(error.code, f"{error} {instructions} {manual}")

    notice(f"{error} {instructions}")
    while True:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("Paste the API key (hidden, Enter to cancel) > ").strip()
        except (EOFError, getpass.GetPassWarning):
            raise client._BatchStop(error.code, f"Hidden input is unavailable. {manual}") from None
        if not key:
            raise client._BatchStop(error.code, f"API key update cancelled. {manual}")
        if client.KEY_PATTERN.fullmatch(key):
            save_key(key)
            notice("Saved the API key to .venv/.env.")
            return key
        notice("A ScamBusters key starts with sb_ followed by 32 lowercase hexadecimal characters.")


def format_report(sites: list[str], results: dict[str, client.SiteResult], stopped: str) -> str:
    sections: dict[str, list[str]] = {status: [] for status in STATUS_LABELS}
    for site in sites:
        result = results.get(site)
        status = result["status"] if result else "not_checked"
        reason = result["error"] if result else None
        display = site if site.isprintable() else repr(site)
        entry = f"- {display}"
        if reason:
            entry += f" ({reason})"
        sections[status].append(entry)

    classified = sum(len(sections[status]) for status in CLASSIFIED_STATUSES)
    lines = ["ScamBusters website check", f"Classified {classified} of {len(sites)} websites."]
    if stopped:
        lines.extend(["", f"Run stopped. {stopped}"])
    for status, entries in sections.items():
        if entries or status in CLASSIFIED_STATUSES:
            lines.extend(["", f"{STATUS_LABELS[status]} ({len(entries)})", *entries])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("websites", nargs="*", metavar="WEBSITE", help="Domains or URLs to check")
    parser.add_argument("-f", "--file", type=Path, help="UTF-8 text file with one website per line")
    parser.add_argument("-o", "--output", type=Path, help="Path for the new text report")
    args = parser.parse_intermixed_args()
    websites = args.websites
    if args.file is not None:
        if websites:
            parser.error("Pass websites directly or use --file.")
        try:
            lines = args.file.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as error:
            parser.error(f"Could not read the website list ({error}).")
        websites = [line for line in lines if not line.lstrip().startswith("#")]
    try:
        sites = client._normalize_websites(websites)
    except ValueError as error:
        parser.error(str(error))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    output = args.output or Path(f"check-report-{stamp}.txt")
    results: dict[str, client.SiteResult] = {}
    stopped = ""
    exit_code = 0
    try:
        with output.open("x", encoding="utf-8") as report:
            try:
                client._run_checks(
                    sites, results, state_dir=client.DEFAULT_STATE_DIR, replace_key=prompt_for_key, notify=notice
                )
            except client._CredentialError as error:
                stopped = f"{error} Update the key in .venv/.env and run the command again."
                exit_code = 2
            except client._BatchStop as error:
                stopped, exit_code = str(error), 2
            except KeyboardInterrupt:
                stopped, exit_code = "Interrupted by the user.", 130
            report.write(format_report(sites, results, stopped))
    except OSError as error:
        notice(f"Could not save the report to {output} ({error}).")
        return 2

    if stopped:
        notice(stopped)
    print(f"Report saved to {output.absolute()}")
    return exit_code or int(any(result["status"] == "failed" for result in results.values()))


if __name__ == "__main__":
    sys.exit(main())
