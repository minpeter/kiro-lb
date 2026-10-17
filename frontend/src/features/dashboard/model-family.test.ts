import { describe, expect, it } from "vitest";
import { compareModels, modelFamily } from "./model-family";

describe("Kimi model family", () => {
  it("recognizes the upstream K3 id and Moonshot aliases", () => {
    expect(modelFamily("kimi-k3")).toBe("kimi");
    expect(modelFamily("moonshot/kimi-k3")).toBe("kimi");
  });

  it("sorts K3 with model families instead of the unbranded tail", () => {
    expect(compareModels("kimi-k3", "other-model")).toBeLessThan(0);
  });
});
