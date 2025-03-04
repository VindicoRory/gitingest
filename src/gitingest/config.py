""" Configuration file for the project. """

import os
import tempfile
from pathlib import Path

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_DIRECTORY_DEPTH = 20  # Maximum depth of directory traversal
MAX_FILES = 10_000  # Maximum number of files to process
MAX_TOTAL_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB

OUTPUT_FILE_NAME = "digest.txt"

TMP_BASE_PATH = Path(tempfile.gettempdir()) / "gitingest"

# GitHub token for private repository access
# Priority: 1. Environment variable, 2. Token file
GITHUB_TOKEN = os.environ.get("GITINGEST_GITHUB_TOKEN", "")
TOKEN_FILE_PATH = os.path.expanduser("~/.gitingest/github_token")

# Load token from file if environment variable is not set
if not GITHUB_TOKEN and os.path.exists(TOKEN_FILE_PATH):
    try:
        with open(TOKEN_FILE_PATH, "r", encoding="utf-8") as token_file:
            GITHUB_TOKEN = token_file.read().strip()
    except (IOError, OSError):
        pass
