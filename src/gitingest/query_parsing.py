""" This module contains functions to parse and validate input sources and patterns. """

import re
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Union
from urllib.parse import unquote, urlparse

from gitingest.cloning import CloneConfig, _check_repo_exists, fetch_remote_branch_list
from gitingest.config import MAX_FILE_SIZE, TMP_BASE_PATH
from gitingest.exceptions import InvalidPatternError
from gitingest.utils.ignore_patterns import DEFAULT_IGNORE_PATTERNS
from gitingest.utils.query_parser_utils import (
    KNOWN_GIT_HOSTS,
    _get_user_and_repo_from_path,
    _is_valid_git_commit_hash,
    _is_valid_pattern,
    _normalize_pattern,
    _validate_host,
    _validate_url_scheme,
)


@dataclass
class ParsedQuery:  # pylint: disable=too-many-instance-attributes
    """
    Dataclass to store the parsed details of the repository or file path.
    """

    user_name: Optional[str]
    repo_name: Optional[str]
    local_path: Path
    url: Optional[str]
    slug: str
    id: str
    subpath: str = "/"
    type: Optional[str] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    max_file_size: int = MAX_FILE_SIZE
    ignore_patterns: Optional[Set[str]] = None
    include_patterns: Optional[Set[str]] = None
    pattern_type: Optional[str] = None
    github_token: Optional[str] = None

    def extact_clone_config(self) -> CloneConfig:
        """
        Extract the relevant fields for the CloneConfig object.

        Returns
        -------
        CloneConfig
            A CloneConfig object containing the relevant fields.

        Raises
        ------
        ValueError
            If the 'url' parameter is not provided.
        """
        if not self.url:
            raise ValueError("The 'url' parameter is required.")

        return CloneConfig(
            url=self.url,
            local_path=str(self.local_path),
            commit=self.commit,
            branch=self.branch,
            subpath=self.subpath,
            blob=self.type == "blob",
            github_token=self.github_token,
        )


async def parse_query(
    source: str,
    max_file_size: int,
    from_web: bool,
    include_patterns: Optional[Union[str, Set[str]]] = None,
    ignore_patterns: Optional[Union[str, Set[str]]] = None,
    github_token: Optional[str] = None,
) -> ParsedQuery:
    """
    Parse the input source (URL or path) to extract relevant details for the query.

    This function parses the input source to extract details such as the username, repository name,
    commit hash, branch name, and other relevant information. It also processes the include and ignore
    patterns to filter the files and directories to include or exclude from the query.

    Parameters
    ----------
    source : str
        The source URL or file path to parse.
    max_file_size : int
        The maximum file size in bytes to include.
    from_web : bool
        Flag indicating whether the source is a web URL.
    include_patterns : Union[str, Set[str]], optional
        Patterns to include, by default None. Can be a set of strings or a single string.
    ignore_patterns : Union[str, Set[str]], optional
        Patterns to ignore, by default None. Can be a set of strings or a single string.
    github_token : str, optional
        GitHub token for private repository access, by default None.

    Returns
    -------
    ParsedQuery
        A dataclass object containing the parsed details of the repository or file path.
    """

    # Determine the parsing method based on the source type
    if from_web or urlparse(source).scheme in ("https", "http") or any(h in source for h in KNOWN_GIT_HOSTS):
        # We either have a full URL or a domain-less slug
        parsed_query = await _parse_remote_repo(source, github_token)
    else:
        # Local path scenario
        parsed_query = _parse_local_dir_path(source)

    # Combine default ignore patterns + custom patterns
    ignore_patterns_set = DEFAULT_IGNORE_PATTERNS.copy()
    if ignore_patterns:
        ignore_patterns_set.update(_parse_patterns(ignore_patterns))

    # Process include patterns and override ignore patterns accordingly
    if include_patterns:
        parsed_include = _parse_patterns(include_patterns)
        # Override ignore patterns with include patterns
        ignore_patterns_set = set(ignore_patterns_set) - set(parsed_include)
    else:
        parsed_include = None

    return ParsedQuery(
        user_name=parsed_query.user_name,
        repo_name=parsed_query.repo_name,
        url=parsed_query.url,
        subpath=parsed_query.subpath,
        local_path=parsed_query.local_path,
        slug=parsed_query.slug,
        id=parsed_query.id,
        type=parsed_query.type,
        branch=parsed_query.branch,
        commit=parsed_query.commit,
        max_file_size=max_file_size,
        ignore_patterns=ignore_patterns_set,
        include_patterns=parsed_include,
        github_token=github_token,
    )


