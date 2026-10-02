# sandbox_kit provenance

Vendored from `git@github.com:nevilgeorge/agent-sandbox.git` at **d572785**.

These are **byte-identical copies**, not generated files. Unlike
`src/assistant_agent/agent_kit/`, nothing installs them anywhere: `deploy/deploy.sh`
reads them straight from the working tree as a `docker buildx` context, so there is no
`install-kit` equivalent and no manifest. `diff` is the drift check.

Do not add a provenance header to `Dockerfile`. Its first line is
`# syntax=docker/dockerfile:1`, and the syntax directive must come first -- a banner above
it silently disables BuildKit frontend selection, which this Dockerfile depends on for
`--mount=type=cache` and `COPY --chmod`.

The upstream build context is only these two files. `agent-sandbox/.dockerignore` is `*`
plus `!docker/entrypoint.sh`, and `docker/entrypoint.sh` is the one `COPY` from the
context, so nothing else in that repo reaches a layer.

## Re-sync

From the repo root, with `agent-sandbox` checked out as a sibling directory:

```sh
rsync -a --delete ../agent-sandbox/Dockerfile ../agent-sandbox/.dockerignore \
    ../agent-sandbox/docker src/assistant_agent/sandbox_kit/
```

Then update the commit recorded above.

## Verify no drift

```sh
diff ../agent-sandbox/Dockerfile    src/assistant_agent/sandbox_kit/Dockerfile
diff ../agent-sandbox/.dockerignore src/assistant_agent/sandbox_kit/.dockerignore
diff -r ../agent-sandbox/docker     src/assistant_agent/sandbox_kit/docker
```
