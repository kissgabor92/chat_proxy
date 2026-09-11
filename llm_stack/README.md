# llm_stack — Open WebUI + Ollama

A two-container local chat stack: Ollama serving models on the RTX 5060 Ti, Open WebUI in front of it. Verified working on this host on 2026-09-11.

```
llm_stack/
├── init.sh              <- start here
├── compose.yaml
├── .dockerignore
├── README.md
└── ollama/              <- model weights, git-ignored, 13 GB
    ├── models/blobs/
    ├── models/manifests/
    └── .ollama/
```

## Run it

```bash
./llm_stack/init.sh      # from the repository root
```

The script resolves its own directory, so it works from anywhere. Open WebUI is not
published by this stack — start the bridge too:

```bash
./proxy_stack/init.sh
```

Open <http://127.0.0.1:3000>. The first visit asks
you to create a local admin account; it is stored in the `chat-proxy-openwebui-data`
volume and never leaves this machine.

`init.sh` checks prerequisites, brings the stack up, waits for both containers to report healthy, pulls the default model if nothing is downloaded yet, and confirms the GPU is attached. It is safe to re-run — it skips a model that is already present and does not recreate healthy containers — and it can be called from any directory.

| | |
| --- | --- |
| `./init.sh` | Up, plus pull `gpt-oss:20b` if no model is present |
| `MODEL=llama3.2:3b ./init.sh` | Pull something else instead |
| `./init.sh --no-pull` | Up only, no download |
| `./init.sh --down` | Stop. Weights and history survive |

It exits non-zero and explains itself if Docker is missing or the daemon is unreachable. A GPU that does not resolve is a warning, not a failure — Ollama still runs on CPU, just far slower. It also warns, rather than fails, if the VS Code endpoint is not up, since that is a separate compose project.

Plain `docker compose up -d` / `docker compose down` from this directory work too.

## Choices, and why

| Choice | Reason |
| --- | --- |
| `ollama/ollama:0.12.3`, `ghcr.io/open-webui/open-webui:v0.6.30-slim` | Immutable tags. `main`/`latest` move, and Open WebUI migrates its SQLite forward on start with no downgrade path — an unpinned rebuild can leave the data volume unreadable by the version you later pin. |
| `-slim` variant | 1.37 GB compressed vs 1.82 GB for `:main`, measured from the registry manifest. `v0.6.30-slim` is the only tag that is both slim and version-pinned. |
| GPU passed as plain `/dev/nvidia*` device mounts | No `nvidia` runtime and no CDI reference — the device nodes are mounted directly. See the caveat below: device nodes alone are not sufficient. |
| Four `libnvidia*` / `libcuda` bind mounts | The Ollama image ships the CUDA **runtime** (`libcublas`, `libcudart`) but not `libcuda.so`, the **driver** library. Without it Ollama logs `no compatible GPUs were discovered` and silently serves from the CPU. Bound via `.so.1`/`.so.4` symlinks so a driver upgrade does not break the path. |
| `user: "1000:1000"` on ollama | Weights are bind-mounted into the project tree. Without this, Ollama runs as root and every blob lands on the host root-owned, needing `sudo` to delete. Verified: blobs are `drain:drain`. |
| `127.0.0.1:3000:8080` | Port 8080 on this host is held by `pewstack-pewshare-backend`, so the upstream `8080:8080` mapping collides. The OpenAI endpoint for VS Code is a **separate** service on 8111 that serves no interface. |
| `WEBUI_SECRET_KEY` pinned from `.env` | Open WebUI's `start.sh` writes a generated key to `/app/backend/.webui_secret_key`, which is in the image, not the data volume. Unpinned, every container recreation silently invalidates every API token and browser session. |
| Ollama's 11434 is **not** published | Ollama has no authentication and its API can pull and run arbitrary models. The UI reaches it over the `chat-proxy` network. Verified absent from `ss -ltn`. |
| Default auth, **not** `WEBUI_AUTH=False` | Disabling auth looks right for a loopback stack, but Open WebUI rejects it as soon as any account exists: *"You can't turn off authentication because there are existing users."* That strands the instance behind a login it refuses to process. One signup is cheaper than that failure mode. |
| `OLLAMA_KEEP_ALIVE=30m` | gpt-oss:20b takes ~30 s to load into VRAM; the 5 m default unloads it between conversations. |
| UI state in a named volume, weights in the tree | Only the weights were wanted in the project directory. Conversations live in `chat-proxy-openwebui-data`. |

