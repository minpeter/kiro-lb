import { afterEach, describe, expect, it, vi } from "vitest";
import { copyCodeAriaLabel, copyUserCode } from "./copy-user-code";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("copyUserCode", () => {
  it("writes the user code to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await copyUserCode("ABCD-EFGH");

    expect(writeText).toHaveBeenCalledWith("ABCD-EFGH");
  });
});

describe("copyCodeAriaLabel", () => {
  it("labels the button Copy code until the write succeeds", () => {
    expect(copyCodeAriaLabel(false)).toBe("Copy code");
    expect(copyCodeAriaLabel(true)).toBe("Copied");
  });
});
