import { expect, test } from "@playwright/test";

// Two end-to-end smokes (M8 §5): the agent loop and the thread loop. They run against a REAL
// running stack — control-plane + (fake or real) inference + Postgres — plus the Vite dev
// server. See README.md for bringing the stack up. A logical model id must be registered;
// override with E2E_MODEL (default "triage-fast").
//
// These are the "would the cockpit being usable break?" tests, not pixel checks. No component
// snapshots, no Storybook (§5: tooling for a UI with more than one user; M8 has one).

const MODEL = process.env.E2E_MODEL ?? "triage-fast";
const SLOW_MODEL = process.env.E2E_SLOW_MODEL ?? "triage-slow";

// A structurally-valid IR that references an UNREGISTERED logical model — it passes the
// composer's client-side structural validation but fails at run time (the backend returns
// 404 model_not_found). Used to prove the UI surfaces the backend's error payload readably.
const FAILING_IR = JSON.stringify(
  {
    schemaVersion: "1.0",
    id: "agt_01J9X8FAIL",
    name: "doomed",
    version: "0.1.0",
    models: { default: { binding: "mlx", model: "ghost-unregistered-xyz" } },
    tools: {},
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        ports: { in: [], out: [{ id: "out", type: "any" }] },
      },
      {
        id: "n_llm",
        type: "llm",
        kind: "activity",
        config: { model: "default", messages: [{ role: "user", content: "$input" }] },
        ports: {
          in: [{ id: "in", type: "any" }],
          out: [
            { id: "ok", type: "any" },
            { id: "err", type: "error" },
          ],
        },
      },
      {
        id: "n_out",
        type: "output",
        kind: "boundary",
        ports: { in: [{ id: "in", type: "any" }], out: [] },
      },
    ],
    edges: [
      {
        id: "e1",
        source: "n_in",
        sourceHandle: "out",
        target: "n_llm",
        targetHandle: "in",
        channel: "data",
      },
      {
        id: "e2",
        source: "n_llm",
        sourceHandle: "ok",
        target: "n_out",
        targetHandle: "in",
        channel: "data",
      },
    ],
  },
  null,
  2,
);

test("agent loop: paste IR in graph mode, submit, watch it complete", async ({ page }) => {
  await page.goto("/compose");
  await page.getByRole("button", { name: "graph mode" }).click();

  // The composer ships a known-good trivial IR; just point it at the registered model.
  await expect(page.getByText(/IR looks structurally valid/)).toBeVisible();
  await page.getByPlaceholder("Input passed to the graph…").fill("hello from the cockpit");

  await page.getByRole("button", { name: /Run & stream/ }).click();

  // Submit navigates to the run detail with streaming attached (§1.4).
  await expect(page).toHaveURL(/\/runs\/.+/);
  // The run reaches a terminal completed status; deltas have populated the output panel.
  await expect(page.getByText("completed").first()).toBeVisible({ timeout: 30_000 });
});

test("thread loop: two runs in one thread show both turns in order", async ({ page }) => {
  const threadId = `e2e-${Date.now()}`;

  // Turn 1 — prompt mode, with a fresh thread id.
  await page.goto(`/compose?threadId=${threadId}`);
  await page.getByPlaceholder("Ask the model something…").fill("first question");
  await page.locator("select").selectOption(MODEL);
  await page.getByRole("button", { name: /Run & stream/ }).click();
  await expect(page.getByText("completed").first()).toBeVisible({ timeout: 30_000 });

  // Turn 2 — same thread, a follow-up.
  await page.goto(`/compose?threadId=${threadId}`);
  await page.getByPlaceholder("Ask the model something…").fill("second question");
  await page.locator("select").selectOption(MODEL);
  await page.getByRole("button", { name: /Run & stream/ }).click();
  await expect(page.getByText("completed").first()).toBeVisible({ timeout: 30_000 });

  // The thread detail shows both turns in position order (user, assistant, user, assistant).
  await page.goto(`/threads/${threadId}`);
  await expect(page.getByText("first question")).toBeVisible();
  await expect(page.getByText("second question")).toBeVisible();
  await expect(page.getByText(/4 messages/)).toBeVisible();
});

