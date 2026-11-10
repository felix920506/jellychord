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

`.github/workflows/build-container.yml` publishes `ghcr.io/felix920506/jellychord`
after the unit tests (`tests.yml`) pass for the commit:

* every push to `main`: `sha-<short>` (plus `-amd64` / `-arm64`) and `latest`;
* every pushed tag `vX.Y.Z`: `X.Y.Z` and `X.Y` (pre-releases such as
  `vX.Y.Z-rc1` only get `X.Y.Z-rc1`). `latest` is not moved by releases.

`sha-<short>` tags are written once. If the commit already has one (a release
tagged on a commit that was already built from `main`, or a re-run), the
workflow adds the new tags to that image instead of rebuilding, so a release is
the same image `main` shipped.

To release, set `version` in `pyproject.toml` to `X.Y.Z`, merge it to `main`,
then push the tag `vX.Y.Z` on that commit (or create a GitHub release, which
creates the tag). The workflow fails if the tag and `pyproject.toml` disagree.

The E2E canary (`e2e.yml`) does not gate publishing: it depends on Discord and
Jellyfin being up and mostly catches Discord-side changes, which it reports as
GitHub issues.

### References
* [Docker's Python guide](https://docs.docker.com/language/python/)