## Measured on this host

| | |
| --- | --- |
| GPU detected | `library=cuda variant=v13 compute=12.0`, RTX 5060 Ti, 15.5 GiB total / 15.0 GiB available |
| gpt-oss:20b offload | **`100% GPU`, 25/25 layers**, 12906 MiB of 16311 MiB VRAM in use |
| Model on disk | 13 GB in `ollama/models/blobs` (5 blobs) |
| Cold load into VRAM | ~30 s |
| Warm reply, end to end through the UI | 4.4 s |
| Disk cost of the whole stack | 705 GB → 728 GB used, 96 GB free (89%) |

gpt-oss:20b fits entirely in VRAM with ~3 GB to spare. A larger model will not — see below.

## Models

Weights are bind-mounted to `./ollama`, **not** a named volume. They are git-ignored via the repo-root `.gitignore` and excluded from any future build context by `.dockerignore`. Both guards matter: 13 GB committed to git is effectively unremovable.

```bash
docker compose exec ollama ollama list
docker compose exec ollama ollama pull llama3.2:3b
docker compose exec ollama ollama rm gpt-oss:20b
```

## Verifying the GPU is actually used

Ollama falls back to CPU **silently** and still answers correctly, so a plausible reply proves nothing.

```bash
docker compose exec ollama ls /dev/nvidia0                  # device attached
docker compose logs ollama | grep -i 'inference compute'    # CUDA selected
docker compose exec ollama ollama ps                        # PROCESSOR column
```

`ollama ps` is decisive. `100% GPU` means fully offloaded; anything mentioning `CPU` means part of the model spilled out of VRAM and generation will be far slower.

`nvidia-smi` is **not** present in these containers — only the four compute libraries are mounted, not the host binaries. Testing for `nvidia-smi` reports a false negative even when the GPU is working. Test for `/dev/nvidia0` and the log line instead.

### Why the library mounts are there

Measured on this host, same image, same devices:

| Config | Result |
| --- | --- |
| `devices:` only | `no compatible GPUs were discovered`, `library=cpu` — answers normally, entirely on CPU |
| `devices:` + the four driver libraries | `library=cuda variant=v13 compute=12.0`, `100% GPU` |

If a driver upgrade renames those `.so` symlinks, `docker compose up` fails with a bind-mount error. That is the intended behaviour — a loud failure is better than the silent CPU fallback above. `init.sh` checks all four before starting.

## Troubleshooting

- **UI lists no models** — check `docker compose exec ollama ollama list`, then `docker compose exec webui curl -s http://ollama:11434/api/tags`.
- **Login screen won't accept anything / "can't turn off authentication"** — a `WEBUI_AUTH=False` was set after an account existed. Remove it, or wipe `chat-proxy-openwebui-data` and start over.
- **`unresolvable CDI devices`** — the CDI spec is missing; regenerate with `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`.
- **Slow replies** — check `ollama ps` for CPU spill.
- **UI unreachable** — check `ss -ltn | grep :3000` and `docker compose ps`.
- **Every token and login stopped working** — `WEBUI_SECRET_KEY` is missing from `llm_stack/.env`. Restore it, or every JWT and browser session is invalidated on each container recreation.
- **Reset the UI only** (keeps the 13 GB of weights): `docker compose down && docker volume rm chat-proxy-openwebui-data && docker compose up -d`.
