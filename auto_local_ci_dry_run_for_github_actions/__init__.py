"""Local CI dry-run for GitHub Actions

Validates and runs a workflow's shell steps in a Docker container locally, with a plain-
language report of which features (matrix, services, secrets) are unsupported. Aimed at
people who don't want to push repeated 'fix ci' commits.
"""

__version__ = "0.1.0"
