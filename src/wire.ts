/**
 * Canonical provider-pin wire mapping: a routes.json `provider` pin (SDK camelCase)
 * -> the OpenRouter chat/completions request body (snake_case). The TS CLI passes
 * the camelCase pin straight to the SDK (which serializes it the same way); the
 * Python port builds the HTTP body directly. This function is the shared spec both
 * are checked against in the parity tests.
 */
export type ProviderPin = {
  order?: string[];
  only?: string[];
  allowFallbacks?: boolean;
  quantizations?: string[];
};

export function providerToHttp(
  pin: ProviderPin | null | undefined,
): Record<string, unknown> | null {
  if (!pin) return null;
  const out: Record<string, unknown> = {};
  for (const k of ['order', 'only', 'quantizations'] as const) {
    if (pin[k] != null) out[k] = pin[k];
  }
  if (pin.allowFallbacks != null) out.allow_fallbacks = pin.allowFallbacks;
  return Object.keys(out).length ? out : null;
}
