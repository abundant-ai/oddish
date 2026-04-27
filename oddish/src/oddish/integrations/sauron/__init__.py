"""Sauron S3 mirror — uploads trial results to sauron's AWS S3 bucket.

This package mirrors trial artifacts to sauron's `abundant-github-workflows-bucket`
in sauron's expected directory layout, so sauron's frontend can render
oddish-originated experiments natively (using its existing renderers,
trajectory viewer, snapshots tab, etc.).

The mirror is optional — disabled when ODDISH_SAURON_S3_BUCKET is empty.
"""

from oddish.integrations.sauron.s3_uploader import (
    SauronS3Uploader,
    get_sauron_uploader,
)

__all__ = ["SauronS3Uploader", "get_sauron_uploader"]
