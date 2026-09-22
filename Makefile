# Release plumbing for ufo-tdkit-tools.
#
# `make publish` is the only irreversible target in here, and PyPI is unusually
# unforgiving about it: a version number can be yanked but never re-uploaded, so a
# wrong artefact is permanent under that number. Everything before the upload is
# therefore a gate, and each one exists because it is cheap here and expensive after.
#
# Credentials are never stored in this file, and never asked for until the moment they
# are used: UV_PUBLISH_TOKEN if set, else ~/.pypirc, else a hidden prompt. Better still,
# publish from CI with Trusted Publishing and keep no token anywhere -- see the bottom.

# bash, not /bin/sh -- and not incidentally: the hidden token prompt below uses
# `read -rs -p`, and dash (which /bin/sh is on Debian and Ubuntu) supports neither -s
# nor -p. Under dash the read silently yields an empty string, so the prompt would look
# like it worked and then abort. Removing this line breaks `make publish` on the systems
# most likely to run it.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

NAME    := ufo-tdkit-tools
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
TAG     := v$(VERSION)

.DEFAULT_GOAL := help
.PHONY: help test lint check build verify clean guard-clean guard-tag guard-unpublished publish-test publish release-checklist

help: ## Show this list
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*?## ' '{printf "  \033[1m%-20s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  $(NAME) $(VERSION)  (tag $(TAG))"

test: ## Run the full suite
	uv run pytest -q

lint: ## Ruff
	uv run ruff check src/ tests/

check: test lint ## Suite + lint

clean: ## Remove built artefacts
	rm -rf dist build *.egg-info

build: clean ## Build the sdist and the wheel from a clean dist/
	uv build
	@ls -1 dist/

# --- gates -------------------------------------------------------------------------

guard-clean: ## Fail unless the working tree is clean and matches origin/main
	@test -z "$$(git status --porcelain)" \
	  || { echo "error: working tree is dirty; publishing it would ship something no commit records"; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" \
	  || { echo "error: not on main (on $$(git rev-parse --abbrev-ref HEAD))"; exit 1; }
	@git fetch --quiet origin main
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse origin/main)" \
	  || { echo "error: HEAD and origin/main differ; push or pull first"; exit 1; }

guard-tag: ## Fail unless tag v<version> exists and points at HEAD
	@git rev-parse -q --verify "refs/tags/$(TAG)" >/dev/null \
	  || { echo "error: tag $(TAG) does not exist. Cut the release first (make release-checklist), then publish."; exit 1; }
	@test "$$(git rev-parse "$(TAG)^{commit}")" = "$$(git rev-parse HEAD)" \
	  || { echo "error: $(TAG) does not point at HEAD -- you would publish code the tag does not describe"; exit 1; }

