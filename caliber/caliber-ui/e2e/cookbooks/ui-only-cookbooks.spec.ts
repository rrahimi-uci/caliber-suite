import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import {
  runUiOnlyCookbook,
  type SkillRecipe,
  type UiOnlyCookbook,
  uiOnlySlug,
} from "./ui-only-driver";

test.describe("Cookbooks built only through visible CALIBER UI", () => {
  test("Cookbook adapters contain no direct API backdoor", async () => {
    const directory = path.dirname(fileURLToPath(import.meta.url));
    const adapterFiles = (await fs.readdir(directory))
      .filter((name) => name.endsWith(".ts"))
      .sort();
    const sources = await Promise.all(
      adapterFiles.map((name) =>
        fs.readFile(path.join(directory, name), "utf8"),
      ),
    );
    const executableSource = sources.join("\n");
    const directRequest = new RegExp(
      `${["page", "request"].join("\\.")}|${["request", "(get|post|put|patch|delete)"].join("\\.")}`,
    );
    const directEndpoint = new RegExp(
      `${["fet", "ch\\s*\\("].join("")}|${["ajax", "-api"].join("")}|${["/", "api", "/"].join("")}`,
    );
    expect(executableSource).not.toMatch(directRequest);
    expect(executableSource).not.toMatch(directEndpoint);
  });

  test("Cookbook 02 builds and validates a precision skill without API seeding", async ({
    page,
  }) => {
    const recipe: SkillRecipe = {
      name: uiOnlySlug("pw-cookbook-skill"),
      owner: "@playwright",
      summary:
        "Customer support replies about refunds, order issues, calm tone, and policy citations.",
      content:
        "# Support response policy\n\nReply to {{audience}} with a calm tone, cite the relevant policy, and state the next step.",
      renderVariables: { audience: "a customer" },
      positiveTrigger: "Help me write a customer support reply about a refund",
      negativeTrigger: "Rotate JWT signing keys with asymmetric cryptography",
    };
    const cookbook: UiOnlyCookbook = {
      id: "CB-02",
      title:
        "create, render, trigger-test, persist, and archive a precision skill",
      async execute(ui) {
        await ui.createSkill(recipe);
        await ui.proveSkillBehavior(recipe);
        await ui.archiveCurrentSkill(recipe.name);
      },
    };

    await runUiOnlyCookbook(page, cookbook);
  });

  test("Cookbook 13 creates a governed review queue without API seeding", async ({
    page,
  }) => {
    const queueName = uiOnlySlug("pw-cookbook-queue");
    const cookbook: UiOnlyCookbook = {
      id: "CB-13",
      title: "create a governed human-review queue",
      async execute(ui) {
        await ui.createReviewQueue({
          name: queueName,
          description: "UI-only Cookbook queue for safety and citation review.",
          reviewer: "admin",
          question: {
            key: "citation_ok",
            title: "Are all material claims supported by the cited evidence?",
          },
        });
      },
    };

    await runUiOnlyCookbook(page, cookbook);
  });
});