async def _parse_remote_repo(source: str, github_token: Optional[str] = None) -> ParsedQuery:
    """
    Parse a repository URL into a structured query dictionary.

    If source is:
      - A fully qualified URL (https://gitlab.com/...), parse & verify that domain
      - A URL missing 'https://' (gitlab.com/...), add 'https://' and parse
      - A 'slug' (like 'pandas-dev/pandas'), attempt known domains until we find one that exists.

    Parameters
    ----------
    source : str
        The URL or domain-less slug to parse.
    github_token : str, optional
        GitHub token for private repository access (default is None).

    Returns
    -------
    ParsedQuery
        A dictionary containing the parsed details of the repository.
    """
    source = unquote(source)

    # Attempt to parse
    parsed_url = urlparse(source)

    if parsed_url.scheme:
        _validate_url_scheme(parsed_url.scheme)
        _validate_host(parsed_url.netloc.lower())

    else:  # Will be of the form 'host/user/repo' or 'user/repo'
        tmp_host = source.split("/")[0].lower()
        if "." in tmp_host:
            _validate_host(tmp_host)
        else:
            # No scheme, no domain => user typed "user/repo", so we'll guess the domain.
            host = await try_domains_for_user_and_repo(*_get_user_and_repo_from_path(source), github_token=github_token)
            source = f"{host}/{source}"

        source = "https://" + source
        parsed_url = urlparse(source)

    host = parsed_url.netloc.lower()
    user_name, repo_name = _get_user_and_repo_from_path(parsed_url.path)

    _id = str(uuid.uuid4())
    slug = f"{user_name}-{repo_name}"
    local_path = TMP_BASE_PATH / _id / slug
    url = f"https://{host}/{user_name}/{repo_name}"

    # Remaining path starts after user/repo
    remaining_parts = parsed_url.path.split("/")[3:]
    subpath = "/"
    query_type = None
    branch = None
    commit = None

    # Analyze the URL path to determine if there's a specific branch, commit, or file path
    try:
        # GitHub-based parsing - can refactor later to detect different repository layouts
        if remaining_parts:
            # Identify the type (blob/tree) and extract the ref portion (branch/commit) or file path
            if remaining_parts[0] in ["blob", "tree"]:
                query_type = remaining_parts[0]
                # Now we need to determine if the next part is a branch or a commit
                if len(remaining_parts) > 1:
                    # Check if it's a commit hash
                    if _is_valid_git_commit_hash(remaining_parts[1]):
                        commit = remaining_parts[1]
                    else:
                        # Assume it's a branch name
                        branch = remaining_parts[1]

                # Set subpath if there are more parts after the ref
                if len(remaining_parts) > 2:
                    subpath = "/" + "/".join(remaining_parts[2:])
                    if subpath and not subpath.endswith("/") and query_type == "tree":
                        subpath += "/"

            # For URLs that don't specify blob/tree, try to identify branch/commit from 'raw' URL format
            elif remaining_parts[0] == "raw" and len(remaining_parts) > 1:
                # For raw URLs, set type to blob as we're dealing with a file
                query_type = "blob"
                # As with blob/tree, determine if next part is branch or commit
                if _is_valid_git_commit_hash(remaining_parts[1]):
                    commit = remaining_parts[1]
                else:
                    branch = remaining_parts[1]

                # Set subpath if there are more parts
                if len(remaining_parts) > 2:
                    subpath = "/" + "/".join(remaining_parts[2:])
    except (IndexError, ValueError):
        pass  # Just use defaults if the path isn't in a recognized format

    # If we haven't identified a specific branch yet, check if we can determine it from the query
    # For example, GitHub URLs can specify branch as a query parameter
    if parsed_url.query:
        query_parts = parsed_url.query.split("&")
        for part in query_parts:
            if part.startswith("ref="):
                branch = part.split("=")[1]

    # If we didn't find a branch in the URL but need one for the query, attempt to detect it
    if not branch and not commit:
        try:
            branch = await _configure_branch_and_subpath(remaining_parts, url)
        except Exception as exc:  # pylint: disable=broad-except
            warnings.warn(f"Failed to determine default branch: {exc}")

    # Return a structured ParsedQuery object with all the extracted information
    return ParsedQuery(
        user_name=user_name,
        repo_name=repo_name,
        local_path=local_path,
        url=url,
        slug=slug,
        id=_id,
        subpath=subpath,
        type=query_type,
        branch=branch,
        commit=commit,
        github_token=github_token,  # Include the GitHub token in the parsed query
    )


