""" This module contains functions for cloning a Git repository to a local path. """

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from gitingest.utils.timeout_wrapper import async_timeout

TIMEOUT: int = 60


@dataclass
class CloneConfig:
    """
    Configuration for cloning a Git repository.

    This class holds the necessary parameters for cloning a repository to a local path, including
    the repository's URL, the target local path, and optional parameters for a specific commit or branch.

    Attributes
    ----------
    url : str
        The URL of the Git repository to clone.
    local_path : str
        The local directory where the repository will be cloned.
    commit : str, optional
        The specific commit hash to check out after cloning (default is None).
    branch : str, optional
        The branch to clone (default is None).
    subpath : str
        The subpath to clone from the repository (default is "/").
    blob : bool
        Whether the path points to a blob (file) or tree (directory) (default is False).
    github_token : str, optional
        GitHub token for private repository access (default is None).
    """

    url: str
    local_path: str
    commit: Optional[str] = None
    branch: Optional[str] = None
    subpath: str = "/"
    blob: bool = False
    github_token: Optional[str] = None


@async_timeout(TIMEOUT)
async def clone_repo(config: CloneConfig) -> None:
    """
    Clone a repository to a local path based on the provided configuration.

    This function handles the process of cloning a Git repository to the local file system.
    It can clone a specific branch or commit if provided, and it raises exceptions if
    any errors occur during the cloning process.

    Parameters
    ----------
    config : CloneConfig
        The configuration for cloning the repository.

    Raises
    ------
    ValueError
        If the repository is not found or if the provided URL is invalid.
    OSError
        If an error occurs while creating the parent directory for the repository.
    """
    # Extract and validate query parameters
    url: str = config.url
    local_path: str = config.local_path
    commit: Optional[str] = config.commit
    branch: Optional[str] = config.branch
    partial_clone: bool = config.subpath != "/"
    github_token: Optional[str] = config.github_token

    # Create parent directory if it doesn't exist
    parent_dir = Path(local_path).parent
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as exc:
        raise OSError(f"Failed to create parent directory {parent_dir}: {exc}") from exc

    # Check if the repository exists and apply token for authentication if necessary
    authenticated_url = url
    if github_token and "github.com" in url:
        if url.startswith("https://"):
            authenticated_url = f"https://{github_token}@{url[8:]}"
    
    # Try with token first if provided
    repo_exists = await _check_repo_exists(authenticated_url if github_token else url)
    
    if not repo_exists:
        if github_token:
            # If we're using a token and it failed, it might be a permission issue
            raise ValueError("Repository not found or access denied. Check your GitHub token permissions.")
        else:
            raise ValueError("Repository not found, make sure it is public or provide a GitHub token.")

    clone_cmd = ["git", "clone", "--single-branch"]
    # TODO re-enable --recurse-submodules

    if partial_clone:
        clone_cmd += ["--filter=blob:none", "--sparse"]

    if not commit:
        clone_cmd += ["--depth=1"]
        if branch and branch.lower() not in ("main", "master"):
            clone_cmd += ["--branch", branch]

    # Use authenticated URL if token is provided
    clone_cmd += [authenticated_url if github_token else url, local_path]

    # Clone the repository
    await _run_command(*clone_cmd)

    if commit or partial_clone:
        checkout_cmd = ["git", "-C", local_path]

        if partial_clone:
            if config.blob:
                checkout_cmd += ["sparse-checkout", "set", config.subpath.lstrip("/")[:-1]]
            else:
                checkout_cmd += ["sparse-checkout", "set", config.subpath.lstrip("/")]

        if commit:
            checkout_cmd += ["checkout", commit]

        # Check out the specific commit and/or subpath
        await _run_command(*checkout_cmd)


