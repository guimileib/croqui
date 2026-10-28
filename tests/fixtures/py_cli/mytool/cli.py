"""Command line front end."""

import argparse
import sys

import requests

from . import util
from .core import run


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    args = parser.parse_args(argv)
    print(util.slug(args.name), run(args.name), requests.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
