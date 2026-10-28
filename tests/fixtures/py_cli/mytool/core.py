"""Does the actual work."""

import json

from .util import slug


class Runner:
    def go(self, name):
        return json.dumps({"slug": slug(name)})


def run(name):
    return Runner().go(name)


def _private():
    return None
