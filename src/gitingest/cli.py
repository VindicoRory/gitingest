""" Command-line interface for the Gitingest package. """

# pylint: disable=no-value-for-parameter

import asyncio
import os
from pathlib import Path
from typing import Optional, Tuple

import click

from gitingest.config import GITHUB_TOKEN, MAX_FILE_SIZE, OUTPUT_FILE_NAME, TOKEN_FILE_PATH
from gitingest.repository_ingest import ingest_async


@click.command()
@click.argument("source", type=str, default=".")
@click.option("--output", "-o", default=None, help="Output file path (default: <repo_name>.txt in current directory)")
@click.option("--max-size", "-s", default=MAX_FILE_SIZE, help="Maximum file size to process in bytes")
@click.option("--exclude-pattern", "-e", multiple=True, help="Patterns to exclude")
@click.option("--include-pattern", "-i", multiple=True, help="Patterns to include")
@click.option("--branch", "-b", default=None, help="Branch to clone and ingest")
@click.option("--github-token", "-g", default=None, help="GitHub token for private repository access")
@click.option("--save-token", is_flag=True, help="Save the provided GitHub token for future use")
def main(
    source: str,
    output: Optional[str],
    max_size: int,
    exclude_pattern: Tuple[str, ...],
    include_pattern: Tuple[str, ...],
    branch: Optional[str],
    github_token: Optional[str],
    save_token: bool,
):
    """
     Main entry point for the CLI. This function is called when the CLI is run as a script.

    It calls the async main function to run the command.

    Parameters
    ----------
    source : str
        The source directory or repository to analyze.
    output : str, optional
        The path where the output file will be written. If not specified, the output will be written
        to a file named `<repo_name>.txt` in the current directory.
    max_size : int
        The maximum file size to process, in bytes. Files larger than this size will be ignored.
    exclude_pattern : Tuple[str, ...]
        A tuple of patterns to exclude during the analysis. Files matching these patterns will be ignored.
    include_pattern : Tuple[str, ...]
        A tuple of patterns to include during the analysis. Only files matching these patterns will be processed.
    branch : str, optional
        The branch to clone (optional).
    github_token : str, optional
        GitHub token for private repository access. If provided, overrides the token from config.
    save_token : bool
        If True, saves the provided GitHub token for future use.
    """
    # If token is provided and save_token is True, save it to the token file
    active_token = GITHUB_TOKEN
    if github_token:
        active_token = github_token
        if save_token:
            try:
                # Create directory if it doesn't exist
                token_dir = os.path.dirname(TOKEN_FILE_PATH)
                os.makedirs(token_dir, exist_ok=True)
                
                # Save token to file with secure permissions
                with open(TOKEN_FILE_PATH, "w", encoding="utf-8") as token_file:
                    token_file.write(github_token)
                os.chmod(TOKEN_FILE_PATH, 0o600)  # Read/write for owner only
                click.echo(f"GitHub token saved to {TOKEN_FILE_PATH}")
            except (IOError, OSError) as exc:
                click.echo(f"Warning: Failed to save GitHub token: {exc}", err=True)
    
    # Main entry point for the CLI. This function is called when the CLI is run as a script.
    asyncio.run(_async_main(source, output, max_size, exclude_pattern, include_pattern, branch, active_token))


async def _async_main(
    source: str,
    output: Optional[str],
    max_size: int,
    exclude_pattern: Tuple[str, ...],
    include_pattern: Tuple[str, ...],
    branch: Optional[str],
    github_token: Optional[str] = None,
) -> None:
    """
    Analyze a directory or repository and create a text dump of its contents.

    This command analyzes the contents of a specified source directory or repository, applies custom include and
    exclude patterns, and generates a text summary of the analysis which is then written to an output file.

    Parameters
    ----------
    source : str
        The source directory or repository to analyze.
    output : str, optional
        The path where the output file will be written. If not specified, the output will be written
        to a file named `<repo_name>.txt` in the current directory.
    max_size : int
        The maximum file size to process, in bytes. Files larger than this size will be ignored.
    exclude_pattern : Tuple[str, ...]
        A tuple of patterns to exclude during the analysis. Files matching these patterns will be ignored.
    include_pattern : Tuple[str, ...]
        A tuple of patterns to include during the analysis. Only files matching these patterns will be processed.
    branch : str, optional
        The branch to clone (optional).
    github_token : str, optional
        GitHub token for private repository access.

    Raises
    ------
    Abort
        If there is an error during the execution of the command, this exception is raised to abort the process.
    """
    try:
        # Combine default and custom ignore patterns
        exclude_patterns = set(exclude_pattern)
        include_patterns = set(include_pattern)

        if not output:
            output = OUTPUT_FILE_NAME
        summary, _, _ = await ingest_async(source, max_size, include_patterns, exclude_patterns, branch, output=output, github_token=github_token)

        click.echo(f"Analysis complete! Output written to: {output}")
        click.echo("\nSummary:")
        click.echo(summary)

    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    main()
