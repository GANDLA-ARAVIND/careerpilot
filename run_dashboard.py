"""Starts the Streamlit dashboard with the noisy defaults turned off.

Doesn't activate a virtualenv - run this with whichever Python/venv is
already active, same as every other script in this project.

Usage: python run_dashboard.py

Why --server.fileWatcherType none specifically: Streamlit's default ("auto")
file watcher walks every importable module's source tree to watch it for
changes. Even after ranking.py's sentence_transformers/torch import was made
lazy (see docs/decisions.md - the dashboard itself never triggers it, since
it only needs agents.coach.missing_skills_below, pure SQL, no embeddings),
anything that DOES exercise an embedding code path during a session (the
Coach tab's market_gap isn't wired into app.py, but future dev work might
import ranking directly) would still let the watcher's tree-walk trip over
transformers' own noisy optional torchvision probing. This is a personal,
single-user local tool (see CLAUDE.md) - not something that benefits from
hot-reload-on-save enough to justify carrying that risk. Edit app.py, then
rerun this script, same as before.

--browser.gatherUsageStats false and --logger.level warning are the other
quieting flags worth setting for the same "one command, no surprises"
reason - verified against this project's installed streamlit==1.61.0 via
`streamlit config show` rather than assumed, since these are config keys,
not part of `streamlit run --help`'s own listed output.
"""

import subprocess
import sys


def main() -> None:
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.fileWatcherType",
        "none",
        "--browser.gatherUsageStats",
        "false",
        "--logger.level",
        "warning",
    ]
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
