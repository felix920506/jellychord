# JellyChord Docker image

To run the published image, see [Using docker compose](README.md#using-docker-compose) in `README.md`.
This file covers building the image yourself and how images are published.

### Building the image yourself

The `Dockerfile` installs dependencies from a `requirements.txt` that is not in
the repository. Export it from `poetry.lock` first, then build:

```sh
poetry install
poetry export --without-hashes -f requirements.txt --output requirements.txt
docker build -t jellychord .
```

`poetry install` is needed even if you only want the export: it installs the
export plugin that `pyproject.toml` asks for.

To run your build, replace the `image:` in `compose.yaml` with `jellychord`
and run `docker compose up -d`. The bot only makes outgoing connections, so no
ports need to be published.

### Published images

`.github/workflows/build-container.yml` publishes `ghcr.io/felix920506/jellychord`.
Nothing is published for a commit whose unit tests (`tests.yml`) fail.

* push to `main`, tests pass: `sha-<short>` (plus `-amd64` / `-arm64`), and
  `latest` / `latest-<arch>` if the commit is still the head of `main` when the
  run finishes (so re-running an old commit's build doesn't roll `latest` back);
* pushed tag `vX.Y.Z`, tests pass: `X.Y.Z` and `X.Y` (pre-releases such as
  `vX.Y.Z-rc1` only get `X.Y.Z-rc1`). `latest` is not moved by releases.

`sha-<short>` tags are written once: if the commit already has one (for example
on a re-run), the workflow reuses it instead of rebuilding. Release tags never
build; they add version tags to the `sha-<short>` image that `main` published,
so a release is the same image `main` shipped. If that image doesn't exist yet
(the `main` build is still running, or the commit was never on `main`), the
release run fails; re-run it once the `main` build has finished.

To release, set `version` in `pyproject.toml` to `X.Y.Z`, merge it to `main`,
wait for its container build, then push the tag `vX.Y.Z` on that commit (or
create a GitHub release, which creates the tag). The workflow fails if the tag
and `pyproject.toml` disagree.

The E2E canary (`e2e.yml`) does not gate publishing: it depends on Discord and
Jellyfin being up and mostly catches Discord-side changes, which it reports as
GitHub issues.
