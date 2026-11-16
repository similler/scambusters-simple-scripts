# ScamBusters tools

Extract the ZIP and open a terminal in the extracted folder. The tools run on Python 3.10 or later using the standard library. On Windows, use `py` if that is your Python command.

## Check websites

```sh
python check.py example.com
python check.py example.com "https://example.org/path?x=1&y=2"
python check.py --file sites.txt
python check.py --file sites.txt --output results.txt
```

Pass 1 to 200 domains or URLs. For a batch file, put one website on each line and save it as UTF-8. Blank lines and lines beginning with `#` are skipped. Exact repeats are checked once.

## Use from another Python program

Put `scambusters_check.py` beside your program or add the extracted folder to your Python import path.

```python
from scambusters_check import check_websites

batch = check_websites(["example.com", "example.org"])
for result in batch["results"]:
    print(result["website"], result["status"])

if batch["error"] is not None:
    print(batch["error"]["code"], batch["error"]["message"])
```

`check_websites` accepts one domain or URL as a string, or an iterable of up to 200 entries. It skips blank entries and returns each distinct website once, in input order. It raises `ValueError` for an empty or oversized batch and `TypeError` for non-string entries.

The return value is a JSON-serializable dictionary. Each item in `batch["results"]` contains `website`, `status`, and `error`. A failed lookup includes a message in its `error` field. The field is `None` for the other statuses.

| Status | Meaning |
| --- | --- |
| `unreported` | The site has never been reported to ScamBusters. |
| `reported_no_wallets` | The site has been reported and has no recorded wallets. |
| `reported_with_wallets` | The site has been reported and has recorded wallets. |
| `failed` | The lookup failed for this site. |
| `not_checked` | The batch stopped before obtaining an outcome for this site. |

If the batch stops early, `batch["error"]` contains `code`, `message`, and `retry_after_seconds`. Use the error code to handle a missing, invalid, or expired key (`api_key_missing`, `api_key_invalid`, `api_key_expired`), a rate limit (`rate_limited`), a connection failure (`connection_failed`), or a local setup error (`local_error`). `retry_after_seconds` is an integer for a rate limit and `None` otherwise. Completed results remain in the list.

Calls are synchronous and return results silently. Pass `api_key=` to supply a key explicitly. Pass `state_dir=` with a directory path to use its `.env` and request history database. To share the CLI's rate-limit history from another project, point `state_dir` to the CLI's `.venv` directory. Use the CLI for interactive key updates and text reports.

## Submission history

`submission_history.py` reads your scored submissions from ScamBusters using the same key as the website checker. Each page has up to five submissions. The report includes the submitted site, points, and the valid and invalid wallet lists.

```sh
python submission_history.py
python submission_history.py --page 3
python submission_history.py --all
python submission_history.py --page 3 --all --output history-from-page-3.txt
```

The default report is a new `submission-history-*.txt` in the current directory. If an export stops after fetching a page, including when you press Ctrl+C, it writes a `.partial.txt` report with the next page to try. The history endpoint allows one request per minute. The tool spaces attempts at least 61 seconds apart across runs in the same state directory and applies the history cooldown separately from website checks.

Python programs can fetch the same data directly.

```python
from scambusters_check import get_submission_history

history = get_submission_history()
for submission in history["submissions"]:
    print(submission["site_key"], submission["total_points"])
```

Pass `page=3` to start at page three and `all_pages=True` to continue through the last page. The result includes `submissions`, the page numbers in `pages`, `total_pages`, `total_submissions`, `next_page`, and `error`. `next_page` is `None` after the last page. On an early stop, the result retains completed pages and `error` gives the reason and any retry delay. Use `api_key=` and `state_dir=` as you would with `check_websites`.

## API key

The checker first looks for `SCAMBUSTERS_API_KEY` in `.venv/.env` beside `scambusters_check.py`. If the file has no key setting, it checks the environment variable of the same name. The `.venv` directory can also be your Python virtual environment. Activating it is optional.

If you need a key, use `/get-api-key` in the ScamBusters Discord. Use `/rotate-api-key` for an expired, revoked, or lost key. When a key is missing or ScamBusters rejects it, the command-line tools prompt for hidden input in an interactive terminal and save the entered key to `.venv/.env`.

You can also create or edit `.venv/.env` yourself. Replace the placeholder with your own key.

```dotenv
SCAMBUSTERS_API_KEY=sb_YOUR_KEY_HERE
```

## Report

The checker writes a new `check-report-*.txt` in the current directory by default and prints the path. Use `--output` to name the report. Choose a new filename each time.

The report groups sites under **Unreported**, **Reported with no wallets**, and **Reported with wallets**. It separates failed lookups and sites left unchecked when a run stops. Exit code `0` means every listed site was classified, `1` means one or more lookups failed, and `2` means a setup error or early stop. Pressing Ctrl+C saves the results collected so far and exits with code `130`.

## Website check pacing

The checker sends lookups one at a time to the read-only ScamBusters endpoint, with at least 1.5 seconds between attempts. It tracks up to 500 attempts in a rolling hour across runs using the same state directory. A clean 200-site run takes about five minutes, plus any additional time for slow responses or retries.

On HTTP 429, the checker honors `Retry-After` when provided. Without that header it waits 61 seconds for a minute limit and allows a full hour for an hourly limit. If the required wait exceeds two minutes, the run returns partial results and the wait time. The cooldown carries over to the next run using the same state directory. The service applies the quota per student across devices.
