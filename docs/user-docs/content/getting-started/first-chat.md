# Your first chat

The quickest way to see TheYgent working is to install a small model and talk to it. This takes a few minutes once the services are up.

**Before you start:** the three services should be [running](running.md) (`make status` to confirm), and you should have at least one local engine [installed](installation.md) with `make engines`. No local engine? You can register a [remote model](../models/remote.md) instead and skip straight to step 3.

## 1. Open the Registries page

In the left sidebar, under **Configuration**, open **Registries**. This page lists the models registered in your inference plane and is where you add new ones.

## 2. Install a small model

Click **Add model**, then **Browse Hugging Face →** to open the browse dialog.

1. **Search** for something small — for example `qwen`, `llama`, or `phi`.
2. Set the **Size** filter to **Small (<3B)**. Small, quantized models download fastest and load quickly.
3. The **Engines** chips are limited to engines your machine can actually run — discovery only shows models you can execute. (If no local engine is ready, the page tells you to install one first.)
4. Expand a card to see its **variants** (quantizations). Each carries a **fit badge** sized against your machine's RAM: **fits** (green), **tight** (amber), or **too large** (red). Choose a variant marked *recommended* that fits.
5. Click **Install**. Pick a **logical id** — the short name agents and chat will use to reference this model (one is suggested for you) — then **Download & install**.

!!! tip "Or paste an id directly"
    In the **Hugging Face** tab you can paste a specific model id or URL — for example `mlx-community/Qwen2.5-0.5B-Instruct-4bit` — and press **Add** to jump straight to its variants.

Download progress appears in the notification center (bottom-right): bytes transferred, speed, an ETA, and a **Cancel** button. It survives navigation, so you can move on while it runs. The weights download **inside your inference plane** — the bytes never pass through the control plane. When it finishes, the model shows up in the **Installed** table with the logical id you chose.

## 3. Start a chat

In the sidebar, open **New Chat**.

1. Leave the **Model / Agent** toggle on **Model**.
2. In the searchable select, pick your model by its logical id.

## 4. Send a message

Type in the composer at the bottom and press **Enter** to send (**Shift+Enter** inserts a newline). Your first message lazily starts the engine — the model loads on demand, so the first reply can take a little longer than the ones after it; the engine then stays warm.

The answer **streams** in token by token. If you picked a reasoning ("thinking") model, its reasoning appears in a collapsible **Thinking…** block above the answer, which auto-collapses once the answer lands.

A few things worth knowing:

- The model is **pinned once the conversation starts** — use the **New chat** button to switch to a different model.
- Every conversation is recorded as a **session**. Find it again under **Chats** in the sidebar, or in the **Recents** list below the navigation.
- Each answer carries a **run** link that opens the full run detail with its execution trace.

## Where to next

| You want to… | Go to |
|---|---|
| Build an agent instead of chatting with a raw model | [Your first agent](first-agent.md) |
| Learn everything the chat surface can do (voice, images, thinking) | [Chat](../chat/index.md) |
| Understand installing, browsing, and managing models | [Installing local models](../models/installing.md) |
| A model won't appear or a call fails | [Troubleshooting](../reference/troubleshooting.md) |