async def _configure_branch_and_subpath(remaining_parts: List[str], url: str) -> Optional[str]:
    """
    Configure the branch and subpath based on the remaining parts of the URL.
    Parameters
    ----------
    remaining_parts : List[str]
        The remaining parts of the URL path.
    url : str
        The URL of the repository.
    Returns
    -------
    str, optional
        The branch name if found, otherwise None.

    """
    try:
        # Fetch the list of branches from the remote repository
        branches: List[str] = await fetch_remote_branch_list(url)
    except RuntimeError as exc:
        warnings.warn(f"Warning: Failed to fetch branch list: {exc}", RuntimeWarning)
        return remaining_parts.pop(0)

    branch = []
    while remaining_parts:
        branch.append(remaining_parts.pop(0))
        branch_name = "/".join(branch)
        if branch_name in branches:
            return branch_name

    return None


def _parse_patterns(pattern: Union[str, Set[str]]) -> Set[str]:
    """
    Parse and validate file/directory patterns for inclusion or exclusion.

    Takes either a single pattern string or set of pattern strings and processes them into a normalized list.
    Patterns are split on commas and spaces, validated for allowed characters, and normalized.

    Parameters
    ----------
    pattern : Set[str] | str
        Pattern(s) to parse - either a single string or set of strings

    Returns
    -------
    Set[str]
        A set of normalized patterns.

    Raises
    ------
    InvalidPatternError
        If any pattern contains invalid characters. Only alphanumeric characters,
        dash (-), underscore (_), dot (.), forward slash (/), plus (+), and
        asterisk (*) are allowed.
    """
    patterns = pattern if isinstance(pattern, set) else {pattern}

    parsed_patterns: Set[str] = set()
    for p in patterns:
        parsed_patterns = parsed_patterns.union(set(re.split(",| ", p)))

    # Remove empty string if present
    parsed_patterns = parsed_patterns - {""}

    # Validate and normalize each pattern
    for p in parsed_patterns:
        if not _is_valid_pattern(p):
            raise InvalidPatternError(p)

    return {_normalize_pattern(p) for p in parsed_patterns}


def _parse_local_dir_path(path_str: str) -> ParsedQuery:
    """
    Parse the given file path into a structured query dictionary.

    Parameters
    ----------
    path_str : str
        The file path to parse.

    Returns
    -------
    ParsedQuery
        A dictionary containing the parsed details of the file path.
    """
    path_obj = Path(path_str).resolve()
    slug = path_obj.name if path_str == "." else path_str.strip("/")
    return ParsedQuery(
        user_name=None,
        repo_name=None,
        url=None,
        local_path=path_obj,
        slug=slug,
        id=str(uuid.uuid4()),
    )


async def try_domains_for_user_and_repo(user_name: str, repo_name: str, github_token: Optional[str] = None) -> str:
    """
    Attempt to find a valid repository host for the given user_name and repo_name.

    Parameters
    ----------
    user_name : str
        The username or owner of the repository.
    repo_name : str
        The name of the repository.
    github_token : str, optional
        GitHub token for private repository access (default is None).

    Returns
    -------
    str
        The domain of the valid repository host.

    Raises
    ------
    ValueError
        If no valid repository host is found for the given user_name and repo_name.
    """
    from gitingest.config import GITHUB_TOKEN

    # Use provided token or fall back to the config token
    token = github_token or GITHUB_TOKEN
    
    # Log a debug message without exposing the token
    if token:
        token_part = token[:4] + "..." + token[-4:] if len(token) > 8 else "***"
        print(f"Checking repository access with token: {token_part}")
    
    for domain in KNOWN_GIT_HOSTS:
        # Prepare candidate URL, with token for GitHub if available
        if token and domain == "github.com":
            candidate = f"https://{token}@{domain}/{user_name}/{repo_name}"
        else:
            candidate = f"https://{domain}/{user_name}/{repo_name}"
            
        # Debug message
        print(f"Checking repository: {domain}/{user_name}/{repo_name}")
        
        if await _check_repo_exists(candidate):
            print(f"Repository found at: {domain}/{user_name}/{repo_name}")
            return domain
    
    # If we're here, no repository was found
    if token:
        raise ValueError(f"Repository not found or access denied. Check if '{user_name}/{repo_name}' exists and your token has correct permissions.")
    else:
        raise ValueError(f"Could not find a public repository for '{user_name}/{repo_name}'. For private repositories, provide a GitHub token.")
