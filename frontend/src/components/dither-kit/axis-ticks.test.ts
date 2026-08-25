import { describe, expect, it } from "vitest"
import { integerTicks, widthAwareTickCount } from "./axis-ticks"

describe("widthAwareTickCount", () => {
  it("fits roughly one time label per 70 plot pixels", () => {
    expect(widthAwareTickCount(300, 6)).toBe(4)
    expect(widthAwareTickCount(560, 6)).toBe(6)
  })

  it("keeps at least two tick positions on narrow plots", () => {
    expect(widthAwareTickCount(80, 8)).toBe(2)
  })
})

describe("integerTicks", () => {
  it("removes fractional ticks from integral count domains", () => {
    expect(integerTicks([0, 0.5, 1, 1.5, 2], [0, 2])).toEqual([0, 1, 2])
  })

  it("falls back to the count domain endpoints when filtering is too sparse", () => {
    expect(integerTicks([0.5], [0, 1])).toEqual([0, 1])
  })

  it("preserves fractional ticks for a fractional domain", () => {
    expect(integerTicks([0, 0.25, 0.5], [0, 0.5])).toEqual([0, 0.25, 0.5])
  })
})
