import re


def slug(text):
    return re.sub(r"\W+", "-", text).strip("-").lower()