guard-unpublished: ## Fail if this version is already on PyPI
	@code=$$(curl -sS -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$(NAME)/$(VERSION)/json" || echo 000); \
	if [ "$$code" = "200" ]; then \
	  echo "error: $(NAME) $(VERSION) is already on PyPI. A version can be yanked but never re-uploaded -- bump the version."; exit 1; \
	elif [ "$$code" != "404" ]; then \
	  echo "error: could not reach PyPI to check (HTTP $$code); refusing to guess"; exit 1; \
	fi

verify: build ## Check the metadata renders, and that the built wheel actually runs
	uvx twine check --strict dist/*
	@tmp=$$(mktemp -d); \
	uv venv --quiet "$$tmp/venv"; \
	VIRTUAL_ENV="$$tmp/venv" uv pip install --quiet dist/*.whl; \
	got=$$("$$tmp/venv/bin/ufo-tdkit-tools" --version); \
	bare=$$("$$tmp/venv/bin/python" -c "import ufo_tdkit_tools.compilation, ufo_tdkit_tools.ps_hints; print('ok')" 2>&1); \
	rm -rf "$$tmp"; \
	test "$$got" = "ufo-tdkit-tools $(VERSION)" \
	  || { echo "error: installed wheel reports '$$got', expected 'ufo-tdkit-tools $(VERSION)'"; exit 1; }; \
	test "$$bare" = "ok" \
	  || { echo "error: bare install (no extras) cannot import the package: $$bare"; exit 1; }
	@echo "ok: wheel installs clean, the entry point runs, and a bare install imports"

# --- publishing --------------------------------------------------------------------

# How the token reaches `uv publish`, in order of preference:
#   1. UV_PUBLISH_TOKEN already in the environment (CI);
#   2. ~/.pypirc, which uv reads by itself;
#   3. a hidden prompt, right before the upload and nowhere else.
#
# The prompt is the default for a person at a terminal, because the alternatives leak:
# `export` puts the token in the shell history and then in the environment of every
# later command, and `--token` on a command line is visible in `ps` to anyone on the
# box. Passing it as a per-command environment variable is neither. It is read with
# `read -rs`, so a paste shows nothing at all .
define upload
@if [ -n "$${UV_PUBLISH_TOKEN:-}" ] || [ -f "$$HOME/.pypirc" ]; then \
  uv publish $(1) dist/*; \
else \
  if [ ! -t 0 ]; then \
    echo "error: no UV_PUBLISH_TOKEN, no ~/.pypirc, and no terminal to ask on."; \
    echo "       In CI, set UV_PUBLISH_TOKEN -- or better, use Trusted Publishing (see below)."; \
    exit 1; \
  fi; \
  read -rs -p "  API token (input is hidden -- paste and press Enter): " token; echo; \
  test -n "$$token" || { echo "  aborted: empty token"; exit 1; }; \
  case "$$token" in pypi-*) ;; *) echo "  note: that does not start with 'pypi-'; tokens do. Continuing anyway." ;; esac; \
  UV_PUBLISH_TOKEN="$$token" uv publish $(1) dist/*; \
fi
endef

publish-test: verify ## Upload to TestPyPI (safe rehearsal; asks for a TestPyPI token)
	$(call upload,--publish-url https://test.pypi.org/legacy/)
	@echo
	@echo "Installed check:"
	@echo "  uv tool install --index-url https://test.pypi.org/simple/ \\"
	@echo "    --extra-index-url https://pypi.org/simple/ $(NAME)==$(VERSION)"

publish: guard-clean guard-tag guard-unpublished check verify ## Upload to PyPI (IRREVERSIBLE)
	@echo
	@echo "  About to publish $(NAME) $(VERSION) to PyPI from $(TAG)."
	@echo "  This cannot be undone: the version can be yanked, never replaced."
	@echo
	@if [ "$${CONFIRM:-}" != "yes" ]; then \
	  read -r -p "  Type the version to confirm: " answer; \
	  test "$$answer" = "$(VERSION)" || { echo "  aborted"; exit 1; }; \
	fi
	$(call upload,)
	@echo
	@echo "Published. Remaining steps that PyPI does not do for you:"
	@echo "  - gh release create $(TAG) ... (its publish.yml run will fail on the existing version; expected)"
	@echo "  - tell TDKit the new version range"

release-checklist: ## Print the order of operations, without doing anything
	@echo "  1. move [Unreleased] into [X.Y.Z] - <today> in CHANGELOG.md, add the compare link"
	@echo "  2. bump version in pyproject.toml, then: uv lock"
	@echo "  3. make check verify"
	@echo "  4. git commit -am 'chore(release): X.Y.Z' && git tag -a vX.Y.Z -m vX.Y.Z"
	@echo "  5. git push origin main --follow-tags"
	@echo "  6. make publish-test   # optional rehearsal on TestPyPI"
	@echo "  7. gh release create vX.Y.Z --title vX.Y.Z --notes-file <CHANGELOG section> --verify-tag --latest"
	@echo "     -> publishing the Release runs .github/workflows/publish.yml, which uploads to PyPI"
	@echo "  8. tell TDKit the new version range"
	@echo
	@echo "  'make publish' is the manual fallback for step 7's upload only; if you use it,"
	@echo "  the later Release will re-run publish.yml, which then fails on the existing version."

# Trusted Publishing (the default path, see .github/workflows/publish.yml)
# ------------------------------------------------------------------------------------
# One-time setup: https://pypi.org/manage/account/publishing/ -> add a pending publisher
# (project ufo-tdkit-tools, owner typedev, repo ufo-tdkit-tools, workflow publish.yml,
# environment pypi), and create the `pypi` environment in the GitHub repo settings.