test("live-stream resume: navigate away mid-stream, come back, stream still attached", async ({
  page,
}) => {
  // The whole point of live.ts parking a stream: a run started in the composer keeps streaming
  // into the store across CLIENT-SIDE navigation (a full reload would, correctly, drop it). Use
  // the slow model so there's a stream in flight while we navigate.
  await page.goto("/compose");
  await page.getByPlaceholder("Ask the model something…").fill("stream slowly please");
  await page.locator("select").selectOption(SLOW_MODEL);
  await page.getByRole("button", { name: /Run & stream/ }).click();
  await expect(page).toHaveURL(/\/runs\/.+/);
  await expect(page.getByText("streaming…")).toBeVisible({ timeout: 5000 });

  // Navigate AWAY via the in-app link (not a reload) while the stream is still running…
  await page.getByRole("link", { name: "← Runs" }).click();
  await expect(page).toHaveURL(/\/$|\/runs$|\/$/);
  await expect(page.getByText("streaming").first()).toBeVisible({ timeout: 5000 });

  // …then click back into the run. The parked stream must still be attached and growing.
  await page.locator("tbody tr").first().getByRole("link").first().click();
  await expect(page).toHaveURL(/\/runs\/.+/);
  const out = page.locator("pre").first();
  await expect(out).toBeVisible();
  const before = (await out.textContent())?.length ?? 0;
  await page.waitForTimeout(900); // ~2 more slow chunks should arrive
  const after = (await out.textContent())?.length ?? 0;
  expect(after).toBeGreaterThan(before); // tokens are STILL arriving post-navigation

  // And it eventually completes.
  await expect(page.getByText("completed").first()).toBeVisible({ timeout: 15_000 });
});

test("error-state visibility: a failing run shows the backend error payload, readably", async ({
  page,
}) => {
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/compose");
  await page.getByRole("button", { name: "graph mode" }).click();

  // Replace the editor's default IR with one referencing an unregistered model. We paste (not
  // type) so CodeMirror's bracket auto-close doesn't corrupt the JSON.
  await page.evaluate((t) => navigator.clipboard.writeText(t), FAILING_IR);
  const content = page.locator(".cm-content");
  await content.click();
  const mod = process.platform === "darwin" ? "Meta" : "Control";
  await page.keyboard.press(`${mod}+a`);
  await page.keyboard.press(`${mod}+v`);
  await expect(page.getByText(/IR looks structurally valid/)).toBeVisible();

  await page.getByPlaceholder("Input passed to the graph…").fill("this will fail");
  await page.getByRole("button", { name: /Run & stream/ }).click();

  // The failure is surfaced in the composer with the mapped CODE + message — not a bare "404".
  await expect(page.getByText(/model_not_found/)).toBeVisible({ timeout: 15_000 });

  // The failed run is persisted; it shows as failed in the list and its error is visible in
  // the run detail (a future restyle that hid the error payload would break this).
  await page.getByRole("link", { name: "Runs" }).click();
  const firstRow = page.locator("tbody tr").first();
  await expect(firstRow.getByText("failed")).toBeVisible({ timeout: 10_000 });
  await firstRow.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/runs\/.+/);
  await expect(page.getByText("failed").first()).toBeVisible();
  await expect(page.getByText(/model_not_found/)).toBeVisible();
});

test("registries mutate state: register → warm → evict → delete a model; register/delete MCP", async ({
  page,
}) => {
  // The only UI-as-CONTROL-SURFACE test (not UI-as-viewer). If registration doesn't actually
  // change backend state, the cockpit is useless for setting up agents.
  await page.goto("/registries");

  // Register a managed model. The form defaults (binding mlx, source hf) are what we want, so
  // we only fill the two text inputs.
  await page.getByPlaceholder("triage-fast").fill("e2e-mlx");
  await page.getByPlaceholder("mlx-community/Qwen2.5-0.5B-4bit").fill("fake-weights");
  await page.getByRole("button", { name: "Register" }).click();

  const row = page.locator("tr", { hasText: "e2e-mlx" });
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row.getByText("cold")).toBeVisible();

  // Warm it → it becomes resident (the fake launcher makes this instant). Proven both by the
  // row badge and the resident-count header reading from /admin/engines.
  await row.getByRole("button", { name: "warm" }).click();
  await expect(row.getByText("resident")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/Resident engines:\s*1\b/)).toBeVisible();

  // Evict it → back to cold, resident set empties.
  await row.getByRole("button", { name: "evict" }).click();
  await expect(row.getByText("cold")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/Resident engines:\s*0\b/)).toBeVisible();

  // Delete it → gone from /admin/models (the table reflects that endpoint).
  await row.getByRole("button", { name: "delete" }).click();
  await expect(page.locator("tr", { hasText: "e2e-mlx" })).toHaveCount(0, { timeout: 10_000 });

  // MCP servers: register → listed → delete → gone (lazy connect, no spawn needed).
  await page.getByRole("button", { name: "MCP servers" }).click();
  // "filesystem" also appears in the Args placeholder, so match the Name field exactly.
  await page.getByPlaceholder("filesystem", { exact: true }).fill("e2e-fs");
  await page.getByPlaceholder("npx").fill("echo");
  await page.getByRole("button", { name: "Register" }).click();

  await expect(page.getByText("e2e-fs")).toBeVisible({ timeout: 10_000 });
  // On the MCP tab the models table is unmounted, and e2e-fs is the only server → one delete.
  await page.getByRole("button", { name: "delete" }).click();
  await expect(page.getByText("e2e-fs")).toHaveCount(0, { timeout: 10_000 });
});
