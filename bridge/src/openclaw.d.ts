/**
 * Ambient module declaration for openclaw/plugin-sdk.
 *
 * At runtime the real module is resolved from the OpenClaw host process.
 * This declaration provides just enough surface for the TS compiler.
 */
declare module "openclaw/plugin-sdk" {
  export function delegateCompactionToRuntime(params: {
    sessionId: string;
    sessionKey?: string;
    sessionFile: string;
    tokenBudget?: number;
    force?: boolean;
    currentTokenCount?: number;
    compactionTarget?: "budget" | "threshold";
    customInstructions?: string;
    runtimeContext?: Record<string, unknown>;
  }): Promise<{
    ok: boolean;
    compacted: boolean;
    reason?: string;
    result?: {
      summary?: string;
      firstKeptEntryId?: string;
      tokensBefore: number;
      tokensAfter?: number;
      details?: unknown;
    };
  }>;
}
