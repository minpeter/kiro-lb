export function copyUserCode(code: string): Promise<void> {
  return navigator.clipboard.writeText(code);
}

export function copyCodeAriaLabel(copied: boolean): string {
  return copied ? "Copied" : "Copy code";
}
