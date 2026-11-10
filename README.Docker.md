### Building and running your application

When you're ready, start your application by running:
`docker compose up --build`.

Your application will be available at http://localhost:8000.

### Deploying your application to the cloud

First, build your image, e.g.: `docker build -t myapp .`.
If your cloud uses a different CPU architecture than your development
machine (e.g., you are on a Mac M1 and your cloud provider is amd64),
you'll want to build the image for that platform, e.g.:
`docker build --platform=linux/amd64 -t myapp .`.

Then, push it to your registry, e.g. `docker push myregistry.com/myapp`.

Consult Docker's [getting started](https://docs.docker.com/go/get-started-sharing/)
docs for more detail on building and pushing.

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

### References
* [Docker's Python guide](https://docs.docker.com/language/python/)