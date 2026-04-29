"""Sauron S3 mirror — optional upload of trial results to sauron's AWS bucket."""

from oddish.integrations.sauron.s3_uploader import get_sauron_uploader

__all__ = ["get_sauron_uploader"]
