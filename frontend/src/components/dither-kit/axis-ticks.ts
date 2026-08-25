const MIN_X_TICKS = 2
const X_TICK_WIDTH = 70

/** Number of x-axis labels that fit without crowding, capped by the caller. */
export function widthAwareTickCount(plotWidth: number, maxTicks: number) {
  return Math.min(maxTicks, Math.max(MIN_X_TICKS, Math.floor(plotWidth / X_TICK_WIDTH)))
}

/** Keep count axes integral without changing axes whose domain is fractional. */
export function integerTicks(ticks: number[], domain: number[]) {
  if (!domain.every(Number.isInteger)) return ticks

  const integral = ticks.filter(Number.isInteger)
  if (integral.length >= 2) return integral

  const max = Math.max(...domain, 0)
  return max === 0 ? [0] : [0, max]
}
