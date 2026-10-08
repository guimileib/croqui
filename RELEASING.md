# Publishing croqui

The package is ready to publish and **has not been published**. The name `croqui` is
free on PyPI (checked: `GET /pypi/croqui/json` → 404), so the first upload claims it.

What is already done:

- `LICENSE` (MIT) exists and is carried into both the wheel and the sdist.
- `pyproject.toml` declares the SPDX licence, the supported Python versions, the
  classifiers, the keywords and the project URLs.
- `python -m build` produces a wheel and an sdist, and `twine check` passes on both.
- The wheel was installed into a clean venv and `scan`, `serve`, `build`, `context`
  and `mcp` all work from it — including `croqui/viewer/*` and every new module.

What is left is a decision and a token.

## Before the first upload

1. **Pick the version.** It lives only in `croqui/__init__.py` (`pyproject.toml` reads it from there).
2. **Look at the README as PyPI will render it** — it is the project's front page:
   `twine check` already validates it, and https://pypi.org/project/croqui/ will show
   exactly that file. Note that PyPI does not resolve *relative* links, which is why
   the Portuguese switcher at the top of `README.md` is an absolute GitHub URL while
   the one in `README.pt-BR.md` is relative. Both translations ship in the sdist
   (`MANIFEST.in`); only the English one is the PyPI description.
3. **Decide about the GitHub repository.** `project.urls` points at
   `github.com/guimileib/croqui`; if that is not public yet, the links on the
   PyPI page will 404.

## Publishing by hand

```sh
python3 -m venv /tmp/build && /tmp/build/bin/pip install -q build twine
rm -rf dist
/tmp/build/bin/python -m build            # wheel + sdist into dist/
/tmp/build/bin/twine check dist/*

# TestPyPI first — it is a separate account and a separate token
/tmp/build/bin/twine upload --repository testpypi dist/*
pipx install --index-url https://test.pypi.org/simple/ --pip-args=--no-deps croqui

# then the real one
/tmp/build/bin/twine upload dist/*
```

The token goes in `~/.pypirc` or the `TWINE_PASSWORD` environment variable with
`TWINE_USERNAME=__token__`. Scope the first token to "entire account" — a
project-scoped token cannot exist before the project does — then replace it with a
project-scoped one right after the first upload.

**A version can never be re-uploaded.** A mistake costs a version number, not a fix.
That is the whole reason for the TestPyPI step.

## Publishing from GitHub instead (no token anywhere)

Trusted Publishing is the better end state: PyPI trusts the repository, and no secret
ever exists. Register the publisher once at
https://pypi.org/manage/account/publishing/ (owner `guimileib`, repository
`croqui`, workflow `publish.yml`, environment `pypi`), then add:

```yaml
# .github/workflows/publish.yml
name: publish
on:
  push:
    tags: ["v*"]
jobs:
  pypi:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pipx run build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

After that, a release is `git tag v0.2.0 && git push --tags`.

## After it is published

`croqui update` starts doing the obvious thing for everyone who installed from PyPI
(`pip install --upgrade croqui`), and the README's install section becomes:

```sh
pipx install croqui
```

which is the line it wrongly claimed for a while before this file existed.