async def _check_repo_exists(url: str) -> bool:
    """
    Check if a Git repository exists at the provided URL.

    Parameters
    ----------
    url : str
        The URL of the Git repository to check. May include authentication credentials.
    Returns
    -------
    bool
        True if the repository exists, False otherwise.

    Raises
    ------
    RuntimeError
        If the curl command returns an unexpected status code.
    """
    # Build a safe command for checking repository existence
    # Use -L to follow redirects if needed
    curl_args = ["curl", "-I", "-L"]
    
    # Handle authentication in URL safely
    if "@" in url and "://" in url:
        # For authenticated URLs, use header-based authentication instead of including token in command
        # This prevents the token from appearing in process listings
        protocol_part, rest = url.split("://", 1)
        if "@" in rest:
            auth_part, domain_part = rest.split("@", 1)
            # Check if this looks like a token
            if ":" not in auth_part and len(auth_part) > 30:
                # Probably a token, not username:password
                curl_args.extend(["-H", f"Authorization: token {auth_part}"])
                safe_url = f"{protocol_part}://{domain_part}"
                curl_args.append(safe_url)
            else:
                # Use -u option for basic auth
                curl_args.extend(["-u", auth_part])
                safe_url = f"{protocol_part}://{domain_part}"
                curl_args.append(safe_url)
        else:
            curl_args.append(url)
    else:
        curl_args.append(url)

    proc = await asyncio.create_subprocess_exec(
        *curl_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        return False

    response = stdout.decode()
    status_code = _get_status_code(response)

    if status_code in (200, 301):
        return True

    if status_code in (404, 302, 401, 403):
        # 401/403 means unauthorized/forbidden - repository exists but access denied
        return False

    raise RuntimeError(f"Unexpected status code: {status_code}")


async def fetch_remote_branch_list(url: str) -> List[str]:
    """
    Fetch the list of branches from a remote Git repository.
    Parameters
    ----------
    url : str
        The URL of the Git repository to fetch branches from. May include authentication credentials.
    Returns
    -------
    List[str]
        A list of branch names available in the remote repository.
    """
    # Handle authenticated URLs safely
    if "@" in url and "://" in url and url.startswith("https://"):
        # Extract protocol and rest
        protocol_part, rest = url.split("://", 1)
        auth_part, domain_part = rest.split("@", 1)
        # Setup git environment to use credential helper to avoid token in command line
        env = os.environ.copy()
        
        # Handle different auth forms
        if ":" not in auth_part and len(auth_part) > 30:
            # This is likely a token
            env["GIT_ASKPASS"] = "echo"
            env["GIT_TERMINAL_PROMPT"] = "0"
            # Use credential store to securely provide credentials
            await _run_command("git", "config", "--global", "--replace-all", "credential.helper", "store")
            
            # Write credentials to git credential store
            credential_process = await asyncio.create_subprocess_exec(
                "git", "credential", "approve",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            credential_input = f"protocol=https\nhost={domain_part.split('/')[0]}\nusername={auth_part}\n\n"
            await credential_process.communicate(credential_input.encode())
            
            # Use URL without auth for the git command
            clean_url = f"{protocol_part}://{domain_part}"
            fetch_branches_command = ["git", "ls-remote", "--heads", clean_url]
            proc = await asyncio.create_subprocess_exec(
                *fetch_branches_command,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            
            # Clean up credentials after use
            cleanup_process = await asyncio.create_subprocess_exec(
                "git", "credential", "reject",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await cleanup_process.communicate(credential_input.encode())
        else:
            # Standard git ls-remote with URL
            fetch_branches_command = ["git", "ls-remote", "--heads", url]
            proc = await asyncio.create_subprocess_exec(
                *fetch_branches_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
    else:
        # Non-authenticated URL
        fetch_branches_command = ["git", "ls-remote", "--heads", url]
        stdout, _ = await _run_command(*fetch_branches_command)
    
    stdout_decoded = stdout.decode()

    return [
        line.split("refs/heads/", 1)[1]
        for line in stdout_decoded.splitlines()
        if line.strip() and "refs/heads/" in line
    ]


async def _run_command(*args: str) -> Tuple[bytes, bytes]:
    """
    Execute a command asynchronously and captures its output.

    Parameters
    ----------
    *args : str
        The command and its arguments to execute.

    Returns
    -------
    Tuple[bytes, bytes]
        A tuple containing the stdout and stderr of the command.

    Raises
    ------
    RuntimeError
        If command exits with a non-zero status.
    """
    await check_git_installed()

    # Execute the requested command
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        error_message = stderr.decode().strip()
        raise RuntimeError(f"Command failed: {' '.join(args)}\nError: {error_message}")

    return stdout, stderr


async def check_git_installed() -> None:
    """
    Check if Git is installed and accessible on the system.

    Raises
    ------
    RuntimeError
        If Git is not installed or if the Git command exits with a non-zero status.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_message = stderr.decode().strip() if stderr else "Git command not found"
            raise RuntimeError(f"Git is not installed or not accessible: {error_message}")

    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed. Please install Git before proceeding.") from exc


def _get_status_code(response: str) -> int:
    """
    Extract the status code from an HTTP response.

    Parameters
    ----------
    response : str
        The HTTP response string.

    Returns
    -------
    int
        The status code of the response
    """
    status_line = response.splitlines()[0].strip()
    status_code = int(status_line.split(" ", 2)[1])
    return status_code
