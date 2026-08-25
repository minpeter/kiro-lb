export function passwordInputType(visible: boolean): "password" | "text" {
  return visible ? "text" : "password";
}

export function passwordToggleLabel(visible: boolean): string {
  return visible ? "Hide password" : "Show password";
}
