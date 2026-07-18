# Installing local models

This page covers getting a local model registered and running: installing one from the Hugging Face catalog, registering weights you already have on disk, where those weights live, and how to delete a model. For an orientation to the whole page, see the [Registries tour](index.md); for hosted APIs, see [Remote models](remote.md).

**Before you start:** installing a *local* model needs a matching engine ready on your machine — run `make engines` (see [Installation](../getting-started/installation.md#3-install-the-local-engines)). The catalog only offers models you have an engine for. If you have no local engine, register a [remote model](remote.md) instead.

## Install from the catalog

The catalog browses Hugging Face and downloads straight into your inference plane.

1. Open **Registries** from the sidebar (under **Configuration**).
2. Click **Add model**, then **Browse Hugging Face →**.
3. **Search** for a model — e.g. `qwen`, `llama`, `phi` — and narrow with the **Size**, **Sort**, **Engines**, and task/capability filters. Only models you can run appear.
4. **Expand a card** to see its variants (quantizations). Each has a **fit badge** against your RAM — prefer one marked *recommended* that shows **fits**.
5. Click **Install** on the variant. In the dialog, confirm the **Logical id** — the short name agents and chat will use ("how agents reference it"). One is suggested; for a llama.cpp variant the quantization is appended so different quants get distinct ids.
6. Click **Download & install**.

The download appears in the notification center (bottom-right) with a progress bar, speed, ETA, and a **Cancel** button. It runs entirely on your machine and survives navigation. When it finishes, the model is auto-registered and shows up in the **Installed** table under your logical id, ready to chat with or wire into an agent.

!!! tip "Quick-add by id or URL"
    If you already know the exact model, skip the browse dialog: in **Add model → Hugging Face**, paste the id or URL (for example `mlx-community/Qwen2.5-0.5B-Instruct-4bit`) and press **Add** to jump straight to its variants.

!!! note "Modality is detected for you"
    A catalog install sets the model's modality from the repository's declared task — a vision model registers as `vision`, an embeddings model as `embeddings`, and so on — so the right server spawns automatically on first use. You don't pick it by hand.

??? note "Gated repositories"
    Some repositories are **gated** and need a Hugging Face token — the catalog marks them with a lock and "needs a Hugging Face token" before you click. If you have logged in to Hugging Face on this machine (or set a token in your environment), gated downloads use that ambient login. There is no token field in the install dialog.

## Register a model already on disk

If you already have model weights on your machine — a GGUF file, or an MLX model directory — you can register them without going through the catalog.

1. **Add model → Hosted / local** tab.
2. Enter a **Logical id** (e.g. `triage-fast`).
3. Choose the **Binding** for the weights: `llamacpp` (a GGUF file) or `mlx` (an MLX model directory). Which one your file is for depends on its format — see [The engines](engines.md).
4. In **Model**, enter the path to the weights on disk.
5. Save.

Models registered this way use **source `local-path`** — TheYgent loads them from where they are and never copies or moves them. The first call to the logical id spawns the engine against that path.

You can also register a local model over the management API directly. The inference plane listens on `http://localhost:8081`, and registration is a `PUT` under the logical id (the wire format is camelCase):

```bash
curl -X PUT http://localhost:8081/admin/models/triage-fast \
  -H 'Content-Type: application/json' \
  -d '{
    "binding": "llamacpp",
    "source": "local-path",
    "model": "/path/to/model-Q4_K_M.gguf",
    "modality": "chat"
  }'
```

## Where weights live on disk

Catalog downloads land under your inference plane's model directory:

```text
~/.theygent/inference/models/<org_name>/
```

The repository's `org/name` is sanitized to `org_name` (the slash becomes an underscore). To keep weights somewhere else — an external volume, say — set `THEYGENT_INFERENCE_PLANE_MODEL_DIR` before starting the inference plane. The registry that maps logical ids to weights is a separate file, `~/.theygent/inference/registry.json`; its parent directory is set by `THEYGENT_INFERENCE_PLANE_STATE_DIR`. Both are covered in the [environment reference](../reference/environment.md).

Models registered with **source `local-path`** are *not* copied here — they stay wherever you pointed the registration.

Catalog installs also record *where they came from* (the repository and variant), so a registry export can re-download the same weights on another machine — the weights themselves are never part of an export. See [Moving models to another machine](index.md#moving-models-to-another-machine).

## Editing a registered model

Click a row in the **Installed** table (not one of its buttons) to open its settings. For a catalog install this reopens the catalog detail; for anything else it opens an editable modal. What you can change depends on the binding:

- **Logical id** and **Binding** are read-only. Renaming isn't an edit — register under a new logical id instead.
- **Model**, **Source** (or **Base URL** for reachable endpoints), and **Modality** are editable.
- For a **managed** engine with a lifecycle, you can set the **Idle timeout (seconds)** and tick **Keep warm (never auto-evict)** to keep the model resident.
- For a **reachable** endpoint, you can edit the **Credential** and a **Params (JSON)** block.

## Deleting a model

To unregister a model, use the **Delete** action on its row (the trash icon). A confirmation dialog warns:

> agents referencing `<id>` will fail. This cannot be undone.

Deleting evicts the engine if it's resident, then removes the registration. It does **not** delete the downloaded weight files from disk — those stay under your model directory; remove them yourself if you want the space back. Any agent, chat, or trigger that still points at the deleted logical id will fail with a `model_not_found` error until you register something under that id again.

!!! warning "There's no undo"
    Deletion removes only the registration, but agents don't know the id is gone until they run. If several agents share a model id, deleting it breaks all of them at once. Consider [re-registering the id](../concepts/models-and-engines.md#swapping-a-model-without-touching-agents) under a different model instead of deleting.

## Related pages

- [Registries](index.md) — the full page tour, capability probing, and browse badges
- [Remote models](remote.md) — registering a hosted or remote endpoint under a logical id
- [The engines](engines.md) — which binding your weights need, and platform support
- [Your first chat](../getting-started/first-chat.md) — install a model and talk to it
- [Environment variables](../reference/environment.md) — `THEYGENT_INFERENCE_PLANE_MODEL_DIR` and friends